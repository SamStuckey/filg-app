#!/usr/bin/env python3
"""
FILG MVP backend — the thin app that runs the engine for a stranger.

Flow: plain-text idea in → async label-don't-chase run (the engine) → graded artifact out, gated by
the free-tier cap + daily kill switch (engine/usage.py). Reuses the engine wholesale; nothing
about the pipeline is reimplemented here.

Auth + billing are real (Supabase JWT + Stripe $39/mo), but degrade gracefully: with no Supabase /
Stripe env set the app still runs free-tier-only on the email typed in the body (mock/dev). Paid is
always gated on a verified user with a live subscription (app/auth.py + app/billing.py). Jobs +
results + subscription state persist in SQLite (app/store.py); swap for Postgres + a real queue and
this stays the same shape.

Run:
  pip install fastapi uvicorn
  FILG_MOCK=1 uvicorn app.main:app --reload        # free, no API/auth/billing (dev/frontend)
  uvicorn app.main:app                              # real runs (~$0.40 each, metered)
Env: FILG_MOCK, FILG_FREE_RUNS, FILG_DAILY_BUDGET, FILG_PAID_EMAILS (csv comp override),
     SUPABASE_URL/SUPABASE_PUBLISHABLE_KEY (auth via JWKS), STRIPE_*/FILG_PUBLIC_URL.
"""

from __future__ import annotations

import io
import json
import contextlib
import os
import re
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path

import markdown
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool  # long engine calls must not block the event loop

# the engine package (research/grading/metering) + the app-side skill/persona/board layer.
_APP_DIR = Path(__file__).resolve().parent
from app import teardown  # noqa: E402 — the research-run product layer (offer summary over graded evidence)
from engine import usage     # noqa: E402
from app import personas  # noqa: E402 — advisor/director registry (shared by ask-an-expert + the board)
from app import board     # noqa: E402 — Board of Directors orchestration
from app import director_forge  # noqa: E402 — forge a custom Board director from a description (distill→draft→QA)
from app import gibberish  # noqa: E402 — pre-LLM "is this even an idea?" gate (saves a run, hands back a roast)
from app import intake     # noqa: E402 — shape + vet (the kill-gate); /revet re-runs it after added substance
from app import brainstorm # noqa: E402 — diverge/merge: the top of the funnel (1-3 directions → one refined idea)
from app import router     # noqa: E402 — the single prompt box (intent routing) + the pivot-fork integration gate
from app import plan_pdf   # noqa: E402 — styled PDF generation (synthesis + fpdf2 render)
from app import advisor    # noqa: E402 — "chat with your plan" (grounded advisory layer)
from app import context    # noqa: E402 — THE CONTEXT ENGINE: every model-facing view of session state
from app import skeptic    # noqa: E402 — adversarial assumption-checking on the live research path
from app import skill_registry as skills  # noqa: E402 — the skill bodies (help/system blocks)
from engine import provider   # noqa: E402 — BYOK: per-run LLM provider (FILG's key vs a user's OpenRouter key)
from engine import pipeline   # noqa: E402 — engine: per-run cost ledger (run_ledger) for safe concurrency
from engine import model_catalog  # noqa: E402 — model ids/prices/slugs + cached Models API availability
from engine import tree as dtree  # noqa: E402 — the decision tree: node wiring, kinds, attachments, run epochs

from . import auth, billing, keys, planner, render, store, tiers  # noqa: E402 — persistence, auth, billing, keys, tiers, share-page HTML
# Entitlement/provider/metering decisions live in access.py (the pure service layer). Re-exported here
# so the route bodies call them as bare names and `main._budget` etc. stay importable by tests.
from .access import (  # noqa: E402,F401
    _is_byok, _acct, _tier, _is_subscriber, _period, _budget, _feature_ok, _has_pdf_access,
    _key_provider_kind, _build_provider, _on_filg_key, _provider_for,
    _meter, _meter_tokens, _needs_key)

MOCK = os.environ.get("FILG_MOCK") == "1"
# "First query on us" + subscriptions: when FILG has its own hosted Anthropic key, a keyless user gets a
# free welcome run (metered by usage.py) and subscribers run on it. The key is FILG_ANTHROPIC_API_KEY
# (preferred, so it doesn't collide with a dev's own ANTHROPIC_API_KEY / Claude Code login), else
# ANTHROPIC_API_KEY. No hosted key → fully BYOK (the user must bring their own key from the first submit).
HOSTED_FREE = bool(provider.hosted_key())
# One startup line so a local run never has to guess its wiring (the #1 source of confusing 500s is a
# hosted key that didn't reach the process env).
print(f"[filg] mock={'ON (canned, no spend)' if MOCK else 'off (REAL runs)'}"
      f" · hosted key={'wired' if HOSTED_FREE else 'MISSING (free/anon runs will fail in real mode)'}"
      f" · BYOK store={'on' if keys.enabled() else 'off (no FILG_KEY_SECRET)'}"
      f" · auth={'on' if auth.AUTH_ENABLED else ('dev as ' + os.environ.get('FILG_DEV_EMAIL', '(anonymous)'))}")


def _plan_key(s: dict) -> str | None:
    """The per-branch unlock key for a plan: "{sid}:{active-leaf-node-id}". A new branch built from an
    earlier decision-tree node finishes on a NEW leaf id → a new key → its own $7 unlock. Legacy
    (pre-tree) plans collapse to "{sid}:flat"."""
    sid = s.get("id")
    if not sid:
        return None
    tree = s.get("tree") if isinstance(s.get("tree"), dict) else None
    leaf = (tree or {}).get("active")
    return f"{sid}:{leaf or 'flat'}"


def _fold_usage(sid: str, s: dict, cost: float, toks: int, **extra) -> tuple[float, int]:
    """Fold one AI op's cost + token count into the session's running totals (persisting any `extra`
    columns in the same write). The client reads the cumulative `cost`/`tokens` off the session and
    ticks an in-memory, per-session usage meter (resets on reload; never tracked on the account).
    Returns the new cumulative (cost, tokens) so side ops that don't return plan-state can echo them."""
    new_cost = round((s.get("cost") or 0) + cost, 4)
    new_tokens = (s.get("tokens") or 0) + int(toks or 0)
    store.plan_save(sid, cost=new_cost, tokens=new_tokens, **extra)
    _meter_tokens(s.get("user"), toks)   # subscriber fair-use meter tracks tokens too (cost via _meter)
    return new_cost, new_tokens


def _key_wall(session: dict):
    """402 if the session owner must bring a key before this (API-calling) action; else None.
    Past the free taste, every engine call needs the user's own key OR an active subscription."""
    if _needs_key(session.get("user")):
        return JSONResponse(
            {"error": "Keep building free on your own API key (OpenRouter or Anthropic), or "
                      "subscribe to run on ours.",
             "needKey": True}, status_code=402)
    return None


def _account_wall(request: Request, session: dict):
    """401 if auth is ON and this engine action has no verified account behind it; else None.

    THE WALL (2026-07-06): the free taste — initial prompt → direction spread → direction select →
    the merged first idea with its first-pass research — runs ANONYMOUS, on the house. The next click
    (commit = the deep build) and everything past it requires an account: sign up, then either bring
    your own key (free BYOK) or subscribe. A signed-in user touching an ownerless (anonymous-taste)
    plan CLAIMS it here, so the plan they tasted joins the account they just created; an unclaimed
    plan is purged after ~48h (store.purge_orphan_plans)."""
    if not auth.AUTH_ENABLED:
        return None
    authed = auth.user_from_request(request)
    if authed and authed["email"]:
        if not (session.get("user") or "").strip():   # their anonymous taste → claim it into the account
            store.plan_claim(session["id"], authed["email"])
            session["user"] = authed["email"]
        return None
    return JSONResponse(
        {"error": "Create a free account to keep building — this plan saves to it. Then bring your "
                  "own API key (free) or subscribe to run on ours.",
         "needAccount": True}, status_code=401)


def _kill_gate(session: dict):
    """422 if the session's idea was graded `kill` — the builder must not roll forward into a full
    plan built on nothing. The operator clears it via /revet (add a real skill/asset/buyer). Returns
    the gate payload (the vet's clarifying question + risk) or None when the verdict isn't a kill."""
    v = session.get("vetting") or {}
    if (v.get("verdict") or "pursue") != "kill":
        return None
    sh = session.get("shaped") or {}
    return JSONResponse({"needSubstance": True, "verdict": "kill",
        "question": sh.get("clarifying_question")
            or "Name one real skill, asset, or audience you already have, and who would pay for it.",
        "risk": v.get("biggest_risk") or "", "reaction": v.get("reaction") or ""}, status_code=422)


app = FastAPI(title="FILG")

# The SPA's CSS/JS are served as real static files from app/web/static (extracted out of this module
# so the display layer is editable with front-end tooling). index.html is read + templated in Python
# (see _render_page) because it carries the __FILG_HEAD__ config hook.
_WEB_DIR = _APP_DIR / "web"
app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

# Anonymous free-taste plans that never got an account are scrapped after ~48h — the wall's promised
# cleanup ("no account → the plan is lost"). One sweep at boot, then a slow background loop; both are
# best-effort (a missed sweep just waits for the next one). Env-tunable without a deploy.
ORPHAN_TTL_HOURS = int(os.environ.get("FILG_ORPHAN_TTL_HOURS", "48"))
_ORPHAN_SWEEP_SECS = 6 * 3600


def _orphan_sweep_loop() -> None:
    while True:
        try:
            n = store.purge_orphan_plans(ORPHAN_TTL_HOURS)
            if n:
                print(f"[filg] purged {n} account-less plan(s) older than {ORPHAN_TTL_HOURS}h")
        except Exception:  # noqa: BLE001 — a failed sweep must never take the app down
            traceback.print_exc()
        time.sleep(_ORPHAN_SWEEP_SECS)


threading.Thread(target=_orphan_sweep_loop, daemon=True).start()

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
    budget snapshot so the response can name the reset date. Caught centrally in `_engine_error`."""
    def __init__(self, budget: dict):
        self.budget = budget or {}
        super().__init__("monthly fair-use allowance reached")


class DailyCapError(Exception):
    """Raised when the daily free-pool kill switch is tripped and a non-subscriber op would run on
    FILG's hosted key. Invariant #3: the funnel FEEDS the meter so it must also READ it — degrade to
    the key prompt, never spend past the pool. Caught centrally in `_engine_error` (402 + needKey)."""
    def __init__(self):
        super().__init__("Today's free pool is tapped. Add your own API key to keep going.")


def _concurrency_cap(user: str) -> int:
    return CONCURRENCY_CAP


def _slot_user(s: dict) -> str:
    """The concurrency-bucket identity for a session's op: the owner, or a per-plan anonymous key.
    The anonymous taste has no email — keying its slot on the bare "" made EVERY anonymous visitor
    share one 3-slot bucket (three strangers brainstorming → the fourth 429s). Per-plan keys keep the
    cap per visitor-ish; total anonymous spend stays bounded by the daily kill switch."""
    return (s.get("user") or "").strip() or f"anon:{s.get('id')}"


@contextlib.contextmanager
def _run_slot(user: str, stack: str | None = None):
    """Reserve a concurrency slot for `user`, bind their provider + model stack + a fresh per-run cost
    ledger, then release the slot on exit. Raises BusyError if they're already at their plan's limit.
    The premium stack is clamped off FILG's free key so a free run can't spend Opus on FILG's dime.
    `user` may be a `_slot_user` anonymous key — it resolves like a keyless free identity."""
    cap = _concurrency_cap(user)
    tier = _tier(user)
    if tier and not _is_byok(user):   # allowance spent AND no own key to fall back to → block (fair-use)
        b = _budget(user, tier)
        if b and b["over"]:
            raise BudgetError(b)
    with _inflight_lock:
        if _inflight.get(user, 0) >= cap:
            raise BusyError(cap)
        _inflight[user] = _inflight.get(user, 0) + 1
    try:
        prov = _provider_for(user)
        # The daily kill switch guards every free op on FILG's key — the v2 funnel feeds the meter, so
        # it must also read it (metered-but-uncapped is an invariant-#3 breach). Degrade, don't block:
        # 402 + needKey routes the user to their own key, same as the v1 taste path.
        if (prov is not None and getattr(prov, "bills_filg", False) and not MOCK
                and not _is_subscriber(user) and usage.kill_switch_tripped()):
            raise DailyCapError()   # the finally below releases the slot
        # Stack ceiling: BYOK (own key) → any stack; a subscriber → up to their tier's ceiling (Opus for
        # Pro/Studio, ON FILG's key); free taste on FILG's key → the Opus-free default (invariant #3).
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


def _busy_response(e: BusyError) -> JSONResponse:
    plural = "s" if e.cap != 1 else ""
    return JSONResponse(
        {"error": f"You already have {e.cap} operation{plural} running. Let one finish, then try again.",
         "busy": True}, status_code=429)


def _budget_response(e: BudgetError) -> JSONResponse:
    """402 when a subscriber has used their monthly allowance. The off-ramps, in the order we pitch
    them: upgrade to the next tier for more monthly credits, add your own key as a BYOK fallback
    (overflow runs on it, free), or wait for the renewal reset. `upgradeTier` names the next rung
    (absent at the top of the ladder)."""
    b = e.budget or {}
    nxt = tiers.next_tier(b.get("tier"))
    up = f"Upgrade to {tiers.label(nxt)} for more monthly credits, or add" if nxt else "Add"
    return JSONResponse(
        {"error": f"You've used this month's allowance on our key. {up} your own API key as a "
                  "fallback — or your allowance resets when your subscription renews.",
         "fairUse": True, "needKey": True, "resetAt": b.get("reset_at"), "tier": b.get("tier"),
         **({"upgradeTier": nxt} if nxt else {})},
        status_code=402)


def _humanize_error(e: Exception) -> tuple[str, bool]:
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


def _engine_error(e: Exception, status_code: int = 500):
    """Standard JSON error for an engine route — humanized message + a needKey flag the frontend uses
    to reopen the key modal. A subscriber's fair-use BudgetError is surfaced as a 402 (every route
    already routes unexpected exceptions here, so no per-route wiring is needed). The full trace goes
    to stdout (Render logs / local terminal); the client only sees the friendly string."""
    if not isinstance(e, BudgetError):
        traceback.print_exc()
    if isinstance(e, BudgetError):
        return _budget_response(e)
    if isinstance(e, DailyCapError):
        return JSONResponse({"error": str(e), "needKey": True}, status_code=402)
    msg, need_key = _humanize_error(e)
    body = {"error": msg}
    if need_key:
        body["needKey"] = True
    return JSONResponse(body, status_code=status_code)


def _run_job(job_id: str, idea: str, user: str, mode: str) -> None:
    try:
        with pipeline.run_ledger():
            res = (teardown.generate_full if mode == "full" else teardown.generate)(idea, mock=MOCK)
        usage.record_run(user, res["cost"])
        store.finish(job_id, res, mode)
    except Exception as e:  # noqa: BLE001 — surface failures to the client, don't crash the worker
        traceback.print_exc()  # full trace → Render stdout logs (client only sees str(e))
        store.fail(job_id, _humanize_error(e)[0])


@app.post("/api/run")
async def api_run(request: Request):
    body = await request.json()
    idea = (body.get("idea") or "").strip()
    if len(idea) < 12:
        return JSONResponse({"error": "Tell me a bit more about the idea."}, status_code=400)

    # Identity: a verified Supabase user wins; otherwise fall back to the email typed in the body.
    authed = auth.user_from_request(request)
    # Legacy teardown endpoint: in the auth-on regime it takes a verified account (an open POST with
    # any typed email would be unauthenticated spend on the hosted key). Dev/auth-off keeps the old
    # body-email behavior.
    if auth.AUTH_ENABLED and not (authed and authed["email"]):
        return JSONResponse({"error": "Sign in first.", "needAccount": True}, status_code=401)
    user = authed["email"] if authed and authed["email"] else (body.get("email") or "").strip().lower()
    if "@" not in user:
        return JSONResponse({"error": "Enter an email so we can send your result."}, status_code=400)

    allowed, reason = usage.can_run(user, is_paid=False)
    if not allowed:
        return JSONResponse({"error": reason}, status_code=402)

    mode = "teardown"
    job_id = uuid.uuid4().hex[:12]
    store.create(job_id, idea, user, mode)
    threading.Thread(target=_run_job, args=(job_id, idea, user, mode), daemon=True).start()
    return {"job_id": job_id, "mode": mode}


@app.get("/api/run/{job_id}")
async def api_status(job_id: str):
    job = store.get(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return {"status": job["status"], "result": job.get("result"), "error": job.get("error")}


@app.get("/api/me")
async def api_me(request: Request):
    """Tell the frontend who it is and whether the polished PDF is already unlocked."""
    authed = auth.user_from_request(request)
    if not authed:
        return {"signed_in": False, "auth_enabled": auth.AUTH_ENABLED,
                "pdf_billing": billing.PDF_BILLING_ENABLED,
                "pdf_price": billing.PDF_PRICE_CENTS,
                "sub_enabled": billing.PDF_BILLING_ENABLED, "tiers": tiers.catalog()}
    tier = _tier(authed["email"])
    return {"signed_in": True, "email": authed["email"],
            "pdf_unlocked": _has_pdf_access(authed["email"]),   # comp grant OR subscription → unlimited
            "pdf_credits": billing.credits_left(authed["email"]),   # paid plan-unlock credits remaining
            "pdf_billing": billing.PDF_BILLING_ENABLED, "pdf_price": billing.PDF_PRICE_CENTS,
            "auth_enabled": auth.AUTH_ENABLED,
            # subscription state for the fork UI + usage meter
            "sub_enabled": billing.PDF_BILLING_ENABLED, "tiers": tiers.catalog(),
            "tier": tier, "tier_label": tiers.label(tier) if tier else None,
            "subscription": _budget(authed["email"], tier)}


@app.post("/api/plan/{sid}/buy-pdf")
async def api_buy_pdf(sid: str, request: Request):
    """Start the $13 Checkout that unlocks THIS plan's clean PDF (re-download free forever). Requires
    a signed-in owner of a finished plan. If the plan is already unlocked (or there's a comp credit
    to spend on it), no payment is needed — the client just downloads. Raw export stays free."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    if not billing.PDF_BILLING_ENABLED:
        return JSONResponse({"error": "Billing isn't configured yet."}, status_code=503)
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    # Already accessible (comped or already unlocked), or there are credits left → no payment needed.
    if _has_pdf_access(authed["email"], _plan_key(s)) or billing.credits_left(authed["email"]) > 0:
        return JSONResponse({"error": "You can download this plan already.", "unlocked": True},
                            status_code=409)
    try:
        url = billing.create_pdf_checkout_url(authed["email"], user_id=authed["id"], plan_id=sid,
                                              plan_key=_plan_key(s))
    except billing.StripeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"url": url}


@app.post("/api/subscribe")
async def api_subscribe(request: Request):
    """Start a MONTHLY subscription Checkout for a paid tier (Starter/Pro/Studio). Signed-in only.
    Returns the hosted Stripe URL; the webhook activates the account on completion."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    if not billing.PDF_BILLING_ENABLED:
        return JSONResponse({"error": "Billing isn't configured yet."}, status_code=503)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    tier = tiers.canonical((body.get("tier") or "").strip())
    if not tiers.purchasable(tier):   # hidden tiers stay honored on accounts but are never sold
        return JSONResponse({"error": "Unknown plan."}, status_code=400)
    if _tier(authed["email"]) == tier:
        return JSONResponse({"error": f"You're already on {tiers.label(tier)}.", "current": True},
                            status_code=409)
    try:
        url = billing.create_subscription_checkout_url(
            authed["email"], tier=tier, price_cents=tiers.price_cents(tier),
            label=tiers.label(tier), user_id=authed["id"])
    except billing.StripeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"url": url}


@app.post("/api/subscription/portal")
async def api_subscription_portal(request: Request):
    """Open a Stripe billing-portal session (update card / cancel). Signed-in subscriber only."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    acct = store.account_get(_acct(authed["email"])) or {}
    cid = acct.get("stripe_customer_id")
    if not cid:
        return JSONResponse({"error": "No subscription to manage."}, status_code=400)
    try:
        url = billing.create_portal_url(cid)
    except billing.StripeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"url": url}


@app.get("/api/plan/{sid}/nudges")
async def api_plan_nudges(sid: str, request: Request):
    """Per-step quick-edit chips for the feedback modal — short, business + current-section specific.
    One cheap call on the owner's key; the frontend caches them per node."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    p = s.get("proposal") or {}
    draft, section = p.get("draft") or "", p.get("section")
    if not draft or not section:
        return {"chips": []}
    idea = planner._working_idea(s)
    try:
        with _run_slot(_slot_user(s), s.get("stack")):
            chips, cost = planner.nudges(idea, section, draft, mock=MOCK)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return {"chips": []}   # nudges are a nicety — never block the modal on them
    nc, nt = _fold_usage(sid, s, cost, toks)
    return {"chips": chips, "cost": nc, "tokens": nt}


def _validate_key(provider_name: str, api_key: str) -> tuple[bool, str]:
    """One cheap call confirms a BYOK key works before we store it. Skipped in mock mode (no spend)."""
    if provider_name not in keys.PROVIDERS:
        return False, "Unsupported provider."
    if len(api_key) < 8:
        return False, "That doesn't look like an API key."
    if MOCK:
        return True, "ok (mock)"
    try:
        from engine import provider as prov_mod
        from engine import pipeline
        with prov_mod.use(_build_provider(provider_name, api_key)):
            out = pipeline.call("key_validate", pipeline.HAIKU, "Reply with: OK", max_tokens=5)
        return (True, "ok") if out else (False, "The key didn't return a response.")
    except Exception as e:  # noqa: BLE001
        msg, _ = _humanize_error(e)
        if msg.startswith("Something went wrong"):
            # validation is the user debugging their OWN key — surface a trimmed real reason
            raw = " ".join(str(e).split())[:180]
            msg = f"That key didn't validate: {raw}"
        return False, msg


@app.get("/api/key")
async def api_key_get(request: Request):
    """BYOK status for the current user: whether the feature is on, plus any saved key (masked)."""
    if not keys.enabled():
        return {"enabled": False}
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return {"enabled": True, "signed_in": False, "key": None}
    return {"enabled": True, "signed_in": True, "key": keys.key_meta(authed["email"]),
            "providers": list(keys.PROVIDERS)}


@app.post("/api/key")
async def api_key_save(request: Request):
    """Validate a user's API key with one cheap call, then store it ENCRYPTED. Requires sign-in."""
    if not keys.enabled():
        return JSONResponse({"error": "BYOK isn't configured yet."}, status_code=503)
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    body = await request.json()
    api_key = (body.get("key") or "").strip()
    sel = (body.get("provider") or "").strip()    # the provider the user picked in the modal
    provider_name = sel if sel in keys.PROVIDERS else _key_provider_kind(api_key)  # fall back to prefix
    ok, why = _validate_key(provider_name, api_key)
    if not ok:
        return JSONResponse({"error": why}, status_code=400)
    try:
        meta = keys.save_key(authed["email"], provider_name, api_key)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "key": meta}


@app.post("/api/key/remove")
async def api_key_remove(request: Request):
    """Forget a user's stored key."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    keys.delete_key(authed["email"])
    return {"ok": True}


@app.delete("/api/account")
async def api_delete_account(request: Request):
    """Permanently delete the signed-in user's account: all their projects, PDF purchases/unlocks, and
    their stored BYOK key. Irreversible. (Supabase identity itself is managed by Supabase; this clears
    everything FILG holds for them.)"""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    store.delete_account(authed["email"], auth.normalize_email(authed["email"]))
    try:
        keys.delete_key(authed["email"])
    except Exception:  # noqa: BLE001 — key store is best-effort; account data is already gone
        pass
    return {"ok": True}


@app.post("/api/stripe/webhook")
async def api_stripe_webhook(request: Request):
    """Stripe → us: flip subscription state. Verifies the signature before trusting the body."""
    payload = await request.body()
    event = billing.verify_webhook(payload, request.headers.get("stripe-signature", ""))
    if event is None:
        return JSONResponse({"error": "invalid signature"}, status_code=400)
    billing.handle_event(event)
    return {"received": True}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mock": MOCK, "auth_enabled": auth.AUTH_ENABLED,
            "pdf_billing": billing.PDF_BILLING_ENABLED, "sub_enabled": billing.PDF_BILLING_ENABLED,
            "models": model_catalog.snapshot()["slots"], **usage.snapshot()}


@app.get("/api/models")
async def api_models(request: Request):
    """The model catalog: each id with its rung / price / OpenRouter slug, the current logical-slot
    resolution, and — with ?check=1 — a cached Anthropic Models API availability pass plus any ids the
    API reports that we don't yet catalog (the 'a new model shipped, go place + price it' signal).
    Public + read-only; ids/prices/rungs aren't secret. Availability is cached (FILG_MODELS_TTL)."""
    check = request.query_params.get("check") in ("1", "true", "yes")
    return model_catalog.snapshot(check_availability=check)


@app.get("/r/{job_id}", response_class=HTMLResponse)
async def share(job_id: str):
    job = store.get(job_id)
    if not job or job.get("status") != "done":
        return HTMLResponse(render.not_found("This result isn't ready yet, failed, or doesn't exist."),
                            status_code=404)
    return render.result_page(job)


@app.get("/p/{sid}", response_class=HTMLResponse)
async def share_plan(sid: str):
    """Public, read-only view of a plan the owner explicitly shared (private by default). Carries the
    two things nothing else in-market shows: the graded receipts and the decision path that led here —
    the share page IS the pitch, watermarked with the maker line."""
    s = store.plan_get(sid)
    if not s or not s.get("shared"):
        return HTMLResponse(render.not_found("This plan isn't shared or doesn't exist."), status_code=404)
    idea = planner._working_idea(s)
    inner = markdown.markdown(planner.bundle_markdown(idea, s.get("files") or {}), extensions=["extra"])
    title = ((s.get("shaped") or {}).get("thesis") or s["idea"] or "Shared business plan")[:120]
    rows = ((s.get("research") or {}).get("rows") or [])
    return HTMLResponse(render.shared_plan_page(title, inner, receipts=rows, path=_share_path(s)))


def _share_path(s: dict) -> list[dict]:
    """The committed decision path (root → active) as public-safe steps: kind + label + the operator's
    pivot/steer note. This is the provenance trail — what the plan decided and why, not just the output.
    Reads the STORED tree (nodes = dict keyed by id; kind derived via _kind), not the frontend view."""
    tree = s.get("tree") or {}
    nodes = tree.get("nodes") or {}
    out = []
    for n in dtree.chain(nodes, tree.get("active")):
        out.append({"kind": _kind(n),
                    "label": dtree.label(n, f"Part {int(n.get('step') or 0) + 1}"),
                    "note": (n.get("feedback") or "").strip()})
    return out


# ── Interactive plan builder (idea → decision tree → downloadable file tree) ──
def _identity(request: Request, body_email: str | None = None) -> tuple[str, bool]:
    """Resolve (user, verified) — a verified Supabase user wins; else the body email (free tier)."""
    authed = auth.user_from_request(request)
    if authed and authed["email"]:
        return authed["email"], True
    return (body_email or "").strip().lower(), False


def _owns(request: Request, session: dict) -> bool:
    """Whether the requester may access this plan session. When auth is OFF (free/dev), the id is the
    capability and there's no identity to enforce, so allow. When auth is ON, a session owned by a
    verified email is private: require the requester's verified email to match. Ownerless legacy/anon
    sessions stay open. Callers return 404 (not 403) on failure so a plan's existence doesn't leak."""
    if not auth.AUTH_ENABLED:
        return True
    owner = (session.get("user") or "").strip().lower()
    if not owner:
        return True
    authed = auth.user_from_request(request)
    return bool(authed and (authed["email"] or "").strip().lower() == owner)


def _plan_state(s: dict) -> dict:
    """Shape a session row for the frontend."""
    return {
        "id": s["id"], "status": s["status"], "idea": s["idea"], "error": s.get("error"),
        "research": s.get("research"),
        "shaped": s.get("shaped"), "vetting": s.get("vetting"),
        "directors": s.get("directors") or [], "board": s.get("board") or [],
        "customDirectors": [{"key": p["key"], "name": p["name"], "first": p.get("first"),
                             "blurb": p.get("blurb"), "domains": p.get("domains") or []}
                            for p in (s.get("custom_directors") or [])],   # custom-forged board chips
        "files": [{"path": p, "content": c} for p, c in (s.get("files") or {}).items()],
        "sections": [{"file": x["file"], "title": x["title"], "sub": x["sub"]}
                     for x in planner.SECTIONS],
        "step": s.get("step", 0), "total": planner.N, "proposal": s.get("proposal"),
        "done": s["status"] == "done", "shared": bool(s.get("shared")),
        "owned": bool((s.get("user") or "").strip()),   # anonymous taste vs claimed-by-an-account
        "qa": s.get("qa"),   # final QA-pass report {notes, fixed} on the finished plan

        "tree": _tree_view(s["tree"]) if s.get("tree") else None,
        "stage": s.get("stage"),                      # funnel position: brainstorm | refined | building | done
        "activeNode": _active_node_view(s),           # the active node's funnel payload (option cards / refined idea / fork)
        "chat": s.get("chat") or [], "chatStarters": advisor.STARTERS,
        "lookups": s.get("lookups") or [],   # persisted chat-lookup claims — the research stack survives a reload
        # the stress-test's durable state (status + result only; live progress rides its poll route)
        "skeptic": ({"status": (s.get("skeptic") or {}).get("status"),
                     "result": (s.get("skeptic") or {}).get("result")} if s.get("skeptic") else None),
        "progress": s.get("progress") or [],
        "cost": s.get("cost") or 0, "tokens": s.get("tokens") or 0,   # live session usage meter
        "stack": s.get("stack") or provider.DEFAULT_STACK,            # chosen model stack
        # PDF access for THIS active branch + the account's remaining credits. The button shows
        # Download when this plan is already unlocked OR there are credits to spend; else Unlock ($7=3).
        # A new branch built from an earlier node is a fresh plan_key → locked until claimed.
        "pdfUnlocked": _has_pdf_access((s.get("user") or "").strip(), _plan_key(s), verified=True),
        "pdfCredits": store.credits_left((s.get("user") or "").strip()),
    }


# ── Branching decision tree — engine/tree.py owns the wiring; planner supplies pure content ──
_new_node = dtree.new_node   # node bookkeeping (id / parent / children) has one owner: the engine
_kind = context.kind         # one owner for "what kind is this node" — the context engine


def _tree_view(tree: dict) -> dict:
    """Trim the stored node tree to what the frontend needs to draw + navigate it. Carries each node's
    `kind` so the graph can render option/refined/section/fork nodes differently. `show` is on from the
    first render (even a single node) so the decision-graph surface is always there."""
    nodes = tree.get("nodes") or {}
    return {"active": tree.get("active"),
            "nodes": [{"id": n["id"], "parent": n.get("parent"), "step": n.get("step", 0),
                       "kind": _kind(n), "title": n.get("title"), "feedback": n.get("feedback"),
                       # a refined node names the options it JOINED — the graph draws it as a merge
                       # of those branches, not a sibling branch off the brainstorm fork
                       **({"selected": n.get("selected")} if n.get("selected") else {})}
                      for n in nodes.values()],
            "show": bool(nodes)}


def _active_node_view(s: dict) -> dict | None:
    """The active node's full funnel payload, so the frontend can render the current stage (the option
    cards, the refined idea, a pending fork) without a second fetch. Section nodes carry no extra
    payload (the existing `proposal`/`sections` fields already cover them)."""
    t = s.get("tree") or {}
    a = (t.get("nodes") or {}).get(t.get("active"))
    if not a:
        return None
    k = _kind(a)
    view = {"id": a["id"], "kind": k, "title": a.get("title"),
            "log": a.get("log") or []}   # persisted build receipts, so a reload restores them (§v2 #10)
    if k == "brainstorm":
        view["spread"] = a.get("spread")
        view["feedback"] = a.get("feedback")   # the pivot ask this spread answers (if any)
        view["set_aside"] = a.get("set_aside")  # declined-out-loud part of the ask — shown, not hidden
        view["options"] = [{"id": c, "direction": ((t["nodes"].get(c) or {}).get("direction"))}
                           for c in a.get("children", []) if _kind(t["nodes"].get(c) or {}) == "option"]
    elif k == "option":
        view["direction"] = a.get("direction")
    elif k == "refined":
        for f in ("thesis", "founder_edge", "mold", "kept", "dropped", "research", "selected",
                  "questions", "feedback"):
            view[f] = a.get(f)
    elif k == "fork":
        view["question"] = a.get("question")
        view["options"] = a.get("options")
    return view


def _mirror(tree: dict) -> dict:
    """Flat session fields (step/files/proposal/history/board/status) for the active node, so the
    existing _plan_state + frontend renders keep working off the active branch unchanged. A funnel node
    (brainstorm/option/refined/fork) isn't a plan section, so it mirrors to neutral 'building' state with
    no proposal — the funnel payload rides on _active_node_view instead."""
    a = tree["nodes"][tree["active"]]
    if _kind(a) != "section":
        # funnel nodes carry no plan state, but chat convenes stored on them still surface
        return {"step": 0, "files": {}, "history": [], "board": a.get("board") or [],
                "proposal": None, "qa": None, "status": "building"}
    done = a["step"] >= planner.N
    return {"step": a["step"], "files": a["files"], "history": a["history"], "board": a["board"],
            "proposal": (None if done else {"section": a["section"], "title": a["title"],
                                            "draft": a["draft"], "change": a.get("change")}),
            "qa": a.get("qa") if done else None,   # the final QA-pass report, surfaced on the finished branch
            "status": "done" if done else "building",
            # the funnel stage must land on 'done' too, or the frontend keeps offering the next
            # chapter forever ('Part 8 of 7' + Keep going — the off-ramp bug, 2026-07-04)
            **({"stage": "done"} if done else {})}


def _regrade_setup(s: dict, node: dict, cost: float) -> tuple[dict | None, float]:
    """When the SETUP (step-0) section is reframed, re-grade the idea against the new angle so the
    PURSUE/PIVOT verdict + reaction track it. Only the setup stage triggers a re-grade. Must run on the
    bound provider (call inside `_run_slot`). Returns (new_vetting_or_None, cost_including_revet). A
    re-grade failure never breaks the underlying redraft/back."""
    if (node or {}).get("step") != 0:
        return None, cost
    try:
        vetting, vc = intake.vet(planner._working_idea(s), s.get("shaped") or {}, s.get("research"),
                                 mock=MOCK, angle=(node.get("draft") or ""))
    except Exception:  # noqa: BLE001
        return None, cost
    return vetting, round(cost + vc, 4)


def _ensure_tree(s: dict) -> dict:
    """Return the session's node tree, lazily seeding a single-node tree from legacy flat state for
    plans created before branching existed (so resumed in-progress plans still go Next/Back)."""
    t = s.get("tree")
    if t and t.get("nodes"):
        return t
    p = s.get("proposal") or {}
    step = s.get("step", 0) or 0
    sec = next((x for x in planner.SECTIONS if x["key"] == p.get("section")), None)
    node = _new_node({"step": step, "section": (sec or {}).get("key"),
                      "title": (sec or {}).get("title", "Plan complete"),
                      "sub": (sec or {}).get("sub", ""), "draft": p.get("draft"),
                      "files": s.get("files") or {}, "history": s.get("history") or [],
                      "board": s.get("board") or [], "feedback": None}, None)
    return dtree.seed(node)


def _plan_research(session_id: str, idea: str, user: str) -> None:
    try:
        progress: list[str] = []
        def on_progress(line: str) -> None:  # write each real milestone/receipt + the running usage so
            progress.append(line)            # the UI spews it live AND the session meter ticks during research
            store.plan_save(session_id, progress=list(progress),
                            tokens=pipeline.LEDGER.tokens(), cost=round(pipeline.LEDGER.cost(), 4))
        prov = _provider_for(user)   # user's own key if they have one, else FILG's hosted key
        sess0 = store.plan_get(session_id) or {}
        # Stack ceiling: BYOK → any; subscriber → up to their tier; free taste on FILG's key → Opus-free default.
        stk = tiers.clamp_stack(sess0.get("stack"), tier=_tier(user),
                                byok=bool(prov and not prov.bills_filg))
        with provider.use(prov), provider.use_stack(stk), pipeline.run_ledger():
            prep = planner.prepare(idea, mock=MOCK, on_progress=on_progress)  # intake → research → vet → draft
            toks = pipeline.LEDGER.tokens()   # the welcome run's token usage → seeds the session meter
        if prov is not None and prov.bills_filg:   # runs on FILG's hosted key → meter it (invariant #3)
            if _is_subscriber(user):               # subscriber → count against their monthly fair-use cap
                usage.record_monthly(_acct(user), _period(user), prep["cost"], toks)
            else:                                  # free taste → per-user counter (alias-deduped) + daily
                usage.record_run(auth.normalize_email(user), prep["research_cost"])  # free run + daily total
                usage.record_spend(prep["cost"] - prep["research_cost"])  # intake + vet + first draft → daily
        root = _new_node(planner.root_node(prep["proposal"]), None)  # seed the decision tree's root
        root["log"] = _op_log(progress, 0)
        tree = dtree.seed(root)
        store.plan_save(session_id, status="building", research=prep["research"], step=0,
                        proposal=prep["proposal"], shaped=prep["shaped"], vetting=prep["vetting"],
                        cost=prep["cost"], tokens=toks, tree=tree, progress=progress)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()  # full trace → Render stdout logs (client only sees str(e))
        store.plan_save(session_id, status="error", error=_humanize_error(e)[0])


# ── The diverge/converge funnel (brainstorm → merge → commit), layered on the same node tree ──
# Per-node history bounds (board=12, log=40) live in the attachment registry (app/domain/nodes.py).
_op_log = dtree.op_log   # one op's progress slice, sentinel-free + bounded, persisted on its node


def _attach_spread(tree: dict, diverge: dict, parent: str | None,
                   board: list | None = None) -> str:
    """Attach a `brainstorm` fork node + one `option` child per direction into `tree`, and land
    the active pointer on the fork (the node being decided). Returns the fork's id. `board` seeds
    the fork and its options with the pivot point's board history (a spread's options branch from
    the FORK, so they inherit through it)."""
    b = dtree.attach(tree, _new_node(
        {"kind": "brainstorm", "step": 0, "title": "A few directions",
         "spread": diverge.get("spread"), "draft": None, "files": {}, "history": [],
         # a declared set-aside (part of the ask the engine declined, with its reason) is
         # SURFACED, never silent — it rides the node so every view can show it
         "set_aside": diverge.get("set_aside"),
         "board": dtree.clip("board", board or [])}, parent))
    for d in (diverge.get("directions") or []):
        dtree.attach(tree, _new_node(
            {"kind": "option", "step": 0, "title": d.get("title") or "Direction",
             "direction": d, "draft": d.get("one_liner"), "files": {}, "history": []},
            b["id"]), activate=False, inherit=True)
    tree["active"] = b["id"]
    return b["id"]


def _refined_node(m: dict, selected: list, parent: str | None, feedback: str | None = None) -> dict:
    """The reconciled single idea (+ the adversarial cull + a light research skim + any clarifying
    questions) as one `refined` node, a child of the brainstorm fork — or, for a /refine re-merge,
    a child of the refined node it sharpens. `selected` records which option ids fed the merge;
    `feedback` is the operator's clarification a refine folded in (visible on the node forever)."""
    return _new_node({"kind": "refined", "step": 0, "title": "Refined idea", "thesis": m["thesis"],
                      "founder_edge": m.get("founder_edge"), "mold": m.get("mold"),
                      "kept": m.get("kept"), "dropped": m.get("dropped"), "research": m.get("research"),
                      "questions": m.get("questions") or [], "feedback": feedback or None,
                      "selected": selected, "draft": m["thesis"], "files": {}, "history": [],
                      "board": []}, parent)


def _meter_bg(user: str, prov, cost: float, toks: int, *, is_run: bool = False,
              research_cost: float = 0.0) -> None:
    """Meter a background funnel op that ran on FILG's hosted key. A subscriber's usage counts against
    their monthly cap; a free user's daily kill-switch is always fed (record_spend); only a COMMIT (the
    deep research run) counts as a metered free 'run'. Ops on a user's own key aren't FILG's spend."""
    if prov is None or not getattr(prov, "bills_filg", False):
        return
    if _is_subscriber(user):
        usage.record_monthly(_acct(user), _period(user), cost, toks)
        return
    if is_run:
        usage.record_run(auth.normalize_email(user), research_cost)
        usage.record_spend(round(cost - research_cost, 4))
    else:
        usage.record_spend(cost)


def _bg_progress(sid: str, base_cost: float, base_tokens: int, progress: list):
    """A progress sink for a background funnel op: append the line + persist the running (base + this
    run's) cost/tokens so the session meter ticks live during the op."""
    def on_progress(line: str) -> None:
        progress.append(line)
        store.plan_save(sid, progress=list(progress), tokens=base_tokens + pipeline.LEDGER.tokens(),
                        cost=round(base_cost + pipeline.LEDGER.cost(), 4))
    return on_progress


def _fresh_tree_or_abandon(sid: str, tok: str | None):
    """Run-epoch check for a finishing background run: re-read the session and return
    (session, tree) to attach into — the FRESH tree, so nothing written mid-run is clobbered.
    If the user moved on (pivoted / started another run: the tree's `_run` token changed),
    return None and mark the abandonment in the progress log. The in-flight spend is already
    metered; only the RESULT is discarded — the user's newer state always wins."""
    s1 = store.plan_get(sid) or {}
    tree = s1.get("tree") or {}
    if not dtree.run_is_current(tree, tok):
        prog = list(s1.get("progress") or []) + ["✂ run abandoned — you moved on before it finished"]
        store.plan_save(sid, progress=prog)
        return None
    return s1, tree


def _run_merge(sid: str, option_ids: list, user: str, tok: str | None = None,
               refine_of: str | None = None, note: str | None = None) -> None:
    """Background: reconcile the chosen directions into one refined idea (+ light research skim), then
    attach a `refined` node under the brainstorm fork and advance the active pointer to it.
    `refine_of` + `note` = the /refine re-merge: fold the operator's clarification (answers to the
    gate's questions, a steer) into a re-reconcile of the SAME picks, and chain the new refined node
    under the one it sharpens — the refinement lineage stays visible in the graph."""
    try:
        s0 = store.plan_get(sid) or {}
        base_cost, base_tokens = s0.get("cost") or 0, s0.get("tokens") or 0
        progress = list(s0.get("progress") or [])
        nodes0 = (s0.get("tree") or {}).get("nodes") or {}
        directions = [(nodes0.get(i) or {}).get("direction") for i in option_ids]
        directions = [d for d in directions if d]
        idea_in = s0["idea"]
        if refine_of and note:
            prev = ((nodes0.get(refine_of) or {}).get("thesis") or "").strip()
            idea_in = (f"{s0['idea']}\n\nTHE MERGED THESIS SO FAR (refine THIS, do not start over):\n"
                       f"{prev}\n\nTHE OPERATOR'S CLARIFICATION — it outweighs everything above; fold "
                       f"it in and do not re-ask what it answers:\n{note}")
        prov = _provider_for(user)
        stk = tiers.clamp_stack(s0.get("stack"), tier=_tier(user), byok=bool(prov and not prov.bills_filg))
        with provider.use(prov), provider.use_stack(stk), pipeline.run_ledger():
            m, cost = brainstorm.merge(idea_in, directions, mock=MOCK,
                                       on_progress=_bg_progress(sid, base_cost, base_tokens, progress))
            toks = pipeline.LEDGER.tokens()
        _meter_bg(user, prov, cost, toks)
        fresh = _fresh_tree_or_abandon(sid, tok)
        if fresh is None:
            return                                # the user pivoted mid-run — their newer state wins
        _s1, tree = fresh
        nodes = tree.get("nodes") or {}
        # a refine chains under the refined node it sharpens (if it survived the wait); a first
        # merge joins its picks under the brainstorm fork
        parent = (refine_of if refine_of and nodes.get(refine_of) else tree.get("active"))
        refined = _refined_node(m, [] if refine_of else option_ids, parent, feedback=note)
        refined["log"] = _op_log(progress, len(s0.get("progress") or []))
        dtree.attach(tree, refined, inherit=True)
        store.plan_save(sid, status="building", stage="refined", tree=tree, progress=progress,
                        cost=round(base_cost + cost, 4), tokens=base_tokens + toks)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        store.plan_save(sid, status="error", error=_humanize_error(e)[0])


def _deep_build(sid: str, thesis: str, user: str, tok: str | None = None,
                picks: list | None = None, prior: dict | None = None) -> None:
    """Background: the COMMIT step — the one deep research run + first section draft, attached to the
    tree under the active (refined) node so the funnel history is preserved. Same engine as the legacy
    welcome run, but it grows the existing tree instead of reseeding a fresh root. `picks` = option
    ids a direct brainstorm-commit chose: recorded as `selected` on the built node so the graph draws
    the join through them (the choice is part of the story, not just its text). `prior` (T2) = the
    refined node's carried merge-skim payload; when the committed thesis is unchanged, prepare reuses
    those already-fetched claims (re-graded at full strength) instead of re-running the web fan-out."""
    try:
        s0 = store.plan_get(sid) or {}
        base_cost, base_tokens = s0.get("cost") or 0, s0.get("tokens") or 0
        progress = list(s0.get("progress") or [])
        prov = _provider_for(user)
        stk = tiers.clamp_stack(s0.get("stack"), tier=_tier(user), byok=bool(prov and not prov.bills_filg))
        with provider.use(prov), provider.use_stack(stk), pipeline.run_ledger():
            prep = planner.prepare(thesis, mock=MOCK, prior=prior,
                                   on_progress=_bg_progress(sid, base_cost, base_tokens, progress))
            toks = pipeline.LEDGER.tokens()
        _meter_bg(user, prov, prep["cost"], toks, is_run=True, research_cost=prep["research_cost"])
        fresh = _fresh_tree_or_abandon(sid, tok)
        if fresh is None:
            return                                # the user pivoted mid-run — their newer state wins
        _s1, tree = fresh
        nodes = tree.get("nodes") or {}
        root = _new_node(planner.root_node(prep["proposal"]), tree.get("active"))  # a plain section node (kind absent)
        if picks:
            root["selected"] = [i for i in picks if i in nodes]   # the join the graph rides through
        root["log"] = _op_log(progress, len(s0.get("progress") or []))
        dtree.attach(tree, root, inherit=True)
        store.plan_save(sid, status="building", stage="building", research=prep["research"], step=0,
                        proposal=prep["proposal"], shaped=prep["shaped"], vetting=prep["vetting"],
                        tree=tree, progress=progress, cost=round(base_cost + prep["cost"], 4),
                        tokens=base_tokens + toks)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        store.plan_save(sid, status="error", error=_humanize_error(e)[0])


# ── The context engine (app/context.py) owns every model-facing view of session state. These
# aliases keep main.py's historical names; DO NOT grow new context strings here — add to the
# engine's renderers/views so every consumer inherits the change (see tests/test_context.py).
_node_snippet = context.snippet
_journey_digest = context.journey
_route_context = context.screen


def _path_snippets(nodes: dict, at_id: str | None) -> list[str]:   # legacy signature shim
    return context.path({"tree": {"nodes": nodes}}, at_id)


@app.post("/api/plan/start")
async def api_plan_start(request: Request):
    body = await request.json()
    idea = (body.get("idea") or "").strip()
    if len(idea) < 12:
        return JSONResponse({"error": "Tell me a bit more about the idea."}, status_code=400)
    if gibberish.looks_like_gibberish(idea):  # total nonsense → roast them for free (no run, no LLM)
        return JSONResponse({"gibberish": True, **gibberish.roast(idea)})
    user, verified = _identity(request, body.get("email"))
    if "@" not in user:
        return JSONResponse({"error": "Enter an email so we can save your plan."}, status_code=400)
    # The legacy one-shot welcome taste retired with v1 (2026-07-06): in the auth-on regime this
    # route requires a verified account AND a key/subscription, same as every other deep-build verb.
    # (The new funnel's free taste is /api/brainstorm → /merge; this route jumps straight to the
    # deep research run, so an open POST here would be an unauthenticated spend hole on our key.)
    if auth.AUTH_ENABLED:
        if not verified:
            return JSONResponse(
                {"error": "Create a free account to build a plan — it saves to your account.",
                 "needAccount": True}, status_code=401)
        if _needs_key(user):
            return JSONResponse(
                {"error": "Keep building free on your own API key (OpenRouter or Anthropic), or "
                          "subscribe to run on ours.", "needKey": True}, status_code=402)
    taste_id = auth.normalize_email(user)   # dedupe the free taste across +suffix / gmail-dot aliases
    if _is_byok(user):
        pass   # has a key → unlimited plans on their own spend
    elif _is_subscriber(user):
        # Subscriber: runs on FILG's key, bounded by the monthly fair-use cap. Over it → offer the
        # two off-ramps (own key, or wait for the renewal reset) instead of building on our dime.
        b = _budget(user)
        if b and b["over"]:
            return JSONResponse(
                {"error": "You've used this month's plan allowance on our key. Add your own API key to "
                          "keep building free, or your allowance resets when your subscription renews.",
                 "fairUse": True, "needKey": True, "resetAt": b.get("reset_at")}, status_code=402)
        # else: runs on FILG's hosted key → _plan_research meters it against the monthly cap
    elif keys.enabled():
        # BYOK on, no key: the FIRST query is on us when FILG has a hosted key (metered + kill-switch).
        # Once the free taste is used (or the daily budget is hit), degrade to a key prompt, not a wall.
        if not HOSTED_FREE:
            return JSONResponse(
                {"error": "Add your API key to build your plan — an OpenRouter key (any model) or your "
                          "own Anthropic key (Claude direct), usually pennies a plan.",
                 "needKey": True}, status_code=402)
        allowed, _reason = usage.can_run(taste_id, is_paid=False)   # per-user free cap + daily kill switch
        if not allowed:
            return JSONResponse(
                {"error": "Your free plan is used up (or today's free pool is tapped). Add your own "
                          "API key to keep building — usually pennies a plan.",
                 "needKey": True}, status_code=402)
        # else: first query on the house → runs on FILG's hosted key; _plan_research meters it
    else:
        # BYOK off (no FILG_KEY_SECRET — dev/local): keep the legacy free-cap behavior so dev works.
        allowed, reason = usage.can_run(taste_id, is_paid=False)
        if not allowed:
            return JSONResponse({"error": reason}, status_code=402)
    directors = [k for k in (body.get("directors") or []) if k in personas.KEYS]  # optional board
    sid = uuid.uuid4().hex[:12]
    store.plan_create(sid, user, idea, directors=directors)
    store.plan_save(sid, stack=provider.stack_name(body.get("stack")))  # honor the crew picked at intake
    threading.Thread(target=_plan_research, args=(sid, idea, user), daemon=True).start()
    return {"id": sid}


@app.post("/api/brainstorm")
async def api_brainstorm(request: Request):
    """Top of the funnel — ANONYMOUS, no email/login required. Spread a raw prompt into 1-3 loose,
    vetted-shape directions (pure LLM, no web, cheap) and seed the decision tree with a brainstorm fork
    + one option node per direction. This is the free, frictionless entry point."""
    body = await request.json()
    idea = (body.get("idea") or "").strip()
    if len(idea) < 12:
        return JSONResponse({"error": "Tell me a bit more about the idea."}, status_code=400)
    if gibberish.looks_like_gibberish(idea):   # total nonsense → free roast, no run
        return JSONResponse({"gibberish": True, **gibberish.roast(idea)})
    user, _verified = _identity(request, body.get("email"))   # may be "" (anonymous) — that's allowed here
    directors = [k for k in (body.get("directors") or []) if k in personas.KEYS]
    sid = uuid.uuid4().hex[:12]
    store.plan_create(sid, user, idea, directors=directors)
    store.plan_save(sid, stack=provider.stack_name(body.get("stack")))
    def _work():   # off the event loop: other requests (node reads, polls) stay live while this thinks
        with _run_slot(user or f"anon:{sid}", body.get("stack")):
            d, cost = brainstorm.diverge(idea, mock=MOCK)
            return d, cost, pipeline.LEDGER.tokens()
    try:
        d, cost, toks = await run_in_threadpool(_work)
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    _meter_bg(user, _provider_for(user), cost, toks)
    # The tree roots at a BASE `idea` node (the raw prompt). Every spread — including later pivots and
    # step-one rebuilds — branches beneath it, so alternate takes always share a common ancestor and
    # no branch is ever orphaned.
    base = _new_node({"kind": "idea", "step": 0, "title": "Your idea", "draft": idea,
                      "files": {}, "history": [], "board": []}, None)
    tree = dtree.seed(base)
    _attach_spread(tree, d, base["id"])
    store.plan_save(sid, status="building", stage="brainstorm", tree=tree,
                    cost=round(cost, 4), tokens=toks)
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/rebrainstorm")
async def api_plan_rebrainstorm(sid: str, request: Request):
    """Re-spread WITHIN the same tree: a pivot, a 'show me other directions', or a 'start over but
    keep X'. Runs diverge and attaches a new brainstorm fork off the PIVOT POINT (the active node; a
    re-spread while already on a fork lands as its sibling), so the old branch stays in the graph."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    idea = (body.get("idea") or s.get("idea") or "").strip()
    feedback = (body.get("feedback") or "").strip()
    if not feedback and len(idea) < 12:
        return JSONResponse({"error": "Tell me a bit more about the idea."}, status_code=400)
    # keyboard-mash pivots get the same free roast as a mash at the landing box (no run, no LLM) —
    # the v2 chat renders it as a bot bubble instead of quietly spreading nonsense into the tree
    if gibberish.looks_like_gibberish(feedback or idea):
        return JSONResponse({"gibberish": True, **gibberish.roast(feedback or idea)})
    tree = s.get("tree") or {"nodes": {}, "active": None}
    nodes = tree.get("nodes") or {}
    # the pivot point: an explicitly named node (the one the user had open) beats the active one
    at = nodes.get((body.get("node") or "").strip()) or nodes.get(tree.get("active")) or {}
    # THE PIVOT CONTRACT: context = the pivot node + its ancestors (root → node, last item = chosen);
    # siblings/descendants dropped; the pivot feedback OUTWEIGHS all of it.
    path = _path_snippets(nodes, at.get("id")) if at else []
    path_block = "\n".join(f"- {p}" for p in path) or f"- the original idea: {s.get('idea', '')[:160]}"
    if feedback:
        div_input = (
            f"THE OPERATOR IS PIVOTING. Their pivot instruction OUTWEIGHS everything below — the new "
            f"directions must be a genuine change of course that honors it:\n{feedback}\n\n"
            f"COMMITTED PATH (root \u2192 the pivot point; treat each item, especially the LAST, as "
            f"chosen context — nothing outside this path applies):\n{path_block}")
    else:
        div_input = (f"{idea}\n\nCOMMITTED PATH (root \u2192 the pivot point; treat each item as "
                     f"chosen context):\n{path_block}")
    def _work():
        with _run_slot(_slot_user(s), s.get("stack")):
            d, cost = brainstorm.diverge(div_input, mock=MOCK)
            return d, cost, pipeline.LEDGER.tokens()
    try:
        d, cost, toks = await run_in_threadpool(_work)
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    _meter_bg(s.get("user"), _provider_for(s.get("user")), cost, toks)
    # every node is branchable: the new spread is a CHILD of the pivot node itself, always
    parent = at.get("id")
    # permanent evidence line — pivots are core IP, every hop must be verifiable in the server log
    print(f"[pivot] sid={sid} node_in={(body.get('node') or None)!r} resolved={at.get('id')}/"
          f"{_kind(at) if at else None} parent={parent} feedback={feedback[:80]!r}")
    tree["nodes"] = nodes
    bid = _attach_spread(tree, d, parent, board=(at or {}).get("board"))
    nodes[bid]["feedback"] = feedback or idea[:120]   # the pivot ask, visible on the fork forever
    dtree.begin_run(tree)                    # pivoting abandons any run still in flight — you moved on
    _fold_usage(sid, s, cost, toks, tree=tree, stage="brainstorm", **_mirror(tree))
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/merge")
async def api_plan_merge(sid: str, request: Request):
    """Converge: reconcile the checked directions into one refined idea (+ adversarial cull + a light
    research skim). Runs in the background (the skim hits the web); the frontend polls /api/plan/{sid}."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    nodes = (s.get("tree") or {}).get("nodes") or {}
    valid = [i for i in (body.get("options") or [])
             if isinstance(i, str) and _kind(nodes.get(i) or {}) == "option"]
    if not valid:
        return JSONResponse({"error": "Pick at least one direction to try."}, status_code=400)
    # The merge is the free taste's one web-touching step and runs in a background thread WITHOUT
    # _run_slot — so the daily kill switch must be read here, before the spawn (invariant #3: the
    # funnel feeds the meter, it must also read it). Subscribers are bounded by their monthly cap.
    prov = _provider_for(s.get("user"))
    if (prov is not None and getattr(prov, "bills_filg", False) and not MOCK
            and not _is_subscriber(s.get("user")) and usage.kill_switch_tripped()):
        return _engine_error(DailyCapError(), 402)
    tree = s.get("tree") or {"nodes": {}, "active": None}
    tok = dtree.begin_run(tree)              # this run's epoch — a pivot mid-run invalidates it
    store.plan_save(sid, status="researching", stage="merging", tree=tree)
    threading.Thread(target=_run_merge, args=(sid, valid, s.get("user"), tok), daemon=True).start()
    return {"id": sid}


@app.post("/api/plan/{sid}/refine")
async def api_plan_refine(sid: str, request: Request):
    """Sharpen the refined idea IN PLACE: fold the operator's clarification (answers to the gate's
    questions, a steer) into a re-merge of the same picks, chained under the refined node it sharpens.
    Part of the pre-commit funnel, so it sits on the free side of the wall like /merge — and reads
    the same kill switch, for the same reason. Background; the frontend polls."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    note = (body.get("note") or "").strip()
    if len(note) < 4:
        return JSONResponse({"error": "Tell me what to fold in."}, status_code=400)
    # a keyboard-mash "answer" gets the same free roast as a mash pivot (no run, no LLM)
    if gibberish.looks_like_gibberish(note):
        return JSONResponse({"gibberish": True, **gibberish.roast(note)})
    tree = s.get("tree") or {"nodes": {}, "active": None}
    nodes = tree.get("nodes") or {}
    at = nodes.get((body.get("node") or "").strip()) or nodes.get(tree.get("active")) or {}
    if _kind(at) != "refined":
        return JSONResponse({"error": "Nothing here to refine yet — pick directions and merge first."},
                            status_code=400)
    # the picks that fed this refined idea: a refine-of-a-refine walks up to the original merge's
    sel, cur = [], at
    while cur and not sel:
        sel = [i for i in (cur.get("selected") or []) if _kind(nodes.get(i) or {}) == "option"]
        cur = nodes.get(cur.get("parent")) if cur.get("parent") else None
    prov = _provider_for(s.get("user"))
    if (prov is not None and getattr(prov, "bills_filg", False) and not MOCK
            and not _is_subscriber(s.get("user")) and usage.kill_switch_tripped()):
        return _engine_error(DailyCapError(), 402)
    tok = dtree.begin_run(tree)              # this run's epoch — a pivot mid-run invalidates it
    store.plan_save(sid, status="researching", stage="merging", tree=tree)
    threading.Thread(target=_run_merge, args=(sid, sel, s.get("user"), tok),
                     kwargs={"refine_of": at.get("id"), "note": note}, daemon=True).start()
    return {"id": sid}


@app.post("/api/plan/{sid}/commit")
async def api_plan_commit(sid: str, request: Request):
    """'I'm sold, build the plan' — the one deep research run + first plan page. Uses the active refined
    node's thesis (or a direction/thesis passed in the body). Background; the frontend polls."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    # THE WALL lives here: the free taste ends at this click (brainstorm → merge ran on the house).
    # The deep build requires an account, then a key or a subscription (see _account_wall).
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    thesis = (body.get("thesis") or "").strip()
    tree = s.get("tree") or {}
    nodes = tree.get("nodes") or {}
    # Building from an explicitly named node (the one the user had open) grows the build out of THAT
    # node — but VALIDATE FIRST, MUTATE LAST. This used to jump the active pointer before checking
    # the thesis: a commit from a browsed brainstorm fork 400'd AND stranded `active` on the fork,
    # so every surface then described two different nodes and every retry re-failed (Sam's
    # roll-forward freeze, 2026-07-06). A rejected request must leave the plan untouched.
    # picks riding a direct commit ("I'm sold" straight off the brainstorm, skipping the merge):
    # the CHOICE must be recorded, not just its text — without `selected` on the built node the
    # graph drew the checked option as passed-over and the pivot read as abandoned (Sam's QA,
    # 2026-07-06), even though the thesis carried it.
    picks = [i for i in (body.get("options") or [])
             if isinstance(i, str) and _kind(nodes.get(i) or {}) == "option"]
    if not thesis and picks:   # derive the joined thesis server-side if the client didn't
        thesis = " + ".join(
            ((nodes[i].get("direction") or {}).get("one_liner")
             or (nodes[i].get("direction") or {}).get("title") or "") for i in picks).strip(" +")

    at_id = (body.get("node") or "").strip()
    target = nodes.get(at_id) if at_id else None
    a = target or nodes.get(tree.get("active")) or {}
    build_from = a

    def _thesis_of(n: dict) -> str:
        k = _kind(n or {})
        if k == "refined":
            return (n.get("thesis") or "").strip()
        if k == "option":
            dr = n.get("direction") or {}
            return (dr.get("one_liner") or dr.get("title") or "").strip()
        return ""

    if not thesis:
        thesis = _thesis_of(a)
        # POINTER-DRIFT RESILIENCE (2026-07-06): "build the plan" must mean the nearest buildable
        # idea, not "hope the active pointer is exactly right". A drifted/stranded pointer (the old
        # mutate-before-validate bug corrupted live sessions) landed commits on forks/sections and
        # every retry re-failed. Resolve instead: walk UP the ancestors for a refined/option node,
        # then fall back to the NEWEST buildable node anywhere. Only refuse when the tree genuinely
        # has nothing to build from (a raw brainstorm with no picks).
        cur = a
        while not thesis and cur is not None:
            cur = nodes.get(cur.get("parent")) if cur.get("parent") else None
            if cur is not None:
                t = _thesis_of(cur)
                if t:
                    thesis, build_from = t, cur
        if not thesis:
            # anywhere-fallback targets REFINED nodes only: a refined idea is a converged CHOICE;
            # an option is an unchosen candidate — never build one the operator didn't pick
            for n in reversed(list(nodes.values())):   # dict order = creation order → newest first
                if _kind(n) == "refined":
                    t = _thesis_of(n)
                    if t:
                        thesis, build_from = t, n
                        break
    if len(thesis) < 8:
        return JSONResponse(
            {"error": "Refine an idea or pick a direction to build first "
                      f"(the current step is a {_kind(a) or 'missing'} node and there is no refined "
                      "idea to build from yet)."}, status_code=400)
    if build_from.get("id") and build_from["id"] != tree.get("active"):
        tree["active"] = build_from["id"]    # the jump happens only when the build actually starts
    # T2 reuse: if we're building the EXACT refined node the merge skim already researched (same
    # thesis, skim claims present), carry those claims into the deep build so it re-grades them at full
    # strength instead of re-running the web fan-out. A steered/explicit-thesis or direct-picks commit
    # (thesis ≠ the refined node's, or no refined node) falls through to a full re-research.
    prior = None
    if _kind(build_from) == "refined":
        skim = build_from.get("research") or {}
        if skim.get("claims") and (build_from.get("thesis") or "").strip() == thesis.strip():
            prior = {"thesis": build_from.get("thesis"), "founder_edge": build_from.get("founder_edge"),
                     "claims": skim["claims"]}
    tok = dtree.begin_run(tree)              # this run's epoch — a pivot mid-run invalidates it
    store.plan_save(sid, status="researching", stage="researching", tree=tree)
    threading.Thread(target=_deep_build, args=(sid, thesis, s.get("user"), tok, picks, prior),
                     daemon=True).start()
    return {"id": sid}


@app.post("/api/plan/{sid}/route")
async def api_plan_route(sid: str, request: Request):
    """The single prompt box. Classify a free-text prompt against the funnel stage + active tool mode
    into one action (steer/commit/diverge/restart_keep/restart_hard/ask). A plan-stage steer that hard-
    clashes with the committed idea returns a `fork` (discard vs pivot) instead of applying. Returns the
    decision; the frontend acts on it (calls /merge, /commit, /next, /redraft, a tool, etc.)."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "Type something."}, status_code=400)
    mode = body.get("mode") or "build"
    node_id = (body.get("node") or "").strip() or None   # the node the user has open (browse context)
    stage = s.get("stage") or ("building" if s.get("proposal") else "plan")
    rstage = {"brainstorm": "brainstorm", "merging": "merge", "refined": "refined",
              "building": "plan", "done": "plan"}.get(stage, "plan")
    def _work():
        with _run_slot(_slot_user(s), s.get("stack")):
            decision, cost = router.route(prompt, stage=rstage, mode=mode,
                                          context=_route_context(s, node_id), mock=MOCK)
            fork = None
            if decision["intent"] == "steer" and rstage == "plan" and decision.get("steer"):
                idea = planner._working_idea(s)
                ic, ic_cost = router.check_integration(
                    decision["steer"], idea, planner.bundle_markdown(idea, s.get("files") or {}), mock=MOCK)
                cost = round(cost + ic_cost, 4)
                if not ic["integrable"]:
                    fork = {"clash": ic["clash"], "skeptic_say": ic["skeptic_say"],
                            "steer": decision["steer"]}
            return decision, fork, cost, pipeline.LEDGER.tokens()
    try:
        decision, fork, cost, toks = await run_in_threadpool(_work)
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    _fold_usage(sid, s, cost, toks)
    out = {"decision": decision, "cost": cost, "tokens": toks}
    if fork:
        out["fork"] = fork
    return out


_MOCK_LOOKUP = [
    {"text": "Companies spend an average of $75 per employee per month on office snacks",
     "url": "https://snackvendor.example.com/report", "tier": "vendor", "flagged": True,
     "reason": "vendor-published stat promoting its own category"},
    {"text": "U.S. office food-service spending grew 4% year over year",
     "url": "https://bls.gov/example", "tier": "primary", "flagged": False, "reason": ""},
]


@app.post("/api/plan/{sid}/lookup")
async def api_plan_lookup(sid: str, request: Request):
    """A fast, GRADED research lookup from the chat's research mode: one web-search lane on the
    question, every claim through the source-credibility gate (the moat), labeled — never laundered."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    question = (body.get("message") or "").strip()
    if not question:
        return JSONResponse({"error": "Ask a research question."}, status_code=400)
    if len(question) > 500:
        return JSONResponse({"error": "Keep it under 500 characters."}, status_code=400)
    if MOCK:
        claims = [dict(c) for c in _MOCK_LOOKUP]
        _save_lookups(sid, s, claims)
        return {"claims": claims, "cost": 0, "tokens": 0}

    def _work():
        with _run_slot(s.get("user"), s.get("stack")):
            claims = pipeline.research_lane(planner._working_idea(s), question)
            verdicts = pipeline.gate_claims(claims)
            return verdicts, round(pipeline.LEDGER.cost(), 4), pipeline.LEDGER.tokens()
    try:
        verdicts, cost, toks = await run_in_threadpool(_work)
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    _meter(s.get("user"), cost)
    nc, nt = _fold_usage(sid, s, cost, toks)
    claims = [{"text": v.claim.text, "url": v.claim.source_url, "tier": v.tier,
               "flagged": bool(v.flagged), "reason": v.reason or ""} for v in verdicts]
    _save_lookups(sid, s, claims)
    return {"claims": claims, "cost": nc, "tokens": nt}


def _save_lookups(sid: str, s: dict, claims: list) -> None:
    """Persist chat-lookup claims on the session (deduped by text|url, bounded) so the research stack
    survives a reload — they used to live only in the client's memory."""
    if not claims:
        return
    have = list(s.get("lookups") or [])
    seen = {f"{c.get('text')}|{c.get('url')}" for c in have}
    have += [c for c in claims if f"{c.get('text')}|{c.get('url')}" not in seen]
    store.plan_save(sid, lookups=have[-60:])


@app.post("/api/plan/{sid}/chatlog")
async def api_plan_chatlog(sid: str, request: Request):
    """Append one message to the session's conversation record (the v2 left-panel chat). Pure logging,
    no AI call — the client posts what it rendered so the conversation survives a reload. Distinct from
    /chat (the v1 advisor, which generates a reply)."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    role = body.get("role")
    content = str(body.get("content") or "").strip()[:2000]
    if role not in ("user", "bot", "status") or not content:
        return JSONResponse({"error": "role must be user/bot/status, content required"}, status_code=400)
    chat = list(s.get("chat") or [])
    chat.append({"role": role, "content": content})
    store.plan_save(sid, chat=chat[-400:])   # a long session stays bounded
    return {"ok": True}


@app.get("/api/plan/{sid}/node/{nid}")
async def api_plan_node(sid: str, nid: str, request: Request):
    """Lazy node content for the decision-graph zoom: given a node id, return its full page (the
    section's built content, the option's direction, the refined idea, or a fork's options)."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    n = ((s.get("tree") or {}).get("nodes") or {}).get(nid)
    if not n:
        return JSONResponse({"error": "unknown node"}, status_code=404)
    k = _kind(n)
    out = {"id": n["id"], "kind": k, "title": n.get("title"), "parent": n.get("parent"),
           "children": n.get("children") or [], "feedback": n.get("feedback"), "step": n.get("step", 0),
           "log": n.get("log") or [],   # the persisted build receipts — survive a reload (§v2 #10)
           "board": n.get("board") or []}   # this step's board history — the node view shows the convenes
    if k == "section":
        sec = next((x for x in planner.SECTIONS if x["key"] == n.get("section")), None)
        content = (n.get("files") or {}).get(sec["file"]) if sec else None
        out.update({"section": n.get("section"), "sub": (sec or {}).get("sub"),
                    "draft": n.get("draft"), "content": content})
    elif k == "option":
        out["direction"] = n.get("direction")
    elif k == "refined":
        for f in ("thesis", "founder_edge", "mold", "kept", "dropped", "research", "selected"):
            out[f] = n.get(f)
    elif k == "brainstorm":
        out["spread"] = n.get("spread")
        out["feedback"] = n.get("feedback")   # the pivot ask this spread was answering (if any)
        out["set_aside"] = n.get("set_aside")  # declined-out-loud part of the ask
        # the fork's story, self-contained: every direction offered + which were picked (a pick =
        # named in any join's `selected` anywhere in the tree)
        nodes = (s.get("tree") or {}).get("nodes") or {}
        chosen = set()
        for x in nodes.values():
            chosen.update(x.get("selected") or [])
        out["options"] = [{"id": c["id"], "direction": c.get("direction"), "picked": c["id"] in chosen}
                          for c in (nodes.get(i) for i in (n.get("children") or []))
                          if c and _kind(c) == "option"]
    elif k == "idea":
        out["draft"] = n.get("draft")   # the raw prompt the whole tree grew from
    elif k == "fork":
        out.update({"question": n.get("question"), "options": n.get("options")})
    return out


@app.get("/api/plans")
async def api_plans(request: Request):
    """The signed-in user's plans — for the profile / 'My plans' view."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in to see your plans."}, status_code=401)
    plans = []
    for p in store.plan_list(authed["email"]):
        done = p["status"] == "done"
        # per-plan PDF unlock (for the "My files" tab → re-download purchased items). Only finished
        # plans can have a polished PDF; the unlock is per the plan's active branch.
        unlocked = False
        if done:
            full = store.plan_get(p["id"]) or {}
            unlocked = _has_pdf_access(authed["email"], _plan_key(full), verified=True)
        plans.append({"id": p["id"], "idea": p["idea"], "status": p["status"], "step": p["step"],
                      "created_at": p["created_at"], "updated_at": p.get("updated_at") or p["created_at"],
                      "done": done, "shared": bool(p.get("shared")), "pdf_unlocked": unlocked})
    return {"email": authed["email"], "total": planner.N, "plans": plans}


# ── In-app product help (a standard website help chat; runs on the user's key) ──
_FEATURE_WORDS = {"director_forge": "Director Forge", "custom_directors": "custom directors",
                  "skeptic": "the assumption stress-test"}


def _help_pricing() -> str:
    """The PRICING FACTS block for the help prompt, GENERATED from tiers.py + billing.py — the single
    sources of truth — so help can never drift from the live ladder again. (The 2026-07-06 QA caught
    help quoting the dead '$13 one-time, no subscription' model from a hardcoded prompt.)"""
    price = billing.PDF_PRICE_CENTS / 100
    lines = [
        "PRICING FACTS (answer any cost/subscription question ONLY from these, never from memory):",
        "- Getting started is free, no account: your idea spreads into directions and merges into a "
        "refined, first-pass-researched idea on the house. Building the full plan (the deep research "
        "step) is where an account comes in — sign up, then either bring your own key or subscribe. "
        "A plan without an account is cleaned up after about 48 hours.",
        "- Free on your own API key (OpenRouter or Anthropic, BYOK): unlimited use, every model crew, "
        "every feature. Model usage bills to their key, typically well under a dollar per plan.",
        f"- Raw export (.zip/.md) is always free. The polished investor-grade PDF: free WITH a small "
        f"'Built with FILG' watermark on your own key, or a one-time ${price:g} unlocks THAT plan's "
        "clean (watermark-free) PDF — re-downloading it is free, a new plan pays its own unlock. "
        "Every subscription includes unlimited clean PDFs.",
    ]
    cat = tiers.catalog()
    up = ("upgrade for a bigger allowance, add your own key as a fallback, or wait for the renewal"
          if len(cat) > 1 else "add your own key as a fallback, or wait for the renewal")
    lines.append(
        "- The monthly subscription runs on FILG's hosted key (no API key needed) and unlocks every "
        "feature and every model crew. It has a monthly usage allowance that resets with the "
        f"billing period; hitting it means {up}:")
    for t in cat:
        lines.append(f"  * {t['label']}: ${t['price']:g}/mo — all features and model crews, "
                     f"unlimited clean PDFs, ~${t['cap_cents'] / 100:g}/mo of included model usage.")
    return "\n".join(lines)


def _help_system() -> str:
    """The help system block: the `help` SKILL (app/skills/help/SKILL.md — how-to, money-answer rules,
    the not-authoritative-on-pricing/legal disclaimer) + the PRICING FACTS generated from the live
    ladder. Skill = judgment and rules; generated block = numbers. Neither can drift alone."""
    return skills.system("help") + "\n\n" + _help_pricing()

_HELP_MOCK = ("This is mock help (no key bound). In the real app: type your idea on the home page, then "
              "use 'I'm with you' to lock each part and build the next, or 'Not feeling it' to redo a part. "
              "It runs on your own API key.")


@app.post("/api/help")
async def api_help(request: Request):
    """A standard website-style help chat for using the product. Runs on the user's own key (BYOK),
    same as every other engine call; no plan/session required."""
    if MOCK:
        return {"reply": _HELP_MOCK}
    authed = auth.user_from_request(request)
    user = authed["email"] if authed else None
    prov = _provider_for(user)   # the user's own key, else FILG's hosted free key (if configured)
    if prov is None:
        return JSONResponse({"error": "Add your API key to use help (it runs on your own key).",
                             "needKey": True}, status_code=402)
    if prov.bills_filg and usage.kill_switch_tripped():   # help on FILG's key respects the daily budget
        return JSONResponse({"error": "Today's free pool is tapped. Add your own API key to keep going.",
                             "needKey": True}, status_code=402)
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Ask a question."}, status_code=400)
    if len(message) > 1000:
        return JSONResponse({"error": "Keep it under 1000 characters."}, status_code=400)
    convo = ""
    for m in (body.get("history") or [])[-6:]:
        who = "User" if m.get("role") == "user" else "Help"
        convo += f"\n{who}: {str(m.get('content', ''))[:600]}"
    try:
        with provider.use(prov), provider.use_stack(provider.DEFAULT_STACK), pipeline.run_ledger():
            reply = pipeline.call("help", pipeline.SONNET, max_tokens=400, system=_help_system(), cache=True,
                                  prompt=f"Conversation so far:{convo or ' (none)'}\n\nUser: {message}\n\n"
                                         "Reply as the FILG help assistant.")
            _meter(user, round(pipeline.LEDGER.cost(), 4))   # FILG-key help → daily + a subscriber's monthly cap
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    return {"reply": (reply or "").strip() or "Sorry, I couldn't generate a reply, try rephrasing."}


@app.get("/api/plan/{sid}")
async def api_plan_get(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if request.query_params.get("touch"):   # explicit open (not a status poll) → bump recency for the profile sort
        store.plan_touch(sid)
    return _plan_state(s)


@app.post("/api/plan/{sid}/claim")
async def api_plan_claim(sid: str, request: Request):
    """Attach an anonymous free-taste plan to the account that just signed in (the wall's happy path:
    taste → sign up → the plan follows you). Idempotent; refuses a plan someone else owns."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first.", "needAccount": True}, status_code=401)
    s = store.plan_get(sid)
    if not s:
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if not store.plan_claim(sid, authed["email"]):
        return JSONResponse({"error": "unknown session"}, status_code=404)   # owned by someone else — don't leak
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/respond")
async def api_plan_respond(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    if s["status"] != "building":
        return JSONResponse({"error": f"session is {s['status']}"}, status_code=409)
    body = await request.json()
    choice = body.get("choice")
    if choice not in planner.CHOICES:
        return JSONResponse({"error": "pick yes_and / not_quite / okay_but"}, status_code=400)
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            # If a board is set it vets each finalized section and its takeaway steers the next draft
            # (planner.advance runs the board inline). cost includes any board review.
            upd = planner.advance(s, choice, body.get("note"), mock=MOCK,
                                  directors=s.get("directors") or None)
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    _meter(s.get("user"), round((upd.get("cost", 0) or 0) - (s.get("cost") or 0), 4))  # draft + board review
    store.plan_save(sid, **upd)
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/next")
async def api_plan_next(sid: str, request: Request):
    """Roll forward: finalize the active node's section (a note steers it) and draft the next one.
    Going forward from a node that already has children naturally creates a new branch."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    feedback = (body.get("feedback") or "").strip() or None
    force = bool(body.get("force"))   # operator chose "build it anyway" past the kill gate (no substance)
    killed = (s.get("vetting") or {}).get("verdict") == "kill"
    if killed and not force:   # soft gate: a non-forced advance still gets the advisement (off-ramp = /revet)
        return _kill_gate(s)
    tree = _ensure_tree(s)
    active = tree["nodes"][tree["active"]]
    if active["step"] >= planner.N:
        return JSONResponse({"error": "This plan is already complete."}, status_code=409)
    def _work():
        with _run_slot(s.get("user"), s.get("stack")):
            if killed:   # forced past the gate with no substance → waste-of-time mode (comedic, skips research → ~$0)
                child, cost = planner.wod_forward(active)
            else:
                child, cost = planner.forward(planner._working_idea(s), s["research"], active, feedback,
                                              directors=s.get("directors") or None,
                                              founder=planner._founder(s), mock=MOCK,
                                              extra_personas=s.get("custom_directors"))
            return child, cost, pipeline.LEDGER.tokens()
    try:
        child, cost, toks = await run_in_threadpool(_work)
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    dtree.attach(tree, _new_node(child, active["id"]))
    _meter(s.get("user"), cost)
    _fold_usage(sid, s, cost, toks, tree=tree, **_mirror(tree))
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/revet")
async def api_plan_revet(sid: str, request: Request):
    """Kill-gate rescue: the operator adds real substance (a skill / asset / who'd pay); we re-shape +
    re-vet. If the verdict clears the kill, we redraft part 1 from the enriched thesis and the builder
    unlocks. Still `kill` → the new (sharper) diagnostic comes back and the gate holds."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    more = (body.get("more") or "").strip()
    if len(more) < 8:
        return JSONResponse(
            {"error": "Give me a bit more — a real skill or asset, and who'd pay for it."}, status_code=400)
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            shaped, vetting, cost = intake.revet(s["idea"], more, s.get("research"), mock=MOCK)
            proposal = None
            if vetting["verdict"] != "kill":   # cleared → redraft part 1 from the now-substantive thesis
                proposal, c2 = planner.first_proposal(
                    shaped["thesis"], s["research"], founder=shaped.get("founder_edge"), mock=MOCK)
                cost = round(cost + c2, 4)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    updates = {"shaped": shaped, "vetting": vetting}
    if proposal is not None:
        root = _new_node(planner.root_node(proposal), None)   # no branches exist yet on a kill, so reseed
        tree = dtree.seed(root)
        updates.update({"proposal": proposal, "step": 0, "tree": tree, **_mirror(tree)})
    _meter(s.get("user"), cost)
    _fold_usage(sid, s, cost, toks, **updates)
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/back")
async def api_plan_back(sid: str, request: Request):
    """Roll back a step: re-draft the PREVIOUS section taking the (required) note as a redirect — a
    new sibling branch from the node before this one. Feedback is required: backing up means you want
    something changed, so an empty note is a form error."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    feedback = (body.get("feedback") or "").strip()
    if not feedback:
        return JSONResponse(
            {"error": "Add a quick note on what to change — feedback's required to go back a step."},
            status_code=400)
    tree = _ensure_tree(s)
    active = tree["nodes"][tree["active"]]
    if not active.get("parent"):
        return JSONResponse({"error": "You're on the first part — nothing to go back to."},
                            status_code=400)
    prev = tree["nodes"][active["parent"]]   # the previous step's node — the one we re-draft
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            sib, cost = planner.rebranch(planner._working_idea(s), s["research"], prev, feedback,
                                         founder=planner._founder(s), mock=MOCK)
            regrade, cost = _regrade_setup(s, sib, cost)   # back onto the setup → re-grade the verdict
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    dtree.attach(tree, _new_node(sib, prev.get("parent")))   # sibling of `prev` → branches from prev's parent
    _meter(s.get("user"), cost)
    updates = dict(tree=tree, **_mirror(tree))
    if regrade:
        updates["vetting"] = regrade
    _fold_usage(sid, s, cost, toks, **updates)
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/redraft")
async def api_plan_redraft(sid: str, request: Request):
    """Regenerate the CURRENT part with the operator's feedback applied — a fresh sibling of the active
    node at the SAME step (not a step back; backing up is done via the tree). Feedback required: a
    rework needs a note to steer it."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    feedback = (body.get("feedback") or "").strip()
    if not feedback:
        return JSONResponse(
            {"error": "Add a quick note on what's not landing — a rework needs something to steer it."},
            status_code=400)
    tree = _ensure_tree(s)
    active = tree["nodes"][tree["active"]]
    if active["step"] >= planner.N:
        return JSONResponse({"error": "This plan is already complete."}, status_code=409)
    def _work():
        with _run_slot(s.get("user"), s.get("stack")):
            sib, cost = planner.rebranch(planner._working_idea(s), s["research"], active, feedback,
                                         founder=planner._founder(s), mock=MOCK)
            regrade, cost2 = _regrade_setup(s, sib, cost)   # setup reframed → re-grade the verdict on the new angle
            return sib, regrade, cost2, pipeline.LEDGER.tokens()
    try:
        sib, regrade, cost, toks = await run_in_threadpool(_work)
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    dtree.attach(tree, _new_node(sib, active.get("parent")))   # sibling of the active node → same step, new branch
    _meter(s.get("user"), cost)
    updates = dict(tree=tree, **_mirror(tree))
    if regrade:
        updates["vetting"] = regrade
    _fold_usage(sid, s, cost, toks, **updates)
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/stack")
async def api_plan_stack(sid: str, request: Request):
    """Choose the model stack for this plan (trust-fund / damn-good / polished-turd). Pure setting — no
    spend. The premium stack is only honored on a BYOK key; on FILG's free key it clamps at run time."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    want = provider.stack_name(body.get("stack"))
    store.plan_save(sid, stack=want)
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/goto")
async def api_plan_goto(sid: str, request: Request):
    """Hop to an existing node in the decision tree (pure navigation — no new branch, no spend).
    Rolling forward from there is what spawns a new branch."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    node_id = body.get("node")
    tree = _ensure_tree(s)
    if node_id not in (tree.get("nodes") or {}):
        return JSONResponse({"error": "unknown node"}, status_code=404)
    tree["active"] = node_id
    store.plan_save(sid, tree=tree, **_mirror(tree))
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/ask")
async def api_plan_ask(sid: str, request: Request):
    """Add-on: ask a composite archetype advisor about the plan-in-progress."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    archetype = body.get("archetype")
    if archetype not in planner.ARCHETYPE_KEYS:
        return JSONResponse({"error": "pick an advisor"}, status_code=400)
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            res, cost = planner.ask_expert(s["idea"], s.get("files") or {}, archetype,
                                           body.get("question") or "", mock=MOCK)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    _meter(s.get("user"), cost)
    nc, nt = _fold_usage(sid, s, cost, toks)
    return {**res, "cost": nc, "tokens": nt}


@app.post("/api/plan/{sid}/board")
async def api_plan_board(sid: str, request: Request):
    """Convene the Board of Directors on the plan-so-far. The standing, multi-advisor version of
    'ask an expert': several composite directors weigh in and FILG synthesizes the collaboration
    matrix (agreement / conflict / net verdict). `directors` in the body re-picks + persists the
    board; otherwise the session's board (or the default starter board) is used."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    customs = s.get("custom_directors") or []
    custom_keys = {p["key"] for p in customs}
    picked = body.get("directors")
    directors = [k for k in (picked or s.get("directors") or personas.DEFAULT_BOARD)
                 if k in personas.KEYS or k in custom_keys] or personas.DEFAULT_BOARD
    question = (body.get("question") or "").strip() or \
        "Vet the plan so far — what's the one thing I should change before continuing?"
    work_idea = planner._working_idea(s)
    plan_text = planner.bundle_markdown(work_idea, s.get("files") or {})
    def _work():
        with _run_slot(s.get("user"), s.get("stack")):
            res, cost = board.convene(work_idea, plan_text, question, directors, mock=MOCK,
                                      extra_personas=customs)
            return res, cost, pipeline.LEDGER.tokens()
    try:
        res, cost, toks = await run_in_threadpool(_work)
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    _meter(s.get("user"), cost)
    extra = {"directors": directors} if picked else {}   # persist a freshly chosen board for later steps
    # Persist the convene on the ACTIVE NODE's board history (bounded), so it survives tree navigation
    # (the flat `board` column is a mirror of the active node — writing only there gets clobbered),
    # steers later drafts via planner._board_notes, and shows up in the exports/handoff.
    tree = _ensure_tree(s)
    a = (tree.get("nodes") or {}).get(tree.get("active"))
    if a is not None:
        entry = {"section": "convene", "title": f"Board convened: “{question[:90]}”", **res}
        # the legacy /respond flow advances the flat mirror without the tree — trust whichever is ahead
        base = max((a.get("board") or []), (s.get("board") or []), key=len)
        reviews = dtree.clip("board", list(base) + [entry])
        a["board"] = reviews
        extra.update(tree=tree, board=reviews)
    nc, nt = _fold_usage(sid, s, cost, toks, **extra)
    return {**res, "cost": nc, "tokens": nt}


_MAX_CUSTOM_DIRECTORS = 8


@app.post("/api/plan/{sid}/director/forge")
async def api_director_forge(sid: str, request: Request):
    """Forge a CUSTOM board director from a description (distill → draft → QA). Returns a DRAFT persona
    (with its trace) that the operator can approve (save) or retry; nothing is seated until they save.
    Runs on the owner's bound key, same as every engine op."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    if not _feature_ok(s.get("user"), "director_forge"):   # premium feature: Pro/Studio, or BYOK
        return JSONResponse(
            {"error": "Forging a custom board director is a Pro feature. Upgrade to Pro, or add your "
                      "own API key to use it free.", "upgrade": True, "feature": "director_forge"},
            status_code=402)
    body = await request.json()
    desc = (body.get("description") or "").strip()
    if len(desc) < 4:
        return JSONResponse({"error": "Describe the director you want."}, status_code=400)
    if len(desc) > 600:
        return JSONResponse({"error": "Keep it under 600 characters."}, status_code=400)
    if len(s.get("custom_directors") or []) >= _MAX_CUSTOM_DIRECTORS:
        return JSONResponse({"error": "You've already forged a full bench of custom directors."},
                            status_code=409)
    existing = list(personas.KEYS) + [p["key"] for p in (s.get("custom_directors") or [])]
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            persona, cost = director_forge.forge(desc, existing_keys=existing, mock=MOCK)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    _meter(s.get("user"), cost)
    nc, nt = _fold_usage(sid, s, cost, toks)
    return {"persona": persona, "cost": nc, "tokens": nt}


@app.post("/api/plan/{sid}/director/save")
async def api_director_save(sid: str, request: Request):
    """Seat a forged director on the board: persist it to the session's custom_directors registry (so
    board.convene can resolve it) and add it to the active board. Owner only."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    p = (await request.json()).get("persona") or {}
    if not (p.get("key") and p.get("name") and p.get("voice")):
        return JSONResponse({"error": "Forge a director first."}, status_code=400)
    customs = list(s.get("custom_directors") or [])
    if len(customs) >= _MAX_CUSTOM_DIRECTORS:
        return JSONResponse({"error": "You've already forged a full bench of custom directors."},
                            status_code=409)
    clean = {k: p.get(k) for k in ("key", "name", "first", "blurb", "domains", "voice")}
    clean["custom"] = True
    if not any(c["key"] == clean["key"] for c in customs):   # idempotent on re-save
        customs.append(clean)
    directors = list(s.get("directors") or [])
    if clean["key"] not in directors:                        # seat them on the active board
        directors.append(clean["key"])
    store.plan_save(sid, custom_directors=customs, directors=directors)
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/research/query")
async def api_research_query(sid: str, request: Request):
    """Query the graded research. mode='quick' reads what's already there (and may say it's not sure);
    mode='deep' spawns a fresh, bounded research pass on the question. Owner only, on their bound key."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    q = (body.get("question") or "").strip()
    if len(q) < 3:
        return JSONResponse({"error": "Ask a question about your research."}, status_code=400)
    if len(q) > 1000:
        return JSONResponse({"error": "Keep it under 1000 characters."}, status_code=400)
    mode = "deep" if body.get("mode") == "deep" else "quick"
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            res, cost = advisor.research_answer(s, q, mode=mode, mock=MOCK)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)
    _meter(s.get("user"), cost)
    nc, nt = _fold_usage(sid, s, cost, toks)
    return {**res, "cost": nc, "tokens": nt}


def _stress_worker(sid: str, idea: str, shaped: dict, research: dict | None,
                   user: str, stack: str | None) -> None:
    """Background worker: run the adversarial stress-test on the user's bound key, streaming the
    §LANES§/§LANEDONE§ runner sentinels into the session's `skeptic.progress` live (so the client's
    runner panel paints the assumption→attack→verdict tree grey→green), then persist the result.
    Mirrors _plan_research's threading + on_progress + provider/ledger binding."""
    progress: list[str] = []
    try:
        def on_progress(line: str) -> None:
            progress.append(line)
            store.plan_save(sid, skeptic={"status": "running", "progress": list(progress),
                                          "result": None})

        prov = _provider_for(user)
        stk = tiers.clamp_stack(stack, tier=_tier(user), byok=bool(prov and not prov.bills_filg))
        with provider.use(prov), provider.use_stack(stk), pipeline.run_ledger():
            res, cost = skeptic.stress_test(idea, shaped, research, mock=MOCK, on_progress=on_progress)
            toks = pipeline.LEDGER.tokens()
        _meter(user, cost)
        s = store.plan_get(sid) or {}
        _fold_usage(sid, s, cost, toks,
                    skeptic={"status": "done", "progress": progress, "result": res, "cost": cost})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        store.plan_save(sid, skeptic={"status": "error", "progress": progress, "result": None,
                                      "error": _humanize_error(e)[0]})


@app.post("/api/plan/{sid}/stress-test")
async def api_plan_stress_test(sid: str, request: Request):
    """Kick off the adversarial assumption stress-test (skeptic.stress_test) in the background so its
    runner tree streams live. High-stakes, real spend — an explicit action, owner only, behind the key
    wall. Returns {started:true}; the client polls GET /api/plan/{sid}/stress-test for progress+result."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    if not _feature_ok(s.get("user"), "skeptic"):   # premium feature: Pro/Studio, or BYOK
        return JSONResponse(
            {"error": "The adversarial stress-test is a Pro feature. Upgrade to Pro, or add your own "
                      "API key to use it free.", "upgrade": True, "feature": "skeptic"}, status_code=402)
    shaped = s.get("shaped")
    if not shaped:
        return JSONResponse({"error": "Shape the idea first, then stress-test it."}, status_code=409)
    if (s.get("skeptic") or {}).get("status") == "running":
        return JSONResponse({"error": "A stress-test is already running.", "running": True},
                            status_code=409)
    store.plan_save(sid, skeptic={"status": "running", "progress": [], "result": None})
    threading.Thread(target=_stress_worker,
                     args=(sid, planner._working_idea(s), shaped, s.get("research"),
                           s.get("user"), s.get("stack")), daemon=True).start()
    return {"started": True}


@app.get("/api/plan/{sid}/stress-test")
async def api_plan_stress_test_state(sid: str, request: Request):
    """Poll the stress-test: {status: idle|running|done|error, progress:[…sentinels…], result, cost}."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    return s.get("skeptic") or {"status": "idle", "progress": [], "result": None}


@app.post("/api/plan/{sid}/chat")
async def api_plan_chat(sid: str, request: Request):
    """Chat with your plan — a standing advisor grounded in the plan, graded research, decisions, and
    the chosen board. Persists the thread on the session so it lives with the plan. Owner only."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if s["status"] == "error":
        return JSONResponse({"error": "This plan hit an error — start over or re-run it first."},
                            status_code=409)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Ask a question."}, status_code=400)
    if len(message) > 2000:
        return JSONResponse({"error": "Keep it under 2000 characters."}, status_code=400)
    # v2 logs the user's bubble itself via /chatlog before routing here — log_user=false stops the
    # double entry, and the trailing duplicate is trimmed from what the model sees
    echo_user = bool(body.get("log_user", True))
    history = list(s.get("chat") or [])
    hist_model = (history[:-1] if (not echo_user and history
                                   and (history[-1].get("content") or "") == message) else history)

    journey = _journey_digest(s)   # the decision tree — options, picks, pivots — else the advisor is blind to it
    situation = context.situation(s, (body.get("node") or "").strip() or None,
                                  (str(body.get("working") or "").strip() or None))

    def _work():
        with _run_slot(s.get("user"), s.get("stack")):
            reply, cost = advisor.chat_reply(s, message, history=hist_model, mock=MOCK,
                                             journey=journey, situation=situation)
            return reply, cost, pipeline.LEDGER.tokens()
    try:
        reply, cost, toks = await run_in_threadpool(_work)
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)
    history = list(store.plan_get(sid).get("chat") or [])   # re-read: a run may have logged mid-flight
    history += (([{"role": "user", "content": message}] if echo_user else [])
                + [{"role": "assistant", "content": reply}])
    _meter(s.get("user"), cost)  # FILG-key chat counts toward the daily kill switch; BYOK is the user's spend
    nc, nt = _fold_usage(sid, s, cost, toks, chat=history)
    return {"reply": reply, "messages": history, "cost": nc, "tokens": nt}


@app.post("/api/plan/{sid}/delete")
async def api_plan_delete(sid: str, request: Request):
    """Permanently delete a plan (owner only). Note: deleting does NOT refund a free-tier run — each
    build already cost compute (cost guardrail), so delete is for tidiness, not free re-rolls."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    store.plan_delete(sid)
    return {"ok": True}


@app.post("/api/plan/{sid}/share")
async def api_plan_share(sid: str, request: Request):
    """Toggle a plan's public read-only share link (owner only). Body {shared: bool} (default true).
    Returns the shareable URL. Plans are private by default — this opts one in."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    shared = body.get("shared", True)
    store.plan_set_shared(sid, shared)
    base = (os.environ.get("FILG_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/"))
    return {"shared": bool(shared), "url": f"{base}/p/{sid}" if shared else None}


@app.get("/api/plan/{sid}/download")
async def api_plan_download(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if s["status"] != "done":
        return JSONResponse({"error": "plan isn't finished yet"}, status_code=400)
    # No paywall (monetization model in flux — own-your-docs is the wedge). `s["files"]` mirrors the
    # ACTIVE decision-tree branch, so the download is exactly the final decision set the user landed on.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", planner.bundle_markdown(s["idea"], s["files"]))
        for path, content in s["files"].items():
            z.writestr(path, content)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="filg-business-plan.zip"'})


def _export_text(s: dict) -> str:
    """Everything the operator has generated so far, as ONE plain-text dump: the idea + offer summary,
    the straight read, the plan part by part, the annotated graded research, and the board's takes.
    Minimal formatting on purpose — it's a get-your-data-out file, available at any point, always free."""
    out: list[str] = []
    W = out.append
    BAR = "=" * 64
    R = s.get("research") or {}
    prose = R.get("prose") or {}
    v = s.get("vetting") or {}
    sh = s.get("shaped") or {}
    W("FILG — YOUR BUSINESS PLAN, SO FAR")
    W("Built with FILG. Every number is graded by a source-credibility gate: vendor-marketing stats are")
    W("labeled, not laundered. This is your raw export, it's always free.")
    W("")
    W(BAR); W("THE IDEA"); W(BAR)
    W((s.get("idea") or "").strip())
    if prose.get("title"):
        W(""); W("Offer: " + prose["title"])
    if prose.get("offer"):
        W("What you'd sell: " + prose["offer"])
    if prose.get("gtm"):
        W("How you'd sell it: " + prose["gtm"])
    if v.get("verdict"):
        shape = ("  ·  shape: " + v["model_type"].replace("-", " ")) if v.get("model_type") else ""
        W("Verdict: " + v["verdict"].upper() + shape)
    W("")
    if sh or v:
        W(BAR); W("THE STRAIGHT READ"); W(BAR)
        if sh.get("thesis"):
            W("Focus: " + sh["thesis"])
        if sh.get("founder_edge"):
            W("Your edge: " + sh["founder_edge"])
        if v.get("reason"):
            W(v["reason"])
        if v.get("biggest_risk"):
            W("Biggest risk: " + v["biggest_risk"])
        if v.get("first_test"):
            W("Cheapest first test: " + v["first_test"])
        for a in (v.get("premortem") or []):
            why = ("  (" + a["why"] + ")") if a.get("why") else ""
            W("  - [" + str(a.get("status", "")) + "] " + str(a.get("assumption", "")) + why)
        W("")
    files = s.get("files") or {}
    if files:
        W(BAR); W("THE PLAN, PART BY PART"); W(BAR)
        for sec in planner.SECTIONS:
            if sec["file"] in files:
                W(""); W("## " + sec["title"] + " — " + sec.get("sub", ""))
                W((files[sec["file"]] or "").strip())
        W("")
    rows = R.get("rows") or []
    if rows:
        W(BAR); W("THE RESEARCH, GRADED"); W(BAR)
        W("Each claim is marked CITED (passed the gate) or VENDOR/UNVERIFIED (treat with care).")
        for r in rows:
            mark = "CITED" if r.get("mark") == "ok" else "VENDOR/UNVERIFIED"
            W(""); W("[" + mark + "] " + (r.get("text") or ""))
            meta = [x for x in (r.get("url"), r.get("note"),
                                ("as of " + str(r["as_of"])) if r.get("as_of") else None) if x]
            if meta:
                W("    " + "  ·  ".join(meta))
        W("")
    board = s.get("board") or []
    if board:
        W(BAR); W("YOUR BOARD'S TAKE (composite advisors, not real people)"); W(BAR)
        for rev in board:
            W(""); W("On “" + (rev.get("title") or "") + "”:")
            sk = rev.get("skeptic") or {}
            if sk.get("rationale"):
                W("  Skeptic (" + str(sk.get("verdict", "")) + "): " + sk["rationale"])
            for d in (rev.get("directors") or []):
                W("  " + (d.get("first") or d.get("name") or "Director") + ": " + (d.get("take") or ""))
            if rev.get("verdict"):
                W("  Board takeaway: " + rev["verdict"])
        W("")
    W(BAR)
    W("Generated by FILG · fuckitletsgo.ai · your raw export is always free.")
    return "\n".join(out)


def _handoff_prompt(s: dict) -> str:
    """An LLM-ingestion prompt so the operator can paste their work into ANY model and pick up exactly
    where they left off: the idea + straight read, the plan parts already written, the part they're on
    now, the graded research WITH source links, and the board's notes — then a continue-from-here task.
    We never include sections ahead of where they are."""
    out: list[str] = []
    W = out.append
    R = s.get("research") or {}
    prose = R.get("prose") or {}
    v = s.get("vetting") or {}
    sh = s.get("shaped") or {}
    files = s.get("files") or {}
    prop = s.get("proposal") or {}
    W("You are a pragmatic business-planning partner for a solo operator. They started building a plan "
      "in another tool and want you to continue from exactly where they left off. Everything generated "
      "so far is below: the idea, the honest read on it, the plan parts already written, the part they "
      "are on now, the graded research (with source links), and their board's notes.")
    W("")
    W("Rules: keep the same buyer, offer, price, and numbers already chosen; stay concise; build on the "
      "parts already written instead of redoing them; treat any source marked VENDOR/UNVERIFIED as "
      "unproven; do not invent statistics.")
    W("")
    W("=== THE IDEA ===")
    W((s.get("idea") or "").strip())
    if prose.get("title"):
        W("Offer: " + prose["title"])
    if prose.get("offer"):
        W("What they'd sell: " + prose["offer"])
    if prose.get("gtm"):
        W("How they'd sell it: " + prose["gtm"])
    if v.get("verdict"):
        W("Verdict so far: " + v["verdict"].upper()
          + (("  ·  shape: " + v["model_type"].replace("-", " ")) if v.get("model_type") else ""))
    if sh.get("founder_edge"):
        W("Founder's edge: " + sh["founder_edge"])
    if v.get("biggest_risk"):
        W("Biggest risk: " + v["biggest_risk"])
    W("")
    if files:
        W("=== THE PLAN SO FAR (already written — build on these, don't repeat) ===")
        for sec in planner.SECTIONS:
            if sec["file"] in files:
                W(""); W("## " + sec["title"]); W((files[sec["file"]] or "").strip())
        W("")
    if prop.get("draft") and not s.get("done"):
        W("=== THE PART THEY'RE ON NOW (a draft, not yet locked) ===")
        W("## " + (prop.get("title") or ""))
        W((prop.get("draft") or "").strip())
        W("")
    rows = R.get("rows") or []
    if rows:
        W("=== GRADED RESEARCH (with source links) ===")
        for r in rows:
            mark = "CITED" if r.get("mark") == "ok" else "VENDOR/UNVERIFIED"
            url = ("  <" + r["url"] + ">") if r.get("url") else ""
            W("- [" + mark + "] " + (r.get("text") or "") + url)
        W("")
    board = s.get("board") or []
    if board:
        W("=== THE BOARD'S NOTES (composite advisors) ===")
        for rev in board:
            sk = rev.get("skeptic") or {}
            tail = ("  | skeptic (" + str(sk.get("verdict", "")) + "): " + sk["rationale"]) if sk.get("rationale") else ""
            W("- on “" + (rev.get("title") or "") + "”: " + (rev.get("verdict") or "") + tail)
        W("")
    W("=== YOUR TASK ===")
    if s.get("done"):
        W("The plan is complete. Pressure-test it, then help them sharpen the weakest part and plan the "
          "first 30 days of execution.")
    else:
        nxt = ""
        st = s.get("step")
        if isinstance(st, int) and 0 <= st + 1 < len(planner.SECTIONS):
            nxt = " The next part to write is “" + planner.SECTIONS[st + 1]["title"] + "”."
        W("Continue the plan from the part they're on now, keeping it tight and consistent with "
          "everything above." + nxt)
    return "\n".join(out)


@app.get("/api/plan/{sid}/handoff.txt")
async def api_plan_handoff(sid: str, request: Request):
    """The LLM-handoff prompt (copy-paste into any model to continue). Free, owner only, any point."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if not (s.get("research") or s.get("files") or s.get("shaped") or s.get("vetting")):
        return JSONResponse({"error": "Nothing to hand off yet."}, status_code=400)
    return Response(_handoff_prompt(s), media_type="text/plain; charset=utf-8")


@app.get("/api/plan/{sid}/export.txt")
async def api_plan_export_txt(sid: str, request: Request):
    """Get-your-data-out: a single plain-text dump of everything generated so far (idea, straight read,
    plan parts, graded research, board takes). Available the moment there's data — no 'done' gate, no
    paywall. Owner only."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if not (s.get("research") or s.get("files") or s.get("shaped") or s.get("vetting")):
        return JSONResponse({"error": "Nothing to export yet."}, status_code=400)
    fn = f"{_slug(s.get('idea'))}-filg-export.txt"
    return Response(_export_text(s), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "business-plan").lower()).strip("-")
    return (s or "business-plan")[:50]


@app.get("/api/plan/{sid}/plan.pdf")
async def api_plan_pdf(sid: str, request: Request):
    """The core artifact: a styled, branded PDF of the finished plan. Synthesizes an exec summary,
    lays out the active branch's sections, and appends the graded-research evidence exhibit. Clean
    copy paid via the $13 per-plan unlock (or a comp credit); re-downloading an unlocked plan is
    free. The synthesis runs on the OWNER'S bound key (`_run_slot` binds their provider). Builds from
    `s["files"]` = the active branch's final decision set."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if s["status"] != "done":
        return JSONResponse({"error": "plan isn't finished yet"}, status_code=400)
    authed = auth.user_from_request(request)
    email = (authed or {}).get("email", "")
    # Subscribers get the polished PDF free (it's part of the plan). Otherwise claim it: free if comped
    # or already unlocked ($13 bought this plan), else spend a comp credit. No access → a BYOK user
    # still gets a FREE WATERMARKED copy (synth runs on their own key — the share loop needs an
    # artifact that circulates; the $13 unlock removes the line). No access and no key → payment.
    watermark = False
    if (billing.PDF_BILLING_ENABLED and not _is_subscriber(email)
            and not billing.claim_pdf(email, _plan_key(s))):
        owner = (s.get("user") or email or "").strip().lower()
        if keys.enabled() and owner and keys.has_key(owner):
            watermark = True
        else:
            return JSONResponse(
                {"error": f"The clean PDF for this plan is a one-time "
                          f"${billing.PDF_PRICE_CENTS // 100} (re-downloads are free). Or add your "
                          "own API key for a free watermarked copy. Your raw export is always free.",
                 "needPurchase": True, "price": billing.PDF_PRICE_CENTS}, status_code=402)
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            plan, cost = plan_pdf.synthesize(s, mock=MOCK)
            data = plan_pdf.render(plan, style=(request.query_params.get("style") or "filg"),
                                   watermark=watermark)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse({"error": f"Could not build the PDF: {e}"}, status_code=500)
    # BYOK → the owner's own key (never touches FILG's budget). A subscriber → FILG's key, so meter the
    # synth against their monthly fair-use cap. The per-session meter reflects it either way.
    _meter(s.get("user"), cost)
    nc, nt = _fold_usage(sid, s, cost, toks)   # binary response → echo usage via headers for the meter
    fn = f"{_slug(s.get('idea'))}-business-plan.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"',
                             "X-FILG-Cost": str(nc), "X-FILG-Tokens": str(nt),
                             "X-FILG-Watermark": "1" if watermark else "0"})


def _page_head() -> str:
    """The `__FILG_HEAD__` block: the window.FILG config + the Supabase script when auth is on."""
    cfg = json.dumps({"authEnabled": auth.AUTH_ENABLED,
                      "pdfBilling": billing.PDF_BILLING_ENABLED, "pdfPrice": billing.PDF_PRICE_CENTS,
                      "byokEnabled": keys.enabled(),
                      "freeTaste": bool(HOSTED_FREE and keys.enabled()),   # first query on FILG's key
                      "subEnabled": billing.PDF_BILLING_ENABLED,           # monthly tiers available (Stripe on)
                      "tiers": tiers.catalog(),                            # pricing table / fork UI
                      # dev-only: with auth off, act as this account so the subscriber UX reflects locally
                      "devEmail": (os.environ.get("FILG_DEV_EMAIL", "") if not auth.AUTH_ENABLED else ""),
                      "supabaseUrl": os.environ.get("SUPABASE_URL", ""),
                      "supabaseAnon": (os.environ.get("SUPABASE_PUBLISHABLE_KEY")
                                       or os.environ.get("SUPABASE_ANON_KEY", "")),
                      "archetypes": personas.catalog(), "defaultBoard": personas.DEFAULT_BOARD})
    head = f"<script>window.FILG={cfg}</script>"
    if auth.AUTH_ENABLED:
        head += '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
    return head


def _shell() -> str:
    """THE app shell (the former v2 surface, promoted to the root namespace 2026-07-06 — v1 retired).
    Served at `/` and every clean deep-link path (`/plan/{id}`, `/account/<tab>`) so real History-API
    URLs direct-load and refresh."""
    return _page_text("index.html").replace("__FILG_HEAD__", _page_head())


@app.get("/", response_class=HTMLResponse)
async def index():
    """The unified two-panel build surface: LEFT = the chat (research/board/help/summary displays),
    RIGHT = the decision graph. The landing IS the workspace with the drawer expanded."""
    return _shell()


@app.get("/plan/{sid}", response_class=HTMLResponse)
async def plan_page(sid: str):
    """Deep link into a plan: same shell, the frontend reads the id from the path and restores the
    session — graph, documents, and the conversation log. (Distinct from `/p/{id}` — the public
    share — and `/r/{id}` teardowns.)"""
    return _shell()


@app.get("/account", response_class=HTMLResponse)
@app.get("/account/{tab}", response_class=HTMLResponse)
async def account_page(tab: str = ""):
    """The account surface (projects / files / API key / account) — same shell, the frontend reads
    the tab from the path. Directly visitable so a refresh or a Stripe return lands on the right tab."""
    return _shell()


# v1 is RETIRED (2026-07-06) and the /v2 namespace folded into the root. Old /v2 links — shares,
# bookmarks, Stripe return URLs minted before the move — redirect permanently to the clean paths.
@app.get("/v2")
@app.get("/v2/{rest:path}")
async def v2_redirect(rest: str = ""):
    return RedirectResponse(f"/{rest}" if rest else "/", status_code=301)


# ── Single-page plan-builder frontend ──────────────────────────────────────
# The shell lives in app/web/index.html (CSS/JS split into app/web/static, served via the /static
# mount above). Cached on mtime, NOT read-once: uvicorn --reload only watches .py files, so a
# read-once shell kept serving YESTERDAY'S HTML against today's fresh /static JS/CSS after a pull —
# the JS then broke on elements the stale page didn't have (found via an empty research pane,
# 2026-07-05). A stat() per request buys shell edits that always propagate.
_PAGE_CACHE: dict = {}


def _page_text(name: str) -> str:
    f = _WEB_DIR / name
    mt = f.stat().st_mtime
    hit = _PAGE_CACHE.get(name)
    if not hit or hit[0] != mt:
        hit = (mt, f.read_text(encoding="utf-8"))
        _PAGE_CACHE[name] = hit
    return hit[1]
