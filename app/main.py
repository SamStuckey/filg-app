#!/usr/bin/env python3
"""
FILG MVP backend — the thin app that runs the engine for a stranger.

Flow: plain-text idea in → async label-don't-chase run (the engine) → graded artifact out, gated by
the free-tier cap + daily kill switch (prototype/usage.py). Reuses the engine wholesale; nothing
about the pipeline is reimplemented here.

This is the launch skeleton. STUBBED (clearly marked) for now: real auth and Stripe billing —
right now a "user" is just the email entered, and `is_paid` is a static allowlist. Jobs + results
persist in SQLite (app/store.py) so share links survive restarts; swap for Postgres + a real queue
in production.

Run:
  pip install fastapi uvicorn
  FILG_MOCK=1 uvicorn app.main:app --reload        # free, no API calls (dev/frontend)
  uvicorn app.main:app                              # real runs (~$0.40 each, metered)
Env: FILG_MOCK, FILG_FREE_RUNS, FILG_DAILY_BUDGET, FILG_PAID_EMAILS (csv).
"""

from __future__ import annotations

import html
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

from . import store  # noqa: E402 — SQLite-backed job/result persistence

MOCK = os.environ.get("FILG_MOCK") == "1"
PAID = {e.strip().lower() for e in os.environ.get("FILG_PAID_EMAILS", "").split(",") if e.strip()}

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
    user = (body.get("email") or "").strip().lower()
    if len(idea) < 12:
        return JSONResponse({"error": "Tell me a bit more about the idea."}, status_code=400)
    if "@" not in user:
        return JSONResponse({"error": "Enter an email so we can send your result."}, status_code=400)

    is_paid = user in PAID
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


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mock": MOCK, **usage.snapshot()}


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
    return PAGE


# ── Minimal single-page frontend (brand-aligned; no build step) ──────────────
PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>FILG — your offer, with receipts</title>
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
</style></head><body><div class=wrap>
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
async function run(){
  const idea=document.getElementById('idea').value, email=document.getElementById('email').value;
  const go=document.getElementById('go'), st=document.getElementById('status'), err=document.getElementById('err');
  err.textContent='';document.getElementById('card').style.display='none';
  go.disabled=true;st.textContent='Researching + grading sources… (~1–2 min)';
  try{
    const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idea,email})});
    const d=await r.json();
    if(!r.ok){err.textContent=d.error||'Something went wrong.';go.disabled=false;st.textContent='';return;}
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
</script>
</div></body></html>"""
