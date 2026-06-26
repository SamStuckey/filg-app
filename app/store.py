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
from datetime import datetime, timezone

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
                con.execute(
                    "CREATE TABLE IF NOT EXISTS jobs ("
                    "  id TEXT PRIMARY KEY,"
                    "  status TEXT NOT NULL,"      # running | done | error
                    "  idea TEXT,"
                    "  user TEXT,"
                    "  mode TEXT,"                 # teardown | full
                    "  result TEXT,"               # JSON blob from the engine
                    "  error TEXT,"
                    "  created_at TEXT NOT NULL)")
                # One-time $13 PDF unlock — the single paid action (no subscription, no tiers). Keyed on
                # the NORMALIZED email (see auth.normalize_email) so a buyer's alias addresses all unlock.
                con.execute(
                    "CREATE TABLE IF NOT EXISTS pdf_purchases ("
                    "  email TEXT PRIMARY KEY,"
                    "  stripe_session TEXT,"
                    "  amount_cents INTEGER,"
                    "  created_at TEXT NOT NULL)")
                # Per-branch PDF unlocks. The $7 buys the polished PDF of ONE finished plan branch,
                # keyed on (email, plan_key) where plan_key = "{sid}:{leaf-node-id}". Go back in the
                # decision tree and build a NEW branch → a new leaf id → a new plan_key → pay again,
                # for the new plan only. Account-wide grants (coupons, legacy buyers) live in
                # pdf_purchases and unlock every branch; this table is the per-branch Stripe purchases.
                con.execute(
                    "CREATE TABLE IF NOT EXISTS pdf_unlocks ("
                    "  email TEXT NOT NULL,"
                    "  plan_key TEXT NOT NULL,"
                    "  stripe_session TEXT,"
                    "  amount_cents INTEGER,"
                    "  created_at TEXT NOT NULL,"
                    "  PRIMARY KEY (email, plan_key))")
                # Coupon codes that unlock the PDF for free (bypass Stripe). `used` is a private counter
                # (admin-only, never surfaced in the UX); a code is spent once used >= max_uses.
                con.execute(
                    "CREATE TABLE IF NOT EXISTS coupons ("
                    "  code TEXT PRIMARY KEY,"
                    "  max_uses INTEGER NOT NULL,"
                    "  used INTEGER NOT NULL DEFAULT 0,"
                    "  active INTEGER NOT NULL DEFAULT 1,"
                    "  created_at TEXT NOT NULL)")
                # Seed the standing comp code. INSERT OR IGNORE → idempotent: re-deploys never reset the
                # `used` counter (it lives on the persistent disk), so 100 uses means 100 across all time.
                con.execute(
                    "INSERT OR IGNORE INTO coupons (code, max_uses, used, active, created_at) "
                    "VALUES (?,?,0,1,?)",
                    ("FUCKYOUIMNOTGIVINGYOU13BUCKS", 100,
                     datetime.now(timezone.utc).isoformat()))
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
                            "custom_directors", "qa"):
                    if col not in have:
                        con.execute(f"ALTER TABLE plan_sessions ADD COLUMN {col} TEXT")
                if "shared" not in have:
                    con.execute("ALTER TABLE plan_sessions ADD COLUMN shared INTEGER NOT NULL DEFAULT 0")
                if "tokens" not in have:
                    con.execute("ALTER TABLE plan_sessions ADD COLUMN tokens INTEGER NOT NULL DEFAULT 0")
                if "stack" not in have:
                    con.execute("ALTER TABLE plan_sessions ADD COLUMN stack TEXT NOT NULL DEFAULT 'the-work-horse'")
        finally:
            con.close()
        _initialized = True


def create(job_id: str, idea: str, user: str, mode: str) -> None:
    init()
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO jobs (id, status, idea, user, mode, created_at) VALUES (?,?,?,?,?,?)",
                (job_id, "running", idea, user, mode, datetime.now(timezone.utc).isoformat()))
    finally:
        con.close()


def finish(job_id: str, result: dict, mode: str) -> None:
    con = _connect()
    try:
        with con:
            con.execute("UPDATE jobs SET status='done', result=?, mode=? WHERE id=?",
                        (json.dumps(result), mode, job_id))
    finally:
        con.close()


def fail(job_id: str, error: str) -> None:
    con = _connect()
    try:
        with con:
            con.execute("UPDATE jobs SET status='error', error=? WHERE id=?", (error, job_id))
    finally:
        con.close()


def get(job_id: str) -> dict | None:
    """Return the job as a dict (result parsed back from JSON), or None if unknown."""
    init()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    job = dict(row)
    job["result"] = json.loads(job["result"]) if job["result"] else None
    return job


# ── PDF purchases ($7 polished export unlock — the single paid action, per finished branch) ──
def record_purchase(email: str, *, plan_key: str | None = None, stripe_session: str | None = None,
                    amount_cents: int | None = None) -> None:
    """Record a $7 polished-PDF unlock. With a `plan_key` ("{sid}:{leaf-node-id}") it unlocks ONE
    finished branch (the per-branch Stripe purchase); without one it's an account-wide grant (coupons,
    comps) that unlocks every branch. Idempotent — re-delivering the same event refreshes the row."""
    if not email:
        return
    email = email.strip().lower()
    init()
    con = _connect()
    try:
        with con:
            if plan_key:
                con.execute(
                    "INSERT INTO pdf_unlocks (email, plan_key, stripe_session, amount_cents, created_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(email, plan_key) DO UPDATE SET "
                    "  stripe_session=COALESCE(excluded.stripe_session, stripe_session),"
                    "  amount_cents=COALESCE(excluded.amount_cents, amount_cents),"
                    "  created_at=excluded.created_at",
                    (email, plan_key, stripe_session, amount_cents,
                     datetime.now(timezone.utc).isoformat()))
            else:
                con.execute(
                    "INSERT INTO pdf_purchases (email, stripe_session, amount_cents, created_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET "
                    "  stripe_session=COALESCE(excluded.stripe_session, stripe_session),"
                    "  amount_cents=COALESCE(excluded.amount_cents, amount_cents),"
                    "  created_at=excluded.created_at",
                    (email, stripe_session, amount_cents,
                     datetime.now(timezone.utc).isoformat()))
    finally:
        con.close()


def has_purchased(email: str, plan_key: str | None = None) -> bool:
    """True iff this (already-normalized) email may download the PDF. An account-wide grant
    (pdf_purchases — coupons, comps, legacy pre-per-branch buyers) unlocks every branch. Otherwise a
    `plan_key` must have its own per-branch unlock row (pdf_unlocks)."""
    if not email:
        return False
    email = email.strip().lower()
    init()
    con = _connect()
    try:
        if con.execute("SELECT 1 FROM pdf_purchases WHERE email=?", (email,)).fetchone():
            return True   # account-wide grant unlocks all branches
        if plan_key and con.execute(
                "SELECT 1 FROM pdf_unlocks WHERE email=? AND plan_key=?",
                (email, plan_key)).fetchone():
            return True
    finally:
        con.close()
    return False


# ── Coupons (free PDF unlock — bypass Stripe) ────────────────────────────────
def redeem_coupon(code: str, normalized_email: str) -> tuple[bool, str]:
    """Atomically redeem a coupon for `normalized_email`: if the code is active and has uses left,
    bump its counter and grant the PDF unlock (record_purchase). Returns (ok, reason). `reason` is
    coarse ('invalid' | 'spent') and never leaks the remaining-uses count. Idempotent-ish: a buyer
    who already unlocked still 'succeeds' without burning a use."""
    code = (code or "").strip()
    if not code or not normalized_email:
        return False, "invalid"
    con = _connect()
    try:
        with con:  # single transaction → the SELECT + UPDATE can't race two redemptions past the cap
            row = con.execute(
                "SELECT max_uses, used, active FROM coupons WHERE code=?", (code,)).fetchone()
            if not row or not row["active"]:
                return False, "invalid"
            # Already unlocked on this account → don't spend a use, just confirm.
            have = con.execute("SELECT 1 FROM pdf_purchases WHERE email=?",
                               (normalized_email,)).fetchone()
            if have:
                return True, "already"
            if row["used"] >= row["max_uses"]:
                return False, "spent"
            now = datetime.now(timezone.utc).isoformat()
            con.execute("UPDATE coupons SET used=used+1 WHERE code=?", (code,))
            con.execute(
                "INSERT INTO pdf_purchases (email, stripe_session, amount_cents, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET created_at=excluded.created_at",
                (normalized_email, f"coupon:{code}", 0, now))
        return True, "redeemed"
    finally:
        con.close()


def coupon_status(code: str) -> dict | None:
    """Admin-only view of a coupon (code, max_uses, used, remaining, active). For Sam, never the UX."""
    init()
    con = _connect()
    try:
        row = con.execute(
            "SELECT code, max_uses, used, active FROM coupons WHERE code=?",
            ((code or "").strip(),)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return {"code": row["code"], "max_uses": row["max_uses"], "used": row["used"],
            "remaining": max(0, row["max_uses"] - row["used"]), "active": bool(row["active"])}


# ── Plan-builder sessions ────────────────────────────────────────────────────
_PLAN_JSON = ("research", "files", "proposal", "history",  # columns stored as JSON
              "shaped", "vetting", "directors", "board", "tree", "chat", "progress",
              "custom_directors", "qa")


def plan_create(session_id: str, user: str, idea: str, directors: list | None = None) -> None:
    init()
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO plan_sessions "
                "(id, user, idea, status, files, history, directors, board, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, user, idea, "researching", "{}", "[]",
                 json.dumps(directors or []), "[]", datetime.now(timezone.utc).isoformat()))
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
    """Update the given columns; JSON-encode the dict/list ones."""
    if not fields:
        return
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k}=?")
        vals.append(json.dumps(v) if k in _PLAN_JSON else v)
    vals.append(session_id)
    con = _connect()
    try:
        with con:
            con.execute(f"UPDATE plan_sessions SET {', '.join(sets)} WHERE id=?", vals)
    finally:
        con.close()


def plan_list(user: str, limit: int = 50) -> list[dict]:
    """Recent plan sessions for a user (newest first) — for the profile / 'My plans' view."""
    if not user:
        return []
    init()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT id, idea, status, step, shared, created_at FROM plan_sessions "
            "WHERE user=? ORDER BY created_at DESC LIMIT ?",
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
                con.execute("DELETE FROM pdf_unlocks WHERE email=?", (e,))
    finally:
        con.close()


if __name__ == "__main__":  # quick self-test (no API)
    import tempfile
    DB = tempfile.mktemp(suffix=".db")
    create("abc123", "an idea about helping dentists", "a@x.com", "teardown")
    assert get("abc123")["status"] == "running"
    finish("abc123", {"prose": {"title": "T"}, "rows": [], "stats": {}, "cost": 0.4}, "teardown")
    j = get("abc123")
    assert j["status"] == "done" and j["result"]["prose"]["title"] == "T"
    fail("abc123", "boom")
    assert get("abc123")["status"] == "error"
    assert get("nope") is None
    # PDF purchases (the $7 unlock — now per finished branch)
    assert has_purchased("buyer@x.com", "pl1:leafA") is False
    record_purchase("buyer@x.com", plan_key="pl1:leafA", stripe_session="cs_1", amount_cents=700)
    assert has_purchased("buyer@x.com", "pl1:leafA") is True
    assert has_purchased("buyer@x.com", "pl1:leafB") is False   # a NEW branch isn't unlocked
    record_purchase("buyer@x.com", plan_key="pl1:leafA")        # idempotent re-delivery
    assert has_purchased("buyer@x.com", "pl1:leafA") is True
    # an account-wide grant (coupon/comp/legacy) unlocks every branch
    record_purchase("comp@x.com")
    assert has_purchased("comp@x.com", "any:key") is True and has_purchased("comp@x.com") is True
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
    print("store.py self-test OK")
