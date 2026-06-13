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
import json
import os
import sys
import threading
import uuid
from pathlib import Path

import markdown
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# import the engine + guardrail (prototype/ is a sibling of app/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "prototype"))
import teardown  # noqa: E402
import usage     # noqa: E402

from . import auth, billing, store  # noqa: E402 — SQLite persistence, Supabase auth, Stripe billing

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


@app.get("/", response_class=HTMLResponse)
async def index():
    cfg = json.dumps({"authEnabled": auth.AUTH_ENABLED, "billingEnabled": billing.BILLING_ENABLED,
                      "supabaseUrl": os.environ.get("SUPABASE_URL", ""),
                      "supabaseAnon": os.environ.get("SUPABASE_ANON_KEY", "")})
    head = f"<script>window.FILG={cfg}</script>"
    if auth.AUTH_ENABLED:
        head += '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
    return PAGE.replace("__FILG_HEAD__", head)


# ── Minimal single-page frontend (brand-aligned; no build step) ──────────────
PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>FILG — your offer, with receipts</title>
__FILG_HEAD__
<style>
:root{--paper:#FBFAF8;--ink:#14110E;--muted:#6B655C;--line:#E7E2D8;--accent:#0F766E;--warn:#B45309;--ok-bg:#EAF4F2;--warn-bg:#FBF3E6}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:40px 22px}
h1{font-family:Georgia,serif;font-size:34px;letter-spacing:-.01em;margin:0 0 8px}
.logo span{color:var(--accent)}.sub{color:var(--muted);margin:0 0 26px}
textarea,input{width:100%;padding:13px 15px;border:1px solid var(--line);border-radius:10px;font:inherit;background:#fff;margin-bottom:12px}
textarea{min-height:110px;resize:vertical}
button{background:var(--accent);color:#fff;border:0;font:inherit;font-weight:600;padding:13px 22px;border-radius:10px;cursor:pointer}
button:disabled{opacity:.6}
.status{color:var(--muted);margin:16px 0}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:24px;margin-top:22px;display:none}
.card h2{font-family:Georgia,serif;font-size:22px;margin:0 0 4px}.tag{color:var(--muted);font-size:14px;margin:0 0 16px}
.ev{list-style:none;padding:0;margin:14px 0 0}.ev li{padding:11px 0;border-top:1px dashed var(--line);display:flex;gap:10px;font-size:15px}
.ev .ok{color:var(--accent)}.ev .warn{color:var(--warn)}.ev .note{color:var(--muted);font-size:13px}
.badge{font-size:11px;font-weight:600;padding:2px 7px;border-radius:6px}.b-ok{background:var(--ok-bg);color:var(--accent)}.b-warn{background:var(--warn-bg);color:var(--warn)}
.recpt{margin-top:18px;padding:14px 16px;background:var(--ok-bg);border-radius:10px;font-size:14px}
.err{color:var(--warn);margin-top:14px}
.authbar{display:flex;justify-content:flex-end;align-items:center;gap:14px;font-size:14px;margin-bottom:8px;min-height:24px}
.authbar .who{color:var(--muted)}.authbar b{color:var(--accent)}
.authbar .link{background:none;color:var(--accent);padding:0;font-weight:600;font-size:14px}
.authbar .up{background:var(--accent);color:#fff;padding:7px 13px;border-radius:8px;font-size:13px}
.note-banner{background:var(--ok-bg);border-radius:10px;padding:12px 15px;font-size:14px;margin-bottom:16px;display:none}
</style></head><body><div class=wrap>
<div class=authbar id=authbar></div>
<div class=note-banner id=banner></div>
<h1 class=logo>FI<span>LG</span></h1>
<p class=sub>Drop in your idea. Get the offer + how to sell it, with the research graded — vendor spin labeled, not laundered.</p>
<textarea id=idea placeholder="I'm good with automation and I think I could help [who] with [problem]... but I don't know what to sell or how."></textarea>
<input id=email type=email placeholder="you@email.com">
<button id=go onclick=run()>Build my offer →</button>
<div class=status id=status></div>
<div class=err id=err></div>
<div id=perma style="margin-top:16px;display:none"><a id=permalink href="#">🔗 Shareable link to this result</a></div>
<div class=card id=card></div>
<script>
const CFG=window.FILG||{authEnabled:false,billingEnabled:false};
let sb=null, session=null, me=null;
function authHeaders(){return session?{'Authorization':'Bearer '+session.access_token}:{};}
async function run(){
  const idea=document.getElementById('idea').value, email=document.getElementById('email').value;
  const go=document.getElementById('go'), st=document.getElementById('status'), err=document.getElementById('err');
  err.textContent='';document.getElementById('card').style.display='none';
  const body={idea}; if(!session) body.email=email;   // signed in → identity comes from the token
  go.disabled=true;st.textContent='Researching + grading sources… (~1–2 min)';
  try{
    const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(body)});
    const d=await r.json();
    if(!r.ok){err.textContent=d.error||'Something went wrong.';if(d.upgrade&&session&&CFG.billingEnabled)err.innerHTML+=' <a href=# onclick="upgrade();return false">Upgrade to Operator →</a>';go.disabled=false;st.textContent='';return;}
    poll(d.job_id);
  }catch(e){err.textContent='Network error.';go.disabled=false;st.textContent='';}
}
async function poll(id){
  const st=document.getElementById('status'),go=document.getElementById('go');
  const r=await fetch('/api/run/'+id);const d=await r.json();
  if(d.status==='running'){setTimeout(()=>poll(id),2500);return;}
  go.disabled=false;st.textContent='';
  if(d.status==='error'){document.getElementById('err').textContent=d.error;return;}
  const pl=document.getElementById('permalink'),pw=document.getElementById('perma');
  pl.href='/r/'+id;pw.style.display='block';
  render(d.result);
}
function render(res){
  const c=document.getElementById('card');const s=res.stats;
  if(res.artifacts_md){
    c.innerHTML=`<h2>Your full offer is ready</h2><p class=tag>Generated for ~$${res.cost.toFixed(2)}. `+
      `${s.checked} claims checked, ${s.cleared} cited, ${s.flagged} flagged.</p>`+
      `<p>Open the full artifact set (offer · pricing · GTM · delivery · roadmap) via the link above.</p>`;
    c.style.display='block';return;
  }
  const p=res.prose;
  const rows=res.rows.map(x=>`<li><span class="${x.mark==='ok'?'ok':'warn'}">${x.mark==='ok'?'✅':'⚠️'}</span>
   <span>${esc(x.text)} <span class="badge ${x.mark==='ok'?'b-ok':'b-warn'}">${x.mark==='ok'?'cited':'vendor — unverified'}</span>
   <br><span class=note><a href="${esc(x.url)}" target=_blank rel=noopener>${esc(host(x.url))}</a> — ${esc(x.note)}</span></span></li>`).join('');
  c.innerHTML=`<h2>${esc(p.title)}</h2><p class=tag>Generated for ~$${res.cost.toFixed(2)}. Every number graded.</p>
   <p><b>The offer:</b> ${esc(p.offer)}</p><p><b>How you'd sell it:</b> ${esc(p.gtm)}</p>
   <p><b>The evidence — graded:</b></p><ul class=ev>${rows}</ul>
   <div class=recpt><b>${s.checked}</b> claims checked · <b>${s.cleared} cited</b> · ${s.flagged} flagged as vendor marketing and labeled.</div>`;
  c.style.display='block';
}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
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
