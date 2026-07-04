/* FILG v2 — the unified build surface.
   LEFT  = the chat: one prompt box (color-moded: build/research/board/help) + tool drawers.
   RIGHT = the decision graph, the whole panel. Documents and steps live INSIDE the nodes:
     - you're on a node (zoomed in, reading its content)
     - you type / choose → the content collapses back into the node, the camera pulls out
     - new lines + nodes DRAW in real time out of your node; a working node streams its own
       processing, research leaflets fan out and resolve; the camera follows the build
     - when the next step/choice is ready, the camera zooms into it and it expands in place
     - zoom/drag anytime to browse; every past node re-expands with its content + build log.
   Everything routes through /api/plan/{sid}/route or the funnel routes. Unlocked (gates later). */
const CFG = window.FILG || {};
let SID = null, S = null, MODE = 'build';
let SEL = new Set();            // selected brainstorm option ids
let PENDING_FORK = null;        // a pivot fork awaiting discard/pivot

// ── tiny helpers ─────────────────────────────────────────────────────────────
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
function v2login(){ toast('Login is stubbed locally — identity is your FILG_DEV_EMAIL.'); }
// Central handler for a gated API error (the prod wall): needKey → the key modal; fairUse → the
// allowance message. Returns true if it handled the response.
function gateV2(d){
  if(!d)return false;
  if(d.fairUse){ toast(d.error||'Monthly allowance used — add your own key or wait for the reset.','err'); keyModal(); return true; }
  if(d.needKey){ toast('Add your own API key to keep building.','err'); keyModal(); return true; }
  return false;
}

// ── The API-key hierarchy: hosted (FILG's key) vs BYOK (your own). Same /api/key endpoints as the
// legacy page; adding/removing a key flips the dev identity between hosted and BYOK (=_provider_for). ──
let BYOK_ON=false, HAS_KEY=false, KEY_META=null, KEY_PROV='openrouter';
async function loadKey(){
  const {ok,d}=await api('GET','/api/key');
  BYOK_ON=!!(ok&&d&&d.enabled); HAS_KEY=!!(ok&&d&&d.key); KEY_META=(d&&d.key)||null;
  keyIndicator();
}
function keyIndicator(){
  const dot=$('keydot'); if(!dot)return;
  if(!BYOK_ON){ dot.textContent='BYOK off'; dot.className='keydot off'; }
  else if(HAS_KEY){ dot.textContent='on: your key ••'+(KEY_META.last4||'????'); dot.className='keydot byok'; }
  else { dot.textContent='on: FILG key'; dot.className='keydot'; }
}
function openModal(title){ $('modal-title').textContent=title; $('v2modal').hidden=false; $('modalback').classList.add('show'); }
function closeModal(){ $('v2modal').hidden=true; $('modalback').classList.remove('show'); }
async function keyModal(){
  await loadKey();
  if(!BYOK_ON){ toast('BYOK is off — set FILG_KEY_SECRET to enable the key store.','err'); return; }
  if(HAS_KEY){
    $('modal-body').innerHTML=`<p class=muted>Running on your own <b>${esc(KEY_META.provider)}</b> key (••${esc(KEY_META.last4||'')}). Remove it to fall back to FILG's hosted key.</p>`;
    $('modal-acts').innerHTML=`<button onclick="keyForm()">Replace</button><button onclick="removeKey()">Remove key</button><button class=primary onclick="closeModal()">Done</button>`;
    openModal('Your API key');
  } else { keyForm(); }
}
function setKeyProv(p){ KEY_PROV=(p==='anthropic')?'anthropic':'openrouter';
  document.querySelectorAll('#provsw button').forEach(b=>b.classList.toggle('on',b.dataset.p===KEY_PROV));
  const inp=$('keyinput'); if(inp)inp.placeholder=(KEY_PROV==='anthropic')?'sk-ant-…':'sk-or-v1-…'; }
function keyPrefixDetect(v){ v=(v||'').trim(); if(v.indexOf('sk-ant-')===0)setKeyProv('anthropic'); else if(v.indexOf('sk-or-')===0)setKeyProv('openrouter'); }
function keyForm(){
  $('modal-body').innerHTML=
    `<p class=muted>Paste your own OpenRouter or Anthropic key. We validate it, store it encrypted, and this identity's runs switch to it. Remove it any time to go back to the hosted key.</p>`+
    `<div class=provsw id=provsw><button type=button data-p=openrouter onclick="setKeyProv('openrouter')">OpenRouter</button><button type=button data-p=anthropic onclick="setKeyProv('anthropic')">Anthropic</button></div>`+
    `<input id=keyinput type=password autocomplete=off spellcheck=false placeholder="sk-or-v1-…" oninput="keyPrefixDetect(this.value)">`+
    `<div class=err id=keyerr></div>`;
  $('modal-acts').innerHTML=`<button onclick="closeModal()">Cancel</button><button class=primary id=keysave onclick="saveKey()">Save &amp; validate</button>`;
  openModal('Bring your own key'); setKeyProv(KEY_PROV);
  setTimeout(()=>{const i=$('keyinput');if(i)i.focus();},40);
}
async function saveKey(){
  const inp=$('keyinput'), er=$('keyerr'), btn=$('keysave');
  const key=(inp.value||'').trim(); er.textContent='';
  if(key.length<8){er.textContent='That does not look like a key.';return;}
  btn.disabled=true; btn.textContent='Validating…';
  const {ok,d}=await api('POST','/api/key',{provider:KEY_PROV,key});
  if(!ok){ er.textContent=(d&&d.error)||'Could not save the key.'; btn.disabled=false; btn.textContent='Save & validate'; return; }
  await loadKey(); closeModal(); toast('Key saved — runs now use your key. ✓');
}
async function removeKey(){
  const {ok,d}=await api('POST','/api/key/remove',{});
  if(!ok){ toast((d&&d.error)||'Could not remove the key.','err'); return; }
  await loadKey(); closeModal(); toast('Key removed — back on the hosted FILG key.');
}

// ── Landing ──────────────────────────────────────────────────────────────────
let LANDING_HELP=false;
function toggleLandingHelp(){
  LANDING_HELP=!LANDING_HELP;
  $('landing-wrap').classList.toggle('help',LANDING_HELP);
  $('landing-q').classList.toggle('on',LANDING_HELP);
  $('landing-box').placeholder=LANDING_HELP
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
  if(d&&d.gibberish){ $('landing-joke').innerHTML=`<div class=jokecard><h3>${esc(d.title||"That's not an idea yet.")}</h3><div>${mdToHtml(d.body||'')}</div></div>`; return; }
  if(!ok){ if(gateV2(d))return; $('landing-err').textContent=(d&&d.error)||'Something went wrong.'; return; }
  adoptPlan(d);
  $('landing').hidden=true; $('workspace').hidden=false;
  requestAnimationFrame(()=>render(d));   // first layout after the panel is visible (it needs real sizes)
}
function v2newPlan(){ location.href='/v2'; }
function adoptPlan(d){ SID=d.id; SEL=new Set(); PENDING_FORK=null; resetGraph(); }

// ── Prompt box + modes ───────────────────────────────────────────────────────
function setMode(m){
  MODE=m;
  document.querySelectorAll('#modechips .mchip').forEach(b=>b.classList.toggle('on',b.dataset.mode===m));
  $('ws-wrap').className='promptwrap'+(m!=='build'?' '+m:'');
  if(m==='build'){ closeTool(); } else { openTool(m); }
}
async function sendPrompt(){
  const box=$('ws-box'), prompt=box.value.trim(); if(!prompt) return;
  $('ws-err').textContent='';
  const btn=$('ws-send'); btn.disabled=true;
  const {ok,d}=await api('POST',`/api/plan/${SID}/route`,{prompt,mode:MODE});
  btn.disabled=false;
  if(!ok){ if(gateV2(d))return; $('ws-err').textContent=(d&&d.error)||'Could not route that.'; return; }
  box.value='';
  if(d.fork){ PENDING_FORK=d.fork; focusActive(true); return; }
  const dec=d.decision||{};
  toast(dec.say||'On it.');
  await dispatch(dec);
}
async function dispatch(dec){
  switch(dec.intent){
    case 'commit':
      if(dec.confirm&&!confirm('Commit to the full research + build?'))return;
      return commit();
    case 'diverge': return reBrainstorm(S.idea);
    case 'restart_keep': return reBrainstorm((dec.keep?dec.keep+' — ':'')+S.idea);
    case 'restart_hard': if(confirm('Throw it all out and start fresh?')) v2newPlan(); return;
    case 'ask': setMode(['research','board','help'].includes(dec.target)?dec.target:'build'); return;
    case 'steer': default: return steer(dec.steer||'');
  }
}
async function steer(note){
  if(!note) return;
  if(S&&(S.stage==='building'||S.stage==='done')){
    await run(`/api/plan/${SID}/redraft`,{feedback:note},'Reworking this part');
  } else {
    await reBrainstorm(note+' — '+(S&&S.idea||''));   // upstream: fold the note into a fresh spread
  }
}

// ── Funnel actions ───────────────────────────────────────────────────────────
async function reBrainstorm(idea){
  const {ok,d}=await api('POST','/api/brainstorm',{idea});
  if(!ok){ if(gateV2(d))return; toast((d&&d.error)||'Could not re-spread.','err'); return; }
  adoptPlan(d); render(d);
}
async function doMerge(){
  if(!SEL.size){toast('Pick at least one direction.','err');return;}
  const {ok,d}=await api('POST',`/api/plan/${SID}/merge`,{options:[...SEL]});
  if(!ok){ if(gateV2(d))return; toast((d&&d.error)||'Could not merge.','err'); return; }
  beginWip('Merging your picks + first-pass research'); poll();
}
async function commit(thesis){
  const {ok,d}=await api('POST',`/api/plan/${SID}/commit`,thesis?{thesis}:{});
  if(!ok){ if(gateV2(d))return; toast((d&&d.error)||'Could not start the build.','err'); return; }
  beginWip('Deep research: pulling + grading sources'); poll();
}
function commitFromBrainstorm(){
  if(!SEL.size){toast('Check a direction to build, or refine first.','err');return;}
  const chosen=(stageOptions()||[]).filter(o=>SEL.has(o.id)).map(o=>o.direction.one_liner||o.direction.title);
  commit(chosen.join(' + '));
}
async function keepGoing(){ await run(`/api/plan/${SID}/next`,{feedback:''},'Writing the next part'); }
async function run(url,body,label){
  beginWip(label);
  const {ok,d}=await api('POST',url,body);
  endWip();
  if(!ok){ render(S); if(gateV2(d))return; toast((d&&d.error)||'Something went wrong.','err'); return; }
  render(d);
}
async function poll(){
  const {d:s}=await api('GET',`/api/plan/${SID}`);
  if(s&&s.id)render(s);
  if(s&&s.status==='researching')setTimeout(poll,1100);
}
function discardFork(){ PENDING_FORK=null; focusActive(true); toast('Dropped it, carrying on.'); }
function pivotFork(){ const st=PENDING_FORK&&PENDING_FORK.steer; PENDING_FORK=null;
  reBrainstorm((st?st+' — ':'')+(S&&S.idea||'')); }

// ═══ THE DECISION GRAPH — the whole right panel ═══════════════════════════════
// Documents/steps live INSIDE nodes; the camera does the storytelling: zoom out on prompt, follow the
// build as edges draw + leaflets fan, zoom into whatever's ready next. Drag to pan, wheel to zoom.
const NW=190, NFOCUS=560, NH=58, COLW=225, ROWH=125, PADX=70, PADY=48;
let VIEW={x:0,y:0,k:1}, GSEEN=new Set(), FOCUS=null, BROWSING=false, LAST_ACTIVE=null, WAS_RESEARCHING=false;
let WIP_LABEL=null, WIPLOG=[], LANES=[], LANES_DONE=new Set(), PROG_N=0;
const NODECACHE={}, NODELOG={};   // fetched past-node content · per-node build-log stash
function resetGraph(){ GSEEN=new Set(); FOCUS=null; BROWSING=false; LAST_ACTIVE=null; WAS_RESEARCHING=false;
  WIP_LABEL=null; WIPLOG=[]; LANES=[]; LANES_DONE=new Set(); PROG_N=0;
  Object.keys(NODECACHE).forEach(k=>delete NODECACHE[k]); Object.keys(NODELOG).forEach(k=>delete NODELOG[k]);
  const gn=$('gnodes'); if(gn)gn.innerHTML=''; const ge=$('gedges'); if(ge)ge.innerHTML=''; }
function beginWip(label){ WIP_LABEL=label; WIPLOG=[]; LANES=[]; LANES_DONE=new Set();
  FOCUS=null; BROWSING=false; renderGraph(); }   // content collapses back into the node, camera pulls out
function endWip(){ WIP_LABEL=null; }
function nodesOf(s){const t=(s&&s.tree)||{};const m={};(t.nodes||[]).forEach(n=>m[n.id]=n);return {m,t};}
function pathSetOf(m,active){const set={};let cur=active;while(cur!=null&&m[cur]){set[cur]=1;cur=m[cur].parent;}return set;}
function nodeLabel(n){
  if(n.kind==='brainstorm')return 'A few directions';
  if(n.kind==='refined')return 'Refined idea';
  if(n.kind==='fork')return 'Fork';
  return n.title||('Part '+((n.step||0)+1));
}
const KICON={brainstorm:'✳',option:'◇',refined:'◆',fork:'⑂',section:'▤'};
function drainProgress(s){   // stream progress into the working node (sentinels drive the leaflets)
  const prog=s.progress||[];
  for(let i=PROG_N;i<prog.length;i++){
    const ln=prog[i]||'';
    if(ln.indexOf('§LANES§')===0){ try{LANES=JSON.parse(ln.slice(7));LANES_DONE=new Set();}catch(e){} }
    else if(ln.indexOf('§LANEDONE§')===0){ LANES_DONE.add(parseInt(ln.slice(10),10)); }
    else if(ln.indexOf('🔎')===0){ /* per-lane detail → the leaflets already show it */ }
    else { WIPLOG.push(ln); if(WIPLOG.length>7)WIPLOG.shift(); }
  }
  PROG_N=Math.max(PROG_N,prog.length);
}
function layoutGraph(m){
  const kids={},roots=[];
  Object.values(m).forEach(n=>{kids[n.id]=kids[n.id]||[];});
  Object.values(m).forEach(n=>{ if(n.parent!=null&&kids[n.parent])kids[n.parent].push(n.id); else if(n.parent==null)roots.push(n.id); });
  const depth={},pos={};
  const dq=[...roots]; roots.forEach(r=>depth[r]=0);
  while(dq.length){const id=dq.shift();(kids[id]||[]).forEach(c=>{depth[c]=depth[id]+1;dq.push(c);});}
  let slot=0; const X={};
  function place(id){const ks=kids[id]||[];
    if(!ks.length){X[id]=slot++;} else {ks.forEach(place);X[id]=(X[ks[0]]+X[ks[ks.length-1]])/2;}}
  roots.forEach(place);
  Object.keys(m).forEach(id=>{pos[id]={x:PADX+(X[id]||0)*COLW, y:PADY+(depth[id]||0)*ROWH};});
  return {pos,kids,roots};
}
function renderGraph(){
  const s=S; const gn=$('gnodes'), ge=$('gedges'); if(!s||!gn)return;
  const {m,t}=nodesOf(s); const active=t.active;
  const {pos,kids}=layoutGraph(m);
  const onPath=pathSetOf(m,active);
  const wip=(WIP_LABEL||s.status==='researching')?active:null;
  // nodes: keyed divs, moved (CSS transition) or created (.enter → fade in)
  const live=new Set(Object.keys(m));
  gn.querySelectorAll('.gnode').forEach(el=>{ if(!live.has(el.dataset.id)) el.remove(); });
  Object.values(m).forEach(n=>{
    const p=pos[n.id]; const isWip=n.id===wip, isFocus=n.id===FOCUS&&!isWip;
    const dim=n.kind==='option'&&!onPath[n.id]&&(kids[n.parent]||[]).some(id=>m[id]&&m[id].kind==='refined');
    let el=gn.querySelector(`.gnode[data-id="${n.id}"]`);
    const fresh=!el;
    if(fresh){ el=document.createElement('div'); el.dataset.id=n.id; el.classList.add('enter');
      el.addEventListener('click',e=>{ if(!e.target.closest('.nbody'))gNodeClick(n.id); }); gn.appendChild(el); }
    el.className='gnode'+(fresh?' enter':'')+(n.id===active?' on':'')+(onPath[n.id]?' path':'')+(dim?' dim':'')+(isWip?' wip':'')+(isFocus?' focus':'');
    const w=isFocus?NFOCUS:(isWip?250:NW);
    el.style.left=(p.x-(w-NW)/2)+'px'; el.style.top=p.y+'px';
    el.title=isFocus?'':((n.feedback?('↳ '+n.feedback+'\n'):'')+nodeLabel(n));
    let inner=`<div class=nk><span aria-hidden=true>${KICON[n.kind]||'▤'}</span>${esc(n.kind||'part')}</div>`+
      `<div class=nt>${esc(nodeLabel(n))}</div>`;
    if(isWip){   // the node's own processing: label + the live receipt tail (leaflets fan outside)
      inner+=`<div class=nk style="margin-top:6px">⚙ ${esc(WIP_LABEL||'Working')}</div>`;
      inner+=`<div class=nspew>`+(WIPLOG.length?WIPLOG.map(l=>`<div>${esc(l)}</div>`).join(''):'<div>starting up…</div>')+`</div>`;
    }
    if(isFocus)inner+=`<div class=nbody>${nodeBody(n)}</div>`;
    el.innerHTML=inner;
    if(fresh)requestAnimationFrame(()=>requestAnimationFrame(()=>el.classList.remove('enter')));
  });
  renderLeaves(gn,pos,wip);
  // edges
  let maxX=0,maxY=0; Object.values(pos).forEach(p=>{maxX=Math.max(maxX,p.x+NFOCUS);maxY=Math.max(maxY,p.y+ROWH);});
  ge.setAttribute('width',maxX+PADX); ge.setAttribute('height',maxY+PADY);
  ge.innerHTML=Object.values(m).filter(n=>n.parent!=null&&m[n.parent]).map(n=>{
    const a=pos[n.parent],b=pos[n.id];
    const x1=a.x+NW/2,y1=a.y+NH,x2=b.x+NW/2,y2=b.y;
    const cls=(onPath[n.id]&&onPath[n.parent]?'on ':'')+(GSEEN.has(n.id)?'':'new');
    return `<path class="${cls}" pathLength=1 d="M${x1},${y1} C${x1},${y1+46} ${x2},${y2-46} ${x2},${y2}"/>`;
  }).join('');
  Object.keys(m).forEach(id=>GSEEN.add(id));
  // camera: follow the build (center the working node), else keep the focused node centered
  if(wip&&pos[wip]){ if(!BROWSING)centerOn(pos[wip],250,0.9,0.42); }
  else if(FOCUS&&pos[FOCUS]){ if(!BROWSING)centerOn(pos[FOCUS],NFOCUS,1,0.14); }
  pill(!wip && stageSurface(S) && FOCUS!==active);
}
function renderLeaves(gn,pos,wip){   // research leaflets: sub-nodes fanning out of the working node
  const want=(wip&&LANES.length)?LANES.length:0;
  gn.querySelectorAll('.gleaf').forEach((el,i)=>{ if(i>=want)el.remove(); });
  if(!want)return;
  const p=pos[wip];
  for(let i=0;i<want;i++){
    let el=gn.querySelector(`.gleaf[data-i="${i}"]`);
    if(!el){ el=document.createElement('div'); el.dataset.i=i; el.className='gleaf enter'; el.textContent='🍃';
      gn.appendChild(el); requestAnimationFrame(()=>requestAnimationFrame(()=>el.classList.remove('enter'))); }
    const spread=(i-(want-1)/2);
    el.style.left=(p.x+125+spread*44-15)+'px';
    el.style.top=(p.y+ROWH-38)+'px';
    el.title=LANES[i]||('lane '+(i+1));
    el.classList.toggle('done',LANES_DONE.has(i));
  }
}
function centerOn(p,w,k,yFrac){   // ease the camera so node at p (width w) sits centered, yFrac down
  const r=$('right').getBoundingClientRect();
  VIEW.k=k;
  VIEW.x=r.width/2-(p.x+NW/2)*k;
  VIEW.y=r.height*yFrac-p.y*k;
  applyView(true);
}
function applyView(ease){ const v=$('gview'); if(!v)return;
  v.classList.toggle('ease',!!ease); v.style.transform=`translate(${VIEW.x}px,${VIEW.y}px) scale(${VIEW.k})`;
  if(ease)setTimeout(()=>v.classList.remove('ease'),650); }
function initGraphInput(){
  const g=$('graph'); if(!g)return;
  let drag=null,moved=false;
  g.addEventListener('pointerdown',e=>{ if(e.target.closest('.gnode.focus .nbody'))return;
    drag={x:e.clientX,y:e.clientY,vx:VIEW.x,vy:VIEW.y}; moved=false; });
  g.addEventListener('pointermove',e=>{ if(!drag)return;
    const dx=e.clientX-drag.x, dy=e.clientY-drag.y;
    if(Math.abs(dx)+Math.abs(dy)>6){ moved=true; BROWSING=true; g.classList.add('dragging'); g.setPointerCapture(e.pointerId); }
    if(moved){ VIEW.x=drag.vx+dx; VIEW.y=drag.vy+dy; applyView(false); } });
  const stop=e=>{ if(drag&&!moved&&!e.target.closest('.gnode')){ unfocus(); }   // background click → zoom out
    drag=null; g.classList.remove('dragging'); };
  g.addEventListener('pointerup',stop); g.addEventListener('pointercancel',stop);
  g.addEventListener('wheel',e=>{ e.preventDefault(); BROWSING=true;
    const r=g.getBoundingClientRect(), mx=e.clientX-r.left, my=e.clientY-r.top;
    const k2=Math.min(1.9,Math.max(0.3,VIEW.k*Math.exp(-e.deltaY*0.0012)));
    VIEW.x=mx-(mx-VIEW.x)*(k2/VIEW.k); VIEW.y=my-(my-VIEW.y)*(k2/VIEW.k); VIEW.k=k2; applyView(false);
  },{passive:false});
}
function gNodeClick(id){
  if(S&&S.status==='researching')return;         // let the machine finish
  if(FOCUS===id){ return; }                       // already reading it
  focusNode(id);
}
function focusActive(force){ const {t}=nodesOf(S); BROWSING=false; if(force)FOCUS=null; focusNode(t.active); }
function focusNode(id){
  const {m,t}=nodesOf(S); if(!m[id])return;
  FOCUS=id; BROWSING=false;
  const n=m[id];
  if(id!==t.active&&!NODECACHE[id]){   // a past node: fetch its full content, then re-render
    api('GET',`/api/plan/${SID}/node/${id}`).then(({ok,d})=>{ if(ok){NODECACHE[id]=d; if(FOCUS===id)renderGraph();} });
  }
  renderGraph();
}
function unfocus(){ if(FOCUS==null&&!BROWSING)return; FOCUS=null; BROWSING=true; renderGraph(); }
function pill(show){ const p=$('gpill'); if(p)p.hidden=!show; }

// ── What lives INSIDE a focused node ─────────────────────────────────────────
function nodeBody(n){
  const {t}=nodesOf(S);
  let body;
  if(n.id===t.active){ const surf=stageSurface(S); body=surf?surf.html:''; }
  else body=pastBody(n);
  const log=NODELOG[n.id];
  if(log&&log.length)body+=`<details class=nhist><summary>⚙ how this was built</summary><div class=nspew>`+
    log.map(l=>`<div>${esc(l)}</div>`).join('')+`</div></details>`;
  return body;
}
function pastBody(n){
  const d=NODECACHE[n.id];
  if(!d)return '<p class=thinking>opening…</p>';
  if(d.kind==='section')return `<div class=draft>${mdToHtml(d.content||d.draft||'')}</div>`;
  if(d.kind==='option'){ const x=d.direction||{}; return `<p>${esc(x.one_liner||'')}</p>${x.mold?`<span class=mold>${esc(x.mold)}</span>`:''}`; }
  if(d.kind==='refined')return `<p class=react>${esc(d.thesis||'')}</p>`+(d.mold?`<span class=mold>${esc(d.mold)}</span>`:'');
  if(d.kind==='brainstorm')return `<p class=thinking>The fork where the directions were offered.</p>`;
  return '';
}
function stageOptions(){ return (S&&S.activeNode&&S.activeNode.options)||[]; }
function stageSurface(s){
  if(!s)return null;
  if(PENDING_FORK)return {html:
    `<p class=eyebrow>We tried to work that in</p><p>${esc(PENDING_FORK.clash||'That clashes with the committed idea.')}</p>`+
    (PENDING_FORK.skeptic_say?`<p class=skept>🧐 ${esc(PENDING_FORK.skeptic_say)}</p>`:'')+
    `<div class=ctarow><button class="stage-cta secondary" onclick=discardFork()>Discard the idea</button>`+
    `<button class=stage-cta onclick=pivotFork()>Pivot the plan →</button></div>`};
  if(s.status==='error')return {html:
    `<p class=eyebrow>Hit a snag</p><p>${esc(s.error||'Something went wrong.')}</p>`+
    `<div class=ctarow><button class="stage-cta secondary" onclick=v2newPlan()>Start over</button></div>`};
  if(s.stage==='brainstorm')return {html:brainstormHtml(s)};
  if(s.stage==='refined')return {html:refinedHtml(s)};
  if(s.stage==='building')return {html:(s.step||0)===0?firstPageHtml(s):chapterHtml(s)};
  if(s.stage==='done'||s.done)return {html:doneHtml(s)};
  return null;
}
function brainstormHtml(s){
  const cards=stageOptions().map(o=>{const d=o.direction||{};const on=SEL.has(o.id);
    return `<label class="opt${on?' sel':''}"><input type=checkbox ${on?'checked':''} onchange="toggleSel('${o.id}')">`+
      `<div><h3>${esc(d.title||'Direction')}</h3><p>${esc(d.one_liner||'')}</p>`+
      `${d.mold?`<span class=mold>${esc(d.mold)}</span>`:''}</div></label>`;}).join('');
  return `<p class=eyebrow>Pick what clicks</p><div class=optgrid>${cards}</div>`+
    `<div class=ctarow><button class=stage-cta onclick=doMerge()>Let's try it →</button>`+
    `<button class="stage-cta secondary" onclick=commitFromBrainstorm()>I'm sold, build the plan</button></div>`+
    `<p class=thinking>Or just type in the box, it always wins.</p>`;
}
function toggleSel(id){ if(SEL.has(id))SEL.delete(id); else SEL.add(id); renderGraph(); }
function refinedHtml(s){
  const a=s.activeNode||{};
  const kept=(a.kept||[]).map(k=>`<li>${esc(k)}</li>`).join('');
  const dropped=(a.dropped||[]).map(d=>`<li>${esc(d.thread)} <span class=why>— ${esc(d.why)}</span></li>`).join('');
  const R=(a.research&&a.research.prose)||{};
  const rows=((a.research&&a.research.rows)||[]).slice(0,4).map(x=>
    `<li>${x.mark==='ok'?'✅':'⚠️'} ${esc(x.text)} <span class="badge ${x.mark==='ok'?'b-ok':'b-warn'}">${x.mark==='ok'?'cited':'vendor'}</span></li>`).join('');
  return `<p class=react>${esc(a.thesis||'')}</p>`+
    (a.mold?`<span class=mold>${esc(a.mold)}</span>`:'')+
    (kept?`<p class=eyebrow style="margin-top:14px">Kept</p><ul class=kept>${kept}</ul>`:'')+
    (dropped?`<p class=eyebrow>Cut (and why)</p><ul class=dropped>${dropped}</ul>`:'')+
    (R.offer?`<p class=eyebrow>First-pass read</p><p><b>${esc(R.title||'')}</b> — ${esc(R.offer)}</p>`:'')+
    (rows?`<ul class=ev>${rows}</ul>`:'')+
    `<div class=ctarow><button class=stage-cta onclick="commit()">I'm sold, build the plan →</button></div>`+
    `<p class=thinking>Not quite? Steer it in the box.</p>`;
}
function firstPageHtml(s){
  const v=s.vetting||{}, R=(s.research&&s.research.prose)||{};
  const MT={'full-time':'Full time','side-hustle':'Side hustle','seasonal':'Seasonal','one-shot':'One shot','gig':'Gig','scalable':'Scalable'};
  const verdict=v.verdict?`<span class="verdict ${esc(v.verdict)}">${esc(v.verdict)}</span>`:'';
  const mt=v.model_type&&MT[v.model_type]?`<span class=mold>${esc(MT[v.model_type])}</span>`:'';
  const points=[["What you'd sell",R.offer],["How you'd win it",R.gtm],['Biggest risk',v.biggest_risk||v.first_test]]
    .filter(p=>p[1]).map(p=>`<li><b>${esc(p[0])}</b>${esc(p[1])}</li>`).join('');
  return `<p class=eyebrow>Is this serious?</p>`+verdict+mt+
    `<p class=react>${esc(v.reaction||R.title||"Here's your idea, graded.")}</p>`+
    `<ul class=keypoints>${points}</ul>`+
    `<div class=ctarow><button class=stage-cta onclick=keepGoing()>Keep going →</button></div>`+
    `<p class=thinking>Comment or steer in the box anytime, it wins.</p>`;
}
function chapterHtml(s){
  const p=s.proposal||{};
  return `<p class=eyebrow>Part ${(s.step||0)+1} of ${s.total}</p><div class=draft>${mdToHtml(p.draft||'')}</div>`+
    `<div class=ctarow><button class=stage-cta onclick=keepGoing()>Keep going →</button></div>`+
    `<p class=thinking>Comment or steer in the box anytime, it wins.</p>`;
}
function doneHtml(s){
  const files=(s.files||[]).map(f=>`<li>${esc(f.path)}</li>`).join('');
  return `<p class=react>🎉 All ${s.total} parts, built with you.</p><ul>${files}</ul>`+
    `<div class=ctarow><a class=stage-cta href="/api/plan/${SID}/download">⬇ Raw files (.zip)</a></div>`;
}

// ── Render choreography ──────────────────────────────────────────────────────
function render(s){
  if(s&&s.id)S=s;
  if(!S)return;
  drainProgress(S);
  // Server status is the truth here: a poll-driven op (merge/commit) sets WIP_LABEL but only the
  // server knows when it's done. Sync ops (run) clear their own label via endWip before render().
  const researching=S.status==='researching';
  if(!researching&&!WAS_RESEARCHING)WIP_LABEL=null;   // belt-and-braces: never let a stale label stick
  const {t}=nodesOf(S);
  if(WAS_RESEARCHING&&!researching){          // a build just finished → stash its log on the node it made
    if(WIPLOG.length)NODELOG[t.active]=WIPLOG.slice();
    WIPLOG=[]; LANES=[]; LANES_DONE=new Set(); WIP_LABEL=null;
  }
  WAS_RESEARCHING=researching;
  if(!researching){
    if(t.active!==LAST_ACTIVE){ LAST_ACTIVE=t.active; FOCUS=t.active; BROWSING=false; }   // the next step is ready → zoom in
    else if(FOCUS==null&&!BROWSING){ FOCUS=t.active; }
  }
  renderGraph();
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

// ── boot ─────────────────────────────────────────────────────────────────────
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();unfocus();}});
initGraphInput();
loadKey();   // paint the key indicator (hosted vs BYOK) on load
