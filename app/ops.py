#!/usr/bin/env python3
"""
The ops layer — how an engine operation RUNS: mode flags, concurrency slots, provider/stack/
ledger binding, budget + kill-switch guards, error surfacing, and usage folding.

Every route and background worker goes through here; no business copy, no HTTP routing, no
markup. main.py stays the route surface, this module owns the runtime discipline:

    with ops.run_slot(user, stack):      # slot + provider + stack + per-run ledger + guards
        ... engine calls ...

Guards enforced in run_slot (the money invariants):
  - per-user concurrency cap (BusyError -> 429)
  - a subscriber's monthly fair-use allowance (BudgetError -> 402)
  - the daily free-pool kill switch for non-subscribers on the hosted key
    (DailyCapError -> 402 + needKey; degrade to the key prompt, never spend past the pool)
  - stack clamping (a free run can't spend Opus on the house key)
"""

from __future__ import annotations

import contextlib
import os
import threading
import traceback

from engine import pipeline, provider, usage

from app import access, auth, store, tiers

MOCK = os.environ.get("FILG_MOCK") == "1"
# "First query on us": when FILG has a hosted key for the TASTE account, a keyless user gets a free
# welcome run (metered by usage.py). No taste key → fully BYOK (the user must bring their own key
# from the first submit). Follows access._taste_key, NOT the raw engine hosted key — the taste has
# its own account now, so keying this off the generic key would switch the free taste off the moment
# the legacy ANTHROPIC_API_KEY is retired from the env.
HOSTED_FREE = bool(access._taste_key())

# Per-user concurrency: a flat cap on simultaneous AI operations per user (cost is isolated per run
# via pipeline.run_ledger, so concurrent runs don't mis-bill each other). Not a monetization tier —
# just a correctness/cost guard so one user can't fan out unbounded work.
CONCURRENCY_CAP = 3
_inflight: dict[str, int] = {}
_inflight_lock = threading.Lock()


class BusyError(Exception):
    """Raised when a user is already at the concurrent-operation limit."""
    def __init__(self, cap: int):
        self.cap = cap
        super().__init__(f"at concurrency limit ({cap})")


class BudgetError(Exception):
    """Raised when a subscriber has spent their monthly fair-use allowance on FILG's key. Carries the
    budget snapshot so the response can name the reset date. Caught centrally in `engine_error`."""
    def __init__(self, budget: dict):
        self.budget = budget or {}
        super().__init__("monthly fair-use allowance reached")


class DailyCapError(Exception):
    """Raised when the daily free-pool kill switch is tripped and a non-subscriber op would run on
    FILG's hosted key. Invariant #3: the funnel FEEDS the meter so it must also READ it — degrade to
    the key prompt, never spend past the pool. Caught centrally in `engine_error` (402 + needKey)."""
    def __init__(self):
        super().__init__("Today's free pool is spent. It refills daily.")


def concurrency_cap(user: str) -> int:
    return CONCURRENCY_CAP


def slot_user(s: dict) -> str:
    """The concurrency-bucket identity for a session's op: the owner, or a per-plan anonymous key.
    The anonymous taste has no email — keying its slot on the bare "" made EVERY anonymous visitor
    share one 3-slot bucket (three strangers brainstorming → the fourth 429s). Per-plan keys keep the
    cap per visitor-ish; total anonymous spend stays bounded by the daily kill switch."""
    return (s.get("user") or "").strip() or f"anon:{s.get('id')}"


def free_pool_tapped(user: str, prov=None) -> bool:
    """The daily kill-switch predicate for an op on FILG's hosted key: True when a NON-subscriber's
    real run would spend from a tapped free pool. One owner — run_slot enforces it, and the routes
    that spawn background work WITHOUT a slot (merge/refine) must read it before the spawn."""
    if prov is None:
        prov = access._provider_for(user)
    return bool(prov is not None and getattr(prov, "bills_filg", False) and not MOCK
                and not access._is_subscriber(user) and usage.taste_pool_tapped())


@contextlib.contextmanager
def run_slot(user: str, stack: str | None = None):
    """Reserve a concurrency slot for `user`, bind their provider + model stack + a fresh per-run cost
    ledger, then release the slot on exit. Raises BusyError if they're already at their plan's limit.
    The premium stack is clamped off FILG's free key so a free run can't spend Opus on FILG's dime.
    `user` may be a `slot_user` anonymous key — it resolves like a keyless free identity."""
    cap = concurrency_cap(user)
    tier = access._tier(user)
    if tier and not access._is_byok(user):   # allowance spent AND no own key to fall back to → block
        b = access._budget(user, tier)
        if b and b["over"]:
            raise BudgetError(b)
    with _inflight_lock:
        if _inflight.get(user, 0) >= cap:
            raise BusyError(cap)
        _inflight[user] = _inflight.get(user, 0) + 1
    try:
        prov = access._provider_for(user)
        # The daily kill switch guards every free op on FILG's key — the funnel feeds the meter, so
        # it must also read it (metered-but-uncapped is an invariant-#3 breach). Degrade, don't block:
        # 402 + needKey routes the user to their own key.
        if free_pool_tapped(user, prov):
            raise DailyCapError()   # the finally below releases the slot
        # Stack ceiling: BYOK (own key) → any stack; a subscriber → up to their tier's ceiling (Opus
        # for paid tiers, ON FILG's key); free taste on FILG's key → the Opus-free default.
        is_byok = bool(prov and not prov.bills_filg)
        stk = tiers.clamp_stack(stack, tier=tier, byok=is_byok)
        with provider.use(prov), provider.use_stack(stk), pipeline.run_ledger():
            yield
    finally:
        with _inflight_lock:
            n = _inflight.get(user, 0) - 1
            if n > 0:
                _inflight[user] = n
            else:
                _inflight.pop(user, None)


def bind_session_run(user: str, stack: str | None):
    """The provider/stack/ledger binding for a BACKGROUND worker (no slot, no guards — the spawning
    route already read the guards). Returns a context manager binding the same three things run_slot
    binds, with the same tier clamp, so every worker binds identically instead of hand-rolling it."""
    prov = access._provider_for(user)
    stk = tiers.clamp_stack(stack, tier=access._tier(user), byok=bool(prov and not prov.bills_filg))

    @contextlib.contextmanager
    def _bound():
        with provider.use(prov), provider.use_stack(stk), pipeline.run_ledger():
            yield prov
    return _bound()


# ── error surfacing ───────────────────────────────────────────────────────────
def busy_response(e: BusyError):
    from fastapi.responses import JSONResponse  # noqa: PLC0415 — keep ops importable without an app
    plural = "s" if e.cap != 1 else ""
    return JSONResponse(
        {"error": f"You already have {e.cap} operation{plural} running. Let one finish, then try again.",
         "busy": True}, status_code=429)


def budget_response(e: BudgetError):
    """402 when a subscriber has used their monthly allowance. The off-ramps, in the order we pitch
    them: upgrade to the next tier for more monthly credits, add your own key as a BYOK fallback
    (overflow runs on it, free), or wait for the renewal reset. `upgradeTier` names the next rung
    (absent at the top of the ladder)."""
    from fastapi.responses import JSONResponse  # noqa: PLC0415
    b = e.budget or {}
    nxt = tiers.next_tier(b.get("tier"))
    up = f"Upgrade to {tiers.label(nxt)} for more monthly credits, or add" if nxt else "Add"
    return JSONResponse(
        {"error": f"You've used this month's allowance on our key. {up} your own API key as a "
                  "fallback — or your allowance resets when your subscription renews.",
         "fairUse": True, "needKey": True, "resetAt": b.get("reset_at"), "tier": b.get("tier"),
         **({"upgradeTier": nxt} if nxt else {})},
        status_code=402)


def humanize_error(e: Exception) -> tuple[str, bool]:
    """Turn a raw provider exception into a message the user can act on. Returns (message, needKey).
    The most common live failure is a bad/expired BYOK key — OpenRouter answers 401 'User not found',
    which is meaningless to a user; we translate it into 'update your key'. Full traces still go to the
    Render logs (the callers print them); only this friendly string reaches the browser."""
    s = str(e)
    low = s.lower()
    name = type(e).__name__.lower()
    is_key = ("authentication" in name or "permissiondenied" in name
              or "code: 401" in low or "'code': 401" in low or " 401 " in f" {low} "
              or "user not found" in low or "invalid api key" in low or "incorrect api key" in low
              or "no auth credentials" in low or ("expired" in low and "key" in low))
    if is_key:
        return ("Your API key was rejected — it looks expired or invalid. Update your key "
                "with the 🔑 button up top, then try again."), True
    if ("credit balance" in low or "insufficient" in low or "billing" in low or "quota" in low
            or "402" in low):
        return ("That key works, but the account is out of credits or has no billing set up. Add "
                "credits/billing with your provider (Anthropic: console.anthropic.com · OpenRouter: "
                "openrouter.ai), then try again."), False
    if "not_found" in low or "model" in low and "404" in low:
        return ("That model isn't available on this key/account. Pick a different model crew, or "
                "check your provider account's model access."), False
    if "429" in low or "rate limit" in low or "overloaded" in low:
        return "The model is busy right now. Give it a few seconds and try again.", False
    return "Something went wrong on our side. Try again in a moment.", False


def engine_error(e: Exception, status_code: int = 500):
    """Standard JSON error for an engine route — humanized message + a needKey flag the frontend uses
    to reopen the key modal. A subscriber's fair-use BudgetError is surfaced as a 402 (every route
    already routes unexpected exceptions here, so no per-route wiring is needed). The full trace goes
    to stdout (Render logs / local terminal); the client only sees the friendly string."""
    from fastapi.responses import JSONResponse  # noqa: PLC0415
    if not isinstance(e, BudgetError):
        traceback.print_exc()
    if isinstance(e, BudgetError):
        return budget_response(e)
    if isinstance(e, DailyCapError):
        # `pool` lets the frontend give an anonymous lander honest tapped-pool copy instead
        # of demanding an account on a first submit that can't run anyway.
        return JSONResponse({"error": str(e), "needKey": True, "pool": True}, status_code=402)
    msg, need_key = humanize_error(e)
    body = {"error": msg}
    if need_key:
        body["needKey"] = True
    return JSONResponse(body, status_code=status_code)


# ── metering + per-plan keys ──────────────────────────────────────────────────
def plan_key(s: dict) -> str | None:
    """The per-branch unlock key for a plan: "{sid}:{active-leaf-node-id}". A new branch built from an
    earlier decision-tree node finishes on a NEW leaf id → a new key → its own unlock. Legacy
    (pre-tree) plans collapse to "{sid}:flat"."""
    sid = s.get("id")
    if not sid:
        return None
    tree = s.get("tree") if isinstance(s.get("tree"), dict) else None
    leaf = (tree or {}).get("active")
    return f"{sid}:{leaf or 'flat'}"


def fold_usage(sid: str, s: dict, cost: float, toks: int, **extra) -> tuple[float, int]:
    """Fold one AI op's cost + token count into the session's running totals (persisting any `extra`
    columns in the same write). The client reads the cumulative `cost`/`tokens` off the session and
    ticks an in-memory, per-session usage meter (resets on reload; never tracked on the account).
    Returns the new cumulative (cost, tokens) so side ops that don't return plan-state can echo them."""
    new_cost = round((s.get("cost") or 0) + cost, 4)
    new_tokens = (s.get("tokens") or 0) + int(toks or 0)
    store.plan_save(sid, cost=new_cost, tokens=new_tokens, **extra)
    access._meter_tokens(s.get("user"), toks)   # subscriber fair-use meter tracks tokens too
    return new_cost, new_tokens


def meter_bg(user: str, prov, cost: float, toks: int, *, is_run: bool = False,
             research_cost: float = 0.0) -> None:
    """Meter a background funnel op that ran on FILG's hosted key. A subscriber's usage counts against
    their monthly cap; a free user's daily kill-switch is always fed (record_spend); only a COMMIT (the
    deep research run) counts as a metered free 'run'. Ops on a user's own key aren't FILG's spend."""
    if prov is None or not getattr(prov, "bills_filg", False):
        return
    if access._is_subscriber(user):
        usage.record_monthly(access._acct(user), access._period(user), cost, toks)
        return
    if is_run:
        usage.record_run(auth.normalize_email(user), research_cost)
        usage.record_spend(round(cost - research_cost, 4), taste=True)
    else:
        usage.record_spend(cost, taste=True)


def bg_progress(sid: str, base_cost: float, base_tokens: int, progress: list):
    """A progress sink for a background funnel op: append the line + persist the running (base + this
    run's) cost/tokens so the session meter ticks live during the op."""
    def on_progress(line: str) -> None:
        progress.append(line)
        store.plan_save(sid, progress=list(progress), tokens=base_tokens + pipeline.LEDGER.tokens(),
                        cost=round(base_cost + pipeline.LEDGER.cost(), 4))
    return on_progress
