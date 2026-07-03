/* FILG v2 — the unified two-panel build surface.
   Left = one prompt box (color-moded) + the decision tree + tool triggers.  Right = the decision graph
   (a node spine; a node zooms to a full page).  Everything routes through /api/plan/{sid}/route or the
   funnel routes (/api/brainstorm, /merge, /commit).  Built unlocked (no gates yet). */
const CFG = window.FILG || {};
let SID = null, S = null, MODE = 'build', ZOOM = null;
let SEL = new Set();            // selected brainstorm option ids
let PENDING_FORK = null;        // a pivot fork awaiting discard/pivot

// ── tiny helpers ──────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function toast(msg,kind){const t=document.createElement('div');t.className='toast'+(kind?' '+kind:'');t.textContent=msg;
  $('toasts').appendChild(t);setTimeout(()=>t.remove(),3200);}
function mdToHtml(t){ if(!t)return'';
  t=esc(t).replace(/^#{1,6} (.*)$/gm,'<h3>$1</h3>').replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')
   .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g,'<a href="$2" target=_blank rel=noopener>$1</a>');
  return t.split(/\n{2,}/).map(p=>/^<h3>/.test(p)?p:'<p>'+p.replace(/\n/g,'<br>')+'</p>').join('');
}
async function api(method,url,body){
  const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},
    body:body?JSON.stringify(body):undefined});
  let d={}; try{d=await r.json();}catch(e){}
  return {ok:r.ok,status:r.status,d};
}
function v2login(){ toast('Login lands later — everything runs unlocked for now.'); }

// ── Landing ─────────────────────────────────────────────────────────────────
let LANDING_HELP=false;
function toggleLandingHelp(){
  LANDING_HELP=!LANDING_HELP;
  $('landing-wrap').classList.toggle('help',LANDING_HELP);
  $('landing-q').classList.toggle('on',LANDING_HELP);
  const box=$('landing-box');
  box.placeholder=LANDING_HELP
    ? "'how does this work?', 'what does this cost?'…"
    : "e.g. 'I want to make my dog internet famous', 'I have a truck, some tools, and free time', 'I'm a book worm with a bad back who likes turtles'…";
}
async function startFromLanding(){
  const idea=$('landing-box').value.trim();
  $('landing-err').textContent='';
  if(idea.length<12){$('landing-err').textContent='Tell me a bit more about the idea.';return;}
  const btn=$('landing-start'); btn.disabled=true; btn.textContent='Thinking…';
  const {ok,d}=await api('POST','/api/brainstorm',{idea});
  btn.disabled=false; btn.textContent='Start →';
  if(d&&d.gibberish){ showLandingJoke(d); return; }
  if(!ok){$('landing-err').textContent=(d&&d.error)||'Something went wrong.';return;}
  SID=d.id; showWorkspace(); render(d);
}
function showLandingJoke(d){
  $('landing-joke').innerHTML=`<div class=jokecard><h3>${esc(d.title||"That's not an idea yet.")}</h3>`+
    `<div>${mdToHtml(d.body||'')}</div></div>`;
}
function showWorkspace(){ $('landing').hidden=true; $('workspace').hidden=false; }
function v2newPlan(){ location.href='/v2'; }

// ── Prompt box + modes ───────────────────────────────────────────────────────
function setMode(m){
  MODE=m;
  document.querySelectorAll('#modechips .mchip').forEach(b=>b.classList.toggle('on',b.dataset.mode===m));
  const w=$('ws-wrap'); w.className='promptwrap'+(m!=='build'?' '+m:'');
  if(m==='build'){ closeTool(); } else { openTool(m); }
}
async function sendPrompt(){
  const box=$('ws-box'), prompt=box.value.trim(); if(!prompt) return;
  $('ws-err').textContent='';
  const btn=$('ws-send'); btn.disabled=true;
  const {ok,d}=await api('POST',`/api/plan/${SID}/route`,{prompt,mode:MODE});
  btn.disabled=false;
  if(!ok){$('ws-err').textContent=(d&&d.error)||'Could not route that.';return;}
  box.value='';
  if(d.fork){ PENDING_FORK=d.fork; ZOOM=null; renderStage(S); return; }
  const dec=d.decision||{};
  toast(dec.say||'On it.');
  await dispatch(dec);
}
async function dispatch(dec){
  switch(dec.intent){
    case 'commit': return commit();
    case 'diverge': return reBrainstorm(S.idea);
    case 'restart_keep': return reBrainstorm((dec.keep?dec.keep+' — ':'')+S.idea);
    case 'restart_hard': if(confirm('Throw it all out and start fresh?')) v2newPlan(); return;
    case 'ask': setMode(dec.target==='research'||dec.target==='board'||dec.target==='help'?dec.target:'build');
                return;
    case 'steer': default: return steer(dec.steer||'');
  }
}
async function steer(note){
  if(!note) return;
  const stage=S&&S.stage;
  if(stage==='building'||stage==='done'){
    // in-place rework of the current part (the pivot fork already cleared this as integrable)
    await run(`/api/plan/${SID}/redraft`,{feedback:note},'Reworking this part');
  } else {
    // upstream: fold the note into a fresh spread
    await reBrainstorm(note+' — '+(S.idea||''));
  }
}

// ── Funnel actions ───────────────────────────────────────────────────────────
async function reBrainstorm(idea){
  const {ok,d}=await api('POST','/api/brainstorm',{idea});
  if(!ok){toast((d&&d.error)||'Could not re-spread.','err');return;}
  SID=d.id; SEL=new Set(); ZOOM=null; render(d);
}
async function doMerge(){
  if(!SEL.size){toast('Pick at least one direction.','err');return;}
  const {ok,d}=await api('POST',`/api/plan/${SID}/merge`,{options:[...SEL]});
  if(!ok){toast((d&&d.error)||'Could not merge.','err');return;}
  poll();
}
async function commit(thesis){
  const {ok,d}=await api('POST',`/api/plan/${SID}/commit`,thesis?{thesis}:{});
  if(!ok){toast((d&&d.error)||'Could not start the build.','err');return;}
  poll();
}
function commitFromBrainstorm(){
  if(!SEL.size){toast('Check a direction to build, or refine first.','err');return;}
  const chosen=(S.activeNode.options||[]).filter(o=>SEL.has(o.id)).map(o=>o.direction.one_liner||o.direction.title);
  commit(chosen.join(' + '));
}
async function keepGoing(){
  await run(`/api/plan/${SID}/next`,{feedback:''},'Building the next part');
}
async function run(url,body,label){
  const stage=$('stagewrap'); const prev=stage.innerHTML;
  stage.insertAdjacentHTML('afterbegin',`<div class=card><span class=thinking>${esc(label)}…</span></div>`);
  const {ok,d}=await api('POST',url,body);
  if(!ok){toast((d&&d.error)||'Something went wrong.','err');stage.innerHTML=prev;return;}
  render(d);
}
async function getState(){ const {d}=await api('GET',`/api/plan/${SID}`); return d; }
async function poll(){
  const s=await getState(); render(s);
  if(s.status==='researching') setTimeout(poll,1200);
}

// ── Pivot fork ────────────────────────────────────────────────────────────────
function discardFork(){ PENDING_FORK=null; renderStage(S); toast('Dropped it, carrying on.'); }
function pivotFork(){ const steer=PENDING_FORK&&PENDING_FORK.steer; PENDING_FORK=null;
  reBrainstorm((steer?steer+' — ':'')+(S.idea||'')); }

// ── Render ────────────────────────────────────────────────────────────────────
function render(s){ if(s&&s.id){S=s;} renderSpine(S); renderTree(S); if(!ZOOM) renderStage(S); }

function renderStage(s){
  const wrap=$('stagewrap');
  if(PENDING_FORK){ wrap.innerHTML=forkHtml(PENDING_FORK); return; }
  if(!s){ wrap.innerHTML=''; return; }
  if(s.status==='error'){ wrap.innerHTML=`<div class=card><p class=eyebrow>Snag</p><p>${esc(s.error||'')}</p></div>`; return; }
  if(s.status==='researching'){ wrap.innerHTML=spewHtml(s); return; }
  if(s.stage==='brainstorm') return renderBrainstorm(s);
  if(s.stage==='refined') return renderRefined(s);
  if(s.stage==='building'||s.stage==='done') return renderPlan(s);
  wrap.innerHTML='';
}
function spewHtml(s){
  const lines=(s.progress||[]).filter(l=>l.indexOf('§')!==0);
  return `<div class=card><p class=eyebrow>The machine</p>`+
    `<div class=spew>${lines.map(l=>`<div>${esc(l)}</div>`).join('')||'<div class=thinking>Spinning up…</div>'}</div></div>`;
}
function renderBrainstorm(s){
  const opts=(s.activeNode&&s.activeNode.options)||[];
  const cards=opts.map(o=>{const d=o.direction||{};const on=SEL.has(o.id);
    return `<label class="opt${on?' sel':''}"><input type=checkbox ${on?'checked':''} onchange="toggleSel('${o.id}')">`+
      `<div><h3>${esc(d.title||'Direction')}</h3><p>${esc(d.one_liner||'')}</p>`+
      `${d.mold?`<span class=mold>${esc(d.mold)}</span>`:''}</div></label>`;}).join('');
  $('stagewrap').innerHTML=`<div class=card><p class=eyebrow>A few directions ${s.activeNode&&s.activeNode.spread==='tight'?'(your idea, sharpened)':'(pick what clicks)'}</p>`+
    `<div class=optgrid>${cards}</div>`+
    `<div class=ctarow><button class=stage-cta onclick=doMerge()>Let's try it →</button>`+
    `<button class="stage-cta secondary" onclick=commitFromBrainstorm()>I'm sold, build the plan</button></div>`+
    `<p class=thinking>Or just type in the box, it always wins.</p></div>`;
}
function toggleSel(id){ if(SEL.has(id))SEL.delete(id); else SEL.add(id); renderBrainstorm(S); }
function renderRefined(s){
  const a=s.activeNode||{};
  const kept=(a.kept||[]).map(k=>`<li>${esc(k)}</li>`).join('');
  const dropped=(a.dropped||[]).map(d=>`<li>${esc(d.thread)} <span class=why>— ${esc(d.why)}</span></li>`).join('');
  const R=(a.research&&a.research.prose)||{};
  const rows=((a.research&&a.research.rows)||[]).slice(0,4).map(x=>
    `<li>${x.mark==='ok'?'✅':'⚠️'} ${esc(x.text)} <span class="badge ${x.mark==='ok'?'b-ok':'b-warn'}">${x.mark==='ok'?'cited':'vendor'}</span></li>`).join('');
  $('stagewrap').innerHTML=`<div class=card><p class=eyebrow>Your refined idea</p>`+
    `<p class=react>${esc(a.thesis||'')}</p>`+
    (a.mold?`<span class=mold>${esc(a.mold)}</span>`:'')+
    (kept?`<p class=eyebrow style="margin-top:14px">Kept</p><ul class=kept>${kept}</ul>`:'')+
    (dropped?`<p class=eyebrow>Cut (and why)</p><ul class=dropped>${dropped}</ul>`:'')+
    (R.offer?`<p class=eyebrow>First-pass read</p><p><b>${esc(R.title||'')}</b> — ${esc(R.offer)}</p>`:'')+
    (rows?`<ul class=ev>${rows}</ul>`:'')+
    `<div class=ctarow><button class=stage-cta onclick="commit()">I'm sold, build the plan →</button></div>`+
    `<p class=thinking>Not quite? Steer it in the box.</p></div>`;
}
function renderPlan(s){
  const wrap=$('stagewrap');
  if(s.stage==='done'){
    const files=(s.files||[]).map(f=>`<li>${esc(f.path)}</li>`).join('');
    wrap.innerHTML=`<div class=card><p class=eyebrow>Done</p><h3>🎉 Your plan is ready</h3>`+
      `<ul>${files}</ul></div>`; return;
  }
  const step=s.step||0;
  let html='';
  if(step===0){ html+=firstPageHtml(s); }         // the gut-check card, not a wall of text
  else if(s.proposal){
    html+=`<div class=card><p class=eyebrow>Part ${step+1} of ${s.total}</p>`+
      `<h3>${esc(s.proposal.title||'')}</h3><div class="draft">${mdToHtml(s.proposal.draft||'')}</div></div>`;
  }
  html+=`<div class=ctarow><button class=stage-cta onclick=keepGoing()>Keep going →</button></div>`+
    `<p class=thinking>Comment or steer in the box anytime, it wins.</p>`;
  wrap.innerHTML=html;
}
function firstPageHtml(s){
  const v=s.vetting||{}, R=(s.research&&s.research.prose)||{};
  const MT={'full-time':'Full time','side-hustle':'Side hustle','seasonal':'Seasonal','one-shot':'One shot','gig':'Gig','scalable':'Scalable'};
  const verdict=v.verdict?`<span class="verdict ${esc(v.verdict)}">${esc(v.verdict)}</span>`:'';
  const mt=v.model_type&&MT[v.model_type]?`<span class=mold>${esc(MT[v.model_type])}</span>`:'';
  const points=[['What you'+"'"+'d sell',R.offer],['How you'+"'"+'d win it',R.gtm],['Biggest risk',v.biggest_risk||v.first_test]]
    .filter(p=>p[1]).map(p=>`<li><b>${esc(p[0])}</b>${esc(p[1])}</li>`).join('');
  return `<div class=card><p class=eyebrow>Is this serious?</p>${verdict}${mt}`+
    `<p class=react>${esc(v.reaction||R.title||'Here'+"'"+'s your idea, graded.')}</p>`+
    `<ul class=keypoints>${points}</ul></div>`;
}
function forkHtml(f){
  return `<div class=fork><p class=eyebrow>We tried to work that in</p>`+
    `<p>${esc(f.clash||'That clashes with the committed idea.')}</p>`+
    (f.skeptic_say?`<p class=skept>🧐 ${esc(f.skeptic_say)}</p>`:'')+
    `<div class=ctarow><button class="stage-cta secondary" onclick=discardFork()>Discard the idea</button>`+
    `<button class=stage-cta onclick=pivotFork()>Pivot the plan →</button></div></div>`;
}

// ── The decision graph: spine (right) + tree list (left) ─────────────────────
function _nodesById(s){const t=(s&&s.tree)||{};const m={};(t.nodes||[]).forEach(n=>m[n.id]=n);return {m,t};}
function _pathSet(s){const {m,t}=_nodesById(s);const set={};let cur=t.active;
  while(cur!=null&&m[cur]){set[cur]=1;cur=m[cur].parent;}return set;}
function renderSpine(s){
  const el=$('spine'); if(!el||!s||!s.tree){if(el)el.innerHTML='';return;}
  const {m,t}=_nodesById(s); const path=_pathSet(s);
  // the active path, root→active, as a horizontal spine
  const chain=[]; let cur=t.active; while(cur!=null&&m[cur]){chain.unshift(m[cur]);cur=m[cur].parent;}
  el.innerHTML=chain.map((n,i)=>{
    const on=n.id===t.active;
    return (i?'<span class=sarrow>→</span>':'')+
      `<button class="snode${on?' on':''}" onclick="zoomNode('${n.id}')">${esc(nodeLabel(n))}</button>`;
  }).join('');
}
function nodeLabel(n){
  if(n.kind==='brainstorm')return 'Directions';
  if(n.kind==='option')return n.title||'Option';
  if(n.kind==='refined')return 'Refined idea';
  if(n.kind==='fork')return 'Fork';
  return n.title||('Part '+((n.step||0)+1));
}
function renderTree(s){
  const el=$('tree'); if(!el){return;} if(!s||!s.tree){el.innerHTML='';return;}
  const {m,t}=_nodesById(s); const path=_pathSet(s);
  const kids={}; (t.nodes||[]).forEach(n=>kids[n.id]=[]);
  (t.nodes||[]).forEach(n=>{if(n.parent!=null&&kids[n.parent])kids[n.parent].push(n.id);});
  const roots=(t.nodes||[]).filter(n=>n.parent==null).map(n=>n.id);
  function row(id,depth){const n=m[id];const on=id===t.active;
    const nottaken=!path[id]&&!on&&n.kind==='option'&&_hasChosenSibling(m,kids,n);
    const cls='tnode'+(on?' on':'')+(path[id]?' path':'')+(nottaken?' nottaken':'');
    let h=`<button class="${cls}" style="padding-left:${8+depth*12}px" onclick="zoomNode('${id}')">`+
      `<span class=tk>${esc(n.kind||'part')}</span>${esc(nodeLabel(n))}</button>`;
    (kids[id]||[]).forEach(c=>h+=row(c,depth+1));
    return h;
  }
  el.innerHTML=roots.map(r=>row(r,0)).join('');
}
function _hasChosenSibling(m,kids,n){
  // an option is a "path not taken" once a refined node was built from its siblings
  const sibs=(kids[n.parent]||[]).map(id=>m[id]);
  const refinedParent=(kids[n.parent]||[]).some(id=>m[id].kind==='refined');
  return refinedParent && n.kind==='option';
}

// ── Node zoom: modal within the right panel ──────────────────────────────────
async function zoomNode(nid){
  const {m,t}=_nodesById(S);
  if(nid===t.active){ ZOOM=null; renderStage(S); return; }   // clicking the active node returns to live
  ZOOM=nid;
  const {ok,d}=await api('GET',`/api/plan/${SID}/node/${nid}`);
  if(!ok){toast('Could not open that node.','err');ZOOM=null;return;}
  $('stagewrap').innerHTML=nodePageHtml(d);
}
function backToCurrent(){ ZOOM=null; renderStage(S); }
function nodePageHtml(n){
  let body='';
  if(n.kind==='section'){ body=`<h3>${esc(n.title||'')}</h3><div class=draft>${mdToHtml(n.content||n.draft||'')}</div>`; }
  else if(n.kind==='option'){ const d=n.direction||{}; body=`<h3>${esc(d.title||'')}</h3><p>${esc(d.one_liner||'')}</p>${d.mold?`<span class=mold>${esc(d.mold)}</span>`:''}`; }
  else if(n.kind==='refined'){ body=`<h3>Refined idea</h3><p class=react>${esc(n.thesis||'')}</p>`; }
  else if(n.kind==='brainstorm'){ body=`<h3>Directions</h3><p class=thinking>The fork where a few directions were offered.</p>`; }
  else { body=`<h3>${esc(n.title||'Node')}</h3>`; }
  return `<div class=card><button class="ghost small" onclick=backToCurrent()>← back to where you are</button>`+
    `<div style="margin-top:10px">${body}</div></div>`;
}

// ── Tool drawers (research / board / help) — one box, colored context ────────
function openTool(mode){
  const dr=$('tooldrawer'); dr.hidden=false; dr.className='tooldrawer '+mode;
  $('drawerback').style.display='block';
  $('tool-title').textContent={research:'Check the facts',board:'Board of Directors',help:'Help'}[mode]||'Tool';
  $('tool-body').innerHTML=toolBody(mode);
}
function closeTool(){ $('tooldrawer').hidden=true; $('drawerback').style.display='none';
  if(MODE!=='build'){ MODE='build'; document.querySelectorAll('#modechips .mchip').forEach(b=>b.classList.toggle('on',b.dataset.mode==='build'));
    $('ws-wrap').className='promptwrap'; } }
function toolBody(mode){
  if(mode==='research'){
    const rows=((S&&S.research&&S.research.rows)||[]);
    if(!rows.length)return '<p class=thinking>No graded research yet — it lands after you commit to the deep run. Ask a question in the box and I\'ll dig.</p>';
    return '<ul class=ev>'+rows.map(x=>`<li>${x.mark==='ok'?'✅':'⚠️'} ${esc(x.text)} <span class="badge ${x.mark==='ok'?'b-ok':'b-warn'}">${x.mark==='ok'?'cited':'vendor'}</span></li>`).join('')+'</ul>';
  }
  if(mode==='board'){ return '<p class=thinking>Your board of advisors reviews each part and can be convened. (Re-homing into v2 next.) Type a question in the box to aim them.</p>'; }
  return '<p>Type your idea on the landing page, then watch it spread into a few directions, merge the ones you like, and research + build the plan. The prompt box always wins: steer, jump ahead, or start over from it anytime. It runs on us to start.</p>';
}

// boot: if we ever deep-link a plan, could load it; for now the landing drives everything.
