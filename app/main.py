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
     SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_JWT_SECRET, STRIPE_*/FILG_PUBLIC_URL.
"""

from __future__ import annotations

import html
import io
import json
import os
import sys
import threading
import uuid
import zipfile
from pathlib import Path

import markdown
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

# import the engine + guardrail (prototype/ is a sibling of app/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "prototype"))
import teardown  # noqa: E402
import usage     # noqa: E402

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


# ── Interactive plan builder (idea → decision tree → downloadable file tree) ──
def _identity(request: Request, body_email: str | None = None) -> tuple[str, bool]:
    """Resolve (user, verified) — a verified Supabase user wins; else the body email (free tier)."""
    authed = auth.user_from_request(request)
    if authed and authed["email"]:
        return authed["email"], True
    return (body_email or "").strip().lower(), False


def _plan_state(s: dict) -> dict:
    """Shape a session row for the frontend."""
    return {
        "id": s["id"], "status": s["status"], "idea": s["idea"], "error": s.get("error"),
        "research": s.get("research"),
        "files": [{"path": p, "content": c} for p, c in (s.get("files") or {}).items()],
        "sections": [{"file": x["file"], "title": x["title"]} for x in planner.SECTIONS],
        "step": s.get("step", 0), "total": planner.N, "proposal": s.get("proposal"),
        "done": s["status"] == "done",
    }


def _plan_research(session_id: str, idea: str, user: str) -> None:
    try:
        with RUN_LOCK:
            res = planner.research(idea, mock=MOCK)
            prop, c0 = planner.first_proposal(idea, res, mock=MOCK)
        usage.record_run(user, res["cost"])   # the metered free run (also bumps the daily total)
        usage.record_spend(c0)                 # first section draft → daily kill switch only
        store.plan_save(session_id, status="building", research=res, step=0, proposal=prop,
                        cost=round(res["cost"] + c0, 4))
    except Exception as e:  # noqa: BLE001
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
    sid = uuid.uuid4().hex[:12]
    store.plan_create(sid, user, idea)
    threading.Thread(target=_plan_research, args=(sid, idea, user), daemon=True).start()
    return {"id": sid}


@app.get("/api/plan/{sid}")
async def api_plan_get(sid: str):
    s = store.plan_get(sid)
    if not s:
        return JSONResponse({"error": "unknown session"}, status_code=404)
    return _plan_state(s)


@app.post("/api/plan/{sid}/respond")
async def api_plan_respond(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s:
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if s["status"] != "building":
        return JSONResponse({"error": f"session is {s['status']}"}, status_code=409)
    body = await request.json()
    choice = body.get("choice")
    if choice not in planner.CHOICES:
        return JSONResponse({"error": "pick yes_and / not_quite / okay_but"}, status_code=400)
    try:
        with RUN_LOCK:
            upd = planner.advance(s, choice, body.get("note"), mock=MOCK)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    usage.record_spend(round((upd.get("cost", 0) or 0) - (s.get("cost") or 0), 4))  # per-section draft
    store.plan_save(sid, **upd)
    return _plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/ask")
async def api_plan_ask(sid: str, request: Request):
    """Add-on: ask a composite archetype advisor about the plan-in-progress."""
    s = store.plan_get(sid)
    if not s:
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


@app.get("/api/plan/{sid}/download")
async def api_plan_download(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or s["status"] != "done":
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
                      "supabaseAnon": os.environ.get("SUPABASE_ANON_KEY", ""),
                      "archetypes": planner.ARCHETYPES})
    head = f"<script>window.FILG={cfg}</script>"
    if auth.AUTH_ENABLED:
        head += '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
    return PAGE.replace("__FILG_HEAD__", head)


# ── Single-page plan-builder frontend (brand-aligned; no build step) ─────────
PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>FILG — build your business plan, with receipts</title>
__FILG_HEAD__
<style>
:root{--bg:#FFFDF7;--ink:#1B1726;--muted:#6E6878;--line:#EFE9DD;--card:#fff;--coral:#FF6B4A;--coral-d:#E85535;--sky:#2E7CF6;--sun:#FFC23F;--ok-bg:#E7F5EC;--ok:#1E9E5A;--warn-bg:#FFF3E0;--warn:#C9740B}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 "Nunito",ui-rounded,"SF Pro Rounded","Segoe UI",system-ui,sans-serif}
.page{max-width:1140px;margin:0 auto;padding:22px 22px 72px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
h1.logo{font-size:30px;font-weight:800;letter-spacing:-.02em;margin:0}.logo span{color:var(--coral)}
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
.tree .body{margin:8px 0 2px 30px;padding:12px 14px;background:var(--bg);border:1px solid var(--line);border-radius:12px;white-space:pre-wrap;font-size:13px;display:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.dl{width:100%;background:var(--sun);color:#3a2c00}
.main{min-width:0}
.answer{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:24px;margin-bottom:20px}
.answer h2{font-size:24px;font-weight:800;letter-spacing:-.01em;margin:0 0 4px}.answer .tag{color:var(--muted);font-size:13px;margin:0 0 14px}
.node{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:24px}
.node .eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--coral);font-weight:800}
.node h3{font-size:21px;font-weight:800;margin:5px 0 2px}.node .h3sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.draft{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:16px 18px;font-size:14.5px;margin-bottom:16px}
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
.expert{margin-top:12px;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:12px 14px;font-size:13px;white-space:pre-wrap;display:none}
.disc{font-size:11px;color:var(--muted);margin-top:8px}
@media(max-width:820px){.workspace{grid-template-columns:1fr}.side{position:static}}
</style></head><body><div class=page>
<div class=top><h1 class=logo>FI<span>LG</span></h1><div class=authbar id=authbar></div></div>
<div class=note-banner id=banner></div>
<div class=intake id=intake>
<h2>You've got a business in you. Let's find it. 🚀</h2>
<p class=sub>Drop in your idea. You'll get the offer + the research graded — then we build the whole plan together, your call at every step.</p>
<textarea id=idea placeholder="e.g. I'm handy with automations and I think I could help dentists stop missing new-patient calls — but I don't know what to sell or how."></textarea>
<input id=email type=email placeholder="you@email.com">
<button id=go class=go onclick=start()>Build my plan →</button>
<div class=err id=err></div>
</div>
<div class=workspace id=workspace style="display:none">
<aside class=side>
<div class=sec><h3>Your plan</h3><ul class=tree id=tree></ul>
<button id=dl class=dl onclick=download() style="display:none;margin-top:12px">⬇ Download plan (.zip)</button></div>
<div class="sec addons"><h3>Add-ons · ask an expert</h3><div class=ax id=addons></div>
<div class=expert id=expert></div><div class=disc id=adisc></div></div>
<div class=sec><h3>Research — graded</h3><div id=research></div></div>
</aside>
<main class=main>
<div id=answer></div>
<div id=node></div>
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
  const body={idea}; if(!session) body.email=email;   // signed in → identity from the token
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
async function poll(){
  const r=await fetch('/api/plan/'+SID,{headers:authHeaders()});
  const s=await r.json();
  renderTree(s);renderAddons(s);     // show the plan outline immediately, even while researching
  if(s.status==='researching'){
    document.getElementById('node').innerHTML='<div class=node><span class=eyebrow>Working</span><h3>Researching + grading your market…</h3><p class=lead>Pulling sources and grading every number — vendor spin gets labeled, not laundered. ~1–2 min. Watch your plan fill in on the left.</p></div>';
    setTimeout(poll,2500);return;
  }
  render(s);
}
function render(s){
  if(s.status==='error'){document.getElementById('node').innerHTML='<div class=node><h3>Hit a snag</h3><p class=lead>'+esc(s.error)+'</p></div>';return;}
  renderResearch(s);renderAnswer(s);renderTree(s);renderNode(s);renderAddons(s);
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
      return `<li class="done built" onclick="var b=this.querySelector('.body');b.style.display=b.style.display==='block'?'none':'block'"><div class=f><span class=ic>✓</span>${nm}</div><div class=body>${esc(built[sec.file])}</div></li>`;
    }
    if(!s.done&&i===step){return `<li class=active><div class=f><span class=ic>✍︎</span>${nm}</div></li>`;}
    return `<li class=pending><div class=f><span class=ic>○</span>${nm}</div></li>`;
  }).join('');
  const dl=document.getElementById('dl'); if(dl)dl.style.display=s.done?'block':'none';
}
function renderNode(s){
  const n=document.getElementById('node');
  if(s.status==='researching')return;
  if(s.done){n.innerHTML='<div class=node><div class=done>🎉 Your plan\\'s ready — all '+s.total+' parts. Grab the download on the left, or ask an expert to pressure-test it.</div></div>';return;}
  const p=s.proposal; if(!p){n.innerHTML='';return;}
  const sec=(s.sections||[]).find(x=>x.title===p.title)||{};
  n.innerHTML=`<div class=node><span class=eyebrow>Part ${s.step+1} of ${s.total}</span><h3>${esc(p.title)}</h3><p class=h3sub>${esc(sec.sub||'')}</p>`+
    `<div class=draft>${esc(p.draft)}</div>`+
    `<p class=lead>Here's a first swing. React and we'll shape it — your call drives what gets written next.</p>`+
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
async function ask(key){
  const out=document.getElementById('expert'); out.style.display='block';
  const q=prompt('Ask the advisor about your plan (optional):'); if(q===null)return;
  out.textContent='Thinking…';
  try{
    const r=await fetch('/api/plan/'+SID+'/ask',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({archetype:key,question:q})});
    const d=await r.json();
    out.textContent=r.ok?d.answer:(d.error||'Could not reach the advisor.');
  }catch(e){out.textContent='Network error.';}
}
async function download(){
  try{
    const r=await fetch('/api/plan/'+SID+'/download',{headers:authHeaders()});
    if(r.status===402){const d=await r.json();if(confirm((d.error||'Unlock the download.')+'\\n\\nGo to checkout?'))upgrade();return;}
    if(!r.ok){alert('Could not download.');return;}
    const blob=await r.blob(),u=URL.createObjectURL(blob);
    const a=document.createElement('a');a.href=u;a.download='filg-business-plan.zip';a.click();URL.revokeObjectURL(u);
  }catch(e){alert('Network error.');}
}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
function host(u){try{return new URL(u).hostname.replace(/^www\\./,'');}catch(e){return u;}}

// ── Auth (Supabase) + billing (Stripe) ──────────────────────────────────────
function renderAuth(){
  const bar=document.getElementById('authbar'), emailEl=document.getElementById('email');
  if(!sb){bar.style.display='none';return;}
  if(session){
    const paid=me&&me.paid;
    bar.innerHTML=`<span class=who>${esc(session.user.email)}${paid?' · <b>Operator</b>':''}</span>`+
      (!paid&&CFG.billingEnabled?`<button class="link up" onclick=upgrade()>Upgrade — $39/mo</button>`:'')+
      `<button class=link onclick=signout()>Sign out</button>`;
    emailEl.style.display='none';
  }else{
    bar.innerHTML=`<button class=link onclick=signin()>Sign in</button>`;
    emailEl.style.display='';
  }
}
async function loadMe(){
  if(!session){me=null;return;}
  try{const r=await fetch('/api/me',{headers:authHeaders()});me=r.ok?await r.json():null;}catch(e){me=null;}
}
async function signin(){
  const email=prompt('Your email — we\\'ll send a one-click sign-in link:');
  if(!email)return;
  const {error}=await sb.auth.signInWithOtp({email,options:{emailRedirectTo:location.origin}});
  alert(error?error.message:'Check your inbox for the sign-in link.');
}
async function signout(){await sb.auth.signOut();session=null;me=null;renderAuth();}
async function upgrade(){
  if(!session){signin();return;}
  try{
    const r=await fetch('/api/checkout',{method:'POST',headers:authHeaders()});
    const d=await r.json();
    if(d.url)location.href=d.url; else alert(d.error||'Could not start checkout.');
  }catch(e){alert('Network error starting checkout.');}
}
function banner(msg){const b=document.getElementById('banner');b.textContent=msg;b.style.display='block';}
async function initAuth(){
  const q=new URLSearchParams(location.search);
  if(q.get('upgraded'))banner('🎉 You\\'re on Operator — full artifact sets are unlocked. Run an idea below.');
  if(q.get('canceled'))banner('Checkout canceled — no charge. You\\'re still on the free tier.');
  if(!CFG.authEnabled||!window.supabase){renderAuth();return;}
  sb=window.supabase.createClient(CFG.supabaseUrl,CFG.supabaseAnon);
  sb.auth.onAuthStateChange(async (_e,s)=>{session=s;await loadMe();renderAuth();});
  const {data}=await sb.auth.getSession();session=data.session;await loadMe();renderAuth();
}
initAuth();
</script>
</div></body></html>"""
