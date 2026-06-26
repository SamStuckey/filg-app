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

import html
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

# import the engine + guardrail (prototype/) and the app-side skill/persona/board layer (app/).
_APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_APP_DIR.parent / "prototype"))
sys.path.insert(0, str(_APP_DIR))  # app/ → bare sibling imports (personas, intake, board, skills)
import teardown  # noqa: E402
import usage     # noqa: E402
import personas  # noqa: E402 — advisor/director registry (shared by ask-an-expert + the board)
import board     # noqa: E402 — Board of Directors orchestration
import gibberish  # noqa: E402 — pre-LLM "is this even an idea?" gate (saves a run, hands back a roast)
import intake     # noqa: E402 — shape + vet (the kill-gate); /revet re-runs it after added substance
import plan_pdf   # noqa: E402 — styled PDF generation (synthesis + fpdf2 render)
import advisor    # noqa: E402 — "chat with your plan" (grounded advisory layer)
import provider   # noqa: E402 — BYOK: per-run LLM provider (FILG's key vs a user's OpenRouter key)
import pipeline   # noqa: E402 — engine: per-run cost ledger (run_ledger) for safe concurrency

from . import auth, billing, keys, planner, store  # noqa: E402 — persistence, auth, billing, BYOK keys

MOCK = os.environ.get("FILG_MOCK") == "1"


def _has_pdf_access(email: str, verified: bool = False) -> bool:
    """True iff this user may generate/download the polished PDF: they bought the one-time $13 unlock,
    OR billing isn't configured (dev/local → open). The monetization model is now a single paid action:
    the app is free to use on your own key; the polished PDF is the one purchase. No tiers, no sub."""
    if not billing.PDF_BILLING_ENABLED:
        return True
    return billing.has_purchased(email)


def _is_byok(user: str) -> bool:
    """True iff this user runs on their own key (BYOK configured + a key saved)."""
    return bool(user and keys.enabled() and keys.has_key(user))


def _key_provider_kind(api_key: str) -> str:
    """Which backend a pasted key is for, by prefix: sk-ant-… → Anthropic direct, else OpenRouter."""
    return "anthropic" if api_key.strip().startswith("sk-ant-") else "openrouter"


def _build_provider(kind: str, key: str):
    """Build the per-run provider for a stored user key — Anthropic direct or OpenRouter. Either way
    the user pays (bills_filg=False), so it never counts against FILG's daily budget."""
    if kind == "anthropic":
        return provider.anthropic_provider(key, bills_filg=False)
    return provider.openrouter_provider(key)


def _provider_for(user: str):
    """The provider a session should run on: the user's saved key (OpenRouter or Anthropic), else None
    (FILG's key). provider.use(None) is a no-op, so callers can wrap unconditionally."""
    if not (user and keys.enabled()):
        return None
    key = keys.get_key(user)
    if not key:
        return None
    kind = (keys.key_meta(user) or {}).get("provider") or "openrouter"
    return _build_provider(kind, key)


def _meter(user: str, cost: float) -> None:
    """Record spend against FILG's daily budget — but ONLY for non-BYOK runs. A BYOK run is the
    user's spend (and resolves to ~$0 on FILG's ledger anyway), so it never touches the kill switch."""
    if not _is_byok(user):
        usage.record_spend(cost)


def _fold_usage(sid: str, s: dict, cost: float, toks: int, **extra) -> tuple[float, int]:
    """Fold one AI op's cost + token count into the session's running totals (persisting any `extra`
    columns in the same write). The client reads the cumulative `cost`/`tokens` off the session and
    ticks an in-memory, per-session usage meter (resets on reload; never tracked on the account).
    Returns the new cumulative (cost, tokens) so side ops that don't return plan-state can echo them."""
    new_cost = round((s.get("cost") or 0) + cost, 4)
    new_tokens = (s.get("tokens") or 0) + int(toks or 0)
    store.plan_save(sid, cost=new_cost, tokens=new_tokens, **extra)
    return new_cost, new_tokens


def _needs_key(user: str) -> bool:
    """BYOK model: when BYOK is on and this user has no saved key, they're behind the wall — every
    API action, including the very first plan, requires their own key."""
    return bool(keys.enabled() and not _is_byok(user))


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


def _concurrency_cap(user: str) -> int:
    return CONCURRENCY_CAP


@contextlib.contextmanager
def _run_slot(user: str, stack: str | None = None):
    """Reserve a concurrency slot for `user`, bind their provider + model stack + a fresh per-run cost
    ledger, then release the slot on exit. Raises BusyError if they're already at their plan's limit.
    The premium stack is clamped off FILG's free key so a free run can't spend Opus on FILG's dime."""
    cap = _concurrency_cap(user)
    with _inflight_lock:
        if _inflight.get(user, 0) >= cap:
            raise BusyError(cap)
        _inflight[user] = _inflight.get(user, 0) + 1
    try:
        prov = _provider_for(user)
        stk = provider.clamp_stack(stack, byok=prov is not None)
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
    to reopen the key modal."""
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
                "pdf_price": billing.PDF_PRICE_CENTS}
    return {"signed_in": True, "email": authed["email"],
            "pdf_unlocked": _has_pdf_access(authed["email"]),
            "pdf_billing": billing.PDF_BILLING_ENABLED, "pdf_price": billing.PDF_PRICE_CENTS,
            "auth_enabled": auth.AUTH_ENABLED}


@app.post("/api/plan/{sid}/buy-pdf")
async def api_buy_pdf(sid: str, request: Request):
    """Start the one-time $13 Checkout that unlocks the polished investor-grade PDF (the single paid
    action). Requires a signed-in owner of a finished plan. Raw export stays free."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    if not billing.PDF_BILLING_ENABLED:
        return JSONResponse({"error": "Billing isn't configured yet."}, status_code=503)
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if _has_pdf_access(authed["email"]):
        return JSONResponse({"error": "You've already unlocked the polished PDF.", "unlocked": True},
                            status_code=409)
    try:
        url = billing.create_pdf_checkout_url(authed["email"], user_id=authed["id"], plan_id=sid)
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
            "pdf_billing": billing.PDF_BILLING_ENABLED, **usage.snapshot()}


CTA = ('<div class="cta"><a class="btn btn-primary" href="https://filg.ai/#start">'
       'Run your own idea →</a></div>')


def _receipt(stats: dict) -> str:
    return (f'<div class="recpt"><strong>The credibility receipt:</strong> {stats["checked"]} '
            f'claims checked · <strong>{stats["cleared"]} cited</strong> · {stats["flagged"]} '
            f'flagged as vendor marketing and labeled.</div>')


def render_result_page(job: dict) -> str:
    """Server-render a finished run as a standalone, shareable branded page (reuses the engine's
    brand shell). Backed by SQLite (app/store.py) so the share link survives restarts."""
    res = job["result"]
    stats = res["stats"]
    ev = f'<h2>The evidence — graded</h2><ul class="ev">{teardown.evidence_li(res["rows"])}</ul>'
    if job.get("mode") == "full":
        title = "Your FILG offer"
        inner = markdown.markdown(res["artifacts_md"], extensions=["extra"])
        desc = "Your full, cited offer + go-to-market from FILG."
        article = (f'<article><span class="eyebrow">Full artifact set · ~${res["cost"]:.2f}</span>'
                   f'{inner}{ev}{_receipt(stats)}{CTA}</article>')
    else:
        p = res["prose"]
        title = p["title"]
        desc = p.get("idea_line", "Your graded offer from FILG.")
        article = (f'<article><span class="eyebrow">Cited Offer Teardown · ~${res["cost"]:.2f}</span>'
                   f'<h1>{html.escape(p["title"])}</h1>'
                   f'<p class="tag">Every number graded — vendor stats labeled, not laundered.</p>'
                   f'<p><strong>The offer:</strong> {html.escape(p["offer"])}</p>'
                   f'<p><strong>How you\'d sell it:</strong> {html.escape(p["gtm"])}</p>'
                   f'{ev}{_receipt(stats)}{CTA}</article>')
    return teardown.page_shell(title, desc, article)


@app.get("/r/{job_id}", response_class=HTMLResponse)
async def share(job_id: str):
    job = store.get(job_id)
    if not job or job.get("status") != "done":
        return HTMLResponse(
            "<p style='font-family:sans-serif;max-width:520px;margin:60px auto;padding:0 22px'>"
            "This result isn't ready yet, failed, or doesn't exist.</p>", status_code=404)
    return render_result_page(job)


@app.get("/p/{sid}", response_class=HTMLResponse)
async def share_plan(sid: str):
    """Public, read-only view of a plan the owner explicitly shared (private by default)."""
    s = store.plan_get(sid)
    if not s or not s.get("shared"):
        return HTMLResponse(
            "<p style='font-family:sans-serif;max-width:520px;margin:60px auto;padding:0 22px'>"
            "This plan isn't shared or doesn't exist.</p>", status_code=404)
    idea = planner._working_idea(s)
    inner = markdown.markdown(planner.bundle_markdown(idea, s.get("files") or {}), extensions=["extra"])
    title = ((s.get("shaped") or {}).get("thesis") or s["idea"] or "Shared business plan")[:120]
    article = (f'<article><span class="eyebrow">Shared business plan · built with FILG</span>'
               f'{inner}{CTA}</article>')
    return teardown.page_shell("Shared plan — FILG", title, article)


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
        "files": [{"path": p, "content": c} for p, c in (s.get("files") or {}).items()],
        "sections": [{"file": x["file"], "title": x["title"], "sub": x["sub"]}
                     for x in planner.SECTIONS],
        "step": s.get("step", 0), "total": planner.N, "proposal": s.get("proposal"),
        "done": s["status"] == "done", "shared": bool(s.get("shared")),
        "tree": _tree_view(s["tree"]) if s.get("tree") else None,
        "chat": s.get("chat") or [], "chatStarters": advisor.STARTERS,
        "progress": s.get("progress") or [],
        "cost": s.get("cost") or 0, "tokens": s.get("tokens") or 0,   # live session usage meter
        "stack": s.get("stack") or provider.DEFAULT_STACK,            # chosen model stack
    }


# ── Branching decision tree — node wiring around planner's pure tree functions ─
def _new_node(content: dict, parent: str | None) -> dict:
    """Wrap pure node content (from planner) with the tree bookkeeping main.py owns."""
    return {"id": uuid.uuid4().hex[:8], "parent": parent, "children": [], **content}


def _tree_view(tree: dict) -> dict:
    """Trim the stored node tree to what the frontend needs to draw + navigate it. `show` flips on
    once a real branch exists (a node with 2+ children, or 2+ roots) — matching 'reveal the tree once
    they branch'."""
    nodes = tree.get("nodes") or {}
    return {"active": tree.get("active"),
            "nodes": [{"id": n["id"], "parent": n.get("parent"), "step": n["step"],
                       "title": n.get("title"), "feedback": n.get("feedback")}
                      for n in nodes.values()],
            "show": bool(nodes)}   # show from the first render (even a single 'setup' node) so the tool's there


def _mirror(tree: dict) -> dict:
    """Flat session fields (step/files/proposal/history/board/status) for the active node, so the
    existing _plan_state + frontend renders keep working off the active branch unchanged."""
    a = tree["nodes"][tree["active"]]
    done = a["step"] >= planner.N
    return {"step": a["step"], "files": a["files"], "history": a["history"], "board": a["board"],
            "proposal": (None if done else {"section": a["section"], "title": a["title"],
                                            "draft": a["draft"], "change": a.get("change")}),
            "status": "done" if done else "building"}


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
        prov = _provider_for(user)   # BYOK: run the whole pre-build pass on the user's key if they have one
        sess0 = store.plan_get(session_id) or {}
        stk = provider.clamp_stack(sess0.get("stack"), byok=prov is not None)  # premium clamped off FILG's key
        with provider.use(prov), provider.use_stack(stk), pipeline.run_ledger():
            prep = planner.prepare(idea, mock=MOCK, on_progress=on_progress)  # intake → research → vet → draft
            toks = pipeline.LEDGER.tokens()   # the welcome run's token usage → seeds the session meter
        if prov is None:   # FILG's key → meter the free run; BYOK is the user's spend, not metered
            # the per-user free-taste counter dedupes on the normalized email (alias anti-abuse)
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
    elif keys.enabled():
        # BYOK on, no key: require a key from the very first submit. FILG covers no runs now —
        # the free "welcome" plan is gone (Sam 2026-06-25); even the first query is on the user's key.
        return JSONResponse(
            {"error": "Add your API key to build your plan — an OpenRouter key (any model) or your "
                      "own Anthropic key (Claude direct). You pay the provider directly, usually "
                      "pennies a plan.",
             "needKey": True}, status_code=402)
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


@app.get("/api/plans")
async def api_plans(request: Request):
    """The signed-in user's plans — for the profile / 'My plans' view."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in to see your plans."}, status_code=401)
    plans = [{"id": p["id"], "idea": p["idea"], "status": p["status"], "step": p["step"],
              "created_at": p["created_at"], "done": p["status"] == "done",
              "shared": bool(p.get("shared"))}
             for p in store.plan_list(authed["email"])]
    return {"email": authed["email"], "total": planner.N, "plans": plans}


@app.get("/api/plan/{sid}")
async def api_plan_get(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
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
                                              founder=planner._founder(s), mock=MOCK)
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
    _fold_usage(sid, s, cost, toks, tree=tree, **_mirror(tree))
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
    _fold_usage(sid, s, cost, toks, tree=tree, **_mirror(tree))
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
    picked = body.get("directors")
    directors = [k for k in (picked or s.get("directors") or personas.DEFAULT_BOARD)
                 if k in personas.KEYS] or personas.DEFAULT_BOARD
    question = (body.get("question") or "").strip() or \
        "Vet the plan so far — what's the one thing I should change before continuing?"
    work_idea = planner._working_idea(s)
    plan_text = planner.bundle_markdown(work_idea, s.get("files") or {})
    try:
        with _run_slot(s.get("user"), s.get("stack")):
            res, cost = board.convene(work_idea, plan_text, question, directors, mock=MOCK)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return _engine_error(e)
    _meter(s.get("user"), cost)
    extra = {"directors": directors} if picked else {}   # persist a freshly chosen board for later steps
    nc, nt = _fold_usage(sid, s, cost, toks, **extra)
    return {**res, "cost": nc, "tokens": nt}


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


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "business-plan").lower()).strip("-")
    return (s or "business-plan")[:50]


@app.get("/api/plan/{sid}/plan.pdf")
async def api_plan_pdf(sid: str, request: Request):
    """The core artifact: a styled, branded PDF of the finished plan. Synthesizes an exec summary,
    lays out the active branch's sections, and appends the graded-research evidence exhibit. This is
    the ONE paid action: a one-time $13 unlocks it; raw `.zip`/`.md` export stays free. The synthesis
    runs on the OWNER'S bound key (user-key-only — `_run_slot` binds their provider), same as every
    other engine call. Builds from `s["files"]` = the final decision set."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if s["status"] != "done":
        return JSONResponse({"error": "plan isn't finished yet"}, status_code=400)
    authed = auth.user_from_request(request)
    if not _has_pdf_access((authed or {}).get("email", ""), verified=authed is not None):
        return JSONResponse(
            {"error": "Unlock the polished, investor-grade PDF for a one-time $13. Your raw export is free.",
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
    # Runs on the owner's own key (user-key-only), so it never touches FILG's budget; the per-session
    # usage meter still reflects it (display-only).
    nc, nt = _fold_usage(sid, s, cost, toks)   # binary response → echo usage via headers for the meter
    fn = f"{_slug(s.get('idea'))}-business-plan.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"',
                             "X-FILG-Cost": str(nc), "X-FILG-Tokens": str(nt)})


def _render_page() -> str:
    """The single-page app shell. Served at `/` and at clean deep-link paths like `/plan/{id}` so the
    frontend can use real History-API URLs (no `#`) and direct-load / refresh still works."""
    cfg = json.dumps({"authEnabled": auth.AUTH_ENABLED,
                      "pdfBilling": billing.PDF_BILLING_ENABLED, "pdfPrice": billing.PDF_PRICE_CENTS,
                      "byokEnabled": keys.enabled(),
                      "supabaseUrl": os.environ.get("SUPABASE_URL", ""),
                      "supabaseAnon": (os.environ.get("SUPABASE_PUBLISHABLE_KEY")
                                       or os.environ.get("SUPABASE_ANON_KEY", "")),
                      "archetypes": personas.catalog(), "defaultBoard": personas.DEFAULT_BOARD})
    head = f"<script>window.FILG={cfg}</script>"
    if auth.AUTH_ENABLED:
        head += '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
    return PAGE.replace("__FILG_HEAD__", head)


@app.get("/", response_class=HTMLResponse)
async def index():
    return _render_page()


@app.get("/plan/{sid}", response_class=HTMLResponse)
async def plan_page(sid: str):
    """Serve the SPA shell for a deep-linked plan; the frontend reads the id from the path and loads
    it. (Distinct from `/p/{id}` — the server-rendered public share — and `/r/{id}` teardowns.)"""
    return _render_page()


# ── Single-page plan-builder frontend (brand-aligned; no build step) ─────────
PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>FILG, build your business plan, with receipts</title>
<link rel="icon" href='data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"%3E%3Crect width="32" height="32" rx="8" fill="%23FF6B4A"/%3E%3Cpath d="M16 4c-3.2 2.8-4.3 7.4-4.3 11.8v3.2h8.6v-3.2C20.3 11.4 19.2 6.8 16 4z" fill="%23fff"/%3E%3Ccircle cx="16" cy="12" r="2.1" fill="%232E7CF6"/%3E%3Cpath d="M11.7 15.5 8.6 20.5l3.1-1.3z" fill="%23fff"/%3E%3Cpath d="M20.3 15.5 23.4 20.5l-3.1-1.3z" fill="%23fff"/%3E%3Cpath d="M13.6 19.5h4.8L16 25.5z" fill="%23FFC23F"/%3E%3C/svg%3E'>
__FILG_HEAD__
<style>
:root{--bg:#fff;--ink:#222;--muted:#666;--line:#ccc;--card:#fff;--paper:#f1f1f1;--hdr:52px;--link:#1a0dab;--ok:#067d2f;--ok-bg:#eef6ef;--warn:#a85b00;--warn-bg:#f7f1e8;--kill:#b3261e;--kill-bg:#f7ecec;--coral:#1a0dab;--coral-d:#b3261e;--sky:#1a0dab;--sun:#a85b00}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Arial,Helvetica,sans-serif}
a{color:var(--link)}
.page{max-width:980px;margin:0 auto;padding:16px 16px 64px}
/* Workspace = a pinned, collapsible left tools drawer + full-width main content. */
.drawerhead{display:none}.drawer-rail{display:none}
body.ws .page{max-width:none}
/* header spans edge-to-edge (FILG top-left), the tools drawer sits BELOW it */
body.ws .top{position:sticky;top:0;z-index:65;background:var(--paper);border-bottom:1px solid #888;margin:-16px -16px 0;padding:0 18px;height:var(--hdr);margin-bottom:0}
body.ws .workspace{display:block;margin-left:300px;transition:margin-left .2s}
body.ws .main{padding-top:14px}   /* line the center column's first card up with the left drawer's */
body.ws .side{position:fixed;left:0;top:var(--hdr);bottom:0;width:300px;overflow-y:auto;background:var(--paper);border-right:1px solid #888;z-index:60;padding:14px 12px;transition:transform .2s}
body.ws .side .sec{background:#fff}
body.ws .drawerhead{display:flex;align-items:center;justify-content:space-between;margin:0 0 8px;padding-bottom:8px;border-bottom:1px solid var(--line)}
body.ws .drawerhead b{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.drawerx{background:none;border:0;color:var(--muted);font-size:20px;line-height:1;cursor:pointer;padding:0 6px}
.drawerx:hover{color:var(--ink)}
body.ws.drawer-collapsed .side{transform:translateX(-100%)}
body.ws.drawer-collapsed .workspace{margin-left:0}
body.ws.drawer-collapsed .drawer-rail{display:flex;align-items:center;gap:7px;position:fixed;left:0;top:50%;transform:translateY(-50%);z-index:61;background:#444;color:#fff;border:0;border-radius:0 8px 8px 0;padding:11px 9px;cursor:pointer;font-size:12px;font-weight:700;writing-mode:vertical-rl;letter-spacing:.05em}
@media(max-width:820px){body.ws .workspace{margin-left:0}body.ws .side{box-shadow:2px 0 18px rgba(0,0,0,.25)}}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.topright{display:flex;align-items:center;gap:12px}
.modesw{display:inline-flex;border:1px solid #888}
.modesw button{background:#fff;color:var(--muted);border:0;border-right:1px solid var(--line);font:inherit;font-size:11.5px;font-weight:700;padding:4px 9px;cursor:pointer}
.modesw button:last-child{border-right:0}
.modesw button.on{background:#444;color:#fff}
.meter{display:inline-flex;align-items:center;gap:6px;background:#fff;border:1px solid var(--line);color:var(--muted);font-size:12px;font-weight:700;padding:3px 8px;cursor:default;font-variant-numeric:tabular-nums}
.stackdial{position:relative;display:inline-flex}
.stackbtn{display:inline-flex;align-items:center;gap:7px;background:#f4f4f4;border:1px solid #888;padding:3px 8px;cursor:pointer;font:inherit;color:var(--ink)}
.stackbtn:hover{border-color:#444}
.stackbtn .stacklbl{font-weight:700;font-size:12px;white-space:nowrap;display:inline-flex;align-items:center;gap:4px;color:var(--ink)}
.stackbtn .sk-star{color:var(--warn);font-size:11px}
.stackcaret{font-size:9px;color:var(--muted)}
.stack-cost{display:inline-flex;gap:2px;align-items:center}
.stack-cost i{width:5px;height:5px;background:var(--line);display:inline-block}
.stack-cost i.on{background:var(--ink)}
.stackpop{position:absolute;right:0;top:calc(100% + 4px);z-index:70;width:300px;max-width:86vw;background:var(--card);border:1px solid #888;padding:4px;display:flex;flex-direction:column;gap:2px}
.stackpop[hidden]{display:none}
.stackpop-h{font-size:11.5px;color:var(--muted);padding:4px 6px;line-height:1.35}
.stacktile{text-align:left;background:transparent;border:1px solid transparent;padding:6px 8px;cursor:pointer;font:inherit;color:var(--ink);display:flex;flex-direction:column;gap:2px}
.stacktile:hover{background:#f4f4f4}
.stacktile.sel{border-color:#444;background:#eee}
.stacktile:focus-visible{outline:2px solid var(--link)}
.st-top{display:flex;align-items:center;gap:7px}
.st-name{font-weight:700;font-size:13px}
.st-badges{display:inline-flex;gap:5px;align-items:center}
.st-badge{font-size:9px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;padding:1px 5px;white-space:nowrap;border:1px solid var(--line)}
.st-badge.rec{color:var(--warn);border-color:var(--warn)}
.st-top .stack-cost{margin-left:auto}
.st-desc{font-size:11.5px;color:var(--muted);line-height:1.4}
.stacktile.sel .st-desc{color:var(--ink)}
.meter[hidden]{display:none}
.meter .m-dot{width:7px;height:7px;background:var(--muted);flex:none}
.meter.live .m-dot{background:var(--ok)}
.meter b{color:var(--ink);font-weight:700}
.meter .m-est{font-weight:400;font-size:10.5px;color:var(--muted);opacity:.8}
h1.logo{font-size:22px;font-weight:700;letter-spacing:-.01em;margin:0}.logo span{color:var(--ink)}
.logobtn{display:inline-flex;align-items:center;gap:6px;background:none;border:0;padding:0;margin:0;font:inherit;color:inherit;letter-spacing:inherit;cursor:pointer}.logobtn:hover{text-decoration:underline}
.logomark{display:none}
.sub{color:var(--muted);margin:0 0 16px;font-size:14px}
textarea,input{width:100%;padding:8px 10px;border:1px solid var(--line);font:inherit;background:#fff;margin-bottom:10px}
textarea:focus,input:focus{outline:none;border-color:#444}textarea{min-height:110px;resize:vertical}
textarea::placeholder,input::placeholder{color:#999;opacity:1}
button{background:#f4f4f4;color:var(--ink);border:1px solid #888;font:inherit;font-weight:700;padding:7px 14px;cursor:pointer}
button:hover{background:#e8e8e8}button:disabled{opacity:.5;cursor:default}
.intake{max-width:680px;margin:22px auto;text-align:center}.intake textarea,.intake input{text-align:left}
.intake h2{font-size:22px;font-weight:700;letter-spacing:-.01em;margin:0 0 6px}.intake .go{font-size:15px;padding:9px 18px}
.brandfoot{margin:34px auto 0;padding-top:14px;border-top:1px solid var(--line);max-width:420px;font-size:13px;color:var(--muted);line-height:1.35}
.brandfoot cite{font-style:normal;font-size:12px}
/* landing CTA row: the real button + the bail-out button */
.golane{display:flex;gap:10px;flex-wrap:wrap;align-items:stretch;justify-content:center;margin-top:2px}
.bail{background:#fff;color:var(--muted);border:1px dashed var(--line);font-size:13px;font-weight:400;padding:9px 14px}
.bail:hover{background:#f7f7f7;color:var(--ink)}
/* unbound scrolling vibe footer — ugly on purpose, craigslist forever */
.vibestrip{position:fixed;bottom:0;left:0;right:0;z-index:40;overflow:hidden;white-space:nowrap;background:transparent;border-top:1px solid var(--line);padding:3px 0;pointer-events:none}
.vibetrack{display:inline-block;white-space:nowrap;will-change:transform;animation:vibescroll 900s linear infinite}
.vibe{font-size:11px;color:var(--muted);opacity:.6;padding:0 2.5em}
@keyframes vibescroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@media(prefers-reduced-motion:reduce){.vibetrack{animation:none}}
body.hasbar .vibestrip{display:none}   /* don't fight the fixed action bar mid-build */
.err{color:var(--kill);margin-top:10px;font-weight:700}
.authbar{display:flex;align-items:center;gap:12px;font-size:13px}
.authbar .who{color:var(--muted)}.authbar b{color:var(--ink)}
.authbar .link{background:none;border:0;color:var(--link);padding:0;font-weight:700;font-size:13px;text-decoration:underline;cursor:pointer}
.authbar .up{background:#f4f4f4;color:var(--ink);border:1px solid #888;padding:4px 10px;font-size:12px}
.note-banner{background:#f4f4f4;border:1px solid var(--line);padding:10px 14px;font-size:13px;margin-bottom:14px;display:none}
.jokecard{margin:14px 0 0;text-align:left;background:#fff;border:1px solid var(--line);padding:14px 16px}
.jokecard h3{margin:0 0 8px;font-size:17px;font-weight:700}
.jokecard .jbody{font-size:14px;color:var(--ink)}.jokecard .jbody p{margin:0 0 8px}
.jokecard button{margin-top:6px}
.workspace{display:grid;grid-template-columns:280px 1fr;gap:22px;align-items:start}
.side{position:sticky;top:16px;display:flex;flex-direction:column;gap:14px}
.sec{background:var(--card);border:1px solid var(--line);padding:14px}
.sec h3{font-size:12px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:700}
.ev{list-style:none;padding:0;margin:0}.ev li{padding:8px 0;border-top:1px dashed var(--line);font-size:13px}.ev li:first-child{border-top:0}
.sec.collap .sechead{display:flex;align-items:center;gap:8px;width:100%;background:none;border:0;padding:0;margin:0;cursor:pointer;text-align:left;font:inherit}
.sec.collap .sechead h3{margin:0;flex:1}
.sec.collap .sechead .caret{color:var(--muted);font-size:12px}
.sec.collap.open .sechead .caret{}
.sec.collap .secbody{display:none;margin-top:10px}
.sec.collap.open .secbody{display:block}
.chatlog{display:flex;flex-direction:column;gap:8px;max-height:42vh;overflow-y:auto;margin-bottom:10px}
.chatlog:empty{display:none;margin:0}
.cmsg{font-size:13px;line-height:1.45;padding:8px 11px;border:1px solid var(--line);max-width:92%}
.cmsg.user{background:#f0f0f0;color:var(--ink);align-self:flex-end}
.cmsg.bot{background:#fff;color:var(--ink);align-self:flex-start}
.cmsg.bot p:first-child{margin-top:0}.cmsg.bot p:last-child{margin-bottom:0}
.cmsg .think{color:var(--muted);font-style:italic}
.chatstart{display:flex;flex-direction:column;gap:6px;margin-bottom:10px}
.chatstart button{background:#fff;border:1px solid var(--line);color:var(--ink);font-size:12.5px;font-weight:700;padding:8px 11px;text-align:left;width:100%}
.chatstart button:hover{border-color:#444}
.chatsend{width:100%}
.ev .note{color:var(--muted);font-size:12px}
.lanes{border:1px solid var(--line);padding:9px 11px;margin:0 0 12px}
.lanesh{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:7px}
.lanerow{font-size:12.5px;padding:4px 0;border-top:1px dashed var(--line)}.lanerow:first-of-type{border-top:0}
.laneown{font-weight:700;color:var(--ink)}.lanesub{color:var(--muted)}
.gate{display:inline-block;margin-top:3px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:var(--muted)}
.badge{font-size:10px;font-weight:700;padding:0 5px;border:1px solid var(--line)}.b-ok{color:var(--ok);border-color:var(--ok)}.b-warn{color:var(--warn);border-color:var(--warn)}
.main{min-width:0}
.node{background:var(--card);border:1px solid var(--line);padding:20px}
.node .eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:700}
.node h3{font-size:19px;font-weight:700;margin:4px 0 2px}.node .h3sub{color:var(--muted);font-size:13px;margin:0 0 12px}
/* sticky offer summary pinned to the top of the center column (hidden until research fills it) */
.main #answer.summary{position:sticky;top:var(--hdr);z-index:30;background:var(--card);border:1px solid var(--line);padding:13px 18px;margin:0 0 14px}
.main #answer.summary:empty{display:none}
#answer.summary h2{font-size:17px;font-weight:700;margin:0 0 3px}
#answer.summary .tag{color:var(--muted);font-size:12px;margin:0 0 7px}
#answer.summary p{margin:4px 0;font-size:13.5px}
/* "Your plan" tab strip — the plan outline + decision tree, merged into the center column */
.planwrap{margin:2px 0 0}
.plantabs{display:flex;gap:4px;overflow-x:auto;padding-top:2px;border-bottom:1px solid #888;scrollbar-width:thin}
.ptab{flex:0 0 auto;display:inline-flex;align-items:center;gap:6px;background:#ececec;color:var(--ink);border:1px solid var(--line);border-bottom:0;border-radius:7px 7px 0 0;font:inherit;font-size:12.5px;font-weight:700;padding:7px 12px;cursor:pointer;white-space:nowrap;position:relative;top:1px}
.ptab .pic{font-size:11px;display:inline-grid;place-items:center;width:15px;height:15px}
.ptab.built .pic{color:var(--ok)}
.ptab.current,.ptab.current .pic{color:var(--link)}
.ptab.pending{opacity:.5;cursor:default}
.ptab:not(.pending):not(.sel):hover{background:#f4f4f4}
.ptab.sel{background:var(--card);border-color:#888;border-bottom:1px solid var(--card)}
.ptab.justdone{box-shadow:0 0 0 2px var(--link) inset}
.pbuild{background:var(--ink);color:#fff;border:1px solid var(--ink);font-weight:700}
.draft{background:#fafafa;border:1px solid var(--line);padding:14px 16px;font-size:14px;margin-bottom:14px;cursor:text}
.md h4,.md h5{font-weight:700;margin:12px 0 4px;line-height:1.3}.md h4{font-size:15px}.md h5{font-size:13.5px}.md>:first-child{margin-top:0}
.md p{margin:0 0 8px}.md p:last-child{margin-bottom:0}.md ul{margin:6px 0 8px;padding-left:20px}.md li{margin:3px 0}
.md a{color:var(--link)}.md strong{font-weight:700}.md code{background:#f0f0f0;border:1px solid var(--line);padding:0 4px;font-size:.92em;font-family:ui-monospace,Menlo,monospace}
.md hr{border:0;border-top:1px solid var(--line);margin:16px 0}
.md table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13.5px;border:1px solid var(--line)}
.md th,.md td{border-bottom:1px solid var(--line);border-right:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
.md th:last-child,.md td:last-child{border-right:0}.md tbody tr:last-child td{border-bottom:0}
.md thead th{background:#f4f4f4;font-weight:700;font-size:12.5px}
.lead{font-size:14px;color:var(--muted);margin:0 0 12px}
.branches{display:flex;gap:10px;flex-wrap:wrap}.branches button{flex:1;min-width:130px;font-size:15px;padding:11px 12px}
.b-but{}.b-no{background:#fff;color:var(--ink);border:1px solid var(--line)}
.fbk{margin-top:14px;border:1px solid #888;padding:12px;background:#fff}
.fbk textarea{margin-bottom:8px}
.killgate{margin-top:14px;border:1px solid var(--kill);padding:12px 14px;background:var(--kill-bg)}
.killgate .kg-head{font-weight:700;color:var(--kill);font-size:15px;margin-bottom:6px}
.killgate .kg-say{margin:0 0 8px;color:var(--ink)}
.killgate .kg-risk{margin:0 0 8px;font-size:13px;color:var(--kill)}
.killgate .kg-q{font-weight:700;margin:0 0 8px}
.killgate textarea{margin-bottom:8px;min-height:80px}
.navrow{display:flex;gap:10px;margin-top:10px}
.navrow button{flex:1}.navrow .b-next{}
.navrow .b-back{background:#fff;color:var(--ink);border:1px solid var(--line);flex:0 0 auto;min-width:96px}
.ferr{color:var(--kill);font-weight:700;font-size:13px;margin-top:8px;min-height:0}
#dtree{display:flex;flex-direction:column;gap:1px}
.compose{margin-top:14px;border:1px solid #888;padding:12px;background:#fff}
.compose .pl{font-weight:700;margin:0 0 8px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 0}
.chip{background:#f4f4f4;border:1px solid var(--line);color:var(--ink);font-size:13px;font-weight:700;padding:5px 10px}
.compose .row{display:flex;gap:8px;margin-top:10px}.compose .row button{flex:none}.compose .ghost{background:#fff;color:var(--muted);border:1px solid var(--line)}
.done{background:var(--ok-bg);border:1px solid var(--line);padding:14px 16px;font-size:15px}
.planacts{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}.planacts .ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
.couponrow{display:flex;gap:8px;margin-top:10px;max-width:340px}
.couponrow input{flex:1;padding:8px 11px;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff}
.couponrow .ghost{background:#fff;color:var(--ink);border:1px solid var(--line);white-space:nowrap}
/* Bottom-pinned step actions: rework (back) vs roll-forward (next). Both open the feedback modal. */
.actionbar{display:none}
.actionbar.show{display:flex;position:fixed;bottom:0;left:300px;right:0;z-index:55;gap:12px;justify-content:flex-end;align-items:center;padding:12px 22px;background:var(--paper);border-top:1px solid #888;box-shadow:0 -2px 14px rgba(0,0,0,.08);transition:left .2s}
body.ws.drawer-collapsed .actionbar.show{left:0}
@media(max-width:820px){.actionbar.show{left:0}}
body.hasbar .workspace{padding-bottom:74px}
.ab-btn{font:inherit;font-weight:700;font-size:14px;padding:11px 20px;border-radius:9px;cursor:pointer;border:1px solid var(--line)}
.ab-btn[disabled]{opacity:.5;cursor:default}
.ab-back{background:#fff;color:var(--ink)}
.ab-back:hover:not([disabled]){background:#f4f4f4}
.ab-next{background:var(--ink);color:#fff;border-color:var(--ink)}
.ab-next:hover:not([disabled]){opacity:.9}
/* Feedback modal: the engine's open questions, the per-step nudge chips, and the note box. */
.mfb-hint{margin:0 0 12px;color:var(--muted);font-size:13.5px}
.mfb-sg{margin:0 0 12px}
.mfb-h{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 6px}
.mfb-q{display:block;width:100%;text-align:left;background:#f7f7f7;border:1px solid var(--line);color:var(--ink);font:inherit;font-size:13px;padding:7px 10px;border-radius:7px;margin:0 0 6px;cursor:pointer}
.mfb-q:hover{background:#eee}
.mfb-chips{display:flex;flex-wrap:wrap;gap:7px;margin:0 0 12px;min-height:30px}
.mfb-chips .chip{cursor:pointer;font-size:12.5px;padding:5px 11px;border-radius:14px}
.mfb-chips .chip:hover{background:#e8e8e8}
.mfb-load{color:var(--muted);font-size:12.5px;font-style:italic;align-self:center}
.mfb-go{background:var(--ink);color:#fff;border:1px solid var(--ink);font-weight:700}
.addons .ax{display:flex;flex-wrap:wrap;gap:8px}
.addons .ax button{flex:1;min-width:120px;background:#fff;border:1px solid var(--line);color:var(--ink);font-size:13px;font-weight:700;padding:9px 10px;text-align:left}
.addons .ax .bl{display:block;font-size:11px;color:var(--muted);font-weight:400}
.bhelp{font-size:12px;color:var(--muted);margin:0 0 10px}
.disc{font-size:11px;color:var(--muted);margin-top:8px}
.vet{background:var(--card);border:1px solid var(--line);padding:16px 18px;margin-bottom:14px}
.vet .vhead{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.vet h3{font-size:16px;font-weight:700;margin:0}
.vet .vethead{display:flex;align-items:center;gap:10px;width:100%;background:none;border:0;padding:0;margin:0;cursor:pointer;font:inherit;text-align:left}
.vet .vtitle{font-size:16px;font-weight:700;color:var(--ink)}
.vet .vcaret{margin-left:auto;color:var(--muted);font-size:12px}
.vet .vetbody{display:none;margin-top:12px}.vet.open .vetbody{display:block}
.vet .filgreact{font-weight:700;font-size:16px;line-height:1.3;color:var(--ink);margin:0 0 12px}
.verdict{font-size:12px;font-weight:700;padding:2px 9px;border:1px solid var(--line);text-transform:uppercase;letter-spacing:.04em}
.verdict.pursue{color:var(--ok);border-color:var(--ok)}.verdict.pivot{color:var(--warn);border-color:var(--warn)}.verdict.kill{color:var(--kill);border-color:var(--kill)}
.vet .thesis{font-size:14px;margin:0 0 10px}.vet .vrow{font-size:13px;color:var(--muted);margin:3px 0}.vet .vrow b{color:var(--ink)}
.premortem{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
.pmh{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:8px}
.pmrow{display:flex;gap:9px;align-items:flex-start;padding:6px 0;border-top:1px dashed var(--line)}.pmrow:first-of-type{border-top:0}
.pmstatus{flex:none;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;padding:1px 6px;border:1px solid var(--line);margin-top:1px}
.pmstatus.pm-holds{color:var(--ok);border-color:var(--ok)}.pmstatus.pm-shaky{color:var(--warn);border-color:var(--warn)}.pmstatus.pm-breaks{color:var(--kill);border-color:var(--kill)}
.pmtext{font-size:13px}.pmtext b{font-weight:700}.pmwhy{display:block;color:var(--muted);font-size:12.5px;margin-top:2px}
.bdirs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.bchip{font-size:12px;font-weight:700;padding:5px 10px;border:1px solid var(--line);background:#fff;cursor:pointer;color:var(--ink)}
.bchip.on{background:#f0f0f0;border-color:#444;color:var(--link)}
.convene{width:100%}
.boardpick{margin:0 0 12px}.boardpick .lab{font-size:13px;color:var(--muted);font-weight:700;margin-bottom:6px;text-align:left}
.boardpick .opts{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-start}
.bround{background:var(--card);border:1px solid var(--line);padding:18px 20px;margin-bottom:18px}
.bround h4{font-size:15px;font-weight:700;margin:0 0 12px}
.balloons{display:flex;flex-direction:column;gap:8px}
.balloon{border:1px solid var(--line)}
.balloon .bh{display:flex;align-items:center;gap:8px;width:100%;padding:9px 12px;cursor:pointer;font:inherit;font-weight:700;font-size:13.5px;color:var(--ink);background:#f4f4f4;border:0;text-align:left}
.balloon .bh:hover{background:#e8e8e8}.balloon .bh .caret{margin-left:auto;color:var(--muted);font-size:12px}
.balloon .bb{padding:0 12px 12px;font-size:13.5px;display:none}.balloon.open .bb{display:block}
.changeflag{background:var(--warn-bg);border:1px solid var(--line);border-left:3px solid var(--warn);padding:10px 13px;margin:0 0 14px;font-size:13.5px;color:var(--ink)}
.changeflag .cf-l{display:block;font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--warn);margin-bottom:4px}
.takeaway{margin-top:14px;background:var(--ok-bg);border:1px solid var(--line);padding:12px 14px;font-size:14px}
.takeaway .tl{font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--ok);margin-bottom:5px}
.takeaway .split{display:block;margin-top:6px;color:var(--muted);font-size:13px}
.skeptic{border:1px solid var(--line);border-left:3px solid var(--warn);padding:11px 13px;margin:0 0 12px;background:#fff}
.skeptic.sk-agree{border-left-color:var(--ok)}
.skeptic.sk-dissent,.skeptic.sk-non-starter{border-left-color:var(--kill)}
.skhead{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.sknm{font-weight:700;font-size:13.5px}
.skverdict{margin-left:auto;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;padding:1px 7px;border:1px solid var(--line)}
.skverdict.sk-agree{color:var(--ok);border-color:var(--ok)}
.skverdict.sk-concern{color:var(--warn);border-color:var(--warn)}
.skverdict.sk-dissent,.skverdict.sk-non-starter{color:var(--kill);border-color:var(--kill)}
.skbody{font-size:13.5px}
.skfix{font-size:13px;margin-top:7px}
.skmeta{font-size:11px;color:var(--muted);margin-top:6px}
.drawer-back{position:fixed;inset:0;background:rgba(0,0,0,.35);opacity:0;visibility:hidden;transition:opacity .15s;z-index:40}
.drawer-back.show{opacity:1;visibility:visible}
.drawer{position:fixed;top:0;right:0;height:100vh;width:min(440px,93vw);background:var(--card);border-left:1px solid #888;transform:translateX(101%);transition:transform .2s;z-index:41;display:flex;flex-direction:column}
.drawer.open{transform:translateX(0)}
.dr-head{display:flex;align-items:center;justify-content:space-between;padding:16px 18px;border-bottom:1px solid var(--line);flex:none}
.dr-head span{font-size:17px;font-weight:700}
.dr-x{background:none;border:0;color:var(--muted);font-size:24px;line-height:1;padding:0 6px;font-weight:400;cursor:pointer}.dr-x:hover{color:var(--ink)}
.dr-body{padding:16px 18px 24px;overflow-y:auto;flex:1}
.dr-sub{color:var(--muted);font-size:13px;margin:0 0 12px}
.dr-go{width:100%;margin-top:2px}
.dr-out{margin-top:16px;font-size:14px;display:none}.dr-out .balloon+.balloon{margin-top:8px}
.modal-back{position:fixed;inset:0;background:rgba(0,0,0,.35);opacity:0;visibility:hidden;transition:opacity .15s;z-index:50}
.modal-back.show{opacity:1;visibility:visible}
.modal{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);width:min(420px,92vw);background:var(--card);border:1px solid #888;padding:20px 22px;z-index:51;opacity:0;visibility:hidden;transition:opacity .15s}
.modal.open{opacity:1;visibility:visible}
.modal h3{font-size:18px;font-weight:700;margin:0 0 8px}
.modal #modal-body{font-size:14px;color:var(--muted);margin-bottom:16px}.modal #modal-body p{margin:0}
.modal-actions{display:flex;justify-content:flex-end;gap:10px}
.cmtpop{position:absolute;z-index:60;width:288px;background:var(--card);border:1px solid #888;padding:10px 12px;opacity:0;visibility:hidden;transition:opacity .12s}
.cmtpop.show{opacity:1;visibility:visible}
.cmtpop .cmtpq{font-size:12px;color:var(--muted);font-style:italic;margin-bottom:7px;max-height:48px;overflow:hidden}
.cmtpop textarea{width:100%;margin:0 0 8px}
.cmtpa{display:flex;justify-content:flex-end;gap:8px}
.cmts{margin:11px 0 2px}
.cmth{font-size:12px;font-weight:700;color:var(--muted);margin-bottom:7px}
.cmt{display:flex;align-items:center;gap:9px;padding:7px 9px;border:1px solid var(--line);border-left:3px solid var(--warn);margin-bottom:6px;background:#fff}
.cmtq{font-style:italic;color:var(--muted);font-size:12.5px;flex:none;max-width:42%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cmtn{font-size:13px;flex:1}
.cmtx{background:none;border:0;color:var(--muted);cursor:pointer;font-size:17px;line-height:1;padding:0 2px;flex:none}
/* inline comment highlight + balloon marker (theme-agnostic tints work on light or dark) */
.draft .hascmt{background:rgba(255,194,63,.13);box-shadow:inset 3px 0 0 var(--warn);border-radius:2px}
.draft .cmt-target{background:rgba(46,124,246,.16);box-shadow:inset 3px 0 0 var(--link);border-radius:2px}
.cmtmark{margin-left:7px;white-space:nowrap;user-select:none;font-size:12px}
.cmtmark button{background:none;border:0;cursor:pointer;font-size:12px;line-height:1;padding:0 2px;color:var(--muted)}
.cmtmark .cmtmark-e:hover{color:var(--ink)}.cmtmark .cmtmark-x:hover{color:var(--kill)}
/* suggested-feedback (the engine's open questions) above the per-part feedback box */
.sfb{margin:0 0 11px;border-bottom:1px dashed var(--line);padding-bottom:9px}
.sfbh{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:5px}
.sfbl{margin:0;padding-left:18px}.sfbl li{margin:4px 0}
.sfbl button{background:none;border:0;color:var(--link);cursor:pointer;text-align:left;font:inherit;padding:0;text-decoration:underline}
.sfbl button:hover{color:var(--ink)}
.toasts{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);display:flex;flex-direction:column;gap:8px;z-index:60;align-items:center;pointer-events:none}
.toast{background:var(--ink);color:#fff;padding:10px 16px;font-size:14px;font-weight:700;transition:opacity .25s;max-width:90vw}
.toast.err{background:var(--kill)}.toast.out{opacity:0}
/* The runner: a permanent main-column panel that swaps in when the intake collapses. */
.runner{border:1px solid #888;background:#f4f4f4;margin:0 0 18px}
.run-head{display:flex;align-items:center;gap:9px;padding:7px 12px;background:#eaeaea;border-bottom:1px solid var(--line);font-size:13px;font-weight:700}
.run-dot{width:8px;height:8px;border-radius:50%;background:#bbb;flex:none}
.runner.busy .run-dot{background:var(--ok);animation:runpulse 1s infinite}
@keyframes runpulse{0%,100%{opacity:1}50%{opacity:.3}}
.run-title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.run-spacer{flex:1}
.run-min{background:none;border:0;color:var(--muted);font-size:12px;cursor:pointer;padding:2px 6px;line-height:1}
.runner.min .run-min{transform:rotate(-90deg)}
.runner.min .run-log{display:none}
/* The fan-out leaves are part of the SAME tree as the spew steps: they nest one level under the
   "Planning the research fan-out" line, inside the op card body. Each leaf expands to its internals. */
.leaftree.leafnest{display:flex;flex-direction:column;font-family:ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;margin:2px 0 6px 12px;border-left:2px solid var(--line);padding-left:8px}
.leafnode{display:flex;align-items:center;gap:7px;width:100%;background:none;border:0;font:inherit;font-size:12.5px;color:var(--muted);cursor:pointer;text-align:left;padding:5px 4px 5px 8px}
.leafnode .leaf-ico{filter:grayscale(1);opacity:.45;transition:filter .25s,opacity .25s;flex:none}
.leafnode.done{color:var(--ink)}
.leafnode.done .leaf-ico{filter:none;opacity:1}
.leafnode .leaf-lbl{font-weight:700;flex:none}
.leafnode .leaf-q{color:var(--muted);font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.leafnode .lcaret{margin-left:auto;flex:none;font-size:10px;opacity:.55;transition:transform .15s}
.leafnode.open .lcaret{transform:rotate(90deg)}
.leafbody{display:none;padding:0 8px 8px 26px;font-size:12px;line-height:1.5}
.leafnode.open + .leafbody{display:block}
.leafbody .lq{color:var(--muted);margin:0 0 7px}
.leafbody .src{padding:6px 0;border-top:1px dashed var(--line);overflow-wrap:anywhere}
.leafbody .src:first-child{border-top:0}
.leafbody .src .note{color:var(--muted)}
.leafbody .src .gate{display:block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:var(--muted);margin-top:2px}
.leafbody .pending{color:var(--muted);font-style:italic}
.run-log{padding:10px 12px;display:flex;flex-direction:column;gap:7px;max-height:46vh;overflow-y:auto}
.atask{border:1px solid var(--line);background:#fff;overflow:hidden}
.atask.done{opacity:.7}
.ah{display:flex;align-items:center;gap:9px;width:100%;background:none;border:0;color:var(--ink);font:inherit;font-size:13px;font-weight:700;padding:8px 12px;cursor:pointer;text-align:left}
.ah .astat{width:13px;flex:none;text-align:center;color:var(--warn)}
.ah .astat::before{content:"\\25cf";font-size:10px}
.atask.done .ah .astat::before{content:"\\2713";color:var(--ok);font-size:13px}
.ah .alabel{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ah .caret{flex:none;font-size:11px;opacity:.6}
.atask.collapsed .abody{display:none}
.abody{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;line-height:1.5;padding:0 12px 9px 12px}
.aline{display:flex;align-items:flex-start;gap:9px;padding:1px 0;background:none;color:var(--muted)}
.aline.done{color:#444}
.aline.active{color:var(--ink);font-weight:600}
.aline .aglyph{width:12px;flex:none;text-align:center;color:var(--warn);margin-top:1px}
.aline.active .aglyph::before{content:"\\203A";font-weight:800}
.aline.done .aglyph::before{content:"\\2713";color:var(--ok)}
.aline .atext{white-space:normal;overflow-wrap:anywhere;min-width:0}
.authgate{margin:6px 0 2px}.authgate button{width:100%;margin-bottom:8px}
.gbtn{display:flex;align-items:center;justify-content:center;gap:10px;background:#fff;color:var(--ink);border:1px solid #888;font-weight:700}
.gicon{width:18px;height:18px;flex:none}
.authgate .or{color:var(--muted);font-size:13px;margin:4px 0 0}
.keysteps{margin:0 0 12px;padding-left:20px;color:var(--muted);font-size:13px;line-height:1.7}
.keysteps a{color:var(--link);font-weight:700}
.plans{max-width:760px;margin:8px auto}.plans h2{font-size:22px;font-weight:700;margin:8px 0 4px}
.pcard{display:flex;justify-content:space-between;align-items:center;gap:12px;background:var(--card);border:1px solid var(--line);padding:14px 16px;margin-bottom:12px}
.pcard .idea{font-weight:700;font-size:15px}.pcard .meta{color:var(--muted);font-size:12px;margin-top:2px}
.pcard .act{display:flex;align-items:center;gap:8px;flex:none}.pcard .act button{font-size:13px;padding:8px 12px}
.pill{font-size:11px;font-weight:700;padding:1px 8px;border:1px solid var(--warn);color:var(--warn)}.pill.done{border-color:var(--ok);color:var(--ok)}
.empty{color:var(--muted);text-align:center;margin:30px 0}
@media(max-width:820px){.workspace{grid-template-columns:1fr}}
a:focus-visible,button:focus-visible,input:focus-visible,textarea:focus-visible,[tabindex]:focus-visible{outline:2px solid var(--link);outline-offset:2px}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
</style></head><body><div class=page>
<div class=top><h1 class=logo><button type=button class=logobtn onclick=newPlan() aria-label="FILG, start a new idea"><svg class=logomark viewBox="0 0 32 32" aria-hidden=true><rect width=32 height=32 rx=8 fill=#FF6B4A></rect><path d="M16 4c-3.2 2.8-4.3 7.4-4.3 11.8v3.2h8.6v-3.2C20.3 11.4 19.2 6.8 16 4z" fill=#fff></path><circle cx=16 cy=12 r=2.1 fill=#2E7CF6></circle><path d="M11.7 15.5 8.6 20.5l3.1-1.3z" fill=#fff></path><path d="M20.3 15.5 23.4 20.5l-3.1-1.3z" fill=#fff></path><path d="M13.6 19.5h4.8L16 25.5z" fill=#FFC23F></path></svg>FI<span>LG</span></button></h1><div class=topright><div class=stackdial id=stackdial hidden><button type=button class=stackbtn id=stackbtn aria-haspopup=true aria-expanded=false aria-label="Choose your model crew" onclick=toggleStackPop()><span class=stacklbl id=stacklbl></span><span class=stack-cost id=stackcost aria-hidden=true></span><span class=stackcaret aria-hidden=true>&#9662;</span></button><div class=stackpop id=stackpop role=menu aria-label="Choose a model crew" hidden></div></div><button type=button class=meter id=meter hidden title="Token usage this session (resets when you reload)"></button><div class=authbar id=authbar></div></div></div>
<div class=note-banner id=banner></div>
<div class=intake id=intake>
<h2>You've got a business in you. Let's find it. 🚀</h2>
<p class=sub>Drop in your idea. You'll get the offer + the research graded, then we build the whole plan together, your call at every step.</p>
<label for=idea class=sr-only>Your business idea</label>
<textarea id=idea placeholder="e.g. I know automation and feel like I could help scale small dental businesses… OR I like doggies, the color purple, and live in a bunker with my 12 brothers, either way, let's find the business."></textarea>
<div class=boardpick id=boardpick></div>
<div id=authgate></div>
<label for=email class=sr-only>Your email</label>
<input id=email type=email placeholder="you@email.com">
<div class=golane><button id=go class=go onclick=start()>Build my plan →</button><button type=button class=bail onclick=goofOff()>nah I'm gonna go fuck around some more</button></div>
<div class=err id=err></div>
<div id=joke></div>
<p class=brandfoot>“Fuck it. Let’s go.” <cite>— You, 30 seconds ago</cite></p>
</div>
<div id=profile style="display:none"></div>
<div id=live class=sr-only aria-live=polite></div>
<aside class=drawer id=drawer role=dialog aria-modal=true aria-labelledby=drawer-title aria-hidden=true>
<div class=dr-head><span id=drawer-title>Ask an expert</span><button type=button class=dr-x onclick=closeDrawer() aria-label="Close panel">×</button></div>
<div class=dr-body>
<p class=dr-sub id=drawer-sub></p>
<label for=drawerq class=sr-only>Your question for the advisor</label>
<textarea id=drawerq rows=3 placeholder="Ask a question, or leave blank for their straight take."></textarea>
<button type=button class=dr-go id=drawer-go onclick=submitDrawer()>Ask →</button>
<div class="dr-out md" id=drawer-out></div>
</div></aside>
<div class=drawer-back id=drawerback onclick=closeDrawer()></div>
<div class=modal-back id=modalback onclick="_closeModal()"></div>
<div class=modal id=modal role=dialog aria-modal=true aria-labelledby=modal-title aria-hidden=true>
<h3 id=modal-title></h3><div id=modal-body></div><div class=modal-actions id=modal-actions></div></div>
<div class=cmtpop id=cmtpop role=dialog aria-label="Add a comment on this part" aria-hidden=true>
<div class=cmtpq id=cmtquote></div>
<label for=cmtnote class=sr-only>Your comment on the highlighted text</label>
<textarea id=cmtnote rows=2 placeholder="Comment on this… (folded in when you regenerate or roll forward)"></textarea>
<div class=cmtpa><button type=button class=ghost onclick=hideCmtPop()>Cancel</button><button type=button onclick=saveComment()>Comment</button></div></div>
<div class=toasts id=toasts aria-live=polite></div>
<div class=workspace id=workspace style="display:none">
<button type=button class=drawer-rail onclick="document.body.classList.remove('drawer-collapsed')" aria-label="Open tools">&#9776; Tools</button>
<aside class=side>
<div class=drawerhead><b>Tools</b><button type=button class=drawerx onclick="document.body.classList.add('drawer-collapsed')" aria-label="Collapse tools">&#8249;</button></div>
<div class="sec collap" id=chatsec style="display:none"><button type=button class=sechead aria-expanded=false onclick="toggleSec('chatsec')"><h3>Chat with your plan</h3><span class=caret aria-hidden=true>▸</span></button>
<div class=secbody>
<div class=chatlog id=chatlog></div>
<div class=chatstart id=chatstart></div>
<label for=chatinput class=sr-only>Ask the planner about your plan</label>
<textarea id=chatinput rows=2 placeholder="Ask anything about your plan…"></textarea>
<button type=button class=chatsend id=chatsend onclick=sendChat()>Ask the planner →</button>
<div class=disc>AI advisor grounded in your plan + graded research, not professional advice.</div></div></div>
<div class="sec collap addons" id=expertsec><button type=button class=sechead aria-expanded=false onclick="toggleSec('expertsec')"><h3>Ask an expert</h3><span class=caret aria-hidden=true>▸</span></button>
<div class=secbody><div class=ax id=addons></div>
<div class=disc id=adisc></div></div></div>
<div class="sec collap board" id=boardsec style="display:none"><button type=button class=sechead aria-expanded=false onclick="toggleSec('boardsec')"><h3>Board of Directors</h3><span class=caret aria-hidden=true>▸</span></button>
<div class=secbody><p class=bhelp>Tap to add or drop a director, then convene them on your plan.</p>
<div class=bdirs id=boarddirs></div>
<button type=button class=convene id=convene onclick=convene()>Convene the board</button>
<div class=disc>AI composite directors, not real people, not professional advice.</div></div></div>
<div class="sec collap open" id=researchsec><button type=button class=sechead aria-expanded=true onclick="toggleSec('researchsec')"><h3>Research, graded</h3><span class=caret aria-hidden=true>▸</span></button>
<div class=secbody><div id=research></div></div></div>
</aside>
<main class=main>
<div id=answer class=summary></div>
<div class=runner id=runner aria-live=polite hidden>
<div class=run-head><span class=run-dot aria-hidden=true></span><span class=run-title id=run-title>The machine, working</span><span class=run-spacer></span><button type=button class=run-min id=run-min onclick="document.getElementById('runner').classList.toggle('min')" aria-label="Collapse or expand the runner">&#9662;</button></div>
<div class=run-log id=runner-log></div>
</div>
<div id=vet></div>
<div class=planwrap id=planwrap style="display:none"><div class=plantabs id=plantabs role=tablist aria-label="Your plan, part by part"></div></div>
<div id=planview style="display:none"></div>
<div id=node></div>
<div id=boardround></div>
<div class=err id=err2></div>
</main>
<div class=actionbar id=actionbar aria-label="Plan step actions"></div>
</div>
<script>
const CFG=window.FILG||{authEnabled:false};
let sb=null, session=null, me=null;
function authHeaders(){return session?{'Authorization':'Bearer '+session.access_token}:{};}
let SID=null;
// ── BYOK: mandatory from the first submit (no free welcome plan) ─────────────
let HAS_KEY=false, KEY_PROVIDER=null;   // which backend the saved key runs on ('openrouter'|'anthropic')
async function loadKey(){            // refresh whether this user has a saved key
  if(!CFG.byokEnabled){HAS_KEY=false;KEY_PROVIDER=null;return;}
  try{const r=await fetch('/api/key',{headers:authHeaders()});const d=await r.json();HAS_KEY=!!(d&&d.key);KEY_PROVIDER=(d&&d.key)?d.key.provider:null;if(typeof paintMeter==='function')paintMeter();}
  catch(e){HAS_KEY=false;KEY_PROVIDER=null;}
}
function requireKey(){               // gate any API-calling button: no key → open the key modal
  if(CFG.byokEnabled&&!HAS_KEY){keyModal();return false;}
  return true;
}
// The bail-out button: send the procrastinator to a random snarky Google search.
const GOOFS=["videos of cats","ways to waste time on the internet","how to sell pogs",
  "competitive frisbee clips","is a hotdog a sandwich","are birds real",
  "how many golf balls fit in a school bus","capybara compilation","how do magnets work",
  "longest yawn world record","cat playing piano","how to do a kickflip",
  "best paper airplane design","why do cats knock things off tables","goat screaming like a human"];
function goofOff(){
  const q=GOOFS[Math.floor(Math.random()*GOOFS.length)];
  location.href='https://www.google.com/search?q='+encodeURIComponent(q);
}
async function start(){
  const idea=document.getElementById('idea').value.trim(), email=document.getElementById('email').value.trim();
  const go=document.getElementById('go'), err=document.getElementById('err');
  err.textContent='';document.getElementById('joke').innerHTML='';
  if(CFG.authEnabled&&!session){authModal();return;}   // signed-out → prompt them with the sign-in modal
  if(!requireKey())return;                              // no key → open the key modal; we cover no runs now
  const body={idea, stack:STACK_CUR}; if(!session) body.email=email;   // signed in → identity from the token
  if(BOARD.length) body.directors=BOARD;               // optional Board of Directors → vets each step
  go.disabled=true; go.textContent='Researching…'; ACT_RESEARCH=false;
  try{
    const r=await fetch('/api/plan/start',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.gibberish){showJoke(d);go.disabled=false;go.textContent='Build my plan →';return;}  // nonsense → roast, no run
    if(!r.ok){err.textContent=d.error||'Something went wrong.';if(d.needKey)err.innerHTML+=' <a href=# onclick="keyModal();return false">Add your key →</a>';go.disabled=false;go.textContent='Build my plan →';return;}
    SID=d.id;meterBaseline(d.id);   // baseline at 0 so this run's tokens fully count as research streams in
    history.replaceState({plan:SID},'','/plan/'+SID);   // put the plan in the URL NOW so a mid-build refresh restores it
    show('workspace');   // reveal the workspace + apply the ws layout (left tools drawer, full-width main)
    poll();
  }catch(e){err.textContent='Network error.';go.disabled=false;go.textContent='Build my plan →';}
}
function showJoke(d){
  const box=document.getElementById('joke');
  box.innerHTML=`<div class=jokecard><h3>${esc(d.title||"That's... not an idea.")}</h3>`+
    `<div class="md jbody">${mdToHtml(d.body||'')}</div>`+
    `<button type=button onclick="dismissJoke()">Okay, for real this time →</button></div>`;
  box.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function dismissJoke(){const i=document.getElementById('idea');i.value='';document.getElementById('joke').innerHTML='';i.focus();}
function say(msg){const l=document.getElementById('live'); if(l)l.textContent=msg;}  // announce to screen readers
async function poll(){
  const r=await fetch('/api/plan/'+SID,{headers:authHeaders()});
  const s=await r.json();
  meterTick(s);     // set the session-meter baseline early (cost 0 mid-research) so the welcome run counts
  renderPlanTabs(s);renderAddons(s);     // show the plan outline immediately, even while researching
  if(s.status==='researching'){
    if(!ACT_RESEARCH){Activity.resetLeaves();ACT_ID=Activity.open('Researching + grading your market');Activity.push(ACT_ID,'Spinning up your research');ACT_RESEARCH=true;ACT_PROG_N=0;}
    _drainProgress(s);                                           // real receipts + the leaf fan-out, streamed
    document.getElementById('node').innerHTML='<div class=node><span class=eyebrow>Working</span><h3>Researching + grading your market…</h3><p class=lead>Pulling sources and grading every number, so vendor spin gets labeled, not laundered. About 1 to 2 minutes. Watch the leaves green up and the receipts spew in above.</p></div>';
    say('Researching and grading your market.');
    setTimeout(poll,1500);return;
  }
  if(ACT_RESEARCH){
    _drainProgress(s);                                           // flush any final lines + leaf events
    Activity.done(ACT_ID,s.status==='error'?'Hit a snag.':'Research graded. Building your plan.');ACT_RESEARCH=false;ACT_ID=null;
  }
  render(s);
}
let ACT_RESEARCH=false, ACT_PROG_N=0, ACT_ID=null;
// Stream new progress lines into the runner. Two sentinel lines drive the leaf viz instead of the text
// spew: "§LANES§<json array>" paints one grey leaf per lane; "§LANEDONE§<index>" greens that leaf.
function _drainProgress(s){
  const prog=s.progress||[];
  for(let i=ACT_PROG_N;i<prog.length;i++){
    const ln=prog[i]||'';
    if(ln.indexOf('\\u00A7LANES\\u00A7')===0){ try{Activity.leaves(ACT_ID,JSON.parse(ln.slice(7)));}catch(e){} }
    else if(ln.indexOf('\\u00A7LANEDONE\\u00A7')===0){ Activity.leafDone(parseInt(ln.slice(10),10)); }
    else if(ln.indexOf('\\uD83D\\uDD0E')===0){ /* "🔎 X is digging into: <lane>" — now shown as the nested leaf, skip */ }
    else Activity.push(ACT_ID,ln);
  }
  ACT_PROG_N=Math.max(ACT_PROG_N,prog.length);
  if(s.research&&s.research.owned_lanes)Activity.relabelLeaves(s.research.owned_lanes);  // Lane N → owner name
  if(s.research)Activity.leafDetails(s.research.owned_lanes,s.research.rows);             // fill each leaf's internals
}
// ── Model crew: pick-a-tile popover; cheap → premium. n = display name, k = engine stack key
// (keys are STABLE — the engine/tests/DB key on them; only the labels were renamed). ──────────
let STACK_CUR=(function(){try{return localStorage.getItem('filg_stack')||'the-work-horse';}catch(e){return 'the-work-horse';}})();
const STACKS_UI=[   // cheap → premium
  {k:'the-turd-polisher',n:'The intern',b:"Cheap and eager. Fast first drafts you'll want to double-check. Fine for spiking, feature-testing, or kicking the tires.",o:false},
  {k:'the-capable-intern',n:'The work horse',b:"Cheap research, solid synthesis. Gets the bulk of the job done well without much hand-holding.",o:false},
  {k:'the-work-horse',n:'The closer',b:"Cheap research bots, advanced synthesis and orchestration. Knows how to bring it home.",o:false},
  {k:'the-wonder-kid',n:'Wonder kid',b:"Advanced research with world-class orchestration and synthesis. Best results without the capital burn.",o:true,rec:true},
  {k:'trust-fund-baby',n:'Trust fund baby',b:"The absolute best models top to bottom. Not cheap, but hey, neither are you.",o:true},
];
function _stackIdx(key){const i=STACKS_UI.findIndex(x=>x.k===key);return i<0?2:i;}
function _stackCost(i){return [0,1,2,3,4].map(n=>'<i class='+(n<=i?'on':'')+'></i>').join('');}
function renderStack(s){   // s optional; updates the header button (+ open panel)
  const d=document.getElementById('stackdial'); if(!d)return; d.hidden=false;
  if(s&&s.stack)STACK_CUR=s.stack;
  const i=_stackIdx(STACK_CUR), u=STACKS_UI[i]||STACKS_UI[2];
  const lbl=document.getElementById('stacklbl');
  if(lbl)lbl.innerHTML=esc(u.n)+(u.rec?' <span class=sk-star aria-hidden=true>\\u2605</span>':'');
  const c=document.getElementById('stackcost'); if(c)c.innerHTML=_stackCost(i);
  const pop=document.getElementById('stackpop'); if(pop&&!pop.hidden)renderStackTiles();
}
function renderStackTiles(){
  const pop=document.getElementById('stackpop'); if(!pop)return;
  const cur=_stackIdx(STACK_CUR);
  pop.innerHTML='<div class=stackpop-h>Pick your crew. Sets the models behind research, the credibility gate, and the writing you read.</div>'+
    STACKS_UI.map((u,i)=>{
      const badges=(u.rec?'<span class="st-badge rec">Recommended</span>':'');
      return '<button type=button role=menuitemradio aria-checked='+(i===cur)+' class="stacktile'+(i===cur?' sel':'')+'" onclick="pickStack('+i+')">'+
        '<span class=st-top><span class=st-name>'+esc(u.n)+'</span><span class=st-badges>'+badges+'</span>'+
        '<span class=stack-cost aria-hidden=true>'+_stackCost(i)+'</span></span>'+
        '<span class=st-desc>'+esc(u.b)+'</span></button>';
    }).join('');
}
function toggleStackPop(){
  const pop=document.getElementById('stackpop'),btn=document.getElementById('stackbtn'); if(!pop)return;
  const opening=pop.hidden;
  if(opening)renderStackTiles();
  pop.hidden=!opening; btn.setAttribute('aria-expanded',opening?'true':'false');
}
function closeStackPop(){const pop=document.getElementById('stackpop'),btn=document.getElementById('stackbtn');
  if(pop&&!pop.hidden){pop.hidden=true;btn.setAttribute('aria-expanded','false');}}
function pickStack(i){const u=STACKS_UI[i];closeStackPop();if(u&&u.k!==STACK_CUR)commitStack(i);}
async function commitStack(i){
  const u=STACKS_UI[i]; if(!u)return;
  STACK_CUR=u.k; try{localStorage.setItem('filg_stack',u.k);}catch(e){}
  renderStack();                  // reflect the choice immediately — works before a plan exists too
  toast(u.n+' is on the job.','ok');
  if(!SID)return;                 // no plan yet → the choice is sent when the plan starts
  try{
    const r=await fetch('/api/plan/'+SID+'/stack',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({stack:u.k})});
    const s=await r.json(); if(r.ok)render(s);
  }catch(e){}
}
// ── Session usage meter ─────────────────────────────────────────────────────
// In-memory only: tokens + $ spent THIS browser session. Resets on reload, never tracked on the
// account. Always visible (starts at 0). Each plan's cumulative cost/tokens is observed per response
// (and streamed during research); only positive deltas tick the meter. A plan created this session is
// baselined at 0 (meterBaseline) so all its usage counts; a plan merely OPENED is baselined at its
// current total so we don't backfill. The shown numbers ease toward the real totals → reads like a live ticker.
let METER={tokens:0,cost:0}; const PLAN_BASE={};
let _mShown={tokens:0,cost:0}, _mRAF=null;
function meterBaseline(id){if(id!=null)PLAN_BASE[id]={c:0,t:0};}   // a fresh plan → count all of its usage
function meterTick(o){
  if(!o||o.id==null||o.cost==null||o.tokens==null)return;
  const c=+o.cost||0,t=+o.tokens||0,id=o.id;
  if(!(id in PLAN_BASE)){PLAN_BASE[id]={c,t};animateMeter();return;}   // first sight of an opened plan → baseline
  const dc=c-PLAN_BASE[id].c,dt=t-PLAN_BASE[id].t;
  if(dc>0||dt>0){METER.cost+=Math.max(0,dc);METER.tokens+=Math.max(0,dt);PLAN_BASE[id]={c,t};}
  animateMeter();
}
function fmtTokens(n){n=Math.round(n);return n>=1000?(n/1000).toFixed(n>=10000?0:1).replace(/\\.0$/,'')+'k':String(n);}
function animateMeter(){   // ease the displayed numbers toward the real totals so the meter reads live
  if(_mRAF)cancelAnimationFrame(_mRAF);
  const from={tokens:_mShown.tokens,cost:_mShown.cost}, t0=performance.now(), dur=650;
  const step=(now)=>{
    const k=Math.min(1,(now-t0)/dur), e=1-Math.pow(1-k,3);
    _mShown.tokens=from.tokens+(METER.tokens-from.tokens)*e;
    _mShown.cost=from.cost+(METER.cost-from.cost)*e;
    paintMeter();
    if(k<1){_mRAF=requestAnimationFrame(step);}else{_mShown={tokens:METER.tokens,cost:METER.cost};paintMeter();_mRAF=null;}
  };
  _mRAF=requestAnimationFrame(step);
}
function paintMeter(){
  const el=document.getElementById('meter');if(!el)return;
  const cost=_mShown.cost, tok=_mShown.tokens, d=cost<1?(cost<0.01?4:3):2;
  el.hidden=false;
  el.className=(typeof Activity!=='undefined'&&Activity.n>0)?'meter live':'meter';
  // Anthropic doesn't report per-call USD, so the $ is estimated from the price table (tokens are exact).
  const est=(KEY_PROVIDER==='anthropic')?'<span class=m-est> (estimated)</span>':'';
  el.innerHTML=`<span class=m-dot></span><b>${fmtTokens(tok)}</b> tokens · <b>$${cost.toFixed(d)}</b>${est}`;
}
let LAST_S=null;
function render(s){
  LAST_S=s;       // stash for the feedback modal (suggested questions, current step)
  meterTick(s);   // tick the session usage meter off this plan's cumulative cost/tokens
  CUR_NODE=(s.tree&&s.tree.active)||null;   // #7 key inline comments to the active node
  hideCmtPop();
  if(s.status==='error'){
    const keyErr=isKeyErr(s.error);   // a rejected/expired key → offer to fix the key, not just restart
    const fix=keyErr?'<button type=button class=b-but onclick=keyModal()>Update your key</button> ':'';
    document.getElementById('node').innerHTML='<div class=node><h3>Hit a snag</h3><p class=lead>'+esc(s.error||'Something went wrong.')+'</p>'+fix+'<button type=button class=ghost onclick=newPlan()>Start over</button></div>';
    say('Something went wrong: '+(s.error||'')); return;
  }
  renderResearch(s);renderVet(s);renderAnswer(s);renderNode(s);renderPlanTabs(s);renderAddons(s);renderBoard(s);renderBoardRound(s);renderChat(s);renderStack(s);syncSidebar(s);
  if(s.done&&SID&&location.pathname!=='/plan/'+SID)history.pushState({plan:SID},'','/plan/'+SID);   // finished plan gets a clean URL (revisit + bookmark)
  if(s.done)say('Your plan is complete, all '+s.total+' parts ready to download.');
  else if(s.vetting&&s.vetting.verdict)say('Research graded. Verdict: '+s.vetting.verdict+'. Ready to build part '+((s.step||0)+1)+'.');
}
// Collapsible sidebar sections. On the first page (the offer + graded research) Research is open and
// the advisor sections are collapsed; once we start building (step ≥ 1) Research collapses and the
// advisors expand, since they're now the relevant tools. This auto-switch fires only on the phase
// change, so any manual collapse/expand the user makes afterward sticks.
let SIDEBAR_PHASE=null;
function setOpen(id,open){const el=document.getElementById(id);if(!el)return;el.classList.toggle('open',open);const h=el.querySelector('.sechead');if(h)h.setAttribute('aria-expanded',String(open));}
function toggleSec(id){const el=document.getElementById(id);if(!el)return;const open=el.classList.toggle('open');const h=el.querySelector('.sechead');if(h)h.setAttribute('aria-expanded',String(open));}
function syncSidebar(s){
  const phase = s.done ? 'plan' : (s.step>=1 ? 'build' : 'intro');
  if(phase===SIDEBAR_PHASE)return;   // only auto-apply on a phase change, respect manual toggles after
  SIDEBAR_PHASE=phase;
  setOpen('chatsec',phase==='plan');         // chat auto-opens on the finished plan page
  setOpen('researchsec',phase==='intro');
  setOpen('expertsec',phase==='build');
  setOpen('boardsec',phase==='build');
}
let CHAT_BUSY=false;
function renderChat(s){
  const sec=document.getElementById('chatsec'); if(!sec)return;
  sec.style.display = (s.status==='building'||s.done) ? '' : 'none';   // available once a plan exists
  const log=document.getElementById('chatlog'); if(!log)return;
  const msgs=s.chat||[];
  log.innerHTML=msgs.map(m=>`<div class="cmsg ${m.role==='user'?'user':'bot md'}">${m.role==='user'?esc(m.content):mdToHtml(m.content)}</div>`).join('');
  log.scrollTop=log.scrollHeight;
  const st=document.getElementById('chatstart');
  if(st)st.innerHTML = msgs.length ? '' : (s.chatStarters||[]).map(q=>`<button type=button onclick="chatStart(this)">${esc(q)}</button>`).join('');
}
function chatStart(btn){const t=document.getElementById('chatinput');if(t){t.value=btn.textContent;t.focus();}}
async function sendChat(){
  if(CHAT_BUSY)return;
  const t=document.getElementById('chatinput'),msg=(t.value||'').trim(); if(!msg)return;
  if(!requireKey())return;
  const log=document.getElementById('chatlog'),btn=document.getElementById('chatsend'),st=document.getElementById('chatstart');
  CHAT_BUSY=true;btn.disabled=true;t.value='';if(st)st.innerHTML='';
  log.insertAdjacentHTML('beforeend',`<div class="cmsg user">${esc(msg)}</div><div class="cmsg bot md" id=chatthinking><span class=think>Thinking…</span></div>`);
  log.scrollTop=log.scrollHeight;
  const aid=Activity.start(["Reading your plan","Checking the graded evidence","Thinking it through"],1200,'Chat with your plan');
  try{
    const [r]=await Promise.all([fetch('/api/plan/'+SID+'/chat',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({message:msg})}),new Promise(res=>setTimeout(res,850))]);
    const d=await r.json();const th=document.getElementById('chatthinking');
    if(!r.ok){Activity.stop(aid);if(th){th.removeAttribute('id');th.innerHTML='<span class=think>'+esc(d.error||'Could not reach the advisor.')+'</span>';}}
    else{Activity.done(aid,'Answered.');meterTick({id:SID,cost:d.cost,tokens:d.tokens});if(th){th.removeAttribute('id');th.innerHTML=mdToHtml(d.reply);}}
  }catch(e){Activity.stop(aid);const th=document.getElementById('chatthinking');if(th)th.innerHTML='<span class=think>Network error.</span>';}
  finally{CHAT_BUSY=false;btn.disabled=false;log.scrollTop=log.scrollHeight;}
}
// The standing adversary's card — a committed verdict + verbatim objection, surfaced as a headline.
// This is the "the grumpy industry vet said Y" moment: the board always has a skeptic in the room.
function skepticCardHtml(sk){
  if(!sk||!sk.rationale)return '';
  const v=['agree','concern','dissent','non-starter'].includes(sk.verdict)?sk.verdict:'concern';
  const who=esc(sk.first||sk.name||'The Skeptic')+((sk.first&&sk.name)?' ('+esc(sk.name)+')':'');
  const fix=sk.suggested_change?`<div class=skfix><b>Strongest fix:</b> ${esc(sk.suggested_change)}</div>`:'';
  const conf=sk.confidence?`<div class=skmeta>${esc(sk.confidence)} confidence</div>`:'';
  return `<div class="skeptic sk-${v}"><div class=skhead><span class=sknm>🧐 ${who} pushed back</span>`+
    `<span class="skverdict sk-${v}">${esc(v)}</span></div>`+
    `<div class="skbody md">${mdToHtml(sk.rationale)}</div>${fix}${conf}</div>`;
}
function renderBoardRound(s){
  const el=document.getElementById('boardround'); if(!el)return;
  const reviews=s.board||[];
  if(!reviews.length){el.innerHTML='';return;}
  const r=reviews[reviews.length-1];   // the board's take on the section just finalized
  const balloons=(r.directors||[]).map((d,i)=>{
    const id='bal_'+i;
    return `<div class=balloon id=${id}><button type=button class=bh onclick="document.getElementById('${id}').classList.toggle('open')">💬 See what ${esc(d.first||d.name)}${d.first&&d.name?' ('+esc(d.name)+')':''} says<span class=caret>▸</span></button><div class="bb md">${mdToHtml(d.take)}</div></div>`;
  }).join('');
  const split=(r.conflicts&&r.conflicts.toLowerCase()!=='none')?`<span class=split>Where they split: ${esc(r.conflicts)}</span>`:'';
  el.innerHTML=`<div class=bround><h4>🗣️ Your board weighed in on “${esc(r.title)}”</h4>`+
    skepticCardHtml(r.skeptic)+
    `<div class=balloons>${balloons}</div>`+
    `<div class=takeaway><div class=tl>Board takeaway</div>${esc(r.verdict||'')}${split}</div></div>`;
}
function renderResearch(s){
  const R=s.research||{};
  const rows=R.rows||[], owned=R.owned_lanes||[];
  const el=document.getElementById('research');
  if(!rows.length&&!owned.length){el.innerHTML='<p style="color:var(--muted);font-size:13px;margin:0">Grading sources…</p>';return;}
  const ownerOf={}; owned.forEach(o=>{ownerOf[o.lane]=o;});
  // who researched what — the fan-out, surfaced as persona-owned lanes
  const lanesHtml=owned.length?('<div class="lanes techonly"><div class=lanesh>Who looked into what</div>'+
    owned.map(o=>`<div class=lanerow><span class=laneown>${esc(o.owner_first||o.owner_name||'Research')}</span> dug into <span class=lanesub>${esc(o.lane)}</span></div>`).join('')+'</div>'):'';
  const JLAB={TRUST:'trusted',CROSS_CHECK:'cross-check',FLAG_SELF_INTERESTED:'flagged: sells the result'};
  const YR=new Date().getFullYear();
  const rowsHtml=rows.length?('<ul class=ev>'+rows.map(x=>{
    const o=ownerOf[x.lane];
    const by=o?` <span class=techonly>· found by ${esc(o.owner_first||o.owner_name)}</span>`:'';
    const jl=JLAB[x.judge]||'';
    // staleness — labeled, never chased (shown in both modes; it's decision-relevant)
    const stale=x.as_of?` · as of ${x.as_of}${(YR-x.as_of>=3)?' (stale)':''}`:'';
    // single-source vs corroborated — structural, no extra research (tech mode)
    const tri=(x.mark==='ok')?(x.corroborated?` · ${x.sources||2} sources`:' · single source'):'';
    const gate=(x.tier||jl||tri)?`<br><span class="gate techonly">⚙ gate: ${esc((x.tier||'').toLowerCase())}${jl?' · '+esc(jl):''}${esc(tri)}</span>`:'';
    return `<li>${x.mark==='ok'?'✅':'⚠️'} ${esc(x.text)} <span class="badge ${x.mark==='ok'?'b-ok':'b-warn'}">${x.mark==='ok'?'cited':'vendor'}</span><br><span class=note>${esc(host(x.url))}, ${esc(x.note)}${esc(stale)}${by}</span>${gate}</li>`;
  }).join('')+'</ul>'):'';
  el.innerHTML=lanesHtml+rowsHtml;
}
function renderAnswer(s){
  const p=s.research&&s.research.prose; if(!p)return;
  document.getElementById('answer').innerHTML=`<h2>${esc(p.title)}</h2><p class=tag>Your offer, with the research graded, vendor spin labeled, not laundered.</p><p><b>What you'd sell:</b> ${esc(p.offer)}</p><p><b>How you'd sell it:</b> ${esc(p.gtm)}</p>`;
}
let BUILT={}, SECMETA={}, PLAN_TAB=-99;
// "Your plan" + the decision tree, merged into a center-column tab strip. Each part of the plan is a
// tab: a built part opens read-only with "jump back and build from here" (the old decision-tree
// branch action); the part you're on shows the live draft + the bottom action bar; parts you haven't
// reached are disabled. Switching tabs is client-only (no refetch) — server state snaps you back to
// the part you're on.
function renderPlanTabs(s){
  const wrap=document.getElementById('planwrap'), strip=document.getElementById('plantabs');
  if(!wrap||!strip)return;
  const secs=s.sections||[];
  if(!secs.length){wrap.style.display='none';return;}
  wrap.style.display='';
  BUILT={}; (s.files||[]).forEach(f=>BUILT[f.path]=f.content);
  SECMETA={}; secs.forEach(sec=>SECMETA[sec.file]={title:sec.title,sub:sec.sub});
  // active path through the decision tree: first node seen per step (walking active → root) gives the
  // node to jump back to for each built part.
  const t=s.tree||{}, byId={}; (t.nodes||[]).forEach(n=>byId[n.id]=n);
  const stepNode={}; let cur=t.active;
  while(cur!=null&&byId[cur]){const n=byId[cur]; if(stepNode[n.step]==null)stepNode[n.step]=n.id; cur=n.parent;}
  const step=s.done?secs.length:(s.step==null?-1:s.step);
  let lastBuilt=-1; secs.forEach((sec,i)=>{if(BUILT[sec.file]!=null)lastBuilt=i;});
  strip.innerHTML=secs.map((sec,i)=>{
    const built=BUILT[sec.file]!=null, isActive=(!s.done&&i===step);
    const state=built?'built':(isActive?'current':'pending');
    const ic=built?'✓':(isActive?'✍︎':'○');
    const just=(!s.done&&built&&i===lastBuilt)?' justdone':'';   // most-recent finish pulses
    const nodeId=stepNode[i]!=null?stepNode[i]:'';
    const dis=(state==='pending')?' disabled':'';
    return `<button type=button role=tab aria-selected=false class="ptab ${state}${just}" data-i="${i}" data-node="${nodeId}"${dis} onclick="selectPlanTab(${i})" title="${esc(sec.sub||'')}"><span class=pic aria-hidden=true>${ic}</span><span class=plab>${esc(sec.title)}</span></button>`;
  }).join('');
  PLAN_TAB = s.done ? -1 : step;   // server state changed → snap to the part you're on (home when done)
  applyPlanTab(s);
}
function selectPlanTab(i){ PLAN_TAB=i; applyPlanTab(LAST_S||{}); }
function applyPlanTab(s){
  const node=document.getElementById('node'), view=document.getElementById('planview');
  const secs=s.sections||[];
  const step=s.done?-1:(s.step==null?-1:s.step);
  document.querySelectorAll('#plantabs .ptab').forEach(b=>{const on=Number(b.dataset.i)===PLAN_TAB;b.classList.toggle('sel',on);b.setAttribute('aria-selected',String(on));});
  const viewingEarlier = PLAN_TAB>=0 && PLAN_TAB!==step;   // looking at an already-built part, not the live one
  if(viewingEarlier && view){
    const sec=secs[PLAN_TAB]||{}, content=BUILT[sec.file]||'';
    const tab=document.querySelector('#plantabs .ptab[data-i="'+PLAN_TAB+'"]');
    const nodeId=tab?tab.dataset.node:'';
    const build=(nodeId&&!s.done)?`<button type=button class=pbuild onclick="gotoNode('${nodeId}')">↩ Jump back and build from here</button>`:'';
    const back=`<button type=button class=ghost onclick=backToCurrent()>${s.done?'Back to overview':"Back to the part you're on"} →</button>`;
    view.innerHTML=`<div class=node><span class=eyebrow>From your plan</span><h3>${esc(sec.title||'Part')}</h3><p class=h3sub>${esc(sec.sub||'')}</p><div class="draft md">${mdToHtml(content)}</div><div class=planacts>${build}${back}</div></div>`;
    view.style.display='block'; if(node)node.style.display='none';
    const bar=document.getElementById('actionbar'); if(bar)bar.classList.remove('show'); document.body.classList.remove('hasbar');
    view.scrollIntoView({behavior:'smooth',block:'nearest'});
  } else {
    if(view){view.style.display='none';view.innerHTML='';}
    if(node)node.style.display='';
    renderActionBar(s);   // back on the live part → restore the bottom action bar
  }
}
function backToCurrent(){ const s=LAST_S||{}; PLAN_TAB=s.done?-1:(s.step==null?-1:s.step); applyPlanTab(s); }
function closeViewer(){ const v=document.getElementById('planview'); if(v){v.style.display='none';v.innerHTML='';} PLAN_TAB=-99; }
function renderNode(s){
  const n=document.getElementById('node');
  if(s.status==='researching')return;
  if(s.done){const cpn=pdfUnlocked()?'':'<div class=couponrow><input id=coupon placeholder="Coupon code" autocomplete=off spellcheck=false><button type=button class=ghost onclick=redeemCoupon()>Apply</button></div>';
    n.innerHTML='<div class=node><div class=done>🎉 <b>Your plan is ready</b>, all '+s.total+' parts. This is your plan\\'s home: grab the <b>polished PDF</b> (or the free raw files), <b>chat with your plan</b> in the sidebar to pressure-test it, or share it.</div>'+
    '<div class=planacts>'+pdfBtn()+'<button type=button class=ghost onclick=downloadZip()>⬇ Raw files (.zip), free</button><button type=button class=ghost onclick="sharePlan(SID)">🔗 Share</button></div>'+cpn+'</div>';return;}
  const p=s.proposal; if(!p){n.innerHTML='';return;}
  const sec=(s.sections||[]).find(x=>x.title===p.title)||{};
  const intro=s.step===0?`<p class=lead>We build your plan in ${s.total} parts, one at a time, your call on each (watch them fill in on the left). First up:</p>`:'';
  const changeFlag=p.change?`<div class=changeflag><span class=cf-l>↳ Your note shaped this</span>${esc(p.change)}</div>`:'';
  const killed=(s.vetting||{}).verdict==='kill';   // hard gate: an unbuildable idea can't roll forward
  // The feedback input now lives in a modal opened by the bottom action bar (see renderActionBar).
  const tail=killed?killGateHtml(s):`<div class=ferr id=ferr></div>`;
  const cmtbox=killed?'':`<div class=cmts id=cmtlist></div>`;   // #7 inline comments live under a buildable draft
  n.innerHTML=`<div class=node><span class=eyebrow>Your plan · part ${s.step+1} of ${s.total}</span><h3>${esc(p.title)}</h3><p class=h3sub>${esc(sec.sub||'')}</p>`+
    changeFlag+intro+
    `<div class="draft md">${mdToHtml(p.draft)}</div>`+cmtbox+tail;
  renderComments();
}
// The two pinned-to-the-bottom actions. They open the feedback modal (regen vs roll-forward); the
// modal carries the optional notes, the suggested questions, and the per-step nudge chips.
function renderActionBar(s){
  const bar=document.getElementById('actionbar'); if(!bar)return;
  const killed=(s.vetting||{}).verdict==='kill';
  const show=!!(s&&s.proposal&&!s.done&&!killed&&(s.status==='building'||s.status==null));
  bar.classList.toggle('show',show);
  document.body.classList.toggle('hasbar',show);
  if(show)bar.innerHTML=
    `<button type=button class="ab-btn ab-back" onclick="openFeedbackModal('regen')" title="Rework this part with a note">\\u21bb Not feeling it</button>`+
    `<button type=button class="ab-btn ab-next" onclick="openFeedbackModal('next')">I'm with you \\u2192</button>`;
}
// The kill gate is now a COACHING LADDER, not a hard wall. First hit = genuine advisement (Coach voice
// + the off-ramps: add substance / re-check, or talk it through). Forcing past it with no substance rolls
// into comedic "waste of time" mode, and the top line escalates in snark with each push (Roast voice).
const WOD_SNARK=[
 "There isn't an idea here to build on yet. Give the gate one real skill or asset, and who'd pay, and this becomes a real plan.",
 "Giving some feedback might make this a viable idea. Still want to just keep going?",
 "Another wise investment of tokens. Still nothing to sell here.",
 "You're a generational genius. You clearly don't need our help.",
 "We can do this all day. The button works great. The business does not.",
];
let WOD_PUSHES=0;
function killGateHtml(s){
  const v=s.vetting||{};
  const q=esc((s.shaped||{}).clarifying_question||'Name one real skill, asset, or audience you already have, and who would pay for it.');
  const risk=(WOD_PUSHES===0&&v.biggest_risk)?`<p class=kg-risk><b>The gap:</b> ${esc(v.biggest_risk)}</p>`:'';
  const say=WOD_PUSHES>0?WOD_SNARK[Math.min(WOD_PUSHES,WOD_SNARK.length-1)]:(v.reaction||WOD_SNARK[0]);
  const head=WOD_PUSHES>0?'\\u26d4 Still nothing to sell':'\\u26d4 Not buildable yet';
  return `<div class=killgate><div class=kg-head>${head}</div>`+
    `<p class=kg-say>${esc(say)}</p>`+
    risk+`<p class=kg-q>${q}</p>`+
    `<label for=substance class=sr-only>Add a real skill, asset, or buyer</label>`+
    `<textarea id=substance rows=3 placeholder="e.g. 'I've run paid ads for SaaS for 4 years and I know founders who need it.' Name a real skill, plus who would pay."></textarea>`+
    `<div class=navrow><button type=button class=b-back onclick=startOver()>Start over</button>`+
    `<button type=button class=ghost onclick=talkItOut()>Talk it through</button>`+
    `<button type=button class=ghost onclick=forceNext()>Build it anyway →</button>`+
    `<button type=button class=b-next onclick=reCheck()>Re-check my idea →</button></div>`+
    `<div class=ferr id=ferr></div></div>`;
}
async function forceNext(){   // operator pushes past the gate with no substance → comedic waste-of-time mode
  if(!requireKey())return;
  if(WOD_PUSHES===0){  // first forced push → one encouraging chance to reconsider (Coach voice)
    const ok=await uiConfirm('Want to give it a real shot?',"Giving some feedback might make this a viable idea. Sure you want to just keep going?",'Keep going anyway');
    if(!ok){const t=document.getElementById('substance');if(t)t.focus();return;}
  }
  _navBusy();
  const aid=Activity.start(["Building this part","Against our better judgment"],1200,'Building anyway');
  try{
    const r=await _aiRun('/api/plan/'+SID+'/next',{feedback:'',force:true});
    const s=await r.json();
    if(!r.ok){Activity.stop(aid);fbErr(s.error||'Something went wrong.');_navFree();return;}
    WOD_PUSHES++;
    Activity.done(aid,'Done, for what it is.');render(s);
  }catch(e){Activity.stop(aid);fbErr('Network error.');_navFree();}
}
function talkItOut(){   // #8 off-ramp: hash the idea out in the side-chat instead of walking the steps
  const sec=document.getElementById('chatsec');
  if(sec){sec.style.display='';setOpen('chatsec',true);sec.scrollIntoView({behavior:'smooth',block:'start'});}
  const t=document.getElementById('chatinput');
  if(t){if(!t.value)t.value="My idea got flagged as not buildable yet. Help me find a real skill, asset, or buyer I could build this around.";t.focus();}
}
// ── #7 Inline comments: select text (or click a line) in the current draft → a popover note. Comments
// are held per node id and folded into the next regenerate / roll-forward, anchored to the quoted span.
// ── #1 suggested feedback: surface the engine's open questions above the per-part feedback box ──
function suggestedFb(s){
  const qs=[]; const cq=((s.shaped||{}).clarifying_question||'').trim(); if(cq)qs.push(cq);
  return qs;
}
function useFb(t){const f=document.getElementById('feedback'); if(!f)return; f.value=(f.value?f.value.replace(/\\s*$/,'')+' ':'')+t; f.focus();}

// ── #2 inline comments: highlight the targeted block, leave a 💬/✕ marker you can edit or remove ──
let COMMENTS={};     // {nodeId:[{quote,note,blk}]}  (blk = index among the draft's block elements)
let CUR_NODE=null;   // active node id (keys the comments)
let CMT_QUOTE='', CMT_BLK=-1, CMT_EDIT=-1;
function nodeComments(){return (CUR_NODE&&COMMENTS[CUR_NODE])||[];}
function commentsSteer(){   // fold inline comments into the feedback string the model receives
  const cs=nodeComments(); if(!cs.length)return '';
  return "\\n\\nInline comments on the current draft (address each, anchored to the quoted text):\\n"+
    cs.map(c=>`- On \\u201c${c.quote}\\u201d: ${c.note}`).join("\\n");
}
function draftBlocks(){const d=document.querySelector('#node .draft');return d?Array.prototype.slice.call(d.querySelectorAll('p,li,h3,h4,h5,h6,td')):[];}
function clearCmtTarget(){document.querySelectorAll('.draft .cmt-target').forEach(el=>el.classList.remove('cmt-target'));}
function decorateComments(){   // re-apply highlights + markers for the active node's comments
  const blocks=draftBlocks();
  blocks.forEach(b=>{b.classList.remove('hascmt');const m=b.querySelector('.cmtmark');if(m)m.remove();});
  nodeComments().forEach((c,i)=>{
    const b=blocks[c.blk]; if(!b)return;
    b.classList.add('hascmt');
    const mk=document.createElement('span'); mk.className='cmtmark'; mk.contentEditable='false';
    mk.innerHTML=`<button type=button class=cmtmark-e title="Edit note: ${esc(c.note)}" onclick="editComment(${i})">💬</button><button type=button class=cmtmark-x aria-label="Remove note" title="Remove note" onclick="removeComment(${i})">\\u00d7</button>`;
    b.appendChild(mk);
  });
}
function renderComments(){const box=document.getElementById('cmtlist'); if(box)box.innerHTML=''; decorateComments();}
function removeComment(i){const cs=nodeComments();cs.splice(i,1);decorateComments();}
function editComment(i){const c=nodeComments()[i]; if(!c)return; CMT_EDIT=i; CMT_QUOTE=c.quote||''; CMT_BLK=c.blk;
  openCmtPop(draftBlocks()[c.blk], c.note);}
function openCmtPop(anchorEl, prefill){
  const pop=document.getElementById('cmtpop'); if(!pop)return;
  clearCmtTarget(); if(anchorEl)anchorEl.classList.add('cmt-target');
  document.getElementById('cmtquote').textContent='\\u201c'+(CMT_QUOTE.length>90?CMT_QUOTE.slice(0,90)+'\\u2026':CMT_QUOTE)+'\\u201d';
  document.getElementById('cmtnote').value=prefill||'';
  const r=anchorEl?anchorEl.getBoundingClientRect():{left:40,bottom:80};
  pop.style.left=Math.max(8,Math.min(window.scrollX+r.left,window.scrollX+window.innerWidth-300))+'px';
  pop.style.top=(window.scrollY+r.bottom+8)+'px';
  pop.classList.add('show'); pop.setAttribute('aria-hidden','false');
  setTimeout(()=>{const n=document.getElementById('cmtnote');if(n)n.focus();},30);
}
function onDraftSelect(e){
  if(e.target.closest('#cmtpop')||e.target.closest('.cmtmark'))return;   // marker buttons handle themselves
  if(!e.target.closest('.draft'))return;
  const blk=e.target.closest('p,li,h3,h4,h5,h6,td'); if(!blk)return;
  const sel=window.getSelection(); let quote=(sel&&sel.toString()||'').trim();
  if(!quote)quote=(blk.textContent||'').trim();
  if(!quote)return;
  CMT_EDIT=-1; CMT_QUOTE=quote.slice(0,180); CMT_BLK=draftBlocks().indexOf(blk);
  openCmtPop(blk,'');
}
function hideCmtPop(){const p=document.getElementById('cmtpop');if(p){p.classList.remove('show');p.setAttribute('aria-hidden','true');}clearCmtTarget();}
function saveComment(){
  const note=((document.getElementById('cmtnote')||{}).value||'').trim();
  if(!note||CMT_BLK<0||!CUR_NODE){hideCmtPop();return;}
  const cs=(COMMENTS[CUR_NODE]=COMMENTS[CUR_NODE]||[]);
  if(CMT_EDIT>=0&&cs[CMT_EDIT])cs[CMT_EDIT].note=note; else cs.push({quote:CMT_QUOTE,note,blk:CMT_BLK});
  CMT_EDIT=-1; CMT_QUOTE=''; CMT_BLK=-1; hideCmtPop(); decorateComments();
  const s=window.getSelection&&window.getSelection(); if(s&&s.removeAllRanges)s.removeAllRanges();
}
document.addEventListener('mouseup',onDraftSelect);
document.addEventListener('keydown',function(e){if(e.key==='Escape')hideCmtPop();});
const FB_CHIPS=["go bolder","narrower niche","cheaper entry","B2B only","more specific","add an upsell"];
function addChip(txt){const t=document.getElementById('feedback'); if(!t)return; t.value=(t.value?t.value.replace(/\\s*$/,'')+', ':'')+txt; t.focus();}
function _navBusy(){const n=document.getElementById('node');if(n)n.querySelectorAll('button').forEach(b=>b.disabled=true);const ab=document.getElementById('actionbar');if(ab)ab.querySelectorAll('button').forEach(b=>b.disabled=true);const f=document.getElementById('ferr');if(f)f.textContent='';document.getElementById('err2').textContent='';}
function _navFree(){const n=document.getElementById('node');if(n)n.querySelectorAll('button').forEach(b=>b.disabled=false);const ab=document.getElementById('actionbar');if(ab)ab.querySelectorAll('button').forEach(b=>b.disabled=false);}
function fbErr(msg){const f=document.getElementById('ferr');if(f)f.textContent=msg;else document.getElementById('err2').textContent=msg;}
// Min-dwell so the spew registers even on fast (mock) responses, without slowing real builds much.
function _aiRun(url,body){   // fetch + a min-show delay; the caller owns its Activity track
  return Promise.all([
    fetch(url,{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(body)}),
    new Promise(res=>setTimeout(res,850))
  ]).then(([r])=>r);
}
// The feedback value comes from the modal; commitFeedback stashes it so the action survives the
// modal closing (and any confirm modal that reuses #modal). Falls back to a live #feedback if present.
let PENDING_FB=null;
function _fbRead(){ if(PENDING_FB!=null){const v=PENDING_FB;PENDING_FB=null;return v;}
  return ((document.getElementById('feedback')||{}).value||'').trim(); }
async function nextStep(){
  if(!requireKey())return;
  const fb=_fbRead();
  const full=(fb+commentsSteer()).trim();   // #7 fold inline comments into the roll-forward
  _navBusy();
  const steps=[]; if(full)steps.push("Folding in your notes");
  steps.push("Drafting the next part of your plan","Checking it against your graded research");
  const aid=Activity.start(steps,1200,'Building the next part');
  try{
    const r=await _aiRun('/api/plan/'+SID+'/next',{feedback:full});
    const s=await r.json();
    if(!r.ok){Activity.stop(aid);fbErr(s.error||'Something went wrong.');_navFree();return;}
    REDRAFTS=0;   // advanced past this part — reset the rework counter
    Activity.done(aid,'Next part ready.');render(s);
  }catch(e){Activity.stop(aid);fbErr('Network error.');_navFree();}
}
async function reCheck(){       // kill-gate rescue: re-vet with the substance the operator just added
  if(!requireKey())return;
  const more=((document.getElementById('substance')||{}).value||'').trim();
  if(more.length<8){fbErr('Add a real skill or asset, and who would pay for it.');const t=document.getElementById('substance');if(t)t.focus();return;}
  _navBusy();
  const aid=Activity.start(["Re-reading your idea with the new detail","Re-grading it against the research","Re-running the kill gate"],1200,'Re-checking your idea');
  try{
    const r=await _aiRun('/api/plan/'+SID+'/revet',{more});
    const s=await r.json();
    if(!r.ok){Activity.stop(aid);fbErr(s.error||'Something went wrong.');_navFree();return;}
    const cleared=s.vetting&&s.vetting.verdict!=='kill';
    WOD_PUSHES=0;   // they engaged with real substance — reset the snark escalation
    Activity.done(aid,cleared?'Cleared. You can build now.':'Still not enough to build on.');render(s);
  }catch(e){Activity.stop(aid);fbErr('Network error.');_navFree();}
}
function startOver(){try{localStorage.removeItem('filg_idea');}catch(e){}location.href='/';}   // clean intake
async function backStep(){
  if(!requireKey())return;
  const fb=_fbRead();
  if(!fb){fbErr('Add a quick note on what to change, a note is required to go back a step.');return;}
  _navBusy();
  const aid=Activity.start(["Re-opening the previous part","Re-drafting it from your note"],1200,'Going back a step');
  try{
    const r=await _aiRun('/api/plan/'+SID+'/back',{feedback:fb});
    const s=await r.json();
    if(!r.ok){Activity.stop(aid);fbErr(s.error||'Something went wrong.');_navFree();return;}
    Activity.done(aid,'New branch ready.');render(s);
  }catch(e){Activity.stop(aid);fbErr('Network error.');_navFree();}
}
let REDRAFTS=0;   // consecutive regenerations of the CURRENT part → escalate to a snark nudge toward the tree
async function regenStep(){
  if(!requireKey())return;
  const fb=_fbRead();
  const steer=(fb+commentsSteer()).trim();   // #7 a note OR inline comments can steer the rework
  if(!steer){fbErr("Tell me what's not landing — add a note or a comment to steer the rework.");return;}
  if(REDRAFTS>=2){   // they keep mashing it — nudge toward backing up via the decision tree
    const ok=await uiConfirm('Still not feeling it?',"We can regenerate this part all day. If a rework keeps missing, try backing up to an earlier part from the decision tree on the left. Regenerate again?",'Regenerate anyway');
    if(!ok)return;
  }
  _navBusy();
  const aid=Activity.start(["Re-reading your notes","Regenerating this part from a different angle"],1200,'Regenerating this part');
  try{
    const r=await _aiRun('/api/plan/'+SID+'/redraft',{feedback:steer});
    const s=await r.json();
    if(!r.ok){Activity.stop(aid);fbErr(s.error||'Something went wrong.');_navFree();return;}
    REDRAFTS++;
    Activity.done(aid,'Reworked this part.');render(s);
  }catch(e){Activity.stop(aid);fbErr('Network error.');_navFree();}
}
// ── Feedback modal: opened by the bottom action bar. Holds the suggested questions, the per-step
// nudge chips, and the note box; "Go" commits to roll-forward (next) or rework (regen). ──
let FB_MODE='next', NUDGE_CACHE={};
function openFeedbackModal(mode){
  if(!requireKey())return;
  FB_MODE=mode; const s=LAST_S||{};
  const qs=suggestedFb(s);
  const sfb=qs.length?(`<div class=mfb-sg><div class=mfb-h>The engine's open questions</div>`+
    qs.map(q=>`<button type=button class=mfb-q data-q="${esc(q)}" onclick="useFb(this.dataset.q)">${esc(q)}</button>`).join('')+`</div>`):'';
  const regen=mode==='regen';
  document.getElementById('modal-title').textContent=regen?"What's not landing?":'Roll forward, any notes?';
  document.getElementById('modal-body').innerHTML=
    `<p class=mfb-hint>${regen?"Tell me what to change and I'll rework this part.":'Add an optional note to steer the next part, or just go.'}</p>`+sfb+
    `<div class=mfb-chips id=fbchips><span class=mfb-load>thinking up quick edits\\u2026</span></div>`+
    `<label for=feedback class=sr-only>Your feedback</label>`+
    `<textarea id=feedback rows=3 placeholder="${regen?'e.g. simpler pricing, drop the second tier':'Optional note\\u2026'}"></textarea>`+
    `<div class=ferr id=ferr></div>`;
  document.getElementById('modal-actions').innerHTML=
    `<button type=button class=ghost onclick="_closeModal()">Cancel</button>`+
    `<button type=button class=mfb-go onclick="commitFeedback()">${regen?'Rework it':'Go'} \\u2192</button>`;
  _openModal('#feedback'); loadNudges();
}
function loadNudges(){
  const node=CUR_NODE||'_';
  const paint=(chips)=>{const el=document.getElementById('fbchips');if(!el)return;
    el.innerHTML=(chips&&chips.length)?chips.map(c=>`<button type=button class=chip data-c="${esc(c)}" onclick="addChip(this.dataset.c)">${esc(c)}</button>`).join(''):'';};
  if(NUDGE_CACHE[node]){paint(NUDGE_CACHE[node]);return;}
  fetch('/api/plan/'+SID+'/nudges',{headers:authHeaders()}).then(r=>r.json()).then(d=>{
    const chips=(d&&d.chips)||[]; NUDGE_CACHE[node]=chips; paint(chips);
    if(d&&d.cost!=null)meterTick({id:SID,cost:d.cost,tokens:d.tokens});
  }).catch(()=>paint([]));
}
function commitFeedback(){
  const fb=((document.getElementById('feedback')||{}).value||'').trim();
  const steer=(fb+commentsSteer()).trim();
  if(FB_MODE==='regen'&&!steer){const f=document.getElementById('ferr');if(f)f.textContent='Add a note (or a comment) so I know what to change.';return;}
  PENDING_FB=fb; _closeModal();
  if(FB_MODE==='regen')regenStep(); else nextStep();
}
async function gotoNode(id){
  document.getElementById('err2').textContent='';
  try{
    const r=await fetch('/api/plan/'+SID+'/goto',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({node:id})});
    const s=await r.json();
    if(!r.ok){document.getElementById('err2').textContent=s.error||'Could not jump there.';return;}
    REDRAFTS=0;   // navigated to another node — reset the rework counter
    render(s);
  }catch(e){document.getElementById('err2').textContent='Network error.';}
}
function renderAddons(s){
  const box=document.getElementById('addons'); if(!box||box.dataset.done)return;
  const ax=CFG.archetypes||[]; if(!ax.length){box.closest('.sec').style.display='none';return;}
  box.innerHTML=ax.map(a=>`<button type=button onclick="ask('${a.key}')">${esc(a.first||a.name)}<span class=bl>${esc(a.name)} · ${esc(a.blurb)}</span></button>`).join('');
  document.getElementById('adisc').textContent='AI composite advisors, not real people, not professional advice.';
  box.dataset.done='1';
}
let VET_OPEN=true, VET_STEPPED=false;
function toggleVet(){VET_OPEN=!VET_OPEN;const c=document.getElementById('vetcard');if(c){c.classList.toggle('open',VET_OPEN);const h=c.querySelector('.vethead');if(h)h.setAttribute('aria-expanded',String(VET_OPEN));}}
function renderVet(s){
  const el=document.getElementById('vet'); if(!el)return;
  const v=s.vetting, sh=s.shaped;
  if(!v&&!sh){el.innerHTML='';return;}
  if(s.step===0&&!s.done){VET_STEPPED=false;}                 // back at the first part → eligible to auto-collapse again
  else if(!VET_STEPPED){VET_OPEN=false;VET_STEPPED=true;}     // collapse once they click through to the next part
  const verdict=(v&&v.verdict)||'';
  const head=verdict?`<span class="verdict ${esc(verdict)}">${esc(verdict)}</span>`:'';
  const react=(v&&v.reaction)?`<p class=filgreact>${esc(v.reaction)}</p>`:'';
  const thesis=sh&&sh.thesis?`<p class=thesis><b>Your focus:</b> ${esc(sh.thesis)}</p>`:'';
  const edge=sh&&sh.founder_edge?`<p class=vrow><b>Your edge:</b> ${esc(sh.founder_edge)}</p>`:'';
  const alts=(sh&&sh.wedges_considered&&sh.wedges_considered.length>1)?`<p class=vrow><b>Also considered:</b> ${esc(sh.wedges_considered.slice(1).join(' · '))}</p>`:'';
  const reason=v&&v.reason?`<p class=vrow>${esc(v.reason)}</p>`:'';
  const risk=v&&v.biggest_risk?`<p class=vrow><b>Biggest risk:</b> ${esc(v.biggest_risk)}</p>`:'';
  const test=v&&v.first_test?`<p class=vrow><b>Cheapest first test:</b> ${esc(v.first_test)}</p>`:'';
  const cq=(sh&&sh.clarifying_question)?`<p class=vrow>🤔 ${esc(sh.clarifying_question)}</p>`:'';
  const PM=(v&&v.premortem)||[];   // the assumption check — the skeptic pass on the operator's OWN plan
  const pm=PM.length?('<div class=premortem><div class=pmh>🧪 Assumptions your plan rests on</div>'+
    PM.map(a=>`<div class="pmrow pm-${esc(a.status)}"><span class="pmstatus pm-${esc(a.status)}">${esc(a.status)}</span><div class=pmtext><b>${esc(a.assumption)}</b>${a.why?`<span class=pmwhy>${esc(a.why)}</span>`:''}</div></div>`).join('')+'</div>'):'';
  el.innerHTML=`<div class="vet${VET_OPEN?' open':''}" id=vetcard><button type=button class=vethead onclick=toggleVet() aria-expanded="${VET_OPEN}">${head}<span class=vtitle>Before we build: the straight read</span><span class=vcaret aria-hidden=true>▸</span></button>`+
    `<div class=vetbody>${react}${thesis}${edge}${alts}${reason}${risk}${test}${cq}${pm}</div></div>`;
}
// Board selection state (keys); seeded from the default board, editable in intake + sidebar.
let BOARD=(CFG.defaultBoard||[]).slice();
function personaName(key){const p=(CFG.archetypes||[]).find(a=>a.key===key);return p?(p.first||p.name):key;}
function renderBoardPick(){
  const el=document.getElementById('boardpick'); if(!el)return;
  const ax=CFG.archetypes||[]; if(!ax.length){el.innerHTML='';return;}
  el.innerHTML=`<div class=lab id=boardpicklab>Pick your Board of Directors, they'll vet every step (optional):</div>`+
    `<div class=opts role=group aria-labelledby=boardpicklab>`+ax.map(a=>`<button type=button class="bchip${BOARD.includes(a.key)?' on':''}" aria-pressed=${BOARD.includes(a.key)} onclick="toggleBoard('${a.key}',this)" title="${esc(a.first?a.first+', ':'')}${esc(a.blurb)}">${esc(a.name)}</button>`).join('')+`</div>`;
}
function toggleBoard(key,btn){
  const i=BOARD.indexOf(key), on=i<0;
  if(i>=0){BOARD.splice(i,1);}else{BOARD.push(key);}
  if(btn){btn.classList.toggle('on',on);btn.setAttribute('aria-pressed',String(on));}
}
function renderBoard(s){
  const sec=document.getElementById('boardsec'); if(!sec)return;
  if(s.status==='researching'){sec.style.display='none';return;}
  sec.style.display='';
  // Chips reflect the active board; tap to add/drop a director for on-demand convening.
  if(SESSION_BOARD===null) SESSION_BOARD=(s.directors&&s.directors.length?s.directors.slice():BOARD.slice());
  document.getElementById('boarddirs').innerHTML=(CFG.archetypes||[]).map(a=>
    `<button type=button class="bchip${SESSION_BOARD.includes(a.key)?' on':''}" aria-pressed=${SESSION_BOARD.includes(a.key)} onclick="toggleSessionBoard('${a.key}',this)" title="${esc(a.first?a.first+', ':'')}${esc(a.blurb)}">${esc(a.name)}</button>`).join('');
}
let SESSION_BOARD=null;
function toggleSessionBoard(key,el){
  if(SESSION_BOARD===null)SESSION_BOARD=[];
  const i=SESSION_BOARD.indexOf(key), on=i<0;
  if(i>=0){SESSION_BOARD.splice(i,1);}else{SESSION_BOARD.push(key);}
  el.classList.toggle('on',on);el.setAttribute('aria-pressed',String(on));
}
// Ask-an-expert + convene open the advisor drawer (a styled flyout, not a browser dialog).
function ask(key){openDrawer('expert',key);}
function convene(){openDrawer('board');}
let DRAWER={mode:null,key:null}, DRAWER_TRIGGER=null;
function openDrawer(mode,key){
  DRAWER={mode,key:key||null};
  DRAWER_TRIGGER=document.activeElement;   // restore focus here on close (WCAG)
  const title=document.getElementById('drawer-title'),sub=document.getElementById('drawer-sub'),
        go=document.getElementById('drawer-go'),out=document.getElementById('drawer-out'),
        q=document.getElementById('drawerq');
  out.style.display='none';out.innerHTML='';q.value='';go.disabled=false;
  if(mode==='expert'){
    const p=(CFG.archetypes||[]).find(a=>a.key===key)||{};
    title.textContent=p.name||'Ask an expert';
    sub.textContent=(p.blurb?('Composite advisor · '+p.blurb):'AI composite advisor')+', not professional advice.';
    go.textContent='Ask '+(p.name||'the advisor')+' →';
  }else{
    const chosen=(SESSION_BOARD&&SESSION_BOARD.length?SESSION_BOARD:(CFG.defaultBoard||[]));
    title.textContent='Your Board of Directors';
    sub.textContent=(chosen.length?('Convening: '+chosen.map(personaName).join(', ')):'Your full board')+', AI composite directors, not professional advice.';
    go.textContent='Convene the board →';
  }
  const d=document.getElementById('drawer');
  d.classList.add('open');d.setAttribute('aria-hidden','false');
  document.getElementById('drawerback').classList.add('show');
  setTimeout(()=>q.focus(),80);
}
function closeDrawer(){
  const d=document.getElementById('drawer');
  if(!d.classList.contains('open'))return;
  d.classList.remove('open');d.setAttribute('aria-hidden','true');
  document.getElementById('drawerback').classList.remove('show');
  if(DRAWER_TRIGGER&&DRAWER_TRIGGER.focus){DRAWER_TRIGGER.focus();DRAWER_TRIGGER=null;}
}
// ── Styled toast + modal (replace native alert/confirm/prompt across the app) ──
function toast(msg,kind){
  const t=document.createElement('div');t.className='toast'+(kind?(' '+kind):'');
  t.setAttribute('role','status');t.textContent=msg;
  document.getElementById('toasts').appendChild(t);
  setTimeout(()=>{t.classList.add('out');setTimeout(()=>t.remove(),320);},3600);
}
let MODAL_RESOLVE=null, MODAL_TRIGGER=null;
function _openModal(focusSel){
  MODAL_TRIGGER=document.activeElement;
  const m=document.getElementById('modal');
  m.classList.add('open');m.setAttribute('aria-hidden','false');
  document.getElementById('modalback').classList.add('show');
  setTimeout(()=>{const el=m.querySelector(focusSel);if(el)el.focus();},60);
}
function _closeModal(val){
  const m=document.getElementById('modal');
  if(!m.classList.contains('open'))return;
  m.classList.remove('open');m.setAttribute('aria-hidden','true');
  document.getElementById('modalback').classList.remove('show');
  if(MODAL_TRIGGER&&MODAL_TRIGGER.focus){MODAL_TRIGGER.focus();MODAL_TRIGGER=null;}
  const r=MODAL_RESOLVE;MODAL_RESOLVE=null;if(r)r(val);
}
function uiConfirm(title,msg,okLabel){
  return new Promise(res=>{MODAL_RESOLVE=res;
    document.getElementById('modal-title').textContent=title;
    document.getElementById('modal-body').innerHTML='<p>'+esc(msg)+'</p>';
    document.getElementById('modal-actions').innerHTML=
      `<button type=button class=ghost onclick="_closeModal(false)">Cancel</button>`+
      `<button type=button onclick="_closeModal(true)">${esc(okLabel||'OK')}</button>`;
    _openModal('#modal-actions button:last-child');
  });
}
function uiPrompt(title,label,type,placeholder){
  return new Promise(res=>{MODAL_RESOLVE=res;
    document.getElementById('modal-title').textContent=title;
    document.getElementById('modal-body').innerHTML=
      `<label for=modalinput class=sr-only>${esc(label)}</label>`+
      `<input id=modalinput type=${type||'text'} placeholder="${esc(placeholder||'')}" style="margin:0">`;
    document.getElementById('modal-actions').innerHTML=
      `<button type=button class=ghost onclick="_closeModal(null)">Cancel</button>`+
      `<button type=button onclick="_submitPrompt()">Send</button>`;
    _openModal('#modalinput');
  });
}
function _submitPrompt(){const i=document.getElementById('modalinput');_closeModal(i?i.value:null);}
async function submitDrawer(){
  if(!requireKey())return;
  const q=document.getElementById('drawerq').value, go=document.getElementById('drawer-go'),
        out=document.getElementById('drawer-out');
  out.style.display='block';
  out.innerHTML='<p class=lead>'+(DRAWER.mode==='board'?'Convening the board…':'Thinking…')+'</p>';
  go.disabled=true;
  const aid=Activity.start(DRAWER.mode==='board'?["Briefing your board on the plan","Each director weighs in","Synthesizing their verdict"]:["Reading your plan","Thinking it through"],1300,DRAWER.mode==='board'?'Convening your board':'Asking your advisor');
  try{
    if(DRAWER.mode==='expert'){
      const [r]=await Promise.all([fetch('/api/plan/'+SID+'/ask',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({archetype:DRAWER.key,question:q})}),new Promise(res=>setTimeout(res,850))]);
      const d=await r.json();go.disabled=false;
      if(r.ok){Activity.done(aid,'Done.');meterTick({id:SID,cost:d.cost,tokens:d.tokens});}else Activity.stop(aid);
      out.innerHTML=r.ok?mdToHtml(d.answer):esc(d.error||'Could not reach the advisor.');
    }else{
      const body={question:q}; if(SESSION_BOARD!==null)body.directors=SESSION_BOARD;
      const [r]=await Promise.all([fetch('/api/plan/'+SID+'/board',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(body)}),new Promise(res=>setTimeout(res,850))]);
      const d=await r.json();go.disabled=false;
      if(!r.ok){Activity.stop(aid);out.innerHTML=esc(d.error||'Could not convene the board.');return;}
      Activity.done(aid,'Your board weighed in.');meterTick({id:SID,cost:d.cost,tokens:d.tokens});
      const split=(d.conflicts&&d.conflicts.toLowerCase()!=='none')?`<span class=split>Where they split: ${esc(d.conflicts)}</span>`:'';
      out.innerHTML=skepticCardHtml(d.skeptic)+
        d.directors.map((x,i)=>`<div class=balloon id=dbal_${i}><button type=button class=bh onclick="document.getElementById('dbal_${i}').classList.toggle('open')">💬 See what ${esc(x.first||x.name)}${x.first&&x.name?' ('+esc(x.name)+')':''} says<span class=caret>▸</span></button><div class="bb md">${mdToHtml(x.take)}</div></div>`).join('')+
        `<div class=takeaway><div class=tl>Board takeaway</div>${esc(d.verdict)}${split}</div>`+
        `<div class=disc>${esc(d.disclaimer||'')}</div>`;
    }
  }catch(e){go.disabled=false;Activity.stop(aid);out.innerHTML='Network error.';}
}
// ── Reusable AI-activity ticker (pinned footer; never covers content) ─────────
// Each AI operation is its OWN collapsible task card: Activity.start([...steps],interval,label) (or
// open(label) for manual push) returns a track id; feed it real lines via push(id,line); end with
// done(id,'…') or stop(id) on error. Parallel ops STACK as separate labelled cards (the header says
// what each is doing) so concurrent spew stays legible; a card removes itself when its task finishes,
// and the footer hides once the last one is gone. start() auto-cycles its steps; open() is manual.
// The runner is a PERMANENT main-column panel (not the old slide-up footer): on submit the intake
// collapses and this expands in its place. Each AI op is its own collapsible card; finished cards stay
// as HISTORY (collapsed, click to re-read its spew) instead of vanishing, and the research fan-out
// paints a row of literal leaves 🍃 that turn grey→green as each lane completes.
const Activity={
  _seq:0, tracks:{}, n:0, _live:0,
  _el(){return document.getElementById('runner');},
  _log(){return document.getElementById('runner-log');},
  _show(){const a=this._el();if(a){a.hidden=false;a.classList.add('show');a.classList.remove('min');}},
  _trim(){ const log=this._log(); if(!log)return; const done=log.querySelectorAll('.atask.done');
    for(let i=0;i<done.length-7;i++)done[i].parentNode.removeChild(done[i]); },   // keep ~7 history cards
  _mkTask(label){
    const log=this._log(); if(!log)return null;
    const wrap=document.createElement('div'); wrap.className='atask';
    wrap.innerHTML='<button type=button class=ah><span class=astat aria-hidden=true></span><span class=alabel></span><span class=caret aria-hidden=true>&#9662;</span></button><div class=abody></div>';
    wrap.querySelector('.alabel').textContent=label||'Working';
    wrap.querySelector('.ah').onclick=()=>wrap.classList.toggle('collapsed');
    log.appendChild(wrap); log.scrollTop=log.scrollHeight;
    return wrap;
  },
  _line(body,text,done){
    if(!body)return null;
    const li=document.createElement('div'); li.className='aline '+(done?'done':'active');
    li.innerHTML='<span class=aglyph aria-hidden=true></span><span class=atext></span>';
    li.querySelector('.atext').textContent=text||'';
    body.appendChild(li);
    while(body.children.length>80)body.removeChild(body.firstChild);
    const log=this._log(); if(log)log.scrollTop=log.scrollHeight;   // outer log scrolls; card bodies show full
    return li;
  },
  _advance(t,text){ if(t.line)t.line.classList.replace('active','done'); t.line=this._line(t.body,text,false); },
  _busy(){ const a=this._el(); if(a)a.classList.toggle('busy',this._live>0); },
  start(steps,interval,label){
    const id=++this._seq; this.n++; this._live++; this._show();
    const wrap=this._mkTask(label); const body=wrap?wrap.querySelector('.abody'):null;
    const s=(steps||[]).slice(); let i=0; const t={wrap,body,line:null,timer:null}; this.tracks[id]=t;
    if(s.length)this._advance(t,s[0]);
    t.timer=setInterval(()=>{ if(i<s.length-1){i++;this._advance(t,s[i]);} else {clearInterval(t.timer);t.timer=null;} }, interval||1600);
    this._busy(); return id;
  },
  open(label){ const id=++this._seq; this.n++; this._live++; this._show(); const wrap=this._mkTask(label); this.tracks[id]={wrap,body:wrap?wrap.querySelector('.abody'):null,line:null,timer:null}; this._busy(); return id; },
  push(id,line){ const t=this.tracks[id]; if(t)this._advance(t,line); },
  done(id,msg){ this._end(id,msg,false); },
  stop(id){ this._end(id,null,true); },
  _end(id,msg,immediate){
    const t=this.tracks[id];
    if(!t)return;
    if(t.timer)clearInterval(t.timer);
    if(t.line)t.line.classList.replace('active','done');
    if(msg)this._line(t.body,msg,true);
    if(t.wrap){ t.wrap.classList.add('done'); t.wrap.classList.add('collapsed'); }  // collapse into history, keep it
    delete this.tracks[id]; this.n=Math.max(0,this.n-1); this._live=Math.max(0,this._live-1);
    this._trim(); this._busy();
  },
  // ── research fan-out leaves: part of the SAME tree as the spew steps. The leaf nodes nest one level
  // under the "Planning the research fan-out" step (the current active line) inside the op card, grey →
  // green as each lane returns. Each leaf expands to its own internals: the question + graded sources. ──
  leaves(id, labels){
    const t=this.tracks[id]; if(!t||!t.body)return;
    labels=labels||[]; this._leaflabels=labels;
    const box=document.createElement('div'); box.className='leaftree leafnest';
    box.innerHTML=labels.map((ln,i)=>
      '<button type=button class=leafnode data-i="'+i+'" onclick="Activity.toggleLeaf('+i+')" aria-expanded=false>'+
        '<span class=leaf-ico aria-hidden=true>\\uD83C\\uDF43</span>'+
        '<span class=leaf-lbl>Lane '+(i+1)+'</span>'+
        '<span class=leaf-q>'+esc(ln)+'</span><span class=lcaret aria-hidden=true>\\u25b8</span></button>'+
      '<div class=leafbody data-i="'+i+'"><div class=lq>'+esc(ln)+'</div>'+
        '<div class=lsrc data-i="'+i+'"><span class=pending>Researching this lane\\u2026</span></div></div>'
    ).join('');
    // nest it directly under the current step line (Planning the research fan-out)
    if(t.line&&t.line.parentNode){ t.line.parentNode.insertBefore(box, t.line.nextSibling); }
    else { t.body.appendChild(box); }
    this._leafbox=box;
  },
  toggleLeaf(i){ const box=this._leafbox; if(!box)return;
    const btn=box.querySelector('.leafnode[data-i="'+i+'"]'); if(!btn)return;
    const open=btn.classList.toggle('open'); btn.setAttribute('aria-expanded',open?'true':'false'); },
  leafDone(i){ const box=this._leafbox; if(!box)return;
    const el=box.querySelector('.leafnode[data-i="'+i+'"]'); if(el)el.classList.add('done'); },
  relabelLeaves(owned){ const box=this._leafbox; if(!box||!owned)return;
    owned.forEach((o,i)=>{ const el=box.querySelector('.leafnode[data-i="'+i+'"] .leaf-lbl');
      if(el){const who=o.owner_first||o.owner_name; if(who)el.textContent=who;} }); },
  // fill each leaf's expandable body with its graded sources once research data lands
  leafDetails(owned, rows){
    const box=this._leafbox; if(!box||!this._leaflabels)return;
    const JL={TRUST:'trusted',CROSS_CHECK:'cross-check',FLAG_SELF_INTERESTED:'flagged: sells the result'};
    this._leaflabels.forEach((ln,i)=>{
      const cell=box.querySelector('.lsrc[data-i="'+i+'"]'); if(!cell)return;
      const mine=(rows||[]).filter(r=>r.lane===ln);
      if(!mine.length)return;   // keep the "Researching…" placeholder until this lane has rows
      cell.innerHTML=mine.map(r=>{
        const ok=r.mark==='ok', jl=JL[r.judge]||'';
        const gate=(r.tier||jl)?'<span class=gate>\\u2699 gate: '+esc((r.tier||'').toLowerCase())+(jl?' \\u00b7 '+esc(jl):'')+'</span>':'';
        return '<div class=src>'+(ok?'\\u2705':'\\u26a0\\ufe0f')+' '+esc(r.text)+
          '<br><span class=note>'+esc(host(r.url))+', '+esc(r.note)+'</span>'+gate+'</div>';
      }).join('');
    });
  },
  resetLeaves(){ this._leafbox=null; this._leaflabels=null; },   // the old leaf tree lives in its card; cleared with the log
  stopAll(){ for(const id in this.tracks){if(this.tracks[id].timer)clearInterval(this.tracks[id].timer);}
    this.tracks={}; this.n=0; this._live=0; this.resetLeaves();
    const a=this._el(); if(a){a.hidden=true;a.classList.remove('show','min','busy');} const l=this._log(); if(l)l.innerHTML=''; }
};
const RESEARCH_STEPS=["Focusing your idea into one sharp thesis","Spinning up research across the web","Pulling sources on the market and competition","Grading every source for credibility","Flagging vendor-marketing spin","Re-sourcing the headline stats to primary sources","Scoring demand, market, and willingness to pay","Drafting your first offer"];
const PDF_STEPS=["Applying your board's input","Pulling your graded evidence","Building the decision matrix","Laying out a modern, on-brand design","Typesetting your PDF"];
// ── The one paid action: polished PDF = one-time $13; raw export stays free ──
function pdfUnlocked(){ return !CFG.pdfBilling || !!(me&&me.pdf_unlocked); }
function pdfPriceStr(){ const c=(me&&me.pdf_price)||CFG.pdfPrice||1300; return '$'+Math.round(c/100); }
function pdfBtn(){ return pdfUnlocked()
  ? '<button type=button onclick=download()>⬇ Download polished PDF</button>'
  : '<button type=button onclick=buyPdf()>🔓 Unlock polished PDF, '+pdfPriceStr()+'</button>'; }
async function buyPdf(){
  if(CFG.authEnabled&&!session){toast('Sign in to unlock your PDF.');signinEmail();return;}
  if(!CFG.pdfBilling){toast('Billing isn\\'t set up yet.','err');return;}
  try{
    const r=await fetch('/api/plan/'+SID+'/buy-pdf',{method:'POST',headers:authHeaders()});
    const d=await r.json();
    if(d.url){location.href=d.url;return;}            // → Stripe Checkout
    if(d.unlocked){await loadMe();download();return;}  // already paid → just grab it
    toast(d.error||'Could not start checkout.','err');
  }catch(e){toast('Network error starting checkout.','err');}
}
async function redeemCoupon(){
  const inp=document.getElementById('coupon'); if(!inp)return;
  const code=(inp.value||'').trim(); if(!code){toast('Enter a code.','err');return;}
  if(CFG.authEnabled&&!session){toast('Sign in to use a code.');signinEmail();return;}
  try{
    const r=await fetch('/api/coupon',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({code})});
    const d=await r.json();
    if(r.ok&&d.unlocked){
      await loadMe();                               // refresh me.pdf_unlocked
      toast('Code applied, your PDF is unlocked.','ok');
      try{const pr=await fetch('/api/plan/'+SID,{headers:authHeaders()});render(await pr.json());}catch(e){}  // flip the button to Download
    } else { toast(d.error||'That code isn\\'t valid.','err'); }
  }catch(e){toast('Network error.','err');}
}
async function download(){
  if(!pdfUnlocked()){buyPdf();return;}     // locked → route to the $13 unlock, not a key prompt
  const aid=Activity.start(PDF_STEPS,1600,'Building your styled PDF');
  const minShow=new Promise(res=>setTimeout(res,2600));   // let the sequence breathe (covers fast mock runs)
  try{
    const [r]=await Promise.all([fetch('/api/plan/'+SID+'/plan.pdf',{headers:authHeaders()}),minShow]);
    if(!r.ok){let d={};try{d=await r.json();}catch(e){} Activity.stop(aid);
      if(d.needPurchase){buyPdf();return;}             // server says locked → open checkout
      toast(d.error||'Could not build the PDF.','err');return;}
    const blob=await r.blob();
    meterTick({id:SID,cost:r.headers.get('X-FILG-Cost'),tokens:r.headers.get('X-FILG-Tokens')});
    Activity.done(aid,'Your PDF is ready.');
    const u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download='filg-business-plan.pdf';a.click();URL.revokeObjectURL(u);
  }catch(e){Activity.stop(aid);toast('Network error building the PDF.','err');}
}
async function downloadZip(){   // power-user escape hatch: the raw source files
  try{
    const r=await fetch('/api/plan/'+SID+'/download',{headers:authHeaders()});
    if(!r.ok){toast('Could not download.','err');return;}
    const blob=await r.blob(),u=URL.createObjectURL(blob);
    const a=document.createElement('a');a.href=u;a.download='filg-business-plan.zip';a.click();URL.revokeObjectURL(u);
  }catch(e){toast('Network error.','err');}
}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
function host(u){try{return new URL(u).hostname.replace(/^www\\./,'');}catch(e){return u;}}
function isKeyErr(m){return /key was rejected|expired or invalid|update your key|401|user not found/i.test(m||'');}
function mdToHtml(md){
  let h=esc(md==null?'':md);
  h=h.replace(/`([^`]+)`/g,'<code>$1</code>');
  h=h.replace(/\\*\\*([^*]+)\\*\\*/g,'<strong>$1</strong>');
  h=h.replace(/\\[([^\\]]+)\\]\\((https?:[^)\\s]+)\\)/g,'<a href="$2" target=_blank rel=noopener>$1</a>');
  h=h.replace(/\\[(https?:[^\\]\\s]+)\\]/g,'<a href="$1" target=_blank rel=noopener>$1</a>');  // [bare url] → link
  h=h.replace(/(^|[\\s(])(https?:\\/\\/[^\\s<)]+)/g,'$1<a href="$2" target=_blank rel=noopener>$2</a>');  // raw url → link
  const lines=h.split('\\n'); const out=[]; let inList=false; let i=0;
  const cells=function(r){return r.replace(/^\\s*\\|/,'').replace(/\\|\\s*$/,'').split('|').map(function(c){return c.trim();});};
  const isRow=function(s){return /^\\s*\\|.*\\|\\s*$/.test(s);};
  const isSep=function(s){return /^\\s*\\|?[\\s:|-]*-{2,}[\\s:|-]*$/.test(s);};
  while(i<lines.length){
    const ln=lines[i]; let m;
    // markdown table: a '| ... |' header row followed by a '|---|---|' separator
    if(isRow(ln)&&i+1<lines.length&&isSep(lines[i+1])){
      if(inList){out.push('</ul>');inList=false;}
      const head=cells(ln); const body=[]; i+=2;
      while(i<lines.length&&isRow(lines[i])){body.push(cells(lines[i]));i++;}
      let t='<table><thead><tr>'+head.map(function(c){return '<th>'+c+'</th>';}).join('')+'</tr></thead><tbody>';
      t+=body.map(function(r){return '<tr>'+r.map(function(c){return '<td>'+c+'</td>';}).join('')+'</tr>';}).join('');
      out.push(t+'</tbody></table>'); continue;
    }
    if(/^\\s*(-{3,}|\\*{3,}|_{3,})\\s*$/.test(ln)){if(inList){out.push('</ul>');inList=false;}out.push('<hr>');i++;continue;}  // --- → real rule, not text
    if(m=ln.match(/^(#{1,6})\\s+(.*)$/)){if(inList){out.push('</ul>');inList=false;}const lvl=Math.min(m[1].length+3,5);out.push('<h'+lvl+'>'+m[2]+'</h'+lvl+'>');i++;continue;}
    if(m=ln.match(/^\\s*[-*]\\s+(.*)$/)){if(!inList){out.push('<ul>');inList=true;}out.push('<li>'+m[1]+'</li>');i++;continue;}
    if(ln.trim()===''){if(inList){out.push('</ul>');inList=false;}i++;continue;}
    if(inList){out.push('</ul>');inList=false;}
    out.push('<p>'+ln+'</p>');i++;
  }
  if(inList)out.push('</ul>');
  return out.join('');
}

// ── Auth (Supabase) + billing (Stripe) + profile ────────────────────────────
function renderAuth(){
  const bar=document.getElementById('authbar');
  if(sb&&session){
    bar.style.display='';
    bar.innerHTML=`<button class=link onclick=showPlans()>My plans</button>`+
      (CFG.byokEnabled?`<button class=link onclick=keyModal()>🔑 Your key</button>`:'')+
      `<span class=who>${esc(session.user.email)}</span>`+
      `<button class=link onclick=signout()>Sign out</button>`;   // no subscription; the $13 PDF unlock lives on the finished plan
  }else if(sb){bar.style.display='';bar.innerHTML=`<button class=link onclick=authModal()>Log in / Sign up</button>`;}
  else{bar.style.display='none';}
  gateIntake();
}
function gateIntake(){
  // Auth options no longer live on the page. The prompt box + Build button always show; a signed-out
  // user picks an idea, hits Build, and start() opens the sign-in modal. The email field is only for
  // the auth-off (free/dev) path — when auth is on, identity comes from the token after the modal.
  const gate=document.getElementById('authgate'),email=document.getElementById('email'),go=document.getElementById('go');
  if(go)go.style.display='';
  if(email)email.style.display=CFG.authEnabled?'none':'';
  if(gate)gate.innerHTML='';
}
const GOOGLE_SVG=`<svg class=gicon viewBox="0 0 18 18" aria-hidden=true><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.71-1.57 2.68-3.89 2.68-6.62z"></path><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.81.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18z"></path><path fill="#FBBC05" d="M3.97 10.72a5.4 5.4 0 0 1 0-3.44V4.95H.96a9 9 0 0 0 0 8.1l3.01-2.33z"></path><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58C13.46.9 11.43 0 9 0A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58z"></path></svg>`;
function authModal(){
  saveIdea();   // keep the typed idea through the OAuth redirect / email round-trip
  document.getElementById('modal-title').textContent='Save your plan';
  document.getElementById('modal-body').innerHTML=
    `<p class=or style="margin:0 0 12px">Sign in so your plan saves to your profile. Free to start.</p>`+
    `<div class=authgate>`+
    (CFG.supabaseUrl?`<button class=gbtn onclick="authGo('google')">${GOOGLE_SVG}Continue with Google</button>`:'')+
    `<button class=gbtn onclick="authGo('email')">✉️ Email me a sign-in link</button></div>`;
  document.getElementById('modal-actions').innerHTML='';
  _openModal('.authgate button');
}
function authGo(kind){_closeModal();if(kind==='google')signinGoogle();else signinEmail();}
async function keyModal(){
  if(!CFG.byokEnabled){toast('Bring-your-own-key isn\\u2019t turned on yet.','err');return;}
  if(CFG.authEnabled&&!session){authModal();return;}   // BYOK is account-scoped → sign in first
  let d; try{const r=await fetch('/api/key',{headers:authHeaders()});d=await r.json();}catch(e){d={key:null};}
  if(d&&d.key){
    document.getElementById('modal-title').textContent='Your API key';
    document.getElementById('modal-body').innerHTML=
      `<p class=or style="margin:0 0 12px">You\\u2019re running on your own <b>${esc(d.key.provider)}</b> key (\\u2022\\u2022\\u2022\\u2022${esc(d.key.last4)}). Plans use your key, not ours.</p>`+
      `<div class=authgate><button class=gbtn onclick="keyForm()">Replace key</button>`+
      `<button class=gbtn onclick="removeKey()">Remove key</button></div>`;
    document.getElementById('modal-actions').innerHTML=`<button type=button onclick="_closeModal()">Done</button>`;
    _openModal('.authgate button');
  }else{keyForm();}
}
let KEY_PROV='openrouter';   // provider chosen in the key modal ('openrouter'|'anthropic')
function setKeyProv(p){
  KEY_PROV=(p==='anthropic')?'anthropic':'openrouter';
  const wrap=document.getElementById('keyprov');
  if(wrap)wrap.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.p===KEY_PROV));
  const inp=document.getElementById('keyinput'); if(inp)inp.placeholder=(KEY_PROV==='anthropic')?'sk-ant-\\u2026':'sk-or-v1-\\u2026';
  const help=document.getElementById('keyprovhelp');
  if(help)help.innerHTML=(KEY_PROV==='anthropic')
    ?'Get it at <a href="https://console.anthropic.com/settings/keys" target=_blank rel=noopener>Anthropic \\u2192 API keys</a> (Claude direct, all tiers, billed by Anthropic).'
    :'Get it at <a href="https://openrouter.ai/keys" target=_blank rel=noopener>OpenRouter \\u2192 Keys</a> (one key fronts every model + cited web search).';
}
function keyPrefixDetect(v){v=(v||'').trim();if(v.indexOf('sk-ant-')===0)setKeyProv('anthropic');else if(v.indexOf('sk-or-')===0)setKeyProv('openrouter');}
function keyForm(){
  document.getElementById('modal-title').textContent='Bring your own key';
  document.getElementById('modal-body').innerHTML=
    `<p class=or style="margin:0 0 10px">Hook up your own key to build your plan and use the full suite of tools: plans, branches, the board, chat, and PDF export. Pick your provider, paste a key, and you pay them directly (usually pennies a plan).</p>`+
    `<div class=modesw id=keyprov role=group aria-label="Key provider" style="margin:0 0 12px"><button type=button data-p=openrouter onclick="setKeyProv('openrouter')">OpenRouter</button><button type=button data-p=anthropic onclick="setKeyProv('anthropic')">Anthropic</button></div>`+
    `<ol class=keysteps><li><span id=keyprovhelp></span></li><li>Create a key and copy it</li><li>Paste it below and save \\u2014 we\\u2019ll test it before storing</li></ol>`+
    `<label for=keyinput class=sr-only>Your API key</label>`+
    `<input id=keyinput type=password placeholder="sk-or-v1-\\u2026" autocomplete=off spellcheck=false oninput="keyPrefixDetect(this.value)" style="margin:4px 0 2px">`+
    `<div class=err id=keyerr></div>`;
  document.getElementById('modal-actions').innerHTML=
    `<button type=button class=ghost onclick="_closeModal()">Cancel</button>`+
    `<button type=button id=keysave onclick="saveKey()">Save &amp; validate</button>`;
  setKeyProv(KEY_PROV);   // sync the toggle + placeholder + help line
  _openModal('#keyinput');
}
async function saveKey(){
  const inp=document.getElementById('keyinput'),btn=document.getElementById('keysave'),er=document.getElementById('keyerr');
  const key=(inp.value||'').trim(); er.textContent='';
  if(key.length<8){er.textContent='That doesn\\u2019t look like a key.';return;}
  btn.disabled=true;btn.textContent='Validating\\u2026';
  try{
    const r=await fetch('/api/key',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({provider:KEY_PROV,key})});  // chosen provider (server falls back to prefix detection)
    const d=await r.json();
    if(!r.ok){er.textContent=d.error||'Could not save the key.';btn.disabled=false;btn.textContent='Save & validate';return;}
    HAS_KEY=true;_closeModal();toast('Key saved \\u2014 build as many plans as you want. \\u2713');
  }catch(e){er.textContent='Network error.';btn.disabled=false;btn.textContent='Save & validate';}
}
async function removeKey(){
  try{const r=await fetch('/api/key/remove',{method:'POST',headers:authHeaders()});
    if(r.ok){HAS_KEY=false;toast('Key removed.');_closeModal();}else toast('Could not remove the key.','err');
  }catch(e){toast('Network error.','err');}
}
function saveIdea(){try{const v=document.getElementById('idea').value;if(v)localStorage.setItem('filg_idea',v);}catch(e){}}
function restoreIdea(){try{const v=localStorage.getItem('filg_idea');if(v){document.getElementById('idea').value=v;localStorage.removeItem('filg_idea');}}catch(e){}}
async function loadMe(){
  if(!session){me=null;HAS_KEY=false;return;}
  try{const r=await fetch('/api/me',{headers:authHeaders()});me=r.ok?await r.json():null;}catch(e){me=null;}
  await loadKey();   // refresh BYOK key state alongside identity
}
async function signinGoogle(){saveIdea();const {error}=await sb.auth.signInWithOAuth({provider:'google',options:{redirectTo:location.origin}});if(error)toast(error.message,'err');}
async function signinEmail(){
  const email=await uiPrompt('Sign in','Your email','email','you@email.com');
  if(!email)return; saveIdea();
  const {error}=await sb.auth.signInWithOtp({email,options:{emailRedirectTo:location.origin}});
  toast(error?error.message:'Check your inbox for the sign-in link.',error?'err':'');
}
async function signout(){await sb.auth.signOut();session=null;me=null;newPlan();renderAuth();}
function show(id){['intake','workspace','profile'].forEach(x=>{const e=document.getElementById(x);if(e)e.style.display=(x===id?(x==='workspace'?'block':'block'):'none');});
  document.body.classList.toggle('ws',id==='workspace');}   // ws → left tools drawer + full-width main
function newPlan(){SIDEBAR_PHASE=null;ACT_RESEARCH=false;ACT_PROG_N=0;ACT_ID=null;VET_OPEN=true;VET_STEPPED=false;Activity.stopAll();closeViewer();SID=null;
  // render a FRESH intake — clear any in-flight button/idea/error left over from a prior build or sign-out
  const g=document.getElementById('go'); if(g){g.disabled=false;g.textContent='Build my plan →';}
  const idea=document.getElementById('idea'); if(idea)idea.value='';
  const err=document.getElementById('err'); if(err)err.textContent='';
  const joke=document.getElementById('joke'); if(joke)joke.innerHTML='';
  try{localStorage.removeItem('filg_idea');}catch(e){}
  if(location.pathname!=='/')history.pushState({},'','/');show('intake');renderBoardPick();gateIntake();}
async function showPlans(){
  let d; try{const r=await fetch('/api/plans',{headers:authHeaders()});if(!r.ok){toast('Sign in to see your plans.','err');return;}d=await r.json();}catch(e){toast('Network error.','err');return;}
  show('profile');renderPlans(d);
}
function renderPlans(d){
  const rows=d.plans.length?d.plans.map(p=>{
    const meta=p.done?`Finished · ${d.total} parts`:(p.status==='researching'?'Researching…':`In progress · part ${(p.step||0)+1} of ${d.total}`);
    const acts=`<button onclick="resume('${p.id}')">${p.done?'Open / iterate':'Resume'}</button>`+
      (p.done?`<button class=gbtn onclick="resumeDownload('${p.id}')">Download</button>`:'')+
      `<button class=gbtn onclick="sharePlan('${p.id}')">${p.shared?'🔗 Shared':'Share'}</button>`+
      `<button class=gbtn onclick="deletePlan('${p.id}')" aria-label="Delete plan">Delete</button>`;
    return `<div class=pcard><div><div class=idea>${esc((p.idea||'Untitled').slice(0,90))}</div><div class=meta>${meta} · ${esc(new Date(p.created_at).toLocaleDateString())}</div></div><div class=act><span class="pill ${p.done?'done':''}">${p.done?'done':'WIP'}</span>${acts}</div></div>`;
  }).join(''):`<p class=empty>No plans yet, build your first one.</p>`;
  document.getElementById('profile').innerHTML=`<div class=plans><h2>Your plans</h2><p class=sub>${esc(d.email)}</p>`+
    `<div style="margin:10px 0 16px"><button onclick=newPlan()>+ New plan</button></div>`+rows+`</div>`;
}
async function resume(id){
  SID=id;if(location.pathname!=='/plan/'+id)history.pushState({plan:id},'','/plan/'+id);   // clean URL for any entry point
  show('workspace');SESSION_BOARD=null;SIDEBAR_PHASE=null;VET_OPEN=true;VET_STEPPED=false;
  const ab=document.getElementById('addons');if(ab)delete ab.dataset.done;
  closeDrawer();closeViewer();
  try{const r=await fetch('/api/plan/'+SID,{headers:authHeaders()});const s=await r.json();render(s);if(s.status==='researching')poll();}catch(e){document.getElementById('err2').textContent='Could not load that plan.';}
}
function resumeDownload(id){SID=id;download();}
async function deletePlan(id){
  if(!await uiConfirm('Delete this plan?','This permanently removes the plan. It won\\'t free up a free build.','Delete'))return;
  try{
    const r=await fetch('/api/plan/'+id+'/delete',{method:'POST',headers:authHeaders()});
    if(!r.ok){toast('Could not delete.','err');return;}
    toast('Plan deleted.'); showPlans();
  }catch(e){toast('Network error.','err');}
}
async function sharePlan(id){
  try{
    const r=await fetch('/api/plan/'+id+'/share',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({shared:true})});
    const d=await r.json();
    if(!r.ok){toast(d.error||'Could not share.','err');return;}
    try{await navigator.clipboard.writeText(d.url);toast('🔗 Share link copied to clipboard');}
    catch(e){toast('Share link: '+d.url);}
    showPlans();
  }catch(e){toast('Network error.','err');}
}
function banner(msg){const b=document.getElementById('banner');b.textContent=msg;b.style.display='block';}
async function initAuth(){
  const q=new URLSearchParams(location.search);
  if(q.get('pdf'))banner('🎉 Polished PDF unlocked. Download it from your finished plan.');
  if(q.get('pdf_canceled'))banner('Checkout canceled, no charge. Your raw export is still free.');
  restoreIdea();renderBoardPick();renderStack();paintMeter();   // show the crew picker + meter from first paint
  if(!CFG.authEnabled||!window.supabase){renderAuth();routeFromPath();return;}
  sb=window.supabase.createClient(CFG.supabaseUrl,CFG.supabaseAnon);
  sb.auth.onAuthStateChange(async (_e,s)=>{session=s;await loadMe();renderAuth();});
  const {data}=await sb.auth.getSession();session=data.session;await loadMe();renderAuth();routeFromPath();
}
function routeFromPath(){   // a finished plan lives at /plan/{id} — deep-link / bookmark / revisit / back-fwd
  const m=(location.pathname||'').match(/^\\/plan\\/([a-z0-9]+)/i);
  if(m&&m[1])resume(m[1]);
  else if(SID){SID=null;show('intake');renderBoardPick();gateIntake();}   // navigated back to home
}
window.addEventListener('popstate',routeFromPath);   // browser back/forward drives the SPA
document.addEventListener('click',function(e){   // click outside the crew picker closes it
  const sp=document.getElementById('stackpop');
  if(sp&&!sp.hidden&&!e.target.closest('#stackdial'))closeStackPop();
});
document.addEventListener('keydown',function(e){
  const drawer=document.getElementById('drawer'), modal=document.getElementById('modal');
  const dOpen=drawer&&drawer.classList.contains('open'), mOpen=modal&&modal.classList.contains('open');
  if(e.key==='Escape'){const sp=document.getElementById('stackpop');
    if(sp&&!sp.hidden){closeStackPop();}else if(mOpen)_closeModal();else if(dOpen)closeDrawer();}
  if((e.metaKey||e.ctrlKey)&&e.key==='Enter'&&e.target&&e.target.id==='drawerq')submitDrawer();
  if(mOpen&&e.key==='Enter'&&e.target&&e.target.id==='modalinput'){e.preventDefault();_submitPrompt();}
  const ov=mOpen?modal:(dOpen?drawer:null);   // trap focus inside whichever overlay is open
  if(ov&&e.key==='Tab'){
    const f=ov.querySelectorAll('button,textarea,input,a[href],[tabindex]:not([tabindex="-1"])');
    if(!f.length)return;
    const first=f[0], last=f[f.length-1];
    if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
    else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
  }
});
initAuth();
</script>
<div class=vibestrip aria-hidden=true><div class=vibetrack><span class=vibe>This UI was vibe coded AF and I know it's butt-ugly but I will never update it, because I believe in my soul that Craigslist was the height of web design and since we started complicating it things have gotten steadily worse in the world and I can't prove that there's a correlation but also you can't prove there's not and anyways it's an app meant for automating planning and building your business so it would kinda be a bad look if I hadn't automated the building of it to some extent and honestly the algorithm stuff was hard and UI is easy so it just made sense to leave it, anyway I'm not a designer I want to get paid to drink coffee and push buttons with my dog curled up between my legs and then a little pillow on top of him to hold my laptop. Really I think if we could all just agree to collectively move on from design and style and good taste in general the world might be a better place, you know? It's just like we've so completely commoditised every aspect of self worth and beauty and it all kind of starts with the concept of aesthetic beauty, like the way one thing looks can really be better than another way, when really it's all just light, and even that's a pretty big maybe considering the light is just signals in our little meat brains that we can't definitively prove exist, and the fact that we even have the ability to conceive of the absurdity of that thought makes any sort of external aesthetic consideration seem silly. I mean everything is silly in the grand scheme of things, and what does it even mean to be silly? There I go placing 'aesthetic' value on the concept of value itself, like I know wtf I'm talking about (I don't). And as long as I'm yapping about aesthetics and absurdity... who the hell was in the room when they came up with 'professionalism'? Like really, of all the personalities in the universe we went with the most boring possible one, based on the human equivalent of a cardboard charcuterie sampler. Like is it really that weird that I wanted to name my app 'Fuck it, let's go'? We all say fuck. You say fuck. You are saying it in your head right now, who cares? Why do we all have to pretend we don't say fuck on LinkedIn? That's weird. I mean if you actually do NOT say fuck then that makes you weird. Not qualitatively bad, but empirically weird in the sense of deviation from the norm. And we all just agreed at some point to pretend to be people who don't say "fuck" for most of our waking social lives.. that seems nuts. I want to say fuck on LinkedIn. Do you want to say fuck on LinkedIn? I'll bet you do. If you are the type of person still reading this you absolutely want to say swears on LinkedIn, and that make you my kind of person. I support you. I believe in you. I.. love you? I love the idea of you. I'm glad you're here, honestly. You are the person this was built for. Go build a business, seriously people do it every day. They have been doing it for millennia. Your ancestors survived war and famine and saber tooth tigers and shit (don't come after me science nerd, I don't care if they co existed, I don't know, and I'm not gonna look it up.) They did all that and all got laid at least once over and over just to make you here now and that means you have it in your genes, in your BONES. Success is in you, you are the proof. You can start a fucking business. Go do it. Say fuck on LinkedIn. Make a million bucks. Buy a tuxedo and rip the sleeves off and keep it on for a month, don't even take it off to shower. Why would you? You are a winner. You are success incarnate. You do what you want. You are gonna make it. You are gonna prove your first crush that shot you down wrong. You are gonna make your dad proud. You are gonna be the best thing that ever happened to your friends and family and everyone that ever believed in you. I believe in you! You've got tenacity, if nothing else. Why are you still reading this anyway? THAT is weird. But like good weird. But there I go qualifying things as good and bad again. Go make some money. FUCK!</span><span class=vibe>This UI was vibe coded AF and I know it's butt-ugly but I will never update it, because I believe in my soul that Craigslist was the height of web design and since we started complicating it things have gotten steadily worse in the world and I can't prove that there's a correlation but also you can't prove there's not and anyways it's an app meant for automating planning and building your business so it would kinda be a bad look if I hadn't automated the building of it to some extent and honestly the algorithm stuff was hard and UI is easy so it just made sense to leave it, anyway I'm not a designer I want to get paid to drink coffee and push buttons with my dog curled up between my legs and then a little pillow on top of him to hold my laptop. Really I think if we could all just agree to collectively move on from design and style and good taste in general the world might be a better place, you know? It's just like we've so completely commoditised every aspect of self worth and beauty and it all kind of starts with the concept of aesthetic beauty, like the way one thing looks can really be better than another way, when really it's all just light, and even that's a pretty big maybe considering the light is just signals in our little meat brains that we can't definitively prove exist, and the fact that we even have the ability to conceive of the absurdity of that thought makes any sort of external aesthetic consideration seem silly. I mean everything is silly in the grand scheme of things, and what does it even mean to be silly? There I go placing 'aesthetic' value on the concept of value itself, like I know wtf I'm talking about (I don't). And as long as I'm yapping about aesthetics and absurdity... who the hell was in the room when they came up with 'professionalism'? Like really, of all the personalities in the universe we went with the most boring possible one, based on the human equivalent of a cardboard charcuterie sampler. Like is it really that weird that I wanted to name my app 'Fuck it, let's go'? We all say fuck. You say fuck. You are saying it in your head right now, who cares? Why do we all have to pretend we don't say fuck on LinkedIn? That's weird. I mean if you actually do NOT say fuck then that makes you weird. Not qualitatively bad, but empirically weird in the sense of deviation from the norm. And we all just agreed at some point to pretend to be people who don't say "fuck" for most of our waking social lives.. that seems nuts. I want to say fuck on LinkedIn. Do you want to say fuck on LinkedIn? I'll bet you do. If you are the type of person still reading this you absolutely want to say swears on LinkedIn, and that make you my kind of person. I support you. I believe in you. I.. love you? I love the idea of you. I'm glad you're here, honestly. You are the person this was built for. Go build a business, seriously people do it every day. They have been doing it for millennia. Your ancestors survived war and famine and saber tooth tigers and shit (don't come after me science nerd, I don't care if they co existed, I don't know, and I'm not gonna look it up.) They did all that and all got laid at least once over and over just to make you here now and that means you have it in your genes, in your BONES. Success is in you, you are the proof. You can start a fucking business. Go do it. Say fuck on LinkedIn. Make a million bucks. Buy a tuxedo and rip the sleeves off and keep it on for a month, don't even take it off to shower. Why would you? You are a winner. You are success incarnate. You do what you want. You are gonna make it. You are gonna prove your first crush that shot you down wrong. You are gonna make your dad proud. You are gonna be the best thing that ever happened to your friends and family and everyone that ever believed in you. I believe in you! You've got tenacity, if nothing else. Why are you still reading this anyway? THAT is weird. But like good weird. But there I go qualifying things as good and bad again. Go make some money. FUCK!</span></div></div>
</div></body></html>"""
