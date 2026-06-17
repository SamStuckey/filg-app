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
import plans      # noqa: E402 — account plans + entitlements (concurrency limit, future feature gates)

from . import auth, billing, keys, planner, store  # noqa: E402 — persistence, auth, billing, BYOK keys

MOCK = os.environ.get("FILG_MOCK") == "1"
# FILG_PAID_EMAILS is now only a manual comp/override; real paid status comes from billing.is_paid.
PAID = {e.strip().lower() for e in os.environ.get("FILG_PAID_EMAILS", "").split(",") if e.strip()}


def _is_paid(email: str, verified: bool) -> bool:
    """Paid = a verified user with a live subscription, OR an allowlisted comp. An unverified
    (free-tier, email-only) caller can't be billed-paid, but the comp allowlist still applies."""
    return (verified and billing.is_paid(email)) or (email in PAID)


def _is_byok(user: str) -> bool:
    """True iff this user runs on their own key (BYOK configured + a key saved)."""
    return bool(user and keys.enabled() and keys.has_key(user))


def _provider_for(user: str):
    """The provider a session should run on: the user's saved OpenRouter key, else None (FILG's key).
    provider.use(None) is a no-op, so callers can wrap unconditionally — with BYOK off this is inert."""
    if not (user and keys.enabled()):
        return None
    key = keys.get_key(user)
    return provider.openrouter_provider(key) if key else None


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
    API action past the one free welcome plan requires their own key."""
    return bool(keys.enabled() and not _is_byok(user))


def _key_wall(session: dict):
    """402 if the session owner must bring a key before this (API-calling) action; else None.
    The one free welcome plan is granted at /api/plan/start, so every later engine call is walled."""
    if _needs_key(session.get("user")):
        return JSONResponse(
            {"error": "Add your OpenRouter key to keep building.", "needKey": True}, status_code=402)
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

# Per-user concurrency: a user may run up to their plan's `max_concurrent` AI operations at once
# (cost is isolated per run via pipeline.run_ledger, so concurrent runs don't mis-bill each other).
_inflight: dict[str, int] = {}
_inflight_lock = threading.Lock()


class BusyError(Exception):
    """Raised when a user is already at their plan's concurrent-operation limit."""
    def __init__(self, cap: int):
        self.cap = cap
        super().__init__(f"at concurrency limit ({cap})")


def _concurrency_cap(user: str) -> int:
    return plans.max_concurrent(store.account_plan(user))


@contextlib.contextmanager
def _run_slot(user: str):
    """Reserve a concurrency slot for `user`, bind their provider + a fresh per-run cost ledger, then
    release the slot on exit. Raises BusyError if they're already at their plan's limit."""
    cap = _concurrency_cap(user)
    with _inflight_lock:
        if _inflight.get(user, 0) >= cap:
            raise BusyError(cap)
        _inflight[user] = _inflight.get(user, 0) + 1
    try:
        with provider.use(_provider_for(user)), pipeline.run_ledger():
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


def _run_job(job_id: str, idea: str, user: str, mode: str) -> None:
    try:
        with pipeline.run_ledger():
            res = (teardown.generate_full if mode == "full" else teardown.generate)(idea, mock=MOCK)
        usage.record_run(user, res["cost"])
        store.finish(job_id, res, mode)
    except Exception as e:  # noqa: BLE001 — surface failures to the client, don't crash the worker
        traceback.print_exc()  # full trace → Render stdout logs (client only sees str(e))
        store.fail(job_id, str(e))


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

    is_paid = _is_paid(user, verified=authed is not None)
    allowed, reason = usage.can_run(user, is_paid=is_paid)
    if not allowed:
        return JSONResponse({"error": reason, "upgrade": True}, status_code=402)

    mode = "full" if is_paid else "teardown"   # free → teardown; paid ($39/mo) → full artifact set
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
    """Tell the frontend who it is and whether to show the Upgrade button."""
    authed = auth.user_from_request(request)
    if not authed:
        return {"signed_in": False, "auth_enabled": auth.AUTH_ENABLED,
                "billing_enabled": billing.BILLING_ENABLED}
    return {"signed_in": True, "email": authed["email"],
            "paid": _is_paid(authed["email"], verified=True),
            "auth_enabled": auth.AUTH_ENABLED, "billing_enabled": billing.BILLING_ENABLED}


@app.post("/api/checkout")
async def api_checkout(request: Request):
    """Start a Stripe Checkout for the $39/mo Operator plan. Requires a verified user."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    if not billing.BILLING_ENABLED:
        return JSONResponse({"error": "Billing isn't configured yet."}, status_code=503)
    if _is_paid(authed["email"], verified=True):
        return JSONResponse({"error": "You're already on Operator."}, status_code=409)
    try:
        url = billing.create_checkout_url(authed["email"], user_id=authed["id"])
    except billing.StripeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"url": url}


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
        with prov_mod.use(prov_mod.openrouter_provider(api_key)):
            out = pipeline.call("key_validate", pipeline.HAIKU, "Reply with: OK", max_tokens=5)
        return (True, "ok") if out else (False, "The key didn't return a response.")
    except Exception:  # noqa: BLE001 — never surface provider internals to the client
        return False, "That key didn't work. Check it and try again."


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
    provider_name = (body.get("provider") or "openrouter").strip()
    api_key = (body.get("key") or "").strip()
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
            "billing_enabled": billing.BILLING_ENABLED, **usage.snapshot()}


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
        with provider.use(prov), pipeline.run_ledger():
            prep = planner.prepare(idea, mock=MOCK, on_progress=on_progress)  # intake → research → vet → draft
            toks = pipeline.LEDGER.tokens()   # the welcome run's token usage → seeds the session meter
        if prov is None:   # FILG's key → meter the free run; BYOK is the user's spend, not metered
            usage.record_run(user, prep["research_cost"])           # the metered free run + daily total
            usage.record_spend(prep["cost"] - prep["research_cost"])  # intake + vet + first draft → daily
        root = _new_node(planner.root_node(prep["proposal"]), None)  # seed the decision tree's root
        tree = {"nodes": {root["id"]: root}, "active": root["id"]}
        store.plan_save(session_id, status="building", research=prep["research"], step=0,
                        proposal=prep["proposal"], shaped=prep["shaped"], vetting=prep["vetting"],
                        cost=prep["cost"], tokens=toks, tree=tree, progress=progress)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()  # full trace → Render stdout logs (client only sees str(e))
        store.plan_save(session_id, status="error", error=str(e))


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
    if _is_byok(user):
        pass   # has a key → unlimited plans on their own spend
    elif keys.enabled():
        # BYOK on, no key: one free welcome plan, then they must bring a key. The daily kill switch
        # still guards FILG's spend on these free runs.
        if usage.free_used(user):
            return JSONResponse(
                {"error": "The first plan is on us. Hook up your OpenRouter key to keep using the full suite of tools.",
                 "needKey": True}, status_code=402)
        if usage.kill_switch_tripped():
            return JSONResponse(
                {"error": "FILG's free-run budget for today is maxed. Add your key to run now.",
                 "needKey": True}, status_code=402)
    else:
        # BYOK off (no FILG_KEY_SECRET — dev/local): keep the legacy free-cap behavior so dev works.
        allowed, reason = usage.can_run(user, is_paid=_is_paid(user, verified))
        if not allowed:
            return JSONResponse({"error": reason, "upgrade": True}, status_code=402)
    directors = [k for k in (body.get("directors") or []) if k in personas.KEYS]  # optional board
    sid = uuid.uuid4().hex[:12]
    store.plan_create(sid, user, idea, directors=directors)
    threading.Thread(target=_plan_research, args=(sid, idea, user), daemon=True).start()
    return {"id": sid}


@app.get("/api/plans")
async def api_plans(request: Request):
    """The signed-in user's plans — for the profile / 'My plans' view."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in to see your plans."}, status_code=401)
    paid = _is_paid(authed["email"], verified=True)
    plans = [{"id": p["id"], "idea": p["idea"], "status": p["status"], "step": p["step"],
              "created_at": p["created_at"], "done": p["status"] == "done",
              "shared": bool(p.get("shared"))}
             for p in store.plan_list(authed["email"])]
    return {"email": authed["email"], "paid": paid, "total": planner.N, "plans": plans}


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
        with _run_slot(s.get("user")):
            # If a board is set it vets each finalized section and its takeaway steers the next draft
            # (planner.advance runs the board inline). cost includes any board review.
            upd = planner.advance(s, choice, body.get("note"), mock=MOCK,
                                  directors=s.get("directors") or None)
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
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
    if (gate := _kill_gate(s)):   # hard gate: a killed idea can't roll forward into a full plan
        return gate
    tree = _ensure_tree(s)
    active = tree["nodes"][tree["active"]]
    if active["step"] >= planner.N:
        return JSONResponse({"error": "This plan is already complete."}, status_code=409)
    try:
        with _run_slot(s.get("user")):
            child, cost = planner.forward(planner._working_idea(s), s["research"], active, feedback,
                                          directors=s.get("directors") or None,
                                          founder=planner._founder(s), mock=MOCK)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
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
        with _run_slot(s.get("user")):
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
        return JSONResponse({"error": str(e)}, status_code=500)
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
        with _run_slot(s.get("user")):
            sib, cost = planner.rebranch(planner._working_idea(s), s["research"], prev, feedback,
                                         founder=planner._founder(s), mock=MOCK)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
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
        with _run_slot(s.get("user")):
            sib, cost = planner.rebranch(planner._working_idea(s), s["research"], active, feedback,
                                         founder=planner._founder(s), mock=MOCK)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    node = _new_node(sib, active.get("parent"))   # sibling of the active node → same step, new branch
    tree["nodes"][node["id"]] = node
    if active.get("parent"):
        tree["nodes"][active["parent"]].setdefault("children", []).append(node["id"])
    tree["active"] = node["id"]
    _meter(s.get("user"), cost)
    _fold_usage(sid, s, cost, toks, tree=tree, **_mirror(tree))
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
        with _run_slot(s.get("user")):
            res, cost = planner.ask_expert(s["idea"], s.get("files") or {}, archetype,
                                           body.get("question") or "", mock=MOCK)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
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
        with _run_slot(s.get("user")):
            res, cost = board.convene(work_idea, plan_text, question, directors, mock=MOCK)
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
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
        with _run_slot(s.get("user")):
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
    lays out the active branch's sections, and appends the graded-research evidence exhibit. No
    paywall (monetization in flux). Builds from `s["files"]` = the final decision set."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if s["status"] != "done":
        return JSONResponse({"error": "plan isn't finished yet"}, status_code=400)
    if (wall := _key_wall(s)):
        return wall
    try:
        with _run_slot(s.get("user")):
            plan, cost = plan_pdf.synthesize(s, mock=MOCK)
            data = plan_pdf.render(plan, style=(request.query_params.get("style") or "filg"))
            toks = pipeline.LEDGER.tokens()
    except BusyError as be:
        return _busy_response(be)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse({"error": f"Could not build the PDF: {e}"}, status_code=500)
    _meter(s.get("user"), cost)
    nc, nt = _fold_usage(sid, s, cost, toks)   # binary response → echo usage via headers for the meter
    fn = f"{_slug(s.get('idea'))}-business-plan.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"',
                             "X-FILG-Cost": str(nc), "X-FILG-Tokens": str(nt)})


def _render_page() -> str:
    """The single-page app shell. Served at `/` and at clean deep-link paths like `/plan/{id}` so the
    frontend can use real History-API URLs (no `#`) and direct-load / refresh still works."""
    cfg = json.dumps({"authEnabled": auth.AUTH_ENABLED, "billingEnabled": billing.BILLING_ENABLED,
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
:root{--bg:#FFFDF7;--ink:#1B1726;--muted:#6E6878;--line:#EFE9DD;--card:#fff;--coral:#FF6B4A;--coral-d:#E85535;--sky:#2E7CF6;--sun:#FFC23F;--ok-bg:#E7F5EC;--ok:#1E9E5A;--warn-bg:#FFF3E0;--warn:#C9740B}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 "Nunito",ui-rounded,"SF Pro Rounded","Segoe UI",system-ui,sans-serif}
.page{max-width:1140px;margin:0 auto;padding:22px 22px 72px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.topright{display:flex;align-items:center;gap:14px}
.meter{display:inline-flex;align-items:center;gap:6px;background:var(--card);border:1.5px solid var(--line);color:var(--muted);font-size:12.5px;font-weight:700;padding:5px 11px;border-radius:999px;cursor:default;font-variant-numeric:tabular-nums}
.meter[hidden]{display:none}   /* the author .meter rule would otherwise override the UA [hidden]=display:none, leaking an empty pill */
.meter .m-dot{width:7px;height:7px;border-radius:50%;background:var(--muted);flex:none;transition:background .3s}
.meter.live .m-dot{background:var(--ok);animation:mpulse 1.1s ease-in-out infinite}
@keyframes mpulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.78)}}
.meter b{color:var(--ink);font-weight:800}
h1.logo{font-size:30px;font-weight:800;letter-spacing:-.02em;margin:0}.logo span{color:var(--coral)}
.logobtn{display:inline-flex;align-items:center;gap:8px;background:none;border:0;padding:0;margin:0;font:inherit;color:inherit;letter-spacing:inherit;cursor:pointer}.logobtn:hover{opacity:.85}
.logomark{width:1.05em;height:1.05em;flex:none}
.sub{color:var(--muted);margin:0 0 18px;font-size:17px}
textarea,input{width:100%;padding:14px 16px;border:1.5px solid var(--line);border-radius:14px;font:inherit;background:#fff;margin-bottom:12px}
textarea:focus,input:focus{outline:none;border-color:var(--sky)}textarea{min-height:120px;resize:vertical}
textarea::placeholder,input::placeholder{color:#B7B1BF;opacity:1}
button{background:var(--coral);color:#fff;border:0;font:inherit;font-weight:800;padding:13px 22px;border-radius:14px;cursor:pointer;transition:transform .06s,filter .15s}
button:hover{filter:brightness(1.04)}button:active{transform:translateY(1px)}button:disabled{opacity:.55;cursor:default}
.intake{max-width:680px;margin:28px auto;text-align:center}.intake textarea,.intake input{text-align:left}
.intake h2{font-size:30px;font-weight:800;letter-spacing:-.02em;margin:0 0 6px}.intake .go{font-size:17px;padding:15px 26px}
.brandfoot{margin:40px auto 0;padding-top:16px;border-top:1px solid var(--line);max-width:420px;font-family:"Fraunces",Georgia,serif;font-style:italic;font-size:14px;color:var(--muted);opacity:.6;line-height:1.35}
.brandfoot cite{font-style:normal;font-family:inherit;font-size:12px;opacity:.85}
.err{color:var(--coral-d);margin-top:12px;font-weight:700}
.authbar{display:flex;align-items:center;gap:14px;font-size:14px}
.authbar .who{color:var(--muted)}.authbar b{color:var(--coral)}
.authbar .link{background:none;color:var(--sky);padding:0;font-weight:700;font-size:14px}
.authbar .up{background:var(--sun);color:#3a2c00;padding:8px 14px;border-radius:10px;font-size:13px}
.note-banner{background:var(--ok-bg);border:1px solid #cfe9d8;border-radius:14px;padding:12px 16px;font-size:14px;margin-bottom:16px;display:none}
.jokecard{margin:16px 0 0;text-align:left;background:#fff;border:1.5px solid var(--sun);border-radius:16px;padding:18px 20px}
.jokecard h3{margin:0 0 8px;font-size:18px;font-weight:800}
.jokecard .jbody{font-size:14.5px;color:var(--ink)}.jokecard .jbody p{margin:0 0 8px}
.jokecard button{margin-top:6px;background:var(--ink)}
.workspace{display:grid;grid-template-columns:300px 1fr;gap:24px;align-items:start}
.side{position:sticky;top:18px;display:flex;flex-direction:column;gap:16px}
.sec{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px}
.sec h3{font-size:12px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:800}
.ev{list-style:none;padding:0;margin:0}.ev li{padding:9px 0;border-top:1px dashed var(--line);font-size:13px}.ev li:first-child{border-top:0}
/* collapsible sidebar sections (accordion), header toggles, body shows when .open */
.sec.collap .sechead{display:flex;align-items:center;gap:8px;width:100%;background:none;border:0;padding:0;margin:0;cursor:pointer;text-align:left;font:inherit}
.sec.collap .sechead h3{margin:0;flex:1}
.sec.collap .sechead .caret{color:var(--muted);font-size:12px;transition:transform .15s}
.sec.collap.open .sechead .caret{transform:rotate(90deg)}
.sec.collap .secbody{display:none;margin-top:12px}
.sec.collap.open .secbody{display:block}
/* chat with your plan */
.chatlog{display:flex;flex-direction:column;gap:8px;max-height:42vh;overflow-y:auto;margin-bottom:10px}
.chatlog:empty{display:none;margin:0}
.cmsg{font-size:13px;line-height:1.45;padding:8px 11px;border-radius:13px;max-width:92%}
.cmsg.user{background:#eef4ff;border:1px solid #dbe6ff;color:var(--ink);align-self:flex-end;border-bottom-right-radius:4px}
.cmsg.bot{background:var(--bg);border:1px solid var(--line);color:var(--ink);align-self:flex-start;border-bottom-left-radius:4px}
.cmsg.bot p:first-child{margin-top:0}.cmsg.bot p:last-child{margin-bottom:0}
.cmsg .think{color:var(--muted);font-style:italic}
.chatstart{display:flex;flex-direction:column;gap:6px;margin-bottom:10px}
.chatstart button{background:#fff;border:1.5px solid var(--line);color:var(--ink);font-size:12.5px;font-weight:700;padding:8px 11px;border-radius:11px;text-align:left;width:100%}
.chatstart button:hover{border-color:var(--sky);color:var(--sky)}
.chatsend{width:100%;background:var(--sky);font-size:14px}
.ev .note{color:var(--muted);font-size:12px}
.badge{font-size:10px;font-weight:800;padding:1px 7px;border-radius:20px}.b-ok{background:var(--ok-bg);color:var(--ok)}.b-warn{background:var(--warn-bg);color:var(--warn)}
.tree{list-style:none;padding:0;margin:0}.tree li{padding:10px 0;border-top:1px solid var(--line)}.tree li:first-child{border-top:0}
.tree .f{display:flex;align-items:center;gap:10px;font-size:14px}
.tree .built{cursor:pointer}.tree .built .nm{font-weight:700}.tree .pending{opacity:.55}.tree .active .nm{color:var(--sky);font-weight:800}
.tree .ic{width:20px;height:20px;flex:none;display:grid;place-items:center;border-radius:50%;font-size:12px}
.tree .done .ic{background:var(--ok-bg);color:var(--ok)}.tree .active .ic{background:#e6efff;color:var(--sky);animation:pulse 1.1s infinite}
.tree .pending .ic{border:1.5px solid var(--line);color:var(--muted)}
.tree .nm .s{display:block;font-size:11px;color:var(--muted);font-weight:500}
.tree li.built button.f{padding:4px 6px;border-radius:9px}
.tree li.viewing button.f{background:#eef4ff}.tree li.viewing .nm{color:var(--sky)}
.tree li.justdone{border-radius:9px;box-shadow:0 0 0 1.5px rgba(46,124,246,.28);animation:readypulse 1.5s ease-in-out infinite}
@keyframes readypulse{0%,100%{box-shadow:0 0 0 1.5px rgba(46,124,246,.22)}50%{box-shadow:0 0 0 3px rgba(46,124,246,.4)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
/* viewer: a built section opened in the main column (not squeezed into the sidebar) */
#viewer .node{position:relative}
.viewer-x{position:absolute;top:16px;right:18px;background:none;color:var(--muted);font-size:24px;line-height:1;padding:0 6px;font-weight:400}
.viewer-x:hover{color:var(--ink)}
.dl{width:100%;background:var(--sun);color:#3a2c00}
.main{min-width:0}
.answer{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:24px;margin-bottom:20px}
.answer h2{font-size:24px;font-weight:800;letter-spacing:-.01em;margin:0 0 4px}.answer .tag{color:var(--muted);font-size:13px;margin:0 0 14px}
.node{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:24px}
.node .eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--coral);font-weight:800}
.node h3{font-size:21px;font-weight:800;margin:5px 0 2px}.node .h3sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.draft{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:16px 18px;font-size:14.5px;margin-bottom:16px}
.md h4,.md h5{font-weight:800;margin:12px 0 4px;line-height:1.3}.md h4{font-size:15px}.md h5{font-size:13.5px}.md>:first-child{margin-top:0}
.md p{margin:0 0 8px}.md p:last-child{margin-bottom:0}.md ul{margin:6px 0 8px;padding-left:20px}.md li{margin:3px 0}
.md a{color:var(--sky)}.md strong{font-weight:800}.md code{background:#fff;border:1px solid var(--line);border-radius:5px;padding:0 4px;font-size:.92em}
.md hr{border:0;border-top:1px solid var(--line);margin:16px 0}
.md table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13.5px;overflow:hidden;border-radius:10px;border:1px solid var(--line)}
.md th,.md td{border-bottom:1px solid var(--line);border-right:1px solid var(--line);padding:8px 11px;text-align:left;vertical-align:top}
.md th:last-child,.md td:last-child{border-right:0}.md tbody tr:last-child td{border-bottom:0}
.md thead th{background:var(--bg);font-weight:800;font-size:12.5px}
.md tbody tr:nth-child(even){background:#fcfbf6}
.lead{font-size:14px;color:var(--muted);margin:0 0 12px}
.branches{display:flex;gap:10px;flex-wrap:wrap}.branches button{flex:1;min-width:130px;font-size:15px;padding:13px 12px}
.b-but{background:var(--sky)}.b-no{background:#fff;color:var(--ink);border:1.5px solid var(--line)}
/* always-open feedback box + Next/Back navigation (replaces the 3 verdict buttons) */
.fbk{margin-top:14px;border:1.5px solid var(--sky);border-radius:14px;padding:14px;background:#fff}
.fbk textarea{margin-bottom:8px}
.killgate{margin-top:14px;border:1.5px solid #f0bdbd;border-radius:14px;padding:14px 16px;background:#fdeaea}
.killgate .kg-head{font-weight:800;color:#c0392b;font-size:15px;margin-bottom:6px}
.killgate .kg-say{margin:0 0 8px;color:var(--ink)}
.killgate .kg-risk{margin:0 0 8px;font-size:13px;color:#7a3b32}
.killgate .kg-q{font-weight:700;margin:0 0 8px}
.killgate textarea{margin-bottom:8px;min-height:84px}
.navrow{display:flex;gap:10px;margin-top:10px}
.navrow button{flex:1}.navrow .b-next{background:var(--sky)}
.navrow .b-back{background:#fff;color:var(--ink);border:1.5px solid var(--line);flex:0 0 auto;min-width:96px}
.ferr{color:var(--coral-d);font-weight:700;font-size:13px;margin-top:8px;min-height:0}
/* decision tree (branches you've explored), hidden until a branch actually exists */
#dtree{display:flex;flex-direction:column;gap:1px}
.dnode{display:block;width:100%;text-align:left;background:none;border:0;font:inherit;color:var(--ink);font-size:12.5px;line-height:1.35;padding:6px 8px;border-radius:9px;cursor:pointer}
.dnode:hover{background:var(--bg)}.dnode.path{font-weight:700}
.dnode.on{background:#eef4ff;color:var(--sky);font-weight:800}
.dnode .ds{display:block;font-size:11px;color:var(--muted);font-weight:500;margin-top:1px}
.dnode.on .ds{color:var(--sky)}
.compose{margin-top:14px;border:1.5px solid var(--sky);border-radius:14px;padding:14px;background:#fff}
.compose .pl{font-weight:700;margin:0 0 8px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 0}
.chip{background:var(--bg);border:1px solid var(--line);color:var(--ink);font-size:13px;font-weight:700;padding:6px 11px;border-radius:20px}
.compose .row{display:flex;gap:8px;margin-top:10px}.compose .row button{flex:none}.compose .ghost{background:#fff;color:var(--muted);border:1.5px solid var(--line)}
.done{background:var(--ok-bg);border:1px solid #cfe9d8;border-radius:14px;padding:16px 18px;font-size:15px}
.planacts{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}.planacts .ghost{background:#fff;color:var(--ink);border:1.5px solid var(--line)}
.addons .ax{display:flex;flex-wrap:wrap;gap:8px}
.addons .ax button{flex:1;min-width:120px;background:#fff;border:1.5px solid var(--line);color:var(--ink);font-size:13px;font-weight:800;padding:9px 10px;text-align:left}
.addons .ax .bl{display:block;font-size:11px;color:var(--muted);font-weight:500}
.bhelp{font-size:12px;color:var(--muted);margin:0 0 10px}
.disc{font-size:11px;color:var(--muted);margin-top:8px}
.vet{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:18px 20px;margin-bottom:16px}
.vet .vhead{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.vet h3{font-size:16px;font-weight:800;margin:0}
.vet .vethead{display:flex;align-items:center;gap:10px;width:100%;background:none;border:0;padding:0;margin:0;cursor:pointer;font:inherit;text-align:left}
.vet .vtitle{font-size:16px;font-weight:800;color:var(--ink)}
.vet .vcaret{margin-left:auto;color:var(--muted);font-size:12px;transition:transform .15s}
.vet.open .vcaret{transform:rotate(90deg)}
.vet .vetbody{display:none;margin-top:12px}.vet.open .vetbody{display:block}
.vet .filgreact{font-family:"Fraunces",Georgia,serif;font-style:italic;font-weight:600;font-size:18px;line-height:1.3;color:var(--ink);margin:0 0 12px}
.verdict{font-size:12px;font-weight:800;padding:3px 11px;border-radius:20px;text-transform:uppercase;letter-spacing:.04em}
.verdict.pursue{background:var(--ok-bg);color:var(--ok)}.verdict.pivot{background:var(--warn-bg);color:var(--warn)}.verdict.kill{background:#fdeaea;color:#c0392b}
.vet .thesis{font-size:14.5px;margin:0 0 10px}.vet .vrow{font-size:13px;color:var(--muted);margin:3px 0}.vet .vrow b{color:var(--ink)}
.bdirs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.bchip{font-size:12px;font-weight:700;padding:5px 10px;border-radius:20px;border:1.5px solid var(--line);background:#fff;cursor:pointer;color:var(--ink)}
.bchip.on{background:#eef4ff;border-color:var(--sky);color:var(--sky)}
.convene{width:100%;background:var(--sky);font-size:14px}
.boardpick{margin:0 0 12px}.boardpick .lab{font-size:13px;color:var(--muted);font-weight:700;margin-bottom:6px;text-align:left}
.boardpick .opts{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-start}
.bround{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:20px 22px;margin-bottom:20px}
.bround h4{font-size:15px;font-weight:800;margin:0 0 12px}
.balloons{display:flex;flex-direction:column;gap:8px}
.balloon{border:1.5px solid var(--line);border-radius:14px;overflow:hidden}
.balloon .bh{display:flex;align-items:center;gap:8px;width:100%;padding:10px 13px;cursor:pointer;font:inherit;font-weight:800;font-size:13.5px;color:var(--ink);background:#fff;border:0;border-radius:0;text-align:left}
.balloon .bh:hover{background:var(--bg)}.balloon .bh .caret{margin-left:auto;color:var(--muted);font-size:12px;transition:transform .12s}
.balloon.open .bh .caret{transform:rotate(90deg)}
.balloon .bb{padding:0 13px 12px;font-size:13.5px;display:none}.balloon.open .bb{display:block}
.changeflag{background:var(--warn-bg);border:1px solid #f4d9a8;border-left:3px solid var(--sun);border-radius:12px;padding:11px 14px;margin:0 0 14px;font-size:13.5px;color:var(--ink)}
.changeflag .cf-l{display:block;font-weight:800;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--warn);margin-bottom:4px}
.takeaway{margin-top:14px;background:var(--ok-bg);border:1px solid #cfe9d8;border-radius:14px;padding:13px 15px;font-size:14px}
.takeaway .tl{font-weight:800;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--ok);margin-bottom:5px}
.takeaway .split{display:block;margin-top:6px;color:var(--muted);font-size:13px}
.drawer-back{position:fixed;inset:0;background:rgba(20,17,14,.38);opacity:0;visibility:hidden;transition:opacity .2s;z-index:40}
.drawer-back.show{opacity:1;visibility:visible}
.drawer{position:fixed;top:0;right:0;height:100vh;width:min(440px,93vw);background:var(--card);border-left:1px solid var(--line);box-shadow:-14px 0 44px rgba(20,17,14,.14);transform:translateX(101%);transition:transform .24s cubic-bezier(.4,0,.2,1);z-index:41;display:flex;flex-direction:column}
.drawer.open{transform:translateX(0)}
.dr-head{display:flex;align-items:center;justify-content:space-between;padding:20px 22px;border-bottom:1px solid var(--line);flex:none}
.dr-head span{font-size:18px;font-weight:800;letter-spacing:-.01em}
.dr-x{background:none;color:var(--muted);font-size:26px;line-height:1;padding:0 6px;font-weight:400}.dr-x:hover{color:var(--ink)}
.dr-body{padding:18px 22px 26px;overflow-y:auto;flex:1}
.dr-sub{color:var(--muted);font-size:13px;margin:0 0 12px}
.dr-go{width:100%;margin-top:2px;background:var(--sky)}
.dr-out{margin-top:18px;font-size:14px;display:none}.dr-out .balloon+.balloon{margin-top:8px}
/* styled modal + toast (replace native confirm/prompt/alert) */
.modal-back{position:fixed;inset:0;background:rgba(20,17,14,.38);opacity:0;visibility:hidden;transition:opacity .18s;z-index:50}
.modal-back.show{opacity:1;visibility:visible}
.modal{position:fixed;left:50%;top:50%;transform:translate(-50%,-46%);width:min(420px,92vw);background:var(--card);border:1px solid var(--line);border-radius:18px;box-shadow:0 24px 60px rgba(20,17,14,.22);padding:22px 24px;z-index:51;opacity:0;visibility:hidden;transition:opacity .18s,transform .18s}
.modal.open{opacity:1;visibility:visible;transform:translate(-50%,-50%)}
.modal h3{font-size:19px;font-weight:800;margin:0 0 8px}
.modal #modal-body{font-size:14.5px;color:var(--muted);margin-bottom:16px}.modal #modal-body p{margin:0}
.modal-actions{display:flex;justify-content:flex-end;gap:10px}
.toasts{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);display:flex;flex-direction:column;gap:8px;z-index:60;align-items:center;pointer-events:none}
.toast{background:var(--ink);color:#fff;padding:11px 18px;border-radius:12px;font-size:14px;font-weight:700;box-shadow:0 8px 24px rgba(20,17,14,.2);transition:opacity .3s,transform .3s;max-width:90vw}
.toast.err{background:var(--coral-d)}.toast.out{opacity:0;transform:translateY(8px)}
/* Reusable AI-activity ticker, a pinned, non-covering footer. Each running task is its OWN collapsible
   card (header = what it's doing + its spew underneath), so parallel ops stack and stay legible. A
   card removes itself when its task finishes; the footer hides once the last one is gone. */
.activity{position:fixed;left:0;right:0;bottom:0;z-index:80;transform:translateY(115%);transition:transform .28s cubic-bezier(.4,0,.2,1);background:var(--ink);color:#fff;box-shadow:0 -8px 30px rgba(20,17,14,.18)}
.activity.show{transform:translateY(0)}
.afoot-bar{display:flex;align-items:center;justify-content:center;gap:9px;width:100%;background:rgba(255,255,255,.05);border:0;border-bottom:1px solid rgba(255,255,255,.1);color:#fff;font:inherit;cursor:pointer;padding:5px 0}
.afoot-grip{width:34px;height:4px;border-radius:3px;background:rgba(255,255,255,.4)}
.afoot-caret{font-size:11px;opacity:.65;transition:transform .2s}
.activity.min .afoot-caret{transform:rotate(180deg)}
.activity.min .alog{display:none}
.alog{max-width:1140px;margin:0 auto;padding:10px 22px;display:flex;flex-direction:column;gap:7px;max-height:42vh;overflow-y:auto;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.3) transparent}
.alog::-webkit-scrollbar{width:6px}.alog::-webkit-scrollbar-thumb{background:rgba(255,255,255,.25);border-radius:6px}
.atask{border:1px solid rgba(255,255,255,.13);border-radius:11px;background:rgba(255,255,255,.04);overflow:hidden;transition:opacity .25s}
.atask.done{opacity:.6}
.ah{display:flex;align-items:center;gap:9px;width:100%;background:none;border:0;color:#fff;font:inherit;font-size:13px;font-weight:700;padding:8px 13px;cursor:pointer;text-align:left}
.ah .astat{width:13px;flex:none;text-align:center;color:var(--sun)}
.ah .astat::before{content:"\\25cf";font-size:10px;animation:blink 1s steps(1) infinite}
.atask.done .ah .astat::before{content:"\\2713";color:#43d17f;font-size:13px;animation:none}
.ah .alabel{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ah .caret{flex:none;font-size:11px;opacity:.6;transition:transform .2s}
.atask.collapsed .ah .caret{transform:rotate(-90deg)}
.atask.collapsed .abody{display:none}
.abody{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;line-height:1.5;padding:0 13px 9px 13px;max-height:108px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.3) transparent}
.abody::-webkit-scrollbar{width:5px}.abody::-webkit-scrollbar-thumb{background:rgba(255,255,255,.22);border-radius:6px}
.aline{display:flex;align-items:center;gap:9px;padding:1px 0;background:none;color:rgba(255,255,255,.55)}
.aline.done{color:rgba(255,255,255,.74)}
.aline.active{color:#fff;font-weight:600}
.aline .aglyph{width:12px;flex:none;text-align:center;color:var(--sun)}
.aline.active .aglyph{animation:blink 1s steps(1) infinite}
.aline.active .aglyph::before{content:"\\203A";font-weight:800}
.aline.done .aglyph::before{content:"\\2713";color:#43d17f}
.aline .atext{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@keyframes blink{50%{opacity:.3}}
.authgate{margin:6px 0 2px}.authgate button{width:100%;margin-bottom:8px}
.gbtn{display:flex;align-items:center;justify-content:center;gap:10px;background:#fff;color:var(--ink);border:1.5px solid var(--line);font-weight:800}
.gicon{width:18px;height:18px;flex:none}
.authgate .or{color:var(--muted);font-size:13px;margin:4px 0 0}
.keysteps{margin:0 0 12px;padding-left:20px;color:var(--muted);font-size:13px;line-height:1.7}
.keysteps a{color:var(--sky);font-weight:700}
.plans{max-width:760px;margin:8px auto}.plans h2{font-size:24px;font-weight:800;margin:8px 0 4px}
.pcard{display:flex;justify-content:space-between;align-items:center;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin-bottom:12px}
.pcard .idea{font-weight:700;font-size:15px}.pcard .meta{color:var(--muted);font-size:12px;margin-top:2px}
.pcard .act{display:flex;align-items:center;gap:8px;flex:none}.pcard .act button{font-size:13px;padding:9px 14px}
.pill{font-size:11px;font-weight:800;padding:2px 9px;border-radius:20px;background:var(--warn-bg);color:var(--warn)}.pill.done{background:var(--ok-bg);color:var(--ok)}
.integrations{background:var(--card);border:1px dashed var(--line);border-radius:16px;padding:16px 18px;margin-top:18px;color:var(--muted);font-size:14px}
.empty{color:var(--muted);text-align:center;margin:30px 0}
@media(max-width:820px){.workspace{grid-template-columns:1fr}.side{position:static}}
/* ── accessibility ───────────────────────────────────────────── */
a:focus-visible,button:focus-visible,input:focus-visible,textarea:focus-visible,[tabindex]:focus-visible{outline:2.5px solid var(--sky);outline-offset:2px}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.tree button.f{width:100%;background:none;border:0;font:inherit;color:inherit;text-align:left;cursor:pointer;padding:0}
.tree button.f:hover .nm{color:var(--sky)}
</style></head><body><div class=page>
<div class=top><h1 class=logo><button type=button class=logobtn onclick=newPlan() aria-label="FILG, start a new idea"><svg class=logomark viewBox="0 0 32 32" aria-hidden=true><rect width=32 height=32 rx=8 fill=#FF6B4A></rect><path d="M16 4c-3.2 2.8-4.3 7.4-4.3 11.8v3.2h8.6v-3.2C20.3 11.4 19.2 6.8 16 4z" fill=#fff></path><circle cx=16 cy=12 r=2.1 fill=#2E7CF6></circle><path d="M11.7 15.5 8.6 20.5l3.1-1.3z" fill=#fff></path><path d="M20.3 15.5 23.4 20.5l-3.1-1.3z" fill=#fff></path><path d="M13.6 19.5h4.8L16 25.5z" fill=#FFC23F></path></svg>FI<span>LG</span></button></h1><div class=topright><button type=button class=meter id=meter hidden title="Token usage this session (resets when you reload)"></button><div class=authbar id=authbar></div></div></div>
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
<button id=go class=go onclick=start()>Build my plan →</button>
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
<div class=toasts id=toasts aria-live=polite></div>
<div class=activity id=activity aria-live=polite aria-hidden=true><button type=button class=afoot-bar onclick="this.parentNode.classList.toggle('min')" aria-label="Collapse or expand the activity log"><span class=afoot-grip aria-hidden=true></span><span class=afoot-caret aria-hidden=true>\\u25be</span></button><div class=alog id=activity-log></div></div>
<div class=workspace id=workspace style="display:none">
<aside class=side>
<div class=sec><h3>Your plan</h3><ul class=tree id=tree></ul>
<button id=dl class=dl onclick=download() style="display:none;margin-top:12px">⬇ Download plan (PDF)</button>
</div>
<div class="sec collap" id=dtreesec style="display:none"><button type=button class=sechead aria-expanded=false onclick="toggleSec('dtreesec')"><h3>Decision tree</h3><span class=caret aria-hidden=true>▸</span></button>
<div class=secbody><p class=bhelp>Each step is a node. Go <b>Back</b> to branch and try another direction; click any node to hop to it. The active branch is highlighted.</p>
<div id=dtree></div></div></div>
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
<div id=viewer style="display:none"></div>
<div id=vet></div>
<div id=answer></div>
<div id=node></div>
<div id=boardround></div>
<div class=err id=err2></div>
</main>
</div>
<script>
const CFG=window.FILG||{authEnabled:false,billingEnabled:false};
let sb=null, session=null, me=null;
function authHeaders(){return session?{'Authorization':'Bearer '+session.access_token}:{};}
let SID=null;
// ── BYOK: mandatory after the one free welcome plan ──────────────────────────
let HAS_KEY=false, WELCOME_PROMPTED=false;
async function loadKey(){            // refresh whether this user has a saved key
  if(!CFG.byokEnabled){HAS_KEY=false;return;}
  try{const r=await fetch('/api/key',{headers:authHeaders()});const d=await r.json();HAS_KEY=!!(d&&d.key);}
  catch(e){HAS_KEY=false;}
}
function requireKey(){               // gate any API-calling button: no key → open the key modal
  if(CFG.byokEnabled&&!HAS_KEY){keyModal();return false;}
  return true;
}
function maybePromptKey(){           // welcome plan finished → require a key, opening the modal once per load
  if(WELCOME_PROMPTED||!CFG.byokEnabled||HAS_KEY)return;
  WELCOME_PROMPTED=true;keyModal();
}
async function start(){
  const idea=document.getElementById('idea').value.trim(), email=document.getElementById('email').value.trim();
  const go=document.getElementById('go'), err=document.getElementById('err');
  err.textContent='';document.getElementById('joke').innerHTML='';
  if(CFG.authEnabled&&!session){authModal();return;}   // signed-out → prompt them with the sign-in modal
  const body={idea}; if(!session) body.email=email;   // signed in → identity from the token
  if(BOARD.length) body.directors=BOARD;               // optional Board of Directors → vets each step
  go.disabled=true; go.textContent='Researching…'; ACT_RESEARCH=false;
  try{
    const r=await fetch('/api/plan/start',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.gibberish){showJoke(d);go.disabled=false;go.textContent='Build my plan →';return;}  // nonsense → roast, no run
    if(!r.ok){err.textContent=d.error||'Something went wrong.';if(d.needKey)err.innerHTML+=' <a href=# onclick="keyModal();return false">Add your key →</a>';else if(d.upgrade)err.innerHTML+=' <a href=# onclick="upgrade();return false">Upgrade →</a>';go.disabled=false;go.textContent='Build my plan →';return;}
    SID=d.id;meterBaseline(d.id);   // baseline at 0 so this run's tokens fully count as research streams in
    document.getElementById('intake').style.display='none';
    document.getElementById('workspace').style.display='grid';
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
  renderTree(s);renderAddons(s);     // show the plan outline immediately, even while researching
  if(s.status==='researching'){
    if(!ACT_RESEARCH){ACT_ID=Activity.open('Researching + grading your market');Activity.push(ACT_ID,'Spinning up your research');ACT_RESEARCH=true;ACT_PROG_N=0;}
    const prog=s.progress||[];                                   // real receipts from the engine, streamed
    for(let i=ACT_PROG_N;i<prog.length;i++)Activity.push(ACT_ID,prog[i]);
    ACT_PROG_N=Math.max(ACT_PROG_N,prog.length);
    document.getElementById('node').innerHTML='<div class=node><span class=eyebrow>Working</span><h3>Researching + grading your market…</h3><p class=lead>Pulling sources and grading every number, so vendor spin gets labeled, not laundered. About 1 to 2 minutes. Watch the receipts spew in below.</p></div>';
    say('Researching and grading your market.');
    setTimeout(poll,1500);return;
  }
  if(ACT_RESEARCH){
    const prog=s.progress||[]; for(let i=ACT_PROG_N;i<prog.length;i++)Activity.push(ACT_ID,prog[i]);  // flush any final lines
    Activity.done(ACT_ID,s.status==='error'?'Hit a snag.':'Research graded. Building your plan.');ACT_RESEARCH=false;ACT_ID=null;
  }
  render(s);
  if(s.status!=='error')maybePromptKey();   // welcome plan is in → require a key to go further
}
let ACT_RESEARCH=false, ACT_PROG_N=0, ACT_ID=null;
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
  el.innerHTML=`<span class=m-dot></span><b>${fmtTokens(tok)}</b> tokens · <b>$${cost.toFixed(d)}</b>`;
}
function render(s){
  meterTick(s);   // tick the session usage meter off this plan's cumulative cost/tokens
  if(s.status==='error'){
    document.getElementById('node').innerHTML='<div class=node><h3>Hit a snag</h3><p class=lead>'+esc(s.error)+'</p><button type=button onclick=newPlan()>Start over</button></div>';
    say('Something went wrong: '+(s.error||'')); return;
  }
  renderResearch(s);renderVet(s);renderAnswer(s);renderTree(s);renderNode(s);renderAddons(s);renderBoard(s);renderBoardRound(s);renderDecisionTree(s);renderChat(s);syncSidebar(s);
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
    `<div class=balloons>${balloons}</div>`+
    `<div class=takeaway><div class=tl>Board takeaway</div>${esc(r.verdict||'')}${split}</div></div>`;
}
function renderResearch(s){
  const rows=(s.research&&s.research.rows)||[];
  if(!rows.length){document.getElementById('research').innerHTML='<p style="color:var(--muted);font-size:13px;margin:0">Grading sources…</p>';return;}
  document.getElementById('research').innerHTML='<ul class=ev>'+rows.map(x=>`<li>${x.mark==='ok'?'✅':'⚠️'} ${esc(x.text)} <span class="badge ${x.mark==='ok'?'b-ok':'b-warn'}">${x.mark==='ok'?'cited':'vendor'}</span><br><span class=note>${esc(host(x.url))}, ${esc(x.note)}</span></li>`).join('')+'</ul>';
}
function renderAnswer(s){
  const p=s.research&&s.research.prose; if(!p)return;
  document.getElementById('answer').innerHTML=`<h2>${esc(p.title)}</h2><p class=tag>Your offer, with the research graded, vendor spin labeled, not laundered.</p><p><b>What you'd sell:</b> ${esc(p.offer)}</p><p><b>How you'd sell it:</b> ${esc(p.gtm)}</p>`;
}
let BUILT={}, SECMETA={}, VIEWING=null;
function renderTree(s){
  BUILT={}; SECMETA={}; (s.files||[]).forEach(f=>BUILT[f.path]=f.content);
  (s.sections||[]).forEach(sec=>SECMETA[sec.file]={title:sec.title,sub:sec.sub});
  const step=s.step==null?-1:s.step;
  let lastBuilt=-1; (s.sections||[]).forEach((sec,i)=>{if(BUILT[sec.file]!=null)lastBuilt=i;});
  document.getElementById('tree').innerHTML=(s.sections||[]).map((sec,i)=>{
    const nm=`<span class=nm>${esc(sec.title)}<span class=s>${esc(sec.sub||'')}</span></span>`;
    if(BUILT[sec.file]!=null){
      // most-recently-finished step pulses while building, signalling the next is ready
      const cls='done built'+(VIEWING===sec.file?' viewing':'')+((!s.done&&i===lastBuilt)?' justdone':'');
      return `<li class="${cls}" data-file="${esc(sec.file)}"><button type=button class=f aria-label="Open ${esc(sec.title)} in the main panel" onclick="viewSection('${esc(sec.file)}')"><span class=ic aria-hidden=true>✓</span>${nm}</button></li>`;
    }
    if(!s.done&&i===step){return `<li class=active><div class=f><span class=ic aria-hidden=true>✍︎</span>${nm}</div></li>`;}
    return `<li class=pending><div class=f><span class=ic aria-hidden=true>○</span>${nm}</div></li>`;
  }).join('');
  const dl=document.getElementById('dl'); if(dl)dl.style.display=s.done?'block':'none';
}
function viewSection(file){
  VIEWING=file;
  const v=document.getElementById('viewer'); if(!v)return;
  const meta=SECMETA[file]||{}, content=BUILT[file]||'';
  v.innerHTML=`<div class=node><button type=button class=viewer-x onclick=closeViewer() aria-label="Close">×</button>`+
    `<span class=eyebrow>From your plan</span><h3>${esc(meta.title||'Section')}</h3><p class=h3sub>${esc(meta.sub||'')}</p>`+
    `<div class="draft md">${mdToHtml(content)}</div></div>`;
  v.style.display='block';
  document.querySelectorAll('#tree li[data-file]').forEach(li=>li.classList.toggle('viewing',li.getAttribute('data-file')===file));
  v.scrollIntoView({behavior:'smooth',block:'start'});
}
function closeViewer(){VIEWING=null;const v=document.getElementById('viewer');if(v){v.style.display='none';v.innerHTML='';}document.querySelectorAll('#tree li.viewing').forEach(li=>li.classList.remove('viewing'));}
function renderNode(s){
  const n=document.getElementById('node');
  if(s.status==='researching')return;
  if(s.done){n.innerHTML='<div class=node><div class=done>🎉 <b>Your plan is ready</b>, all '+s.total+' parts. This is your plan\\'s home: <b>download the PDF</b> on the left, <b>chat with your plan</b> in the sidebar to pressure-test it, or share it.</div>'+
    '<div class=planacts><button type=button onclick=download()>⬇ Download (PDF)</button><button type=button class=ghost onclick="sharePlan(SID)">🔗 Share</button></div></div>';return;}
  const p=s.proposal; if(!p){n.innerHTML='';return;}
  const sec=(s.sections||[]).find(x=>x.title===p.title)||{};
  const intro=s.step===0?`<p class=lead>We build your plan in ${s.total} parts, one at a time, your call on each (watch them fill in on the left). First up:</p>`:'';
  const changeFlag=p.change?`<div class=changeflag><span class=cf-l>↳ Your note shaped this</span>${esc(p.change)}</div>`:'';
  const killed=(s.vetting||{}).verdict==='kill';   // hard gate: an unbuildable idea can't roll forward
  const tail=killed?killGateHtml(s):
    `<div class=fbk><label for=feedback class=sr-only>Your feedback on this part</label>`+
    `<textarea id=feedback rows=2 placeholder="Give optional feedback and roll forward, or say what's not landing and regenerate this part."></textarea>`+
    `<div class=chips>${FB_CHIPS.map(x=>`<button type=button class=chip onclick="addChip('${x}')">${esc(x)}</button>`).join('')}</div>`+
    `<div class=navrow><button type=button class=b-back onclick=regenStep() title="Regenerate this part with your feedback (to go back a step, use the decision tree)">\\u21bb Not feeling it</button>`+
    `<button type=button class=b-next onclick=nextStep()>I'm with you →</button></div>`+
    `<div class=ferr id=ferr></div></div>`;
  n.innerHTML=`<div class=node><span class=eyebrow>Your plan · part ${s.step+1} of ${s.total}</span><h3>${esc(p.title)}</h3><p class=h3sub>${esc(sec.sub||'')}</p>`+
    changeFlag+intro+
    `<div class="draft md">${mdToHtml(p.draft)}</div>`+tail;
}
function killGateHtml(s){
  const v=s.vetting||{};
  const q=esc((s.shaped||{}).clarifying_question||'Name one real skill, asset, or audience you already have, and who would pay for it.');
  const risk=v.biggest_risk?`<p class=kg-risk><b>The gap:</b> ${esc(v.biggest_risk)}</p>`:'';
  return `<div class=killgate><div class=kg-head>\\u26d4 Not buildable yet</div>`+
    `<p class=kg-say>${esc(v.reaction||"There isn't an idea here to build on yet. To keep going, give the gate something real to work with.")}</p>`+
    risk+`<p class=kg-q>${q}</p>`+
    `<label for=substance class=sr-only>Add a real skill, asset, or buyer</label>`+
    `<textarea id=substance rows=3 placeholder="e.g. 'I've run paid ads for SaaS for 4 years and I know founders who need it.' Name a real skill, plus who would pay."></textarea>`+
    `<div class=navrow><button type=button class=b-back onclick=startOver()>Start over</button>`+
    `<button type=button class=b-next onclick=reCheck()>Re-check my idea →</button></div>`+
    `<div class=ferr id=ferr></div></div>`;
}
const FB_CHIPS=["go bolder","narrower niche","cheaper entry","B2B only","more specific","add an upsell"];
function addChip(txt){const t=document.getElementById('feedback'); if(!t)return; t.value=(t.value?t.value.replace(/\\s*$/,'')+', ':'')+txt; t.focus();}
function _navBusy(){const n=document.getElementById('node');if(n)n.querySelectorAll('button').forEach(b=>b.disabled=true);const f=document.getElementById('ferr');if(f)f.textContent='';document.getElementById('err2').textContent='';}
function _navFree(){const n=document.getElementById('node');if(n)n.querySelectorAll('button').forEach(b=>b.disabled=false);}
function fbErr(msg){const f=document.getElementById('ferr');if(f)f.textContent=msg;else document.getElementById('err2').textContent=msg;}
// Min-dwell so the spew registers even on fast (mock) responses, without slowing real builds much.
function _aiRun(url,body){   // fetch + a min-show delay; the caller owns its Activity track
  return Promise.all([
    fetch(url,{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(body)}),
    new Promise(res=>setTimeout(res,850))
  ]).then(([r])=>r);
}
async function nextStep(){
  if(!requireKey())return;
  const fb=(document.getElementById('feedback')||{}).value||'';
  _navBusy();
  const steps=[]; if(fb.trim())steps.push("Folding in your note");
  steps.push("Drafting the next part of your plan","Checking it against your graded research");
  const aid=Activity.start(steps,1200,'Building the next part');
  try{
    const r=await _aiRun('/api/plan/'+SID+'/next',{feedback:fb});
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
    Activity.done(aid,cleared?'Cleared. You can build now.':'Still not enough to build on.');render(s);
  }catch(e){Activity.stop(aid);fbErr('Network error.');_navFree();}
}
function startOver(){try{localStorage.removeItem('filg_idea');}catch(e){}location.href='/';}   // clean intake
async function backStep(){
  if(!requireKey())return;
  const fb=((document.getElementById('feedback')||{}).value||'').trim();
  if(!fb){fbErr('Add a quick note on what to change, a note is required to go back a step.');const t=document.getElementById('feedback');if(t)t.focus();return;}
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
  const fb=((document.getElementById('feedback')||{}).value||'').trim();
  if(!fb){fbErr("Tell me what's not landing — a rework needs a note to steer it.");const t=document.getElementById('feedback');if(t)t.focus();return;}
  if(REDRAFTS>=2){   // they keep mashing it — nudge toward backing up via the decision tree
    const ok=await uiConfirm('Still not feeling it?',"We can regenerate this part all day. If a rework keeps missing, try backing up to an earlier part from the decision tree on the left. Regenerate again?",'Regenerate anyway');
    if(!ok)return;
  }
  _navBusy();
  const aid=Activity.start(["Re-reading your note","Regenerating this part from a different angle"],1200,'Regenerating this part');
  try{
    const r=await _aiRun('/api/plan/'+SID+'/redraft',{feedback:fb});
    const s=await r.json();
    if(!r.ok){Activity.stop(aid);fbErr(s.error||'Something went wrong.');_navFree();return;}
    REDRAFTS++;
    Activity.done(aid,'Reworked this part.');render(s);
  }catch(e){Activity.stop(aid);fbErr('Network error.');_navFree();}
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
let DTREE_STEP=-99;
function renderDecisionTree(s){
  const sec=document.getElementById('dtreesec'),box=document.getElementById('dtree');
  if(!sec||!box)return;
  const t=s.tree;
  if(!t||!t.show){sec.style.display='none';return;}
  sec.style.display='';
  const dstep=s.done?9999:(s.step==null?-1:s.step);   // auto-open the tree on each new step
  if(dstep!==DTREE_STEP){DTREE_STEP=dstep;setOpen('dtreesec',true);}
  const nodes=t.nodes||[],byId={},kids={};
  nodes.forEach(n=>{byId[n.id]=n;kids[n.id]=[];});
  nodes.forEach(n=>{if(n.parent!=null&&kids[n.parent])kids[n.parent].push(n.id);});
  const path={}; let cur=t.active; while(cur!=null&&byId[cur]){path[cur]=1;cur=byId[cur].parent;}
  const roots=nodes.filter(n=>n.parent==null).map(n=>n.id);
  function row(id,depth){
    const n=byId[id];
    const cls='dnode'+(id===t.active?' on':'')+(path[id]?' path':'');
    const tag=n.feedback?`<span class=ds>↳ ${esc(n.feedback.slice(0,46))}</span>`:'';
    let h=`<button type=button class="${cls}" style="padding-left:${8+depth*14}px" onclick="gotoNode('${id}')" aria-current="${id===t.active?'true':'false'}">${esc(n.title||('Part '+(n.step+1)))}${tag}</button>`;
    (kids[id]||[]).forEach(c=>{h+=row(c,depth+1);});
    return h;
  }
  box.innerHTML=roots.map(r=>row(r,0)).join('');
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
  el.innerHTML=`<div class="vet${VET_OPEN?' open':''}" id=vetcard><button type=button class=vethead onclick=toggleVet() aria-expanded="${VET_OPEN}">${head}<span class=vtitle>Before we build: the straight read</span><span class=vcaret aria-hidden=true>▸</span></button>`+
    `<div class=vetbody>${react}${thesis}${edge}${alts}${reason}${risk}${test}${cq}</div></div>`;
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
      out.innerHTML=d.directors.map((x,i)=>`<div class=balloon id=dbal_${i}><button type=button class=bh onclick="document.getElementById('dbal_${i}').classList.toggle('open')">💬 See what ${esc(x.first||x.name)}${x.first&&x.name?' ('+esc(x.name)+')':''} says<span class=caret>▸</span></button><div class="bb md">${mdToHtml(x.take)}</div></div>`).join('')+
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
const Activity={
  _seq:0, tracks:{}, n:0,
  _el(){return document.getElementById('activity');},
  _log(){return document.getElementById('activity-log');},
  _show(){const a=this._el();if(a){a.classList.add('show');a.setAttribute('aria-hidden','false');}},
  _mkTask(label){
    const log=this._log(); if(!log)return null;
    const wrap=document.createElement('div'); wrap.className='atask';
    wrap.innerHTML='<button type=button class=ah><span class=astat aria-hidden=true></span><span class=alabel></span><span class=caret aria-hidden=true>\\u25be</span></button><div class=abody></div>';
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
    while(body.children.length>40)body.removeChild(body.firstChild);
    body.scrollTop=body.scrollHeight;
    return li;
  },
  _advance(t,text){ if(t.line)t.line.classList.replace('active','done'); t.line=this._line(t.body,text,false); },
  start(steps,interval,label){
    const id=++this._seq; this.n++; this._show();
    const wrap=this._mkTask(label); const body=wrap?wrap.querySelector('.abody'):null;
    const s=(steps||[]).slice(); let i=0; const t={wrap,body,line:null,timer:null}; this.tracks[id]=t;
    if(s.length)this._advance(t,s[0]);
    t.timer=setInterval(()=>{ if(i<s.length-1){i++;this._advance(t,s[i]);} else {clearInterval(t.timer);t.timer=null;} }, interval||1600);
    return id;
  },
  open(label){ const id=++this._seq; this.n++; this._show(); const wrap=this._mkTask(label); this.tracks[id]={wrap,body:wrap?wrap.querySelector('.abody'):null,line:null,timer:null}; return id; },
  push(id,line){ const t=this.tracks[id]; if(t)this._advance(t,line); },
  done(id,msg){ this._end(id,msg,false); },
  stop(id){ this._end(id,null,true); },
  _end(id,msg,immediate){
    const t=this.tracks[id];
    if(!t){ this._maybeHide(immediate); return; }
    if(t.timer)clearInterval(t.timer);
    if(t.line)t.line.classList.replace('active','done');
    if(msg)this._line(t.body,msg,true);
    if(t.wrap)t.wrap.classList.add('done');
    delete this.tracks[id]; this.n=Math.max(0,this.n-1);
    setTimeout(()=>{ if(t.wrap&&t.wrap.parentNode)t.wrap.parentNode.removeChild(t.wrap); this._maybeHide(false); }, immediate?250:1300);
  },
  _maybeHide(immediate){ if(this.n>0)return; setTimeout(()=>{ if(this.n<=0)this._hide(); }, immediate?0:250); },
  stopAll(){ for(const id in this.tracks){if(this.tracks[id].timer)clearInterval(this.tracks[id].timer);} this.tracks={}; this.n=0; this._hide(); },
  _hide(){ const a=this._el(); if(a){a.classList.remove('show');a.classList.remove('min');a.setAttribute('aria-hidden','true');} const l=this._log(); if(l)l.innerHTML=''; }
};
const RESEARCH_STEPS=["Focusing your idea into one sharp thesis","Spinning up research across the web","Pulling sources on the market and competition","Grading every source for credibility","Flagging vendor-marketing spin","Re-sourcing the headline stats to primary sources","Scoring demand, market, and willingness to pay","Drafting your first offer"];
const PDF_STEPS=["Applying your board's input","Pulling your graded evidence","Building the decision matrix","Laying out a modern, on-brand design","Typesetting your PDF"];
async function download(){
  if(!requireKey())return;
  const aid=Activity.start(PDF_STEPS,1600,'Building your styled PDF');
  const minShow=new Promise(res=>setTimeout(res,2600));   // let the sequence breathe (covers fast mock runs)
  try{
    const [r]=await Promise.all([fetch('/api/plan/'+SID+'/plan.pdf',{headers:authHeaders()}),minShow]);
    if(!r.ok){let d={};try{d=await r.json();}catch(e){} Activity.stop(aid);toast(d.error||'Could not build the PDF.','err');return;}
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
    const paid=me&&me.paid; bar.style.display='';
    bar.innerHTML=`<button class=link onclick=showPlans()>My plans</button>`+
      (CFG.byokEnabled?`<button class=link onclick=keyModal()>🔑 Your key</button>`:'')+
      `<span class=who>${esc(session.user.email)}${paid?' · <b>Operator</b>':''}</span>`+
      (!paid&&CFG.billingEnabled?`<button class="link up" onclick=upgrade()>Upgrade, $39/mo</button>`:'')+
      `<button class=link onclick=signout()>Sign out</button>`;
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
function keyForm(){
  document.getElementById('modal-title').textContent='Bring your own key';
  document.getElementById('modal-body').innerHTML=
    `<p class=or style="margin:0 0 10px">The first plan is on us. Hook up your own OpenRouter key to keep using the full suite of tools: more plans, branches, the board, chat, and PDF export. One key gives you every model plus cited web search, and you pay OpenRouter directly (usually pennies a plan).</p>`+
    `<ol class=keysteps>`+
      `<li><a href="https://openrouter.ai/keys" target=_blank rel=noopener>Open OpenRouter \\u2192 Keys</a> and sign up (free)</li>`+
      `<li>Click <b>Create Key</b> and copy it</li>`+
      `<li>Paste it below and save \\u2014 we\\u2019ll test it before storing</li></ol>`+
    `<label for=keyinput class=sr-only>Your OpenRouter API key</label>`+
    `<input id=keyinput type=password placeholder="sk-or-v1-\\u2026" autocomplete=off spellcheck=false style="margin:4px 0 2px">`+
    `<div class=err id=keyerr></div>`;
  document.getElementById('modal-actions').innerHTML=
    `<button type=button class=ghost onclick="_closeModal()">Cancel</button>`+
    `<button type=button id=keysave onclick="saveKey()">Save &amp; validate</button>`;
  _openModal('#keyinput');
}
async function saveKey(){
  const inp=document.getElementById('keyinput'),btn=document.getElementById('keysave'),er=document.getElementById('keyerr');
  const key=(inp.value||'').trim(); er.textContent='';
  if(key.length<8){er.textContent='That doesn\\u2019t look like a key.';return;}
  btn.disabled=true;btn.textContent='Validating\\u2026';
  try{
    const r=await fetch('/api/key',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({provider:'openrouter',key})});
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
async function upgrade(){
  if(!session){signinEmail();return;}
  try{
    const r=await fetch('/api/checkout',{method:'POST',headers:authHeaders()});
    const d=await r.json();
    if(d.url)location.href=d.url; else toast(d.error||'Could not start checkout.','err');
  }catch(e){toast('Network error starting checkout.','err');}
}
function show(id){['intake','workspace','profile'].forEach(x=>{const e=document.getElementById(x);if(e)e.style.display=(x===id?(x==='workspace'?'grid':'block'):'none');});}
function newPlan(){SIDEBAR_PHASE=null;ACT_RESEARCH=false;VET_OPEN=true;VET_STEPPED=false;DTREE_STEP=-99;Activity.stopAll();closeViewer();SID=null;if(location.pathname!=='/')history.pushState({},'','/');show('intake');renderBoardPick();gateIntake();}
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
  const integ=d.paid?`<div class=integrations><b>Operator integrations</b>, CRM kickstarts &amp; more, coming soon.</div>`:`<div class=integrations>Upgrade to Operator for integrations (CRM kickstarts &amp; more), coming soon.</div>`;
  document.getElementById('profile').innerHTML=`<div class=plans><h2>Your plans</h2><p class=sub>${esc(d.email)} · ${d.paid?'Operator':'Free'}</p>`+
    `<div style="margin:10px 0 16px"><button onclick=newPlan()>+ New plan</button></div>`+rows+integ+`</div>`;
}
async function resume(id){
  SID=id;if(location.pathname!=='/plan/'+id)history.pushState({plan:id},'','/plan/'+id);   // clean URL for any entry point
  show('workspace');SESSION_BOARD=null;SIDEBAR_PHASE=null;VET_OPEN=true;VET_STEPPED=false;DTREE_STEP=-99;
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
  if(q.get('upgraded'))banner('🎉 You\\'re on Operator. Your plans + integrations are unlocked.');
  if(q.get('canceled'))banner('Checkout canceled, no charge. You\\'re still on the free tier.');
  restoreIdea();renderBoardPick();paintMeter();   // show the session meter at 0 from first paint
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
document.addEventListener('keydown',function(e){
  const drawer=document.getElementById('drawer'), modal=document.getElementById('modal');
  const dOpen=drawer&&drawer.classList.contains('open'), mOpen=modal&&modal.classList.contains('open');
  if(e.key==='Escape'){if(mOpen)_closeModal();else if(dOpen)closeDrawer();}
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
</div></body></html>"""
