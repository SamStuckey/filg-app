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
import os
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

from . import auth, billing, planner, store  # noqa: E402 — persistence, auth, billing, plan-builder

MOCK = os.environ.get("FILG_MOCK") == "1"
# FILG_PAID_EMAILS is now only a manual comp/override; real paid status comes from billing.is_paid.
PAID = {e.strip().lower() for e in os.environ.get("FILG_PAID_EMAILS", "").split(",") if e.strip()}


def _is_paid(email: str, verified: bool) -> bool:
    """Paid = a verified user with a live subscription, OR an allowlisted comp. An unverified
    (free-tier, email-only) caller can't be billed-paid, but the comp allowlist still applies."""
    return (verified and billing.is_paid(email)) or (email in PAID)

app = FastAPI(title="FILG")
RUN_LOCK = threading.Lock()         # serialize runs so per-run cost metering stays accurate


def _run_job(job_id: str, idea: str, user: str, mode: str) -> None:
    try:
        with RUN_LOCK:
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
        "sections": [{"file": x["file"], "title": x["title"]} for x in planner.SECTIONS],
        "step": s.get("step", 0), "total": planner.N, "proposal": s.get("proposal"),
        "done": s["status"] == "done", "shared": bool(s.get("shared")),
    }


def _plan_research(session_id: str, idea: str, user: str) -> None:
    try:
        with RUN_LOCK:
            prep = planner.prepare(idea, mock=MOCK)   # intake → research(thesis) → vet → first draft
        usage.record_run(user, prep["research_cost"])           # the metered free run + daily total
        usage.record_spend(prep["cost"] - prep["research_cost"])  # intake + vet + first draft → daily
        store.plan_save(session_id, status="building", research=prep["research"], step=0,
                        proposal=prep["proposal"], shaped=prep["shaped"], vetting=prep["vetting"],
                        cost=prep["cost"])
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()  # full trace → Render stdout logs (client only sees str(e))
        store.plan_save(session_id, status="error", error=str(e))


@app.post("/api/plan/start")
async def api_plan_start(request: Request):
    body = await request.json()
    idea = (body.get("idea") or "").strip()
    if len(idea) < 12:
        return JSONResponse({"error": "Tell me a bit more about the idea."}, status_code=400)
    user, verified = _identity(request, body.get("email"))
    if "@" not in user:
        return JSONResponse({"error": "Enter an email so we can save your plan."}, status_code=400)
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
    if s["status"] != "building":
        return JSONResponse({"error": f"session is {s['status']}"}, status_code=409)
    body = await request.json()
    choice = body.get("choice")
    if choice not in planner.CHOICES:
        return JSONResponse({"error": "pick yes_and / not_quite / okay_but"}, status_code=400)
    try:
        with RUN_LOCK:
            # If a board is set it vets each finalized section and its takeaway steers the next draft
            # (planner.advance runs the board inline). cost includes any board review.
            upd = planner.advance(s, choice, body.get("note"), mock=MOCK,
                                  directors=s.get("directors") or None)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    usage.record_spend(round((upd.get("cost", 0) or 0) - (s.get("cost") or 0), 4))  # draft + board review
    store.plan_save(sid, **upd)
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/ask")
async def api_plan_ask(sid: str, request: Request):
    """Add-on: ask a composite archetype advisor about the plan-in-progress."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    archetype = body.get("archetype")
    if archetype not in planner.ARCHETYPE_KEYS:
        return JSONResponse({"error": "pick an advisor"}, status_code=400)
    try:
        with RUN_LOCK:
            res, cost = planner.ask_expert(s["idea"], s.get("files") or {}, archetype,
                                           body.get("question") or "", mock=MOCK)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    usage.record_spend(cost)
    return res


@app.post("/api/plan/{sid}/board")
async def api_plan_board(sid: str, request: Request):
    """Convene the Board of Directors on the plan-so-far. The standing, multi-advisor version of
    'ask an expert': several composite directors weigh in and FILG synthesizes the collaboration
    matrix (agreement / conflict / net verdict). `directors` in the body re-picks + persists the
    board; otherwise the session's board (or the default starter board) is used."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    picked = body.get("directors")
    directors = [k for k in (picked or s.get("directors") or personas.DEFAULT_BOARD)
                 if k in personas.KEYS] or personas.DEFAULT_BOARD
    question = (body.get("question") or "").strip() or \
        "Vet the plan so far — what's the one thing I should change before continuing?"
    work_idea = planner._working_idea(s)
    plan_text = planner.bundle_markdown(work_idea, s.get("files") or {})
    try:
        with RUN_LOCK:
            res, cost = board.convene(work_idea, plan_text, question, directors, mock=MOCK)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    usage.record_spend(cost)
    if picked:  # persist a freshly chosen board so later steps are vetted by it
        store.plan_save(sid, directors=directors)
    return res


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
    user, verified = _identity(request)
    if not _is_paid(user, verified):
        return JSONResponse(
            {"error": "Unlock the download to get your full plan.", "upgrade": True},
            status_code=402)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", planner.bundle_markdown(s["idea"], s["files"]))
        for path, content in s["files"].items():
            z.writestr(path, content)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="filg-business-plan.zip"'})


@app.get("/", response_class=HTMLResponse)
async def index():
    cfg = json.dumps({"authEnabled": auth.AUTH_ENABLED, "billingEnabled": billing.BILLING_ENABLED,
                      "supabaseUrl": os.environ.get("SUPABASE_URL", ""),
                      "supabaseAnon": (os.environ.get("SUPABASE_PUBLISHABLE_KEY")
                                       or os.environ.get("SUPABASE_ANON_KEY", "")),
                      "archetypes": personas.catalog(), "defaultBoard": personas.DEFAULT_BOARD})
    head = f"<script>window.FILG={cfg}</script>"
    if auth.AUTH_ENABLED:
        head += '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
    return PAGE.replace("__FILG_HEAD__", head)


# ── Single-page plan-builder frontend (brand-aligned; no build step) ─────────
PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>FILG — build your business plan, with receipts</title>
<link rel="icon" href='data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"%3E%3Crect width="32" height="32" rx="8" fill="%23FF6B4A"/%3E%3Cpath d="M16 4c-3.2 2.8-4.3 7.4-4.3 11.8v3.2h8.6v-3.2C20.3 11.4 19.2 6.8 16 4z" fill="%23fff"/%3E%3Ccircle cx="16" cy="12" r="2.1" fill="%232E7CF6"/%3E%3Cpath d="M11.7 15.5 8.6 20.5l3.1-1.3z" fill="%23fff"/%3E%3Cpath d="M20.3 15.5 23.4 20.5l-3.1-1.3z" fill="%23fff"/%3E%3Cpath d="M13.6 19.5h4.8L16 25.5z" fill="%23FFC23F"/%3E%3C/svg%3E'>
__FILG_HEAD__
<style>
:root{--bg:#FFFDF7;--ink:#1B1726;--muted:#6E6878;--line:#EFE9DD;--card:#fff;--coral:#FF6B4A;--coral-d:#E85535;--sky:#2E7CF6;--sun:#FFC23F;--ok-bg:#E7F5EC;--ok:#1E9E5A;--warn-bg:#FFF3E0;--warn:#C9740B}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 "Nunito",ui-rounded,"SF Pro Rounded","Segoe UI",system-ui,sans-serif}
.page{max-width:1140px;margin:0 auto;padding:22px 22px 72px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
h1.logo{font-size:30px;font-weight:800;letter-spacing:-.02em;margin:0}.logo span{color:var(--coral)}
.logobtn{display:inline-flex;align-items:center;gap:8px;background:none;border:0;padding:0;margin:0;font:inherit;color:inherit;letter-spacing:inherit;cursor:pointer}.logobtn:hover{opacity:.85}
.logomark{width:1.05em;height:1.05em;flex:none}
.sub{color:var(--muted);margin:0 0 18px;font-size:17px}
textarea,input{width:100%;padding:14px 16px;border:1.5px solid var(--line);border-radius:14px;font:inherit;background:#fff;margin-bottom:12px}
textarea:focus,input:focus{outline:none;border-color:var(--sky)}textarea{min-height:120px;resize:vertical}
button{background:var(--coral);color:#fff;border:0;font:inherit;font-weight:800;padding:13px 22px;border-radius:14px;cursor:pointer;transition:transform .06s,filter .15s}
button:hover{filter:brightness(1.04)}button:active{transform:translateY(1px)}button:disabled{opacity:.55;cursor:default}
.intake{max-width:680px;margin:28px auto;text-align:center}.intake textarea,.intake input{text-align:left}
.intake h2{font-size:30px;font-weight:800;letter-spacing:-.02em;margin:0 0 6px}.intake .go{font-size:17px;padding:15px 26px}
.err{color:var(--coral-d);margin-top:12px;font-weight:700}
.authbar{display:flex;align-items:center;gap:14px;font-size:14px}
.authbar .who{color:var(--muted)}.authbar b{color:var(--coral)}
.authbar .link{background:none;color:var(--sky);padding:0;font-weight:700;font-size:14px}
.authbar .up{background:var(--sun);color:#3a2c00;padding:8px 14px;border-radius:10px;font-size:13px}
.note-banner{background:var(--ok-bg);border:1px solid #cfe9d8;border-radius:14px;padding:12px 16px;font-size:14px;margin-bottom:16px;display:none}
.workspace{display:grid;grid-template-columns:300px 1fr;gap:24px;align-items:start}
.side{position:sticky;top:18px;display:flex;flex-direction:column;gap:16px}
.sec{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px}
.sec h3{font-size:12px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:800}
.ev{list-style:none;padding:0;margin:0}.ev li{padding:9px 0;border-top:1px dashed var(--line);font-size:13px}.ev li:first-child{border-top:0}
.ev .note{color:var(--muted);font-size:12px}
.badge{font-size:10px;font-weight:800;padding:1px 7px;border-radius:20px}.b-ok{background:var(--ok-bg);color:var(--ok)}.b-warn{background:var(--warn-bg);color:var(--warn)}
.tree{list-style:none;padding:0;margin:0}.tree li{padding:10px 0;border-top:1px solid var(--line)}.tree li:first-child{border-top:0}
.tree .f{display:flex;align-items:center;gap:10px;font-size:14px}
.tree .built{cursor:pointer}.tree .built .nm{font-weight:700}.tree .pending{opacity:.55}.tree .active .nm{color:var(--sky);font-weight:800}
.tree .ic{width:20px;height:20px;flex:none;display:grid;place-items:center;border-radius:50%;font-size:12px}
.tree .done .ic{background:var(--ok-bg);color:var(--ok)}.tree .active .ic{background:#e6efff;color:var(--sky);animation:pulse 1.1s infinite}
.tree .pending .ic{border:1.5px solid var(--line);color:var(--muted)}
.tree .nm .s{display:block;font-size:11px;color:var(--muted);font-weight:500}
.tree .body{margin:8px 0 2px 30px;padding:12px 14px;background:var(--bg);border:1px solid var(--line);border-radius:12px;font-size:13px;display:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
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
.md table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13.5px;overflow:hidden;border-radius:10px;border:1px solid var(--line)}
.md th,.md td{border-bottom:1px solid var(--line);border-right:1px solid var(--line);padding:8px 11px;text-align:left;vertical-align:top}
.md th:last-child,.md td:last-child{border-right:0}.md tbody tr:last-child td{border-bottom:0}
.md thead th{background:var(--bg);font-weight:800;font-size:12.5px}
.md tbody tr:nth-child(even){background:#fcfbf6}
.lead{font-size:14px;color:var(--muted);margin:0 0 12px}
.branches{display:flex;gap:10px;flex-wrap:wrap}.branches button{flex:1;min-width:130px;font-size:15px;padding:13px 12px}
.b-but{background:var(--sky)}.b-no{background:#fff;color:var(--ink);border:1.5px solid var(--line)}
.compose{margin-top:14px;border:1.5px solid var(--sky);border-radius:14px;padding:14px;background:#fff}
.compose .pl{font-weight:700;margin:0 0 8px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 0}
.chip{background:var(--bg);border:1px solid var(--line);color:var(--ink);font-size:13px;font-weight:700;padding:6px 11px;border-radius:20px}
.compose .row{display:flex;gap:8px;margin-top:10px}.compose .row button{flex:none}.compose .ghost{background:#fff;color:var(--muted);border:1.5px solid var(--line)}
.done{background:var(--ok-bg);border:1px solid #cfe9d8;border-radius:14px;padding:16px 18px;font-size:15px}
.addons .ax{display:flex;flex-wrap:wrap;gap:8px}
.addons .ax button{flex:1;min-width:120px;background:#fff;border:1.5px solid var(--line);color:var(--ink);font-size:13px;font-weight:800;padding:9px 10px;text-align:left}
.addons .ax .bl{display:block;font-size:11px;color:var(--muted);font-weight:500}
.bhelp{font-size:12px;color:var(--muted);margin:0 0 10px}
.disc{font-size:11px;color:var(--muted);margin-top:8px}
.vet{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:18px 20px;margin-bottom:16px}
.vet .vhead{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.vet h3{font-size:16px;font-weight:800;margin:0}
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
.authgate{margin:6px 0 2px}.authgate button{width:100%;margin-bottom:8px}
.gbtn{display:flex;align-items:center;justify-content:center;gap:10px;background:#fff;color:var(--ink);border:1.5px solid var(--line);font-weight:800}
.gicon{width:18px;height:18px;flex:none}
.authgate .or{color:var(--muted);font-size:13px;margin:4px 0 0}
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
<div class=top><h1 class=logo><button type=button class=logobtn onclick=newPlan() aria-label="FILG — start a new idea"><svg class=logomark viewBox="0 0 32 32" aria-hidden=true><rect width=32 height=32 rx=8 fill=#FF6B4A></rect><path d="M16 4c-3.2 2.8-4.3 7.4-4.3 11.8v3.2h8.6v-3.2C20.3 11.4 19.2 6.8 16 4z" fill=#fff></path><circle cx=16 cy=12 r=2.1 fill=#2E7CF6></circle><path d="M11.7 15.5 8.6 20.5l3.1-1.3z" fill=#fff></path><path d="M20.3 15.5 23.4 20.5l-3.1-1.3z" fill=#fff></path><path d="M13.6 19.5h4.8L16 25.5z" fill=#FFC23F></path></svg>FI<span>LG</span></button></h1><div class=authbar id=authbar></div></div>
<div class=note-banner id=banner></div>
<div class=intake id=intake>
<h2>You've got a business in you. Let's find it. 🚀</h2>
<p class=sub>Drop in your idea. You'll get the offer + the research graded — then we build the whole plan together, your call at every step.</p>
<label for=idea class=sr-only>Your business idea</label>
<textarea id=idea placeholder="e.g. I'm handy with automations and I think I could help dentists stop missing new-patient calls — but I don't know what to sell or how."></textarea>
<div class=boardpick id=boardpick></div>
<div id=authgate></div>
<label for=email class=sr-only>Your email</label>
<input id=email type=email placeholder="you@email.com">
<button id=go class=go onclick=start()>Build my plan →</button>
<div class=err id=err></div>
</div>
<div id=profile style="display:none"></div>
<div id=live class=sr-only aria-live=polite></div>
<aside class=drawer id=drawer role=dialog aria-modal=true aria-labelledby=drawer-title aria-hidden=true>
<div class=dr-head><span id=drawer-title>Ask an expert</span><button type=button class=dr-x onclick=closeDrawer() aria-label="Close panel">×</button></div>
<div class=dr-body>
<p class=dr-sub id=drawer-sub></p>
<label for=drawerq class=sr-only>Your question for the advisor</label>
<textarea id=drawerq rows=3 placeholder="Ask a question — or leave blank for their honest take."></textarea>
<button type=button class=dr-go id=drawer-go onclick=submitDrawer()>Ask →</button>
<div class="dr-out md" id=drawer-out></div>
</div></aside>
<div class=drawer-back id=drawerback onclick=closeDrawer()></div>
<div class=modal-back id=modalback onclick="_closeModal()"></div>
<div class=modal id=modal role=dialog aria-modal=true aria-labelledby=modal-title aria-hidden=true>
<h3 id=modal-title></h3><div id=modal-body></div><div class=modal-actions id=modal-actions></div></div>
<div class=toasts id=toasts aria-live=polite></div>
<div class=workspace id=workspace style="display:none">
<aside class=side>
<div class=sec><h3>Your plan</h3><ul class=tree id=tree></ul>
<button id=dl class=dl onclick=download() style="display:none;margin-top:12px">⬇ Download plan (.zip)</button></div>
<div class="sec addons"><h3>Ask an expert</h3><div class=ax id=addons></div>
<div class=disc id=adisc></div></div>
<div class="sec board" id=boardsec style="display:none"><h3>Board of Directors</h3>
<p class=bhelp>Tap to add or drop a director, then convene them on your plan.</p>
<div class=bdirs id=boarddirs></div>
<button type=button class=convene id=convene onclick=convene()>Convene the board</button>
<div class=disc>AI composite directors — not real people, not professional advice.</div></div>
<div class=sec><h3>Research — graded</h3><div id=research></div></div>
</aside>
<main class=main>
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
async function start(){
  const idea=document.getElementById('idea').value.trim(), email=document.getElementById('email').value.trim();
  const go=document.getElementById('go'), err=document.getElementById('err');
  err.textContent='';
  if(CFG.authEnabled&&!session){gateIntake();return;}   // login required when auth is on
  const body={idea}; if(!session) body.email=email;   // signed in → identity from the token
  if(BOARD.length) body.directors=BOARD;               // optional Board of Directors → vets each step
  go.disabled=true; go.textContent='Researching…';
  try{
    const r=await fetch('/api/plan/start',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(body)});
    const d=await r.json();
    if(!r.ok){err.textContent=d.error||'Something went wrong.';if(d.upgrade)err.innerHTML+=' <a href=# onclick="upgrade();return false">Upgrade →</a>';go.disabled=false;go.textContent='Build my plan →';return;}
    SID=d.id;
    document.getElementById('intake').style.display='none';
    document.getElementById('workspace').style.display='grid';
    poll();
  }catch(e){err.textContent='Network error.';go.disabled=false;go.textContent='Build my plan →';}
}
function say(msg){const l=document.getElementById('live'); if(l)l.textContent=msg;}  // announce to screen readers
async function poll(){
  const r=await fetch('/api/plan/'+SID,{headers:authHeaders()});
  const s=await r.json();
  renderTree(s);renderAddons(s);     // show the plan outline immediately, even while researching
  if(s.status==='researching'){
    document.getElementById('node').innerHTML='<div class=node><span class=eyebrow>Working</span><h3>Researching + grading your market…</h3><p class=lead>Pulling sources and grading every number — vendor spin gets labeled, not laundered. ~1–2 min. Watch your plan fill in on the left.</p></div>';
    say('Researching and grading your market.');
    setTimeout(poll,2500);return;
  }
  render(s);
}
function render(s){
  if(s.status==='error'){
    document.getElementById('node').innerHTML='<div class=node><h3>Hit a snag</h3><p class=lead>'+esc(s.error)+'</p><button type=button onclick=newPlan()>Start over</button></div>';
    say('Something went wrong: '+(s.error||'')); return;
  }
  renderResearch(s);renderVet(s);renderAnswer(s);renderTree(s);renderNode(s);renderAddons(s);renderBoard(s);renderBoardRound(s);
  if(s.done)say('Your plan is complete — all '+s.total+' parts ready to download.');
  else if(s.vetting&&s.vetting.verdict)say('Research graded. Verdict: '+s.vetting.verdict+'. Ready to build part '+((s.step||0)+1)+'.');
}
function renderBoardRound(s){
  const el=document.getElementById('boardround'); if(!el)return;
  const reviews=s.board||[];
  if(!reviews.length){el.innerHTML='';return;}
  const r=reviews[reviews.length-1];   // the board's take on the section just finalized
  const balloons=(r.directors||[]).map((d,i)=>{
    const id='bal_'+i;
    return `<div class=balloon id=${id}><button type=button class=bh onclick="document.getElementById('${id}').classList.toggle('open')">💬 See what ${esc(d.name)} says<span class=caret>▸</span></button><div class="bb md">${mdToHtml(d.take)}</div></div>`;
  }).join('');
  const split=(r.conflicts&&r.conflicts.toLowerCase()!=='none')?`<span class=split>Where they split: ${esc(r.conflicts)}</span>`:'';
  el.innerHTML=`<div class=bround><h4>🗣️ Your board weighed in on “${esc(r.title)}”</h4>`+
    `<div class=balloons>${balloons}</div>`+
    `<div class=takeaway><div class=tl>Board takeaway</div>${esc(r.verdict||'')}${split}</div></div>`;
}
function renderResearch(s){
  const rows=(s.research&&s.research.rows)||[];
  if(!rows.length){document.getElementById('research').innerHTML='<p style="color:var(--muted);font-size:13px;margin:0">Grading sources…</p>';return;}
  document.getElementById('research').innerHTML='<ul class=ev>'+rows.map(x=>`<li>${x.mark==='ok'?'✅':'⚠️'} ${esc(x.text)} <span class="badge ${x.mark==='ok'?'b-ok':'b-warn'}">${x.mark==='ok'?'cited':'vendor'}</span><br><span class=note>${esc(host(x.url))} — ${esc(x.note)}</span></li>`).join('')+'</ul>';
}
function renderAnswer(s){
  const p=s.research&&s.research.prose; if(!p)return;
  document.getElementById('answer').innerHTML=`<h2>${esc(p.title)}</h2><p class=tag>Your offer, with the research graded — vendor spin labeled, not laundered.</p><p><b>What you'd sell:</b> ${esc(p.offer)}</p><p><b>How you'd sell it:</b> ${esc(p.gtm)}</p>`;
}
function renderTree(s){
  const built={}; (s.files||[]).forEach(f=>built[f.path]=f.content);
  const step=s.step==null?-1:s.step;
  document.getElementById('tree').innerHTML=(s.sections||[]).map((sec,i)=>{
    const nm=`<span class=nm>${esc(sec.title)}<span class=s>${esc(sec.sub||'')}</span></span>`;
    if(built[sec.file]!=null){
      return `<li class="done built"><button type=button class=f aria-expanded=false onclick="var b=this.parentNode.querySelector('.body');var o=b.style.display==='block';b.style.display=o?'none':'block';this.setAttribute('aria-expanded',String(!o))"><span class=ic aria-hidden=true>✓</span>${nm}</button><div class="body md">${mdToHtml(built[sec.file])}</div></li>`;
    }
    if(!s.done&&i===step){return `<li class=active><div class=f><span class=ic aria-hidden=true>✍︎</span>${nm}</div></li>`;}
    return `<li class=pending><div class=f><span class=ic aria-hidden=true>○</span>${nm}</div></li>`;
  }).join('');
  const dl=document.getElementById('dl'); if(dl)dl.style.display=s.done?'block':'none';
}
function renderNode(s){
  const n=document.getElementById('node');
  if(s.status==='researching')return;
  if(s.done){n.innerHTML='<div class=node><div class=done>🎉 Your plan\\'s ready — all '+s.total+' parts. Grab the download on the left, or ask an expert to pressure-test it.</div></div>';return;}
  const p=s.proposal; if(!p){n.innerHTML='';return;}
  const sec=(s.sections||[]).find(x=>x.title===p.title)||{};
  const intro=s.step===0?`<p class=lead>We build your plan in ${s.total} parts — one at a time, your call on each (watch them fill in on the left). First up:</p>`:'';
  n.innerHTML=`<div class=node><span class=eyebrow>Your plan · part ${s.step+1} of ${s.total}</span><h3>${esc(p.title)}</h3><p class=h3sub>${esc(sec.sub||'')}</p>`+
    intro+
    `<div class="draft md">${mdToHtml(p.draft)}</div>`+
    `<p class=lead>Here's a first swing — react and we'll shape it. Your call drives what gets written next.</p>`+
    `<div class=branches><button class=b-yes onclick="branch('yes_and')">Yes, and…</button>`+
    `<button class=b-but onclick="branch('okay_but')">Okay, but…</button>`+
    `<button class=b-no onclick="branch('not_quite')">Not quite</button></div><div id=compose></div></div>`;
}
const BRANCH={
  yes_and:{pl:"Yes — and what should it add or push further?",chips:["go bolder","add a second audience","make it premium","add an upsell"],btn:"Add it →"},
  okay_but:{pl:"Okay — but what should change?",chips:["cheaper entry","B2B only","faster timeline","narrower niche"],btn:"Change it →"},
  not_quite:{pl:"Not quite — what would you rather see?",chips:["a different model","more specific","less risky","more ambitious"],btn:"Show me another →"}};
function branch(choice){
  const b=BRANCH[choice], c=document.getElementById('compose');
  c.innerHTML=`<div class=compose><p class=pl>${esc(b.pl)}</p>`+
    `<textarea id=note rows=2 placeholder="Optional — type a note, or just send."></textarea>`+
    `<div class=chips>${b.chips.map(x=>`<button type=button class=chip onclick="addChip('${x.replace(/'/g,"")}')">${esc(x)}</button>`).join('')}</div>`+
    `<div class=row><button onclick="respond('${choice}')">${esc(b.btn)}</button><button type=button class=ghost onclick="document.getElementById('compose').innerHTML=''">Cancel</button></div></div>`;
  const t=document.getElementById('note'); if(t)t.focus();
}
function addChip(txt){const t=document.getElementById('note'); if(!t)return; t.value=(t.value?t.value.replace(/\\s*$/,'')+', ':'')+txt; t.focus();}
async function respond(choice){
  const noteEl=document.getElementById('note'); const note=noteEl?noteEl.value:'';
  const node=document.getElementById('node'); node.querySelectorAll('button').forEach(b=>b.disabled=true);
  const err=document.getElementById('err2'); err.textContent='';
  const comp=document.getElementById('compose'); if(comp)comp.innerHTML='<p class=lead>Writing…</p>';
  try{
    const r=await fetch('/api/plan/'+SID+'/respond',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({choice,note})});
    const s=await r.json();
    if(!r.ok){err.textContent=s.error||'Something went wrong.';node.querySelectorAll('button').forEach(b=>b.disabled=false);return;}
    render(s);
  }catch(e){err.textContent='Network error.';node.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
function renderAddons(s){
  const box=document.getElementById('addons'); if(!box||box.dataset.done)return;
  const ax=CFG.archetypes||[]; if(!ax.length){box.closest('.sec').style.display='none';return;}
  box.innerHTML=ax.map(a=>`<button type=button onclick="ask('${a.key}')">${esc(a.name)}<span class=bl>${esc(a.blurb)}</span></button>`).join('');
  document.getElementById('adisc').textContent='AI composite advisors — not real people, not professional advice.';
  box.dataset.done='1';
}
function renderVet(s){
  const el=document.getElementById('vet'); if(!el)return;
  const v=s.vetting, sh=s.shaped;
  if(!v&&!sh){el.innerHTML='';return;}
  const verdict=(v&&v.verdict)||'';
  const head=verdict?`<span class="verdict ${esc(verdict)}">${esc(verdict)}</span>`:'';
  const thesis=sh&&sh.thesis?`<p class=thesis><b>Your focus:</b> ${esc(sh.thesis)}</p>`:'';
  const edge=sh&&sh.founder_edge?`<p class=vrow><b>Your edge:</b> ${esc(sh.founder_edge)}</p>`:'';
  const alts=(sh&&sh.wedges_considered&&sh.wedges_considered.length>1)?`<p class=vrow><b>Also considered:</b> ${esc(sh.wedges_considered.slice(1).join(' · '))}</p>`:'';
  const reason=v&&v.reason?`<p class=vrow>${esc(v.reason)}</p>`:'';
  const risk=v&&v.biggest_risk?`<p class=vrow><b>Biggest risk:</b> ${esc(v.biggest_risk)}</p>`:'';
  const test=v&&v.first_test?`<p class=vrow><b>Cheapest first test:</b> ${esc(v.first_test)}</p>`:'';
  const cq=(sh&&sh.clarifying_question)?`<p class=vrow>🤔 ${esc(sh.clarifying_question)}</p>`:'';
  el.innerHTML=`<div class=vet><div class=vhead>${head}<h3>Before we build — the honest read</h3></div>${thesis}${edge}${alts}${reason}${risk}${test}${cq}</div>`;
}
// Board selection state (keys); seeded from the default board, editable in intake + sidebar.
let BOARD=(CFG.defaultBoard||[]).slice();
function personaName(key){const p=(CFG.archetypes||[]).find(a=>a.key===key);return p?p.name:key;}
function renderBoardPick(){
  const el=document.getElementById('boardpick'); if(!el)return;
  const ax=CFG.archetypes||[]; if(!ax.length){el.innerHTML='';return;}
  el.innerHTML=`<div class=lab id=boardpicklab>Pick your Board of Directors — they'll vet every step (optional):</div>`+
    `<div class=opts role=group aria-labelledby=boardpicklab>`+ax.map(a=>`<button type=button class="bchip${BOARD.includes(a.key)?' on':''}" aria-pressed=${BOARD.includes(a.key)} onclick="toggleBoard('${a.key}',this)" title="${esc(a.blurb)}">${esc(a.name)}</button>`).join('')+`</div>`;
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
    `<button type=button class="bchip${SESSION_BOARD.includes(a.key)?' on':''}" aria-pressed=${SESSION_BOARD.includes(a.key)} onclick="toggleSessionBoard('${a.key}',this)" title="${esc(a.blurb)}">${esc(a.name)}</button>`).join('');
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
    sub.textContent=(p.blurb?('Composite advisor · '+p.blurb):'AI composite advisor')+' — not professional advice.';
    go.textContent='Ask '+(p.name||'the advisor')+' →';
  }else{
    const chosen=(SESSION_BOARD&&SESSION_BOARD.length?SESSION_BOARD:(CFG.defaultBoard||[]));
    title.textContent='Your Board of Directors';
    sub.textContent=(chosen.length?('Convening: '+chosen.map(personaName).join(', ')):'Your full board')+' — AI composite directors, not professional advice.';
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
  const q=document.getElementById('drawerq').value, go=document.getElementById('drawer-go'),
        out=document.getElementById('drawer-out');
  out.style.display='block';
  out.innerHTML='<p class=lead>'+(DRAWER.mode==='board'?'Convening the board…':'Thinking…')+'</p>';
  go.disabled=true;
  try{
    if(DRAWER.mode==='expert'){
      const r=await fetch('/api/plan/'+SID+'/ask',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({archetype:DRAWER.key,question:q})});
      const d=await r.json();go.disabled=false;
      out.innerHTML=r.ok?mdToHtml(d.answer):esc(d.error||'Could not reach the advisor.');
    }else{
      const body={question:q}; if(SESSION_BOARD!==null)body.directors=SESSION_BOARD;
      const r=await fetch('/api/plan/'+SID+'/board',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(body)});
      const d=await r.json();go.disabled=false;
      if(!r.ok){out.innerHTML=esc(d.error||'Could not convene the board.');return;}
      const split=(d.conflicts&&d.conflicts.toLowerCase()!=='none')?`<span class=split>Where they split: ${esc(d.conflicts)}</span>`:'';
      out.innerHTML=d.directors.map((x,i)=>`<div class=balloon id=dbal_${i}><button type=button class=bh onclick="document.getElementById('dbal_${i}').classList.toggle('open')">💬 See what ${esc(x.name)} says<span class=caret>▸</span></button><div class="bb md">${mdToHtml(x.take)}</div></div>`).join('')+
        `<div class=takeaway><div class=tl>Board takeaway</div>${esc(d.verdict)}${split}</div>`+
        `<div class=disc>${esc(d.disclaimer||'')}</div>`;
    }
  }catch(e){go.disabled=false;out.innerHTML='Network error.';}
}
async function download(){
  try{
    const r=await fetch('/api/plan/'+SID+'/download',{headers:authHeaders()});
    if(r.status===402){const d=await r.json();if(await uiConfirm('Unlock the download',(d.error||'Unlock the download.'),'Go to checkout'))upgrade();return;}
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
      `<span class=who>${esc(session.user.email)}${paid?' · <b>Operator</b>':''}</span>`+
      (!paid&&CFG.billingEnabled?`<button class="link up" onclick=upgrade()>Upgrade — $39/mo</button>`:'')+
      `<button class=link onclick=signout()>Sign out</button>`;
  }else if(sb){bar.style.display='';bar.innerHTML=`<button class=link onclick=signinEmail()>Sign in</button>`;}
  else{bar.style.display='none';}
  gateIntake();
}
function gateIntake(){
  const gate=document.getElementById('authgate'),email=document.getElementById('email'),go=document.getElementById('go');
  if(!gate)return;
  if(CFG.authEnabled&&!session){
    email.style.display='none';go.style.display='none';
    gate.innerHTML=`<div class=authgate>`+
      (CFG.supabaseUrl?`<button class=gbtn onclick=signinGoogle()><svg class=gicon viewBox="0 0 18 18" aria-hidden=true><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.71-1.57 2.68-3.89 2.68-6.62z"></path><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.81.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18z"></path><path fill="#FBBC05" d="M3.97 10.72a5.4 5.4 0 0 1 0-3.44V4.95H.96a9 9 0 0 0 0 8.1l3.01-2.33z"></path><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58C13.46.9 11.43 0 9 0A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58z"></path></svg>Continue with Google</button>`:'')+
      `<button class=gbtn onclick=signinEmail()>✉️ Email me a sign-in link</button>`+
      `<p class=or>Free to start — sign in so your plans save to your profile.</p></div>`;
  }else{gate.innerHTML='';go.style.display='';email.style.display=CFG.authEnabled?'none':'';}
}
function saveIdea(){try{const v=document.getElementById('idea').value;if(v)localStorage.setItem('filg_idea',v);}catch(e){}}
function restoreIdea(){try{const v=localStorage.getItem('filg_idea');if(v){document.getElementById('idea').value=v;localStorage.removeItem('filg_idea');}}catch(e){}}
async function loadMe(){
  if(!session){me=null;return;}
  try{const r=await fetch('/api/me',{headers:authHeaders()});me=r.ok?await r.json():null;}catch(e){me=null;}
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
function newPlan(){show('intake');renderBoardPick();gateIntake();}
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
  }).join(''):`<p class=empty>No plans yet — build your first one.</p>`;
  const integ=d.paid?`<div class=integrations><b>Operator integrations</b> — CRM kickstarts &amp; more, coming soon.</div>`:`<div class=integrations>Upgrade to Operator for integrations (CRM kickstarts &amp; more) — coming soon.</div>`;
  document.getElementById('profile').innerHTML=`<div class=plans><h2>Your plans</h2><p class=sub>${esc(d.email)} · ${d.paid?'Operator':'Free'}</p>`+
    `<div style="margin:10px 0 16px"><button onclick=newPlan()>+ New plan</button></div>`+rows+integ+`</div>`;
}
async function resume(id){
  SID=id;show('workspace');SESSION_BOARD=null;
  const ab=document.getElementById('addons');if(ab)delete ab.dataset.done;
  closeDrawer();
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
  if(q.get('canceled'))banner('Checkout canceled — no charge. You\\'re still on the free tier.');
  restoreIdea();renderBoardPick();
  if(!CFG.authEnabled||!window.supabase){renderAuth();return;}
  sb=window.supabase.createClient(CFG.supabaseUrl,CFG.supabaseAnon);
  sb.auth.onAuthStateChange(async (_e,s)=>{session=s;await loadMe();renderAuth();});
  const {data}=await sb.auth.getSession();session=data.session;await loadMe();renderAuth();
}
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
