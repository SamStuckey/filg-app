#!/usr/bin/env python3
"""
SQLite-backed job store for the FILG app.

Replaces the in-memory `JOBS` dict in the launch skeleton so async runs, their graded results, and
the share links they back (`/r/{id}`) survive a process restart — on Render the in-memory store
dropped every in-flight job and expired every share link on each redeploy. Results are stashed as
JSON in a single-file DB.

The DB path is `FILG_DB` (default `app/filg.db`); usage.py shares the same file. On Render the
filesystem is ephemeral, so point `FILG_DB` at a mounted persistent disk for it to actually persist.
stdlib-only (sqlite3) so it still "runs anywhere".
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("FILG_DB") or os.path.join(_HERE, "filg.db")

_init_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    # New connection per call: safe to use across the request handlers and the background run thread.
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
        con = _connect()
        try:
            with con:
                con.execute("PRAGMA journal_mode=WAL")  # readers (status/share) don't block the writer
                # v1's async teardown-job store is retired (routes deleted 2026-07-07); an existing
                # DB sheds the dead table here.
                con.execute("DROP TABLE IF EXISTS jobs")
                # PDF access model: a flat $13 unlocks ONE plan's clean PDF + comp grants + fallback
                # credits. A "plan" = one finished branch, keyed plan_key "{sid}:{leaf-node-id}". Go
                # back and change the plan → a NEW branch → a NEW plan_key → its own $13. Re-downloading
                # a plan you already unlocked is FREE (it's in pdf_unlocks).
                #   pdf_purchases — account-wide UNLIMITED comp grants (admin/dev.py): unlock every plan.
                #   pdf_credits   — fallback plan-unlock credits (admin grants; legacy webhook events).
                #   pdf_unlocks   — the set of (email, plan_key) already unlocked → free re-downloads.
                con.execute(
                    "CREATE TABLE IF NOT EXISTS pdf_purchases ("
                    "  email TEXT PRIMARY KEY,"
                    "  stripe_session TEXT,"
                    "  amount_cents INTEGER,"
                    "  created_at TEXT NOT NULL)")
                con.execute(
                    "CREATE TABLE IF NOT EXISTS pdf_credits ("
                    "  email TEXT PRIMARY KEY,"
                    "  credits INTEGER NOT NULL DEFAULT 0,"
                    "  updated_at TEXT NOT NULL)")
                # Processed Stripe checkout sessions → credit grants are idempotent (Stripe retries webhooks).
                con.execute(
                    "CREATE TABLE IF NOT EXISTS stripe_sessions ("
                    "  id TEXT PRIMARY KEY,"
                    "  created_at TEXT NOT NULL)")
                con.execute(
                    "CREATE TABLE IF NOT EXISTS pdf_unlocks ("
                    "  email TEXT NOT NULL,"
                    "  plan_key TEXT NOT NULL,"
                    "  stripe_session TEXT,"
                    "  amount_cents INTEGER,"
                    "  created_at TEXT NOT NULL,"
                    "  PRIMARY KEY (email, plan_key))")
                # COUPONS RETIRED (2026-07-06, Sam): the redemption route and codes are gone. The table
                # may exist on older disks — deactivate every code so nothing dormant can be redeemed if
                # the route ever returns. Comp grants now happen only via dev.py / record_purchase.
                con.execute(
                    "CREATE TABLE IF NOT EXISTS coupons ("
                    "  code TEXT PRIMARY KEY,"
                    "  max_uses INTEGER NOT NULL,"
                    "  used INTEGER NOT NULL DEFAULT 0,"
                    "  active INTEGER NOT NULL DEFAULT 1,"
                    "  created_at TEXT NOT NULL)")
                con.execute("UPDATE coupons SET active=0")
                # Interactive plan-builder sessions (idea → decision-tree → downloadable file tree).
                con.execute(
                    "CREATE TABLE IF NOT EXISTS plan_sessions ("
                    "  id TEXT PRIMARY KEY,"
                    "  user TEXT,"
                    "  idea TEXT,"
                    "  status TEXT NOT NULL,"       # researching | building | done | error
                    "  research TEXT,"              # JSON {prose, rows, stats}
                    "  files TEXT,"                 # JSON {path: content}
                    "  step INTEGER NOT NULL DEFAULT 0,"
                    "  proposal TEXT,"              # JSON {section, title, draft}
                    "  history TEXT,"               # JSON [{section, choice, note}]
                    "  shaped TEXT,"                # JSON intake result {thesis, founder_edge, wedges...}
                    "  vetting TEXT,"               # JSON vet result {verdict, scores, first_test...}
                    "  directors TEXT,"             # JSON [persona_key] — the chosen Board of Directors
                    "  board TEXT,"                 # JSON [{section, ...review}] — per-step board reviews
                    "  tree TEXT,"                  # JSON {nodes:{id:node}, active} — branching decision tree
                    "  chat TEXT,"                  # JSON [{role, content}] — "chat with your plan" thread
                    "  progress TEXT,"              # JSON [str] — live research-spew lines (the receipts)
                    "  qa TEXT,"                    # JSON {notes:[...], fixed:[file]} — final QA pass report
                    "  shared INTEGER NOT NULL DEFAULT 0,"  # 1 → readable at the public /p/{id} share link
                    "  cost REAL NOT NULL DEFAULT 0,"
                    "  tokens INTEGER NOT NULL DEFAULT 0,"  # cumulative input+output tokens (usage meter)
                    "  stack TEXT NOT NULL DEFAULT 'the-work-horse',"  # chosen model stack (provider.STACKS)
                    "  error TEXT,"
                    "  created_at TEXT NOT NULL)")
                # Migration for DBs created before later columns existed (SQLite has no ADD COLUMN IF
                # NOT EXISTS) — add any missing ones, ignore if already present.
                have = {r["name"] for r in con.execute("PRAGMA table_info(plan_sessions)")}
                for col in ("shaped", "vetting", "directors", "board", "tree", "chat", "progress",
                            "custom_directors", "qa", "skeptic", "stage", "lookups", "decisions",
                            "roadmap", "codex"):
                    if col not in have:
                        con.execute(f"ALTER TABLE plan_sessions ADD COLUMN {col} TEXT")
                if "shared" not in have:
                    con.execute("ALTER TABLE plan_sessions ADD COLUMN shared INTEGER NOT NULL DEFAULT 0")
                if "tokens" not in have:
                    con.execute("ALTER TABLE plan_sessions ADD COLUMN tokens INTEGER NOT NULL DEFAULT 0")
                if "stack" not in have:
                    con.execute("ALTER TABLE plan_sessions ADD COLUMN stack TEXT NOT NULL DEFAULT 'the-work-horse'")
                if "updated_at" not in have:   # recency for the profile sort (most recently viewed/edited first)
                    con.execute("ALTER TABLE plan_sessions ADD COLUMN updated_at TEXT")
                    con.execute("UPDATE plan_sessions SET updated_at=created_at WHERE updated_at IS NULL")
                # Subscription accounts: a paid tier that runs on FILG's key (see app/tiers.py). `email`
                # is the NORMALIZED account id (alias-collapsed) so it lines up with billing/usage dedup.
                # `current_period_end` doubles as the fair-use window boundary + the reset date shown to
                # the user; a renewal (customer.subscription.updated) advances it → the monthly cap resets.
                con.execute(
                    "CREATE TABLE IF NOT EXISTS accounts ("
                    "  email TEXT PRIMARY KEY,"
                    "  tier TEXT,"                       # pro | ultimate | NULL (canceled; legacy starter/studio fold in)
                    "  status TEXT,"                     # active | trialing | past_due | canceled
                    "  stripe_customer_id TEXT,"
                    "  stripe_subscription_id TEXT,"
                    "  current_period_end TEXT,"         # ISO ts — fair-use window boundary / reset date
                    "  updated_at TEXT NOT NULL)")
        finally:
            con.close()
        _initialized = True


# ── PDF access: $13 unlocks ONE plan's clean PDF (re-download free); admin comp grants ──
PDF_CREDITS_PER_PURCHASE = 1   # a $13 purchase unlocks exactly the plan it was bought for


def grant_credits(email: str, n: int = PDF_CREDITS_PER_PURCHASE) -> None:
    """Add `n` plan-unlock credits to an account. Credits are the fallback currency (admin grants; a paid
    unlock whose webhook lost its plan_key) — the normal $13 path unlocks the exact plan directly via
    unlock_for_session. Idempotency for re-delivered Stripe events is handled by the caller."""
    if not email or n <= 0:
        return
    email = email.strip().lower()
    init()
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO pdf_credits (email, credits, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET credits=credits+excluded.credits, updated_at=excluded.updated_at",
                (email, n, datetime.now(timezone.utc).isoformat()))
    finally:
        con.close()


def credit_for_session(email: str, session_id: str | None, n: int = PDF_CREDITS_PER_PURCHASE) -> bool:
    """Grant `n` credits for a Stripe checkout session EXACTLY ONCE (idempotent on session_id, since
    Stripe retries webhooks). Returns True if granted, False if this session was already processed."""
    if not email:
        return False
    if not session_id:
        grant_credits(email, n)   # no id to dedupe on → best-effort grant
        return True
    email = email.strip().lower()
    init()
    con = _connect()
    try:
        with con:
            cur = con.execute("INSERT OR IGNORE INTO stripe_sessions (id, created_at) VALUES (?,?)",
                              (session_id, datetime.now(timezone.utc).isoformat()))
            if cur.rowcount != 1:
                return False   # already processed this event
            con.execute(
                "INSERT INTO pdf_credits (email, credits, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET credits=credits+excluded.credits, updated_at=excluded.updated_at",
                (email, n, datetime.now(timezone.utc).isoformat()))
        return True
    finally:
        con.close()


def unlock_for_session(email: str, session_id: str | None, plan_key: str) -> bool:
    """Record a PAID unlock of one exact plan (the $13 purchase path) EXACTLY ONCE per Stripe checkout
    session (Stripe retries webhooks). Re-downloads of an unlocked plan are free (has_purchased).
    Returns True if recorded, False if this session was already processed."""
    if not email or not plan_key:
        return False
    email = email.strip().lower()
    init()
    con = _connect()
    try:
        with con:
            if session_id:
                cur = con.execute("INSERT OR IGNORE INTO stripe_sessions (id, created_at) VALUES (?,?)",
                                  (session_id, datetime.now(timezone.utc).isoformat()))
                if cur.rowcount != 1:
                    return False   # already processed this event
            con.execute(
                "INSERT INTO pdf_unlocks (email, plan_key, stripe_session, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(email, plan_key) DO NOTHING",
                (email, plan_key, session_id, datetime.now(timezone.utc).isoformat()))
        return True
    finally:
        con.close()


def credits_left(email: str) -> int:
    """Remaining paid plan-unlock credits for an account (0 if none)."""
    if not email:
        return 0
    init()
    con = _connect()
    try:
        row = con.execute("SELECT credits FROM pdf_credits WHERE email=?",
                          (email.strip().lower(),)).fetchone()
    finally:
        con.close()
    return int(row["credits"]) if row and row["credits"] else 0


def is_comped(email: str) -> bool:
    """True iff the account holds an unlimited comp grant (admin/dev.py) → every plan unlocked."""
    if not email:
        return False
    init()
    con = _connect()
    try:
        row = con.execute("SELECT 1 FROM pdf_purchases WHERE email=?",
                          (email.strip().lower(),)).fetchone()
    finally:
        con.close()
    return row is not None


def has_purchased(email: str, plan_key: str | None = None) -> bool:
    """READ-only: may this (normalized) account download `plan_key`'s PDF right now WITHOUT spending a
    credit — because it's comped, or already unlocked this exact plan (free re-download)? Does NOT
    consume a credit and does NOT count available-but-unspent credits. (Name kept for callers.)"""
    if not email:
        return False
    email = email.strip().lower()
    init()
    con = _connect()
    try:
        if con.execute("SELECT 1 FROM pdf_purchases WHERE email=?", (email,)).fetchone():
            return True   # comp → unlimited
        if plan_key and con.execute(
                "SELECT 1 FROM pdf_unlocks WHERE email=? AND plan_key=?",
                (email, plan_key)).fetchone():
            return True   # already unlocked → free re-download
    finally:
        con.close()
    return False


def claim_pdf(email: str, plan_key: str) -> bool:
    """Claim a plan's PDF for download. FREE (no consume) if comped or already unlocked. Otherwise
    spends ONE credit and records the unlock (so re-downloads stay free). Returns False iff there's no
    access and no credit left → the caller should ask for payment. Atomic; credits never go negative."""
    if not email:
        return False
    email = email.strip().lower()
    if not plan_key:
        return has_purchased(email)   # no branch key → only a comp grant can clear it
    init()
    con = _connect()
    try:
        with con:
            if con.execute("SELECT 1 FROM pdf_purchases WHERE email=?", (email,)).fetchone():
                return True   # comp → unlimited, no credit spent
            if con.execute("SELECT 1 FROM pdf_unlocks WHERE email=? AND plan_key=?",
                           (email, plan_key)).fetchone():
                return True   # already unlocked → free re-download
            cur = con.execute("UPDATE pdf_credits SET credits=credits-1 WHERE email=? AND credits>0",
                              (email,))
            if cur.rowcount != 1:
                return False   # no credits → needs payment
            con.execute(
                "INSERT INTO pdf_unlocks (email, plan_key, created_at) VALUES (?,?,?) "
                "ON CONFLICT(email, plan_key) DO NOTHING",
                (email, plan_key, datetime.now(timezone.utc).isoformat()))
        return True
    finally:
        con.close()


def record_purchase(email: str, *, stripe_session: str | None = None,
                    amount_cents: int | None = None) -> None:
    """Grant an account-wide UNLIMITED comp (admin/dev.py). For PAID purchases use unlock_for_session."""
    if not email:
        return
    email = email.strip().lower()
    init()
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO pdf_purchases (email, stripe_session, amount_cents, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET created_at=excluded.created_at",
                (email, stripe_session, amount_cents, datetime.now(timezone.utc).isoformat()))
    finally:
        con.close()


# ── Subscription accounts (paid tiers on FILG's key — see app/tiers.py) ───────
# past_due keeps access through Stripe's dunning/retry window; canceled drops to free/BYOK.
ACTIVE_STATUSES = ("active", "trialing", "past_due")


def account_get(email: str) -> dict | None:
    """The subscription row for a normalized account, or None."""
    if not email:
        return None
    init()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM accounts WHERE email=?", (email.strip().lower(),)).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def account_tier(email: str) -> str | None:
    """The ACTIVE paid tier for a normalized account (None if no subscription, or it's canceled)."""
    row = account_get(email)
    if row and row.get("status") in ACTIVE_STATUSES and row.get("tier"):
        return row["tier"]
    return None


def set_subscription(email: str, *, tier: str | None, status: str,
                     stripe_customer_id: str | None = None,
                     stripe_subscription_id: str | None = None,
                     current_period_end: str | None = None) -> None:
    """Upsert an account's subscription state from a Stripe webhook. COALESCE preserves a previously
    stored id/period when a later event doesn't carry it (checkout gives the ids but no period end; the
    subscription.updated event fills the period end and advances it on each renewal)."""
    if not email:
        return
    email = email.strip().lower()
    init()
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO accounts (email, tier, status, stripe_customer_id, "
                "  stripe_subscription_id, current_period_end, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET "
                "  tier=excluded.tier, status=excluded.status, "
                "  stripe_customer_id=COALESCE(excluded.stripe_customer_id, accounts.stripe_customer_id), "
                "  stripe_subscription_id=COALESCE(excluded.stripe_subscription_id, accounts.stripe_subscription_id), "
                "  current_period_end=COALESCE(excluded.current_period_end, accounts.current_period_end), "
                "  updated_at=excluded.updated_at",
                (email, tier, status, stripe_customer_id, stripe_subscription_id,
                 current_period_end, datetime.now(timezone.utc).isoformat()))
    finally:
        con.close()


def cancel_subscription(email: str) -> None:
    """Mark an account canceled (subscription ended) → drops back to free/BYOK. Row kept for history."""
    if not email:
        return
    init()
    con = _connect()
    try:
        with con:
            con.execute("UPDATE accounts SET status='canceled', tier=NULL, updated_at=? WHERE email=?",
                        (datetime.now(timezone.utc).isoformat(), email.strip().lower()))
    finally:
        con.close()


def account_by_stripe(*, customer_id: str | None = None,
                      subscription_id: str | None = None) -> dict | None:
    """Look up an account by Stripe subscription id (preferred) or customer id — for webhook events
    (subscription.updated/deleted) whose payload carries ids but not the account email."""
    init()
    con = _connect()
    try:
        if subscription_id:
            row = con.execute("SELECT * FROM accounts WHERE stripe_subscription_id=?",
                              (subscription_id,)).fetchone()
            if row:
                return dict(row)
        if customer_id:
            row = con.execute("SELECT * FROM accounts WHERE stripe_customer_id=?",
                              (customer_id,)).fetchone()
            if row:
                return dict(row)
    finally:
        con.close()
    return None


# ── Plan-builder sessions ────────────────────────────────────────────────────
_PLAN_JSON = ("research", "files", "proposal", "history",  # columns stored as JSON
              "shaped", "vetting", "directors", "board", "tree", "chat", "progress",
              "custom_directors", "qa", "skeptic", "lookups", "decisions",
              # execution layer: roadmap = {tasks, milestones, goals}; codex = [artifact pointers]
              "roadmap", "codex")


def plan_create(session_id: str, user: str, idea: str, directors: list | None = None) -> None:
    init()
    con = _connect()
    try:
        with con:
            now = datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT INTO plan_sessions "
                "(id, user, idea, status, files, history, directors, board, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session_id, user, idea, "researching", "{}", "[]",
                 json.dumps(directors or []), "[]", now, now))
    finally:
        con.close()


def plan_get(session_id: str) -> dict | None:
    """Return the session with its JSON columns decoded, or None."""
    init()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM plan_sessions WHERE id=?", (session_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    s = dict(row)
    for col in _PLAN_JSON:
        s[col] = json.loads(s[col]) if s[col] else None
    return s


def plan_save(session_id: str, **fields) -> None:
    """Update the given columns; JSON-encode the dict/list ones. Every write bumps updated_at so the
    profile can sort plans by most-recently-edited."""
    if not fields:
        return
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k}=?")
        vals.append(json.dumps(v) if k in _PLAN_JSON else v)
    sets.append("updated_at=?")
    vals.append(datetime.now(timezone.utc).isoformat())
    vals.append(session_id)
    con = _connect()
    try:
        with con:
            con.execute(f"UPDATE plan_sessions SET {', '.join(sets)} WHERE id=?", vals)
    finally:
        con.close()


def plan_touch(session_id: str) -> None:
    """Bump updated_at without changing anything else — called when a plan is opened/viewed, so the
    profile sort treats a recent view as recent activity."""
    con = _connect()
    try:
        with con:
            con.execute("UPDATE plan_sessions SET updated_at=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), session_id))
    finally:
        con.close()


def plan_list(user: str, limit: int = 50) -> list[dict]:
    """Recent plan sessions for a user — for the profile / 'My plans' view. Ordered by most recently
    viewed/edited first (updated_at), falling back to created_at for any pre-migration row."""
    if not user:
        return []
    init()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT id, idea, status, step, shared, created_at, updated_at FROM plan_sessions "
            "WHERE user=? ORDER BY COALESCE(updated_at, created_at) DESC LIMIT ?",
            (user.strip().lower(), limit)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def plan_set_shared(session_id: str, shared: bool) -> None:
    """Toggle whether a plan is readable at its public /p/{id} share link."""
    init()
    con = _connect()
    try:
        with con:
            con.execute("UPDATE plan_sessions SET shared=? WHERE id=?",
                        (1 if shared else 0, session_id))
    finally:
        con.close()


def plan_delete(session_id: str) -> None:
    """Permanently remove a plan session."""
    init()
    con = _connect()
    try:
        with con:
            con.execute("DELETE FROM plan_sessions WHERE id=?", (session_id,))
    finally:
        con.close()


def purge_orphan_plans(hours: int = 48) -> int:
    """Delete plan sessions that never got an account (user empty/NULL) and are older than `hours`.
    The free taste runs anonymously through the first research step; a visitor who declines to sign
    up at the wall simply loses the plan — this is the promised cleanup. Returns the rows removed."""
    init()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    con = _connect()
    try:
        with con:
            cur = con.execute(
                "DELETE FROM plan_sessions WHERE (user IS NULL OR trim(user)='') AND created_at < ?",
                (cutoff,))
            return cur.rowcount or 0
    finally:
        con.close()


def plan_claim(session_id: str, email: str) -> bool:
    """Attach an OWNERLESS (anonymous free-taste) plan to a signed-in account. Refuses to reassign a
    plan someone already owns. Returns True if the plan is now owned by `email` (idempotent)."""
    if not email:
        return False
    email = email.strip().lower()
    init()
    con = _connect()
    try:
        with con:
            row = con.execute("SELECT user FROM plan_sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                return False
            owner = (row["user"] or "").strip().lower()
            if owner == email:
                return True   # already theirs
            if owner:
                return False  # someone else's plan — never reassign
            con.execute("UPDATE plan_sessions SET user=? WHERE id=?", (email, session_id))
        return True
    finally:
        con.close()


def delete_account(email: str, normalized: str | None = None) -> None:
    """Permanently delete a user's data: every plan session they own, their PDF purchases, and their
    per-branch unlocks. (BYOK keys live in app/keys.py — the caller removes those.) Purchases dedupe on
    the normalized email, so pass it too to catch alias rows."""
    if not email:
        return
    raw = email.strip().lower()
    emails = {raw}
    if normalized:
        emails.add(normalized.strip().lower())
    init()
    con = _connect()
    try:
        with con:
            con.execute("DELETE FROM plan_sessions WHERE lower(user)=?", (raw,))
            for e in emails:
                con.execute("DELETE FROM pdf_purchases WHERE email=?", (e,))
                con.execute("DELETE FROM pdf_credits WHERE email=?", (e,))
                con.execute("DELETE FROM pdf_unlocks WHERE email=?", (e,))
                con.execute("DELETE FROM accounts WHERE email=?", (e,))
    finally:
        con.close()


if __name__ == "__main__":  # quick self-test (no API)
    import tempfile
    DB = tempfile.mktemp(suffix=".db")
    init()
    # PDF access: $13 unlocks the exact plan it was bought for; re-downloads free; new branch pays again
    assert has_purchased("buyer@x.com", "pl1:leafA") is False and credits_left("buyer@x.com") == 0
    assert unlock_for_session("buyer@x.com", "cs_13a", "pl1:leafA") is True     # the $13 purchase lands
    assert unlock_for_session("buyer@x.com", "cs_13a", "pl1:leafA") is False    # Stripe retry → no double
    assert has_purchased("buyer@x.com", "pl1:leafA") is True                    # unlocked → free re-download
    assert claim_pdf("buyer@x.com", "pl1:leafA") is True and credits_left("buyer@x.com") == 0
    assert claim_pdf("buyer@x.com", "pl1:leafB") is False        # a NEW branch → pays its own $13
    # fallback credits (admin grants / legacy events): one credit = one plan unlock
    grant_credits("buyer@x.com", 3)
    assert credits_left("buyer@x.com") == 3
    assert claim_pdf("buyer@x.com", "pl1:leafB") is True and credits_left("buyer@x.com") == 2  # 1 spent
    assert claim_pdf("buyer@x.com", "pl1:leafB") is True and credits_left("buyer@x.com") == 2  # re-download free
    # an unlimited comp grant unlocks every plan, no credits needed
    record_purchase("comp@x.com")
    assert is_comped("comp@x.com") and has_purchased("comp@x.com", "any:key") is True
    assert claim_pdf("comp@x.com", "z:z") is True and credits_left("comp@x.com") == 0
    # plan sessions
    plan_create("pl1", "u@x.com", "an idea about guitar coaching", directors=["closer", "cfo"])
    assert plan_get("pl1")["status"] == "researching"
    assert plan_get("pl1")["directors"] == ["closer", "cfo"]   # board persists from creation
    plan_save("pl1", status="building", step=1, files={"01_brief.md": "# Brief"},
              proposal={"section": "offer", "draft": "..."},
              shaped={"thesis": "focused idea"}, vetting={"verdict": "pursue"},
              board=[{"section": "01_brief.md", "verdict": "ship it"}])
    s = plan_get("pl1")
    assert s["status"] == "building" and s["step"] == 1
    assert s["files"]["01_brief.md"] == "# Brief" and s["proposal"]["section"] == "offer"
    assert s["shaped"]["thesis"] == "focused idea" and s["vetting"]["verdict"] == "pursue"
    assert s["board"][0]["section"] == "01_brief.md"
    plan_save("pl1", tree={"nodes": {"n1": {"id": "n1", "parent": None, "step": 0}}, "active": "n1"})
    assert plan_get("pl1")["tree"]["active"] == "n1"   # branching tree round-trips through JSON
    assert plan_get("nope") is None
    mine = plan_list("u@x.com")
    assert len(mine) == 1 and mine[0]["id"] == "pl1" and plan_list("nobody@x.com") == []
    # share toggle + delete
    assert plan_get("pl1")["shared"] == 0 and mine[0]["shared"] == 0
    plan_set_shared("pl1", True)
    assert plan_get("pl1")["shared"] == 1
    plan_set_shared("pl1", False)
    assert plan_get("pl1")["shared"] == 0
    plan_delete("pl1")
    assert plan_get("pl1") is None and plan_list("u@x.com") == []
    # subscription accounts: activate → tier live; renewal advances period; cancel → drops to free
    assert account_tier("sub@x.com") is None
    set_subscription("sub@x.com", tier="pro", status="active",
                     stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                     current_period_end="2026-08-01T00:00:00Z")
    assert account_tier("sub@x.com") == "pro"
    assert account_by_stripe(subscription_id="sub_1")["email"] == "sub@x.com"
    assert account_by_stripe(customer_id="cus_1")["email"] == "sub@x.com"
    set_subscription("sub@x.com", tier="pro", status="active",   # renewal event: no ids, new period
                     current_period_end="2026-09-01T00:00:00Z")
    a = account_get("sub@x.com")
    assert a["current_period_end"] == "2026-09-01T00:00:00Z" and a["stripe_customer_id"] == "cus_1"
    set_subscription("sub@x.com", tier="pro", status="past_due")   # dunning → access holds
    assert account_tier("sub@x.com") == "pro"
    cancel_subscription("sub@x.com")
    assert account_tier("sub@x.com") is None and account_get("sub@x.com")["status"] == "canceled"
    print("store.py self-test OK")
