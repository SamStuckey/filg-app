#!/usr/bin/env python3
"""
FILG MVP backend — the thin app that runs the engine for a stranger.

Flow: plain-text idea in → async label-don't-chase run (the engine) → graded artifact out, gated by
the free-tier cap + daily kill switch (prototype/usage.py). Reuses the engine wholesale; nothing
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
import sys
import threading
import traceback
import uuid
import zipfile
from pathlib import Path

import markdown
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# import the engine + guardrail (prototype/) and the app-side skill/persona/board layer (app/).
_APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_APP_DIR.parent / "prototype"))
sys.path.insert(0, str(_APP_DIR))  # app/ → bare sibling imports (personas, intake, board, skills)
import teardown  # noqa: E402
import usage     # noqa: E402
import personas  # noqa: E402 — advisor/director registry (shared by ask-an-expert + the board)
import board     # noqa: E402 — Board of Directors orchestration
import director_forge  # noqa: E402 — forge a custom Board director from a description (distill→draft→QA)
import gibberish  # noqa: E402 — pre-LLM "is this even an idea?" gate (saves a run, hands back a roast)
import intake     # noqa: E402 — shape + vet (the kill-gate); /revet re-runs it after added substance
import brainstorm # noqa: E402 — diverge/merge: the top of the funnel (1-3 directions → one refined idea)
import router     # noqa: E402 — the single prompt box (intent routing) + the pivot-fork integration gate
import plan_pdf   # noqa: E402 — styled PDF generation (synthesis + fpdf2 render)
import advisor    # noqa: E402 — "chat with your plan" (grounded advisory layer)
import skeptic    # noqa: E402 — adversarial assumption-checking on the live research path
import provider   # noqa: E402 — BYOK: per-run LLM provider (FILG's key vs a user's OpenRouter key)
import pipeline   # noqa: E402 — engine: per-run cost ledger (run_ledger) for safe concurrency
import model_catalog  # noqa: E402 — model ids/prices/slugs + cached Models API availability

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
    BYOK is required from the first submit (/api/plan/start), so every engine call is walled."""
    if _needs_key(session.get("user")):
        return JSONResponse(
            {"error": "Add your API key (OpenRouter or Anthropic) to keep building.",
             "needKey": True}, status_code=402)
    return None


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


def _concurrency_cap(user: str) -> int:
    return CONCURRENCY_CAP


@contextlib.contextmanager
def _run_slot(user: str, stack: str | None = None):
    """Reserve a concurrency slot for `user`, bind their provider + model stack + a fresh per-run cost
    ledger, then release the slot on exit. Raises BusyError if they're already at their plan's limit.
    The premium stack is clamped off FILG's free key so a free run can't spend Opus on FILG's dime."""
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
    """402 when a subscriber has used their monthly allowance: offer the two off-ramps (bring a key to
    keep going free, or wait for the renewal reset). `needKey` reopens the key modal in the frontend."""
    b = e.budget or {}
    return JSONResponse(
        {"error": "You've used this month's plan allowance on our key. Add your own API key to keep "
                  "building for free, or your allowance resets when your subscription renews.",
         "fairUse": True, "needKey": True, "resetAt": b.get("reset_at"), "tier": b.get("tier")},
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
            "pdf_unlocked": _has_pdf_access(authed["email"]),   # comp (coupon) OR subscription → unlimited
            "pdf_credits": billing.credits_left(authed["email"]),   # paid plan-unlock credits remaining
            "pdf_billing": billing.PDF_BILLING_ENABLED, "pdf_price": billing.PDF_PRICE_CENTS,
            "auth_enabled": auth.AUTH_ENABLED,
            # subscription state for the fork UI + usage meter
            "sub_enabled": billing.PDF_BILLING_ENABLED, "tiers": tiers.catalog(),
            "tier": tier, "tier_label": tiers.label(tier) if tier else None,
            "subscription": _budget(authed["email"], tier)}


@app.post("/api/plan/{sid}/buy-pdf")
async def api_buy_pdf(sid: str, request: Request):
    """Start a $7 Checkout that grants 3 PDF plan-unlock credits. Requires a signed-in owner of a
    finished plan. If this plan is already unlocked (or there are credits to spend on it), no payment
    is needed — the client just downloads. Raw export stays free."""
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
    tier = (body.get("tier") or "").strip()
    if not tiers.is_tier(tier):
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


@app.post("/api/coupon")
async def api_coupon(request: Request):
    """Redeem a coupon code to unlock the polished PDF for free (account-wide). Signed-in only. The
    remaining-uses counter is never returned — a spent/invalid code gets the same coarse message."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    code = (body.get("code") or "").strip()
    if not code:
        return JSONResponse({"error": "Enter a code."}, status_code=400)
    ok, _reason = store.redeem_coupon(code, auth.normalize_email(authed["email"]))
    if not ok:
        return JSONResponse({"error": "That code isn't valid."}, status_code=400)
    return {"unlocked": True}


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
        with _run_slot(s.get("user"), s.get("stack")):
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
        import provider as prov_mod
        import pipeline
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
    """Public, read-only view of a plan the owner explicitly shared (private by default)."""
    s = store.plan_get(sid)
    if not s or not s.get("shared"):
        return HTMLResponse(render.not_found("This plan isn't shared or doesn't exist."), status_code=404)
    idea = planner._working_idea(s)
    inner = markdown.markdown(planner.bundle_markdown(idea, s.get("files") or {}), extensions=["extra"])
    title = ((s.get("shaped") or {}).get("thesis") or s["idea"] or "Shared business plan")[:120]
    return HTMLResponse(render.shared_plan_page(title, inner))


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
        "qa": s.get("qa"),   # final QA-pass report {notes, fixed} on the finished plan

        "tree": _tree_view(s["tree"]) if s.get("tree") else None,
        "stage": s.get("stage"),                      # funnel position: brainstorm | refined | building | done
        "activeNode": _active_node_view(s),           # the active node's funnel payload (option cards / refined idea / fork)
        "chat": s.get("chat") or [], "chatStarters": advisor.STARTERS,
        "progress": s.get("progress") or [],
        "cost": s.get("cost") or 0, "tokens": s.get("tokens") or 0,   # live session usage meter
        "stack": s.get("stack") or provider.DEFAULT_STACK,            # chosen model stack
        # PDF access for THIS active branch + the account's remaining credits. The button shows
        # Download when this plan is already unlocked OR there are credits to spend; else Unlock ($7=3).
        # A new branch built from an earlier node is a fresh plan_key → locked until claimed.
        "pdfUnlocked": _has_pdf_access((s.get("user") or "").strip(), _plan_key(s), verified=True),
        "pdfCredits": store.credits_left((s.get("user") or "").strip()),
    }


# ── Branching decision tree — node wiring around planner's pure tree functions ─
def _new_node(content: dict, parent: str | None) -> dict:
    """Wrap pure node content (from planner) with the tree bookkeeping main.py owns."""
    return {"id": uuid.uuid4().hex[:8], "parent": parent, "children": [], **content}


def _kind(node: dict) -> str:
    """A node's kind. Legacy plan-section nodes (built before the funnel existed) have no `kind`, so an
    absent kind means 'section'. Funnel kinds: brainstorm | option | refined | fork."""
    return (node or {}).get("kind") or "section"


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
    view = {"id": a["id"], "kind": k, "title": a.get("title")}
    if k == "brainstorm":
        view["spread"] = a.get("spread")
        view["feedback"] = a.get("feedback")   # the pivot ask this spread answers (if any)
        view["options"] = [{"id": c, "direction": ((t["nodes"].get(c) or {}).get("direction"))}
                           for c in a.get("children", []) if _kind(t["nodes"].get(c) or {}) == "option"]
    elif k == "option":
        view["direction"] = a.get("direction")
    elif k == "refined":
        for f in ("thesis", "founder_edge", "mold", "kept", "dropped", "research", "selected"):
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
        return {"step": 0, "files": {}, "history": [], "board": [], "proposal": None,
                "qa": None, "status": "building"}
    done = a["step"] >= planner.N
    return {"step": a["step"], "files": a["files"], "history": a["history"], "board": a["board"],
            "proposal": (None if done else {"section": a["section"], "title": a["title"],
                                            "draft": a["draft"], "change": a.get("change")}),
            "qa": a.get("qa") if done else None,   # the final QA-pass report, surfaced on the finished branch
            "status": "done" if done else "building"}


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
    return {"nodes": {node["id"]: node}, "active": node["id"]}


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
        tree = {"nodes": {root["id"]: root}, "active": root["id"]}
        store.plan_save(session_id, status="building", research=prep["research"], step=0,
                        proposal=prep["proposal"], shaped=prep["shaped"], vetting=prep["vetting"],
                        cost=prep["cost"], tokens=toks, tree=tree, progress=progress)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()  # full trace → Render stdout logs (client only sees str(e))
        store.plan_save(session_id, status="error", error=_humanize_error(e)[0])


# ── The diverge/converge funnel (brainstorm → merge → commit), layered on the same node tree ──
def _diverge_tree(diverge: dict, parent: str | None = None) -> tuple[dict, str]:
    """A `brainstorm` fork node with one `option` child per direction. Returns (nodes_by_id,
    brainstorm_node_id). The active pointer sits on the brainstorm node (the fork being decided)."""
    b = _new_node({"kind": "brainstorm", "step": 0, "title": "A few directions",
                   "spread": diverge.get("spread"), "draft": None, "files": {}, "history": [],
                   "board": []}, parent)
    nodes = {b["id"]: b}
    for d in (diverge.get("directions") or []):
        o = _new_node({"kind": "option", "step": 0, "title": d.get("title") or "Direction",
                       "direction": d, "draft": d.get("one_liner"), "files": {}, "history": [],
                       "board": []}, b["id"])
        b["children"].append(o["id"])
        nodes[o["id"]] = o
    return nodes, b["id"]


def _refined_node(m: dict, selected: list, parent: str | None) -> dict:
    """The reconciled single idea (+ the adversarial cull + a light research skim) as one `refined`
    node, a child of the brainstorm fork. `selected` records which option ids fed the merge."""
    return _new_node({"kind": "refined", "step": 0, "title": "Refined idea", "thesis": m["thesis"],
                      "founder_edge": m.get("founder_edge"), "mold": m.get("mold"),
                      "kept": m.get("kept"), "dropped": m.get("dropped"), "research": m.get("research"),
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
    if tok and tree.get("_run") != tok:
        prog = list(s1.get("progress") or []) + ["✂ run abandoned — you moved on before it finished"]
        store.plan_save(sid, progress=prog)
        return None
    return s1, tree


def _run_merge(sid: str, option_ids: list, user: str, tok: str | None = None) -> None:
    """Background: reconcile the chosen directions into one refined idea (+ light research skim), then
    attach a `refined` node under the brainstorm fork and advance the active pointer to it."""
    try:
        s0 = store.plan_get(sid) or {}
        base_cost, base_tokens = s0.get("cost") or 0, s0.get("tokens") or 0
        progress = list(s0.get("progress") or [])
        nodes0 = (s0.get("tree") or {}).get("nodes") or {}
        directions = [(nodes0.get(i) or {}).get("direction") for i in option_ids]
        directions = [d for d in directions if d]
        prov = _provider_for(user)
        stk = tiers.clamp_stack(s0.get("stack"), tier=_tier(user), byok=bool(prov and not prov.bills_filg))
        with provider.use(prov), provider.use_stack(stk), pipeline.run_ledger():
            m, cost = brainstorm.merge(s0["idea"], directions, mock=MOCK,
                                       on_progress=_bg_progress(sid, base_cost, base_tokens, progress))
            toks = pipeline.LEDGER.tokens()
        _meter_bg(user, prov, cost, toks)
        fresh = _fresh_tree_or_abandon(sid, tok)
        if fresh is None:
            return                                # the user pivoted mid-run — their newer state wins
        _s1, tree = fresh
        nodes = tree.get("nodes") or {}
        parent = tree.get("active")
        refined = _refined_node(m, option_ids, parent)
        nodes[refined["id"]] = refined
        if parent and nodes.get(parent):
            nodes[parent].setdefault("children", []).append(refined["id"])
        tree["active"] = refined["id"]
        store.plan_save(sid, status="building", stage="refined", tree=tree, progress=progress,
                        cost=round(base_cost + cost, 4), tokens=base_tokens + toks)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        store.plan_save(sid, status="error", error=_humanize_error(e)[0])


def _deep_build(sid: str, thesis: str, user: str, tok: str | None = None) -> None:
    """Background: the COMMIT step — the one deep research run + first section draft, attached to the
    tree under the active (refined) node so the funnel history is preserved. Same engine as the legacy
    welcome run, but it grows the existing tree instead of reseeding a fresh root."""
    try:
        s0 = store.plan_get(sid) or {}
        base_cost, base_tokens = s0.get("cost") or 0, s0.get("tokens") or 0
        progress = list(s0.get("progress") or [])
        prov = _provider_for(user)
        stk = tiers.clamp_stack(s0.get("stack"), tier=_tier(user), byok=bool(prov and not prov.bills_filg))
        with provider.use(prov), provider.use_stack(stk), pipeline.run_ledger():
            prep = planner.prepare(thesis, mock=MOCK,
                                   on_progress=_bg_progress(sid, base_cost, base_tokens, progress))
            toks = pipeline.LEDGER.tokens()
        _meter_bg(user, prov, prep["cost"], toks, is_run=True, research_cost=prep["research_cost"])
        fresh = _fresh_tree_or_abandon(sid, tok)
        if fresh is None:
            return                                # the user pivoted mid-run — their newer state wins
        _s1, tree = fresh
        nodes = tree.get("nodes") or {}
        parent = tree.get("active")
        root = _new_node(planner.root_node(prep["proposal"]), parent)   # a plain section node (kind absent)
        nodes[root["id"]] = root
        if parent and nodes.get(parent):
            nodes[parent].setdefault("children", []).append(root["id"])
        tree["active"] = root["id"]
        store.plan_save(sid, status="building", stage="building", research=prep["research"], step=0,
                        proposal=prep["proposal"], shaped=prep["shaped"], vetting=prep["vetting"],
                        tree=tree, progress=progress, cost=round(base_cost + prep["cost"], 4),
                        tokens=base_tokens + toks)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        store.plan_save(sid, status="error", error=_humanize_error(e)[0])


def _node_snippet(n: dict) -> str:
    """One line describing a node, for router context."""
    k = _kind(n)
    if k == "refined":
        return "the refined idea: " + (n.get("thesis") or "")[:200]
    if k == "option":
        d = n.get("direction") or {}
        return "the direction '" + (d.get("title") or "")[:80] + "': " + (d.get("one_liner") or "")[:160]
    if k == "idea":
        return "the original idea: " + (n.get("draft") or "")[:160]
    if k == "brainstorm":
        return "the fork where a few directions were offered"
    body = (n.get("files") or {}).get(next(iter(n.get("files") or {}), ""), "") or n.get("draft") or ""
    return "the '" + (n.get("title") or "part") + "' section of the plan: " + str(body)[:200]


def _path_snippets(nodes: dict, at_id: str | None) -> list[str]:
    """The pivot contract's context: the pivot node and its ANCESTORS only (root → node, in order) —
    siblings and descendants are dropped. Pivoting from an option means the question was re-answered
    with ONLY that option selected, so the path ending at it IS the affirmative context."""
    chain = []
    cur = at_id
    while cur is not None and nodes.get(cur):
        chain.append(nodes[cur])
        cur = nodes[cur].get("parent")
    return [_node_snippet(n) for n in reversed(chain)]


def _route_context(s: dict, node_id: str | None = None) -> str:
    """A compact summary of what the user is looking at, so the router reads their prompt in context.
    `node_id` (the node the user has OPEN, when it isn't the active one) takes over the frame — a
    question like 'how did we make this decision' is about THAT node, and a pivot grows from it."""
    parts = []
    t = s.get("tree") or {}
    nodes = t.get("nodes") or {}
    opened = nodes.get(node_id) if node_id else None
    if opened and node_id != t.get("active"):
        parts.append("The user is looking at an EARLIER node of their build: " + _node_snippet(opened))
        parts.append("A steer/pivot should grow from that node; a question is about it")
    a = nodes.get(t.get("active")) or {}
    if _kind(a) == "refined" and a.get("thesis"):
        parts.append("Refined idea: " + a["thesis"])
    elif (s.get("shaped") or {}).get("thesis"):
        parts.append("Idea: " + s["shaped"]["thesis"])
    else:
        parts.append("Idea: " + (s.get("idea") or "")[:160])
    p = s.get("proposal") or {}
    if p.get("title"):
        parts.append("Currently on the '" + p["title"] + "' part of the plan")
    return " · ".join(parts)[:700]


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
    try:
        with _run_slot(user, body.get("stack")):
            d, cost = brainstorm.diverge(idea, mock=MOCK)
            toks = pipeline.LEDGER.tokens()
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
    nodes, bid = _diverge_tree(d, base["id"])
    base["children"].append(bid)
    nodes[base["id"]] = base
    store.plan_save(sid, status="building", stage="brainstorm", tree={"nodes": nodes, "active": bid},
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
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            d, cost = brainstorm.diverge(div_input, mock=MOCK)
            toks = pipeline.LEDGER.tokens()
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
    new_nodes, bid = _diverge_tree(d, parent)
    new_nodes[bid]["feedback"] = feedback or idea[:120]   # the pivot ask, visible on the fork forever
    nodes.update(new_nodes)
    if parent and nodes.get(parent):
        nodes[parent].setdefault("children", []).append(bid)
    tree["nodes"] = nodes
    tree["active"] = bid
    tree["_run"] = uuid.uuid4().hex[:8]      # pivoting abandons any run still in flight — you moved on
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
    tok = uuid.uuid4().hex[:8]
    tree = s.get("tree") or {"nodes": {}, "active": None}
    tree["_run"] = tok                       # this run's epoch — a pivot mid-run invalidates it
    store.plan_save(sid, status="researching", stage="merging", tree=tree)
    threading.Thread(target=_run_merge, args=(sid, valid, s.get("user"), tok), daemon=True).start()
    return {"id": sid}


@app.post("/api/plan/{sid}/commit")
async def api_plan_commit(sid: str, request: Request):
    """'I'm sold, build the plan' — the one deep research run + first plan page. Uses the active refined
    node's thesis (or a direction/thesis passed in the body). Background; the frontend polls."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    thesis = (body.get("thesis") or "").strip()
    tree = s.get("tree") or {}
    nodes = tree.get("nodes") or {}
    # building from an explicitly named node (the one the user had open) jumps the active pointer
    # there first, so the deep build grows out of THAT node
    at_id = (body.get("node") or "").strip()
    if at_id and nodes.get(at_id):
        tree["active"] = at_id
        store.plan_save(sid, tree=tree)
    a = nodes.get(tree.get("active")) or {}
    if not thesis:
        if _kind(a) == "refined":
            thesis = a.get("thesis") or ""
        elif _kind(a) == "option":
            dr = a.get("direction") or {}
            thesis = dr.get("one_liner") or dr.get("title") or ""
    if len(thesis) < 8:
        return JSONResponse({"error": "Refine an idea or pick a direction to build first."}, status_code=400)
    tok = uuid.uuid4().hex[:8]
    tree["_run"] = tok                       # this run's epoch — a pivot mid-run invalidates it
    store.plan_save(sid, status="researching", stage="researching", tree=tree)
    threading.Thread(target=_deep_build, args=(sid, thesis, s.get("user"), tok), daemon=True).start()
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
    try:
        with _run_slot(s.get("user"), s.get("stack")):
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
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    _fold_usage(sid, s, cost, toks)
    out = {"decision": decision, "cost": cost, "tokens": toks}
    if fork:
        out["fork"] = fork
    return out


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
           "children": n.get("children") or [], "feedback": n.get("feedback"), "step": n.get("step", 0)}
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
HELP_SYSTEM = (
    "You are the in-app help assistant for FILG (a tool that turns a rough business idea, or just "
    "someone's skills and interests, into a vetted, buildable business plan). Help the user USE the "
    "product. Be brief and concrete (2 to 5 sentences), friendly and plain.\n\n"
    "How FILG works:\n"
    "- Start on the home page: type your idea (or just what you're good at) and submit. FILG researches "
    "the market and grades every stat through a source-credibility gate, so vendor marketing is labeled, "
    "not repeated as fact. Then it vets the idea (pursue / pivot / kill).\n"
    "- Then you build the plan one part at a time (7 parts: the setup, what you sell, why you win, "
    "pricing, go-to-market, delivery, and a 30-day plan).\n"
    "- To move through the build, use the buttons at the bottom: 'I'm with you' locks the current part in "
    "and builds the next one; 'Not feeling it' redraws the current part, and you can add a note to steer "
    "the rewrite. You can branch back to an earlier part anytime from the plan tree.\n"
    "- Board of Directors: optional AI advisors that review your sections; you can convene them or forge a "
    "custom one. 'Chat with your plan' is an advisor grounded in your actual plan and research.\n"
    "- Export: the raw files (.zip) and the LLM hand-off prompt are free; the polished investor-grade PDF "
    "is a one-time $13 unlock.\n"
    "- Your key: FILG runs on your own API key (OpenRouter or Anthropic). Add or change it in the key "
    "modal or the API config tab of your profile. Everything uses your key, usually pennies per plan.\n"
    "- Your profile (/account) has tabs for your plans, files, API config, and account settings.\n\n"
    "Only answer questions about USING FILG. If they ask for strategy on their specific business, point "
    "them to 'Chat with your plan' or the Board. Do not invent features you're unsure about. Write "
    "plainly: no em-dashes, no AI-tell words."
)

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
            reply = pipeline.call("help", pipeline.SONNET, max_tokens=400, system=HELP_SYSTEM, cache=True,
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


@app.post("/api/plan/{sid}/respond")
async def api_plan_respond(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _key_wall(s)):
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
    if (wall := _key_wall(s)):
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
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            if killed:   # forced past the gate with no substance → waste-of-time mode (comedic, skips research → ~$0)
                child, cost = planner.wod_forward(active)
            else:
                child, cost = planner.forward(planner._working_idea(s), s["research"], active, feedback,
                                              directors=s.get("directors") or None,
                                              founder=planner._founder(s), mock=MOCK,
                                              extra_personas=s.get("custom_directors"))
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    node = _new_node(child, active["id"])
    tree["nodes"][node["id"]] = node
    active.setdefault("children", []).append(node["id"])
    tree["active"] = node["id"]
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
    if (wall := _key_wall(s)):
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
        tree = {"nodes": {root["id"]: root}, "active": root["id"]}
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
    if (wall := _key_wall(s)):
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
    node = _new_node(sib, prev.get("parent"))   # sibling of `prev` → branches from prev's parent
    tree["nodes"][node["id"]] = node
    if prev.get("parent"):
        tree["nodes"][prev["parent"]].setdefault("children", []).append(node["id"])
    tree["active"] = node["id"]
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
    if (wall := _key_wall(s)):
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
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            sib, cost = planner.rebranch(planner._working_idea(s), s["research"], active, feedback,
                                         founder=planner._founder(s), mock=MOCK)
            regrade, cost = _regrade_setup(s, sib, cost)   # setup reframed → re-grade the verdict on the new angle
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    node = _new_node(sib, active.get("parent"))   # sibling of the active node → same step, new branch
    tree["nodes"][node["id"]] = node
    if active.get("parent"):
        tree["nodes"][active["parent"]].setdefault("children", []).append(node["id"])
    tree["active"] = node["id"]
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
    if (wall := _key_wall(s)):
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
    if (wall := _key_wall(s)):
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
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            res, cost = board.convene(work_idea, plan_text, question, directors, mock=MOCK,
                                      extra_personas=customs)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    _meter(s.get("user"), cost)
    extra = {"directors": directors} if picked else {}   # persist a freshly chosen board for later steps
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
    if (wall := _key_wall(s)):
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
    if (wall := _key_wall(s)):
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
    if (wall := _key_wall(s)):
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
    if s["status"] in ("researching", "error"):
        return JSONResponse({"error": "Finish building the plan first."}, status_code=409)
    if (wall := _key_wall(s)):
        return wall
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Ask a question."}, status_code=400)
    if len(message) > 2000:
        return JSONResponse({"error": "Keep it under 2000 characters."}, status_code=400)
    history = list(s.get("chat") or [])
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            reply, cost = advisor.chat_reply(s, message, history=history, mock=MOCK)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)
    history += [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
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
    lays out the active branch's sections, and appends the graded-research evidence exhibit. Paid via
    plan-unlock credits ($7 = 3 plans); re-downloading a plan you've already unlocked is free. The
    synthesis runs on the OWNER'S bound key (`_run_slot` binds their provider). Builds from `s["files"]`
    = the active branch's final decision set."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if s["status"] != "done":
        return JSONResponse({"error": "plan isn't finished yet"}, status_code=400)
    authed = auth.user_from_request(request)
    email = (authed or {}).get("email", "")
    # Subscribers get the polished PDF free (it's part of the plan). Otherwise claim it: free if comped
    # or already unlocked, else spend one of the account's credits. Billing off (dev) → always open.
    # False → no access and no credits → ask for payment.
    if (billing.PDF_BILLING_ENABLED and not _is_subscriber(email)
            and not billing.claim_pdf(email, _plan_key(s))):
        return JSONResponse(
            {"error": "You're out of PDF credits. Unlock 3 plans for $7. Your raw export is free.",
             "needPurchase": True, "price": billing.PDF_PRICE_CENTS}, status_code=402)
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            plan, cost = plan_pdf.synthesize(s, mock=MOCK)
            data = plan_pdf.render(plan, style=(request.query_params.get("style") or "filg"))
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
                             "X-FILG-Cost": str(nc), "X-FILG-Tokens": str(nt)})


def _page_head(deep: bool = False) -> str:
    """The `__FILG_HEAD__` block: the window.FILG config + optional deep-link + Supabase script. Shared
    by the live shell (_render_page) and the v2 surface so both boot with the same config."""
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
    # Set the routing class on <html> BEFORE the body paints → no intake flash on a deep-link/refresh.
    if deep:
        head += "<script>document.documentElement.className+=' route-plan'</script>"
    if auth.AUTH_ENABLED:
        head += '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
    return head


def _render_page(deep: bool = False) -> str:
    """The single-page app shell. Served at `/` and at clean deep-link paths like `/plan/{id}` so the
    frontend can use real History-API URLs (no `#`) and direct-load / refresh still works."""
    return PAGE.replace("__FILG_HEAD__", _page_head(deep))


@app.get("/", response_class=HTMLResponse)
async def index():
    return _render_page()


@app.get("/v2", response_class=HTMLResponse)
async def v2():
    """The UX-overhaul surface: the unified two-panel build (left = prompt box + tree + tools, right =
    the decision graph). Wired to the funnel routes (/api/brainstorm → /merge → /commit) + /route.
    Served alongside the live app so the new experience can be built + shown without destabilizing it."""
    return V2_PAGE.replace("__FILG_HEAD__", _page_head())


@app.get("/plan/{sid}", response_class=HTMLResponse)
async def plan_page(sid: str):
    """Serve the SPA shell for a deep-linked plan; the frontend reads the id from the path and loads
    it. (Distinct from `/p/{id}` — the server-rendered public share — and `/r/{id}` teardowns.)"""
    return _render_page(deep=True)


@app.get("/account", response_class=HTMLResponse)
@app.get("/account/{tab}", response_class=HTMLResponse)
async def account_page(tab: str = ""):
    """Serve the SPA shell for the profile/account tabs (/account/plans, /account/api-config, …) so they
    are directly visitable and survive a refresh. The frontend reads the tab from the path and opens it.
    `deep=True` boots with the loader (no landing-page flash before the tab renders)."""
    return _render_page(deep=True)


# ── Single-page plan-builder frontend ──────────────────────────────────────
# The shell lives in app/web/index.html (CSS/JS split into app/web/static, served via the /static
# mount above). Read once at import; _render_page injects __FILG_HEAD__ per request.
PAGE = (_WEB_DIR / "index.html").read_text(encoding="utf-8")
V2_PAGE = (_WEB_DIR / "v2.html").read_text(encoding="utf-8")   # the UX-overhaul two-panel surface (/v2)
