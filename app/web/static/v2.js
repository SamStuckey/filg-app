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

// ── Model stacks: the same 5-tier crew picker as v1 (engine keys are STABLE; labels display-only).
// Chosen stack rides on /api/brainstorm and persists per session via /api/plan/{sid}/stack. ──
const STACKS_UI=[   // cheap → premium
  {k:'the-turd-polisher',n:'The intern',b:"Cheap and eager. Fast first drafts you'll want to double-check. Fine for spiking and kicking the tires."},
  {k:'the-capable-intern',n:'The work horse',b:"Cheap research, solid synthesis. Gets the bulk of the job done well."},
  {k:'the-work-horse',n:'The closer',b:"Cheap research bots, advanced synthesis and orchestration.",def:true},
  {k:'the-wonder-kid',n:'Wonder kid',b:"Advanced research with world-class orchestration and synthesis.",rec:true},
  {k:'trust-fund-baby',n:'Trust fund baby',b:"The absolute best models top to bottom. Not cheap, but hey, neither are you."},
];
let STACK_CUR=(function(){try{return localStorage.getItem('filg_stack')||'the-work-horse';}catch(e){return 'the-work-horse';}})();
function _stackIdx(){const i=STACKS_UI.findIndex(x=>x.k===STACK_CUR);return i<0?2:i;}
function _pips(i){return '<span class=pips aria-hidden=true>'+[0,1,2,3,4].map(n=>'<i class="'+(n<=i?'on':'')+'"></i>').join('')+'</span>';}
function renderStackChips(){
  const i=_stackIdx(), u=STACKS_UI[i];
  ['stackchip-l','stackchip-w'].forEach(id=>{const el=$(id);if(el)el.innerHTML=esc(u.n)+' '+_pips(i);});
}
function stackModal(){
  const cur=_stackIdx();
  $('modal-body').innerHTML='<p class=muted>Pick your crew. Sets the models behind research, the credibility gate, and the writing you read.</p>'+
    STACKS_UI.map((u,i)=>`<button type=button class="sktile${i===cur?' on':''}" onclick="pickStack(${i})">`+
      `<span class=sk-top><b>${esc(u.n)}</b>${u.rec?'<span class=mold>Recommended</span>':''}${_pips(i)}</span>`+
      `<span class=sk-desc>${esc(u.b)}</span></button>`).join('');
  $('modal-acts').innerHTML='<button class=primary onclick="closeModal()">Done</button>';
  openModal('Model crew');
}
async function pickStack(i){
  const u=STACKS_UI[i]; if(!u)return;
  STACK_CUR=u.k; try{localStorage.setItem('filg_stack',u.k);}catch(e){}
  renderStackChips(); closeModal(); toast(u.n+' is on the job.');
  if(SID)await api('POST',`/api/plan/${SID}/stack`,{stack:u.k});   // future ops on this tree use it
}
function paintMeter(s){
  const el=$('meter'); if(!el||!s)return;
  const t=s.tokens||0, c=s.cost||0;
  const tok=t>=1000?(t/1000).toFixed(t>=10000?0:1).replace(/\.0$/,'')+'k':String(t);
  el.textContent=tok+' tok · $'+c.toFixed(c<1?3:2);
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
  // Slide straight into the workspace: the landing collapses toward the sidebar and the canvas shows
  // a seed node working immediately — the tree starts building in front of you, not behind a spinner.
  $('landing').classList.add('shrink');
  $('workspace').hidden=false;
  setTimeout(()=>{$('landing').hidden=true;},500);
  requestAnimationFrame(()=>seedGraph(idea));
  const {ok,d}=await api('POST','/api/brainstorm',{idea,stack:STACK_CUR});
  if(!ok||(d&&d.gibberish)){   // rejected → slide back to the landing and say why
    $('landing').hidden=false; $('landing').classList.remove('shrink'); $('workspace').hidden=true;
    if(d&&d.gibberish){ $('landing-joke').innerHTML=`<div class=jokecard><h3>${esc(d.title||"That's not an idea yet.")}</h3><div>${mdToHtml(d.body||'')}</div></div>`; return; }
    if(gateV2(d))return;
    $('landing-err').textContent=(d&&d.error)||'Something went wrong.'; return;
  }
  adoptPlan(d); render(d);
}
function seedGraph(idea){   // a placeholder working node while the first spread thinks
  const gn=$('gnodes'), ge=$('gedges'); if(!gn)return;
  if(ge)ge.innerHTML='';
  WIP_T0=Date.now();
  gn.innerHTML=`<div class="gnode wip" style="left:${PADX}px;top:${PADY}px">`+
    `<div class=nk><span aria-hidden=true>◉</span>idea</div><div class=nt>${esc(idea.slice(0,70))}</div>`+
    `<div class="nk wipl" style="margin-top:6px"><span class=spin aria-hidden=true></span>`+
    `Spreading into directions · <span id=wiptime>0s</span></div>`+
    `<div class=nspew><div>reading what you've got…</div></div></div>`;
  const r=$('right').getBoundingClientRect();
  VIEW={x:r.width/2-(PADX+NW/2),y:r.height*0.30-PADY,k:1}; applyView(false);
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
  // an armed "Pivot from here" ghost: the input IS the pivot feedback — spread from that node directly
  if(PIVOT_FROM){ const from=PIVOT_FROM; box.value=''; clearGhost();
    toast('Pivoting from there.'); return pivotSpread(from,prompt); }
  // browsing an earlier node? it rides along as context (a steer pivots from it, a question is about it)
  const {t}=nodesOf(S);
  const fromNode=(FOCUS&&FOCUS!==t.active)?FOCUS:null;
  const btn=$('ws-send'); btn.disabled=true;
  btn.innerHTML='<span class="spin sendspin" aria-hidden=true></span> Routing…';   // instant feedback
  const body={prompt,mode:MODE}; if(fromNode)body.node=fromNode;
  const {ok,d}=await api('POST',`/api/plan/${SID}/route`,body);
  btn.disabled=false; btn.textContent='Send →';
  if(!ok){ if(gateV2(d))return; $('ws-err').textContent=(d&&d.error)||'Could not route that.'; return; }
  box.value='';
  if(d.fork){ PENDING_FORK=d.fork; focusActive(true); return; }
  const dec=d.decision||{};
  toast(dec.say||'On it.');
  await dispatch(dec,fromNode);
}
async function dispatch(dec,fromNode){
  switch(dec.intent){
    case 'commit':
      if(dec.confirm&&!confirm('Commit to the full research + build?'))return;
      return commit(null,fromNode);
    case 'diverge': return fromNode?pivotSpread(fromNode,S.idea):reBrainstorm(S.idea);
    case 'restart_keep':
      return fromNode?pivotSpread(fromNode,(dec.keep?dec.keep+' — ':'')+S.idea)
                     :reBrainstorm((dec.keep?dec.keep+' — ':'')+S.idea);
    case 'restart_hard': if(confirm('Throw it all out and start fresh?')) v2newPlan(); return;
    case 'ask': setMode(['research','board','help'].includes(dec.target)?dec.target:'build'); return;
    case 'steer': default:
      if(fromNode)return pivotSpread(fromNode,dec.steer||'');   // feedback on an earlier node = pivot from it
      return steer(dec.steer||'');
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
  if(SID){   // same tree: the new spread branches off the pivot point, the old branch stays visible
    beginWip('Spreading new directions',{parent:pivotParent()});
    const {ok,d}=await api('POST',`/api/plan/${SID}/rebrainstorm`,{idea});
    endWip();
    if(!ok){ render(S); if(gateV2(d))return; toast((d&&d.error)||'Could not re-spread.','err'); return; }
    SEL=new Set(); PENDING_FORK=null; render(d); return;
  }
  const {ok,d}=await api('POST','/api/brainstorm',{idea,stack:STACK_CUR});
  if(!ok){ if(gateV2(d))return; toast((d&&d.error)||'Could not re-spread.','err'); return; }
  adoptPlan(d); render(d);
}
async function doMerge(){
  if(!SEL.size){toast('Pick at least one direction.','err');return;}
  const picks=[...SEL];
  const {ok,d}=await api('POST',`/api/plan/${SID}/merge`,{options:picks});
  if(!ok){ if(gateV2(d))return; toast((d&&d.error)||'Could not merge.','err'); return; }
  beginWip('Merging your picks + first-pass research',{join:picks}); poll();
}
async function commit(thesis,fromNode){
  const body={}; if(thesis)body.thesis=thesis; if(fromNode)body.node=fromNode;   // build out of THAT node
  const {ok,d}=await api('POST',`/api/plan/${SID}/commit`,body);
  if(!ok){ if(gateV2(d))return; toast((d&&d.error)||'Could not start the build.','err'); return; }
  beginWip('Deep research: pulling + grading sources',{parent:fromNode||(nodesOf(S).t||{}).active}); poll();
}
// ── Pivot-from-a-node: an armed ghost child ("enter feedback to pivot…") + the direct spread ──
let PIVOT_FROM=null;
function pivotFromHere(id){ PIVOT_FROM=id; FOCUS=null; BROWSING=true; renderGraph();
  const b=$('ws-box'); if(b){b.placeholder='Your pivot: what should change from here?';b.focus();} }
function clearGhost(){ PIVOT_FROM=null; const b=$('ws-box'); if(b)b.placeholder='Tell me what to change, or just talk to it…'; renderGraph(); }
async function pivotSpread(fromNode,idea){
  beginWip('Spreading new directions',{parent:pivotParent(fromNode)});
  const {ok,d}=await api('POST',`/api/plan/${SID}/rebrainstorm`,{idea:idea+' — '+(S&&S.idea||''),node:fromNode});
  endWip();
  if(!ok){ render(S); if(gateV2(d))return; toast((d&&d.error)||'Could not pivot.','err'); return; }
  SEL=new Set(); PENDING_FORK=null; render(d);
}
function commitFromBrainstorm(){
  if(!SEL.size){toast('Check a direction to build, or refine first.','err');return;}
  const chosen=(stageOptions()||[]).filter(o=>SEL.has(o.id)).map(o=>o.direction.one_liner||o.direction.title);
  commit(chosen.join(' + '));
}
async function keepGoing(){ await run(`/api/plan/${SID}/next`,{feedback:''},'Writing the next part'); }
async function run(url,body,label){
  beginWip(label,{parent:(nodesOf(S).t||{}).active});
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
const WIP_BOX=210;   // an expanded working node's height — its row grows so children never sit under it
let VIEW={x:0,y:0,k:1}, GSEEN=new Set(), FOCUS=null, BROWSING=false, LAST_ACTIVE=null, WAS_RESEARCHING=false;
let WIP_LABEL=null, WIP_T0=null, WIPLOG=[], LANES=[], LANES_DONE=new Set(), PROG_N=0;
let WIP_PENDING=null;   // {label,parent,join} — the node BEING BORN; rendered nodes never show loading
const NODECACHE={}, NODELOG={};   // fetched past-node content · per-node build-log stash
function resetGraph(){ GSEEN=new Set(); FOCUS=null; BROWSING=false; LAST_ACTIVE=null; WAS_RESEARCHING=false;
  WIP_LABEL=null; WIP_PENDING=null; WIPLOG=[]; LANES=[]; LANES_DONE=new Set(); PROG_N=0;
  Object.keys(NODECACHE).forEach(k=>delete NODECACHE[k]); Object.keys(NODELOG).forEach(k=>delete NODELOG[k]);
  const gn=$('gnodes'); if(gn)gn.innerHTML=''; const ge=$('gedges'); if(ge)ge.innerHTML=''; }
function beginWip(label,opts){ WIP_LABEL=label; WIP_T0=Date.now(); WIPLOG=[]; LANES=[]; LANES_DONE=new Set();
  WIP_PENDING={label,parent:(opts&&opts.parent)||null,join:(opts&&opts.join)||null};
  LEAF_OPEN=new Set(); FOCUS=null; BROWSING=false; PREFOCUS_VIEW=null; renderView(); }   // content collapses back, the pending node takes the stage
function endWip(){ WIP_LABEL=null; WIP_T0=null; WIP_PENDING=null; }
function pivotParent(id){   // where a re-spread visually grows from (mirrors the server's sibling rule)
  const {m,t}=nodesOf(S); const a=m[id||t.active]||{};
  return ['brainstorm','option'].includes(a.kind)?(a.parent||t.active):(a.id||t.active); }
function nodesOf(s){const t=(s&&s.tree)||{};const m={};(t.nodes||[]).forEach(n=>m[n.id]=n);return {m,t};}
function pathSetOf(m,active){const set={};let cur=active;
  while(cur!=null&&m[cur]){set[cur]=1;
    const n=m[cur];
    // a refined node JOINS its selected options — they're part of the taken path, not passed-over
    if(n.kind==='refined'&&Array.isArray(n.selected))n.selected.forEach(s=>{if(m[s])set[s]=1;});
    cur=n.parent;}
  return set;}
function nodeLabel(n){
  if(n.kind==='idea')return n.title||'Your idea';
  if(n.kind==='brainstorm')return 'A few directions';
  if(n.kind==='refined')return 'Refined idea';
  if(n.kind==='fork')return 'Fork';
  if(n.kind==='pivot')return n.title||'enter feedback to pivot…';
  return n.title||('Part '+((n.step||0)+1));
}
const KICON={idea:'◉',brainstorm:'✳',option:'◇',refined:'◆',fork:'⑂',pivot:'⑂',wip:'⚙',section:'▤'};
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
function layoutGraph(m,wip){
  const kids={},roots=[];
  Object.values(m).forEach(n=>{kids[n.id]=kids[n.id]||[];});
  Object.values(m).forEach(n=>{ if(n.parent!=null&&kids[n.parent])kids[n.parent].push(n.id); else if(n.parent==null)roots.push(n.id); });
  // a refined node with `selected` is a JOIN of those option branches: it sits BELOW the options it
  // merged, centered between them, with edges from each — not a sibling branch off the brainstorm.
  const joins={};
  Object.values(m).forEach(n=>{ if(Array.isArray(n.selected)){
    const sel=n.selected.filter(id=>m[id]); if(sel.length)joins[n.id]=sel; }});
  const depth={},pos={};
  const dq=[...roots]; roots.forEach(r=>depth[r]=0);
  while(dq.length){const id=dq.shift();(kids[id]||[]).forEach(c=>{depth[c]=(depth[id]||0)+1;dq.push(c);});}
  Object.keys(joins).forEach(id=>{
    depth[id]=Math.max(...joins[id].map(s=>depth[s]||0))+1;
    const q=[id]; while(q.length){const x=q.shift();(kids[x]||[]).forEach(c=>{depth[c]=depth[x]+1;q.push(c);});}
  });
  // x slots: a joined node doesn't consume a slot under its parent; it centers on what it merged
  let slot=0; const X={};
  function place(id){const ks=(kids[id]||[]).filter(c=>!joins[c]);
    if(!ks.length){X[id]=slot++;} else {ks.forEach(place);X[id]=(X[ks[0]]+X[ks[ks.length-1]])/2;}}
  roots.forEach(place);
  function placeUnder(id,x){X[id]=x;const ks=kids[id]||[];ks.forEach((c,i)=>placeUnder(c,x+(i-(ks.length-1)/2)));}
  Object.keys(joins).forEach(id=>{ const xs=joins[id].map(s=>X[s]||0);
    placeUnder(id,xs.reduce((a,b)=>a+b,0)/xs.length); });
  // per-row y: the WIP row grows so an expanded working node (+ its leaflets) never covers children
  const maxDepth=Math.max(0,...Object.values(depth));
  const rowY={}; let y=PADY;
  for(let d=0;d<=maxDepth;d++){ rowY[d]=y;
    y+=ROWH+((wip!=null&&depth[wip]===d)?(WIP_BOX-NH):0); }   // leaves fan out SIDEWAYS, no extra row
  Object.keys(m).forEach(id=>{pos[id]={x:PADX+(X[id]||0)*COLW, y:rowY[depth[id]||0]};});
  return {pos,kids,joins};
}
function renderGraph(){
  const s=S; const gn=$('gnodes'), ge=$('gedges'); if(!s||!gn)return;
  const {m,t}=nodesOf(s); const active=t.active;
  if(PIVOT_FROM&&m[PIVOT_FROM])m._ghost={id:'_ghost',parent:PIVOT_FROM,kind:'pivot',
    title:'enter feedback to pivot…',step:0};   // the armed pivot: a blank child awaiting your words
  const working=!!(WIP_LABEL||s.status==='researching');
  if(working&&!WIP_PENDING)WIP_PENDING={label:WIP_LABEL||'Working',parent:active,join:null};   // e.g. reload mid-build
  if(!working)WIP_PENDING=null;
  if(WIP_PENDING)m._wip={id:'_wip',kind:'wip',step:0,title:WIP_PENDING.label,
    parent:WIP_PENDING.join?null:(WIP_PENDING.parent||active),
    selected:WIP_PENDING.join||undefined};   // the node being born — loading lives HERE
  const wip=WIP_PENDING?'_wip':null;
  const {pos,kids,joins}=layoutGraph(m,wip);
  const onPath=pathSetOf(m,active);
  // nodes: keyed divs, moved (CSS transition) or created (.enter → fade in)
  const live=new Set(Object.keys(m));
  gn.querySelectorAll('.gnode').forEach(el=>{ if(!live.has(el.dataset.id)) el.remove(); });
  const rrect=$('right').getBoundingClientRect();
  const FW=focusW(rrect), FH=rrect.height-120;   // open-doc box: panel-fit, margin on every side
  const JOINSEL=new Set(); Object.values(joins).forEach(a=>a.forEach(id=>JOINSEL.add(id)));
  Object.values(m).forEach(n=>{
    const p=pos[n.id]; const isWip=n.id===wip, isFocus=n.id===FOCUS&&!isWip;
    // selection state on options: green rim = picked (checked now, or joined by a refined node);
    // red-dimmed rim = passed over once its siblings were decided (merge running or join landed)
    const isOpt=n.kind==='option';
    const sel=isOpt&&(JOINSEL.has(n.id)||(SEL.has(n.id)&&S.stage==='brainstorm'));
    const decided=isOpt&&(Object.values(m).some(x=>x.kind==='refined'&&x.parent===n.parent)||JOINSEL.size>0);
    const rej=decided&&!sel&&!onPath[n.id];
    let el=gn.querySelector(`.gnode[data-id="${n.id}"]`);
    const fresh=!el;
    if(fresh){ el=document.createElement('div'); el.dataset.id=n.id; el.classList.add('enter');
      el.addEventListener('click',e=>{ if(!e.target.closest('.nbody'))gNodeClick(n.id); }); gn.appendChild(el); }
    el.className='gnode'+(n.id==='_ghost'?' ghost':'')+(fresh?' enter':'')+(n.id===active?' on':'')+(onPath[n.id]?' path':'')+(sel?' sel':'')+(rej?' rej':'')+(isWip?' wip':'')+(isFocus?' focus':'');
    const w=isFocus?FW:(isWip?250:NW);
    el.style.width=isFocus?FW+'px':''; el.style.maxHeight=isFocus?FH+'px':'';
    el.style.left=(p.x-(w-NW)/2)+'px'; el.style.top=p.y+'px';
    el.title=isFocus?'':((n.feedback?('↳ '+n.feedback+'\n'):'')+nodeLabel(n));
    let inner;
    if(isWip){   // the node being born: spinner + label + elapsed + the live receipt tail
      const secs=WIP_T0?Math.round((Date.now()-WIP_T0)/1000)+'s':'';
      inner=`<div class="nk wipl"><span class=spin aria-hidden=true></span>`+
        `${esc(WIP_PENDING.label||'Working')} · <span id=wiptime>${secs}</span></div>`;
      inner+=`<div class=nspew>`+(WIPLOG.length?WIPLOG.map(l=>`<div>${esc(l)}</div>`).join(''):'<div>warming up…</div>')+`</div>`;
    } else {
      inner=`<div class=nk><span aria-hidden=true>${KICON[n.kind]||'▤'}</span>${esc(n.kind||'part')}</div>`+
        `<div class=nt>${esc(nodeLabel(n))}</div>`;
    }
    if(isFocus)inner+=`<div class=nbody>${nodeBody(n)}</div>`;
    el.innerHTML=inner;
    if(fresh)requestAnimationFrame(()=>requestAnimationFrame(()=>el.classList.remove('enter')));
  });
  renderLeaves(gn,pos,wip);
  // edges: parent→child, except a refined JOIN which draws from each option it merged
  let maxX=0,maxY=0; Object.values(pos).forEach(p=>{maxX=Math.max(maxX,p.x+NFOCUS);maxY=Math.max(maxY,p.y+ROWH);});
  ge.setAttribute('width',maxX+PADX); ge.setAttribute('height',maxY+PADY+WIP_BOX);
  const edge=(a,b,on,seen)=>{const x1=a.x+NW/2,y1=a.y+NH,x2=b.x+NW/2,y2=b.y;
    return `<path class="${(on?'on ':'')+(seen?'':'new')}" pathLength=1 d="M${x1},${y1} C${x1},${y1+46} ${x2},${y2-46} ${x2},${y2}"/>`;};
  ge.innerHTML=Object.values(m).map(n=>{
    if(joins[n.id])return joins[n.id].map(sid=>edge(pos[sid],pos[n.id],onPath[n.id]&&onPath[sid],GSEEN.has(n.id))).join('');
    if(n.parent==null||!m[n.parent])return '';
    return edge(pos[n.parent],pos[n.id],onPath[n.id]&&onPath[n.parent],GSEEN.has(n.id));
  }).join('');
  Object.keys(m).forEach(id=>GSEEN.add(id));
  // camera: what YOU opened wins, else follow the build, unless you're browsing on your own
  if(!BROWSING){
    if(FOCUS&&pos[FOCUS])focusCam(pos[FOCUS]);
    else if(wip&&pos[wip])centerOn(pos[wip],250,0.9,0.30);
  }
  pill(!wip && stageSurface(S) && FOCUS!==active);
}
let LEAF_OPEN=new Set();   // expanded leaflets (click a leaf to read its full research question)
function renderLeaves(gn,pos,wip){   // research leaflets: labeled sub-nodes fanning out of the working node
  const want=(wip&&LANES.length)?LANES.length:0;
  gn.querySelectorAll('.gleaf').forEach((el,i)=>{ if(i>=want)el.remove(); });
  if(!want){ if(LEAF_OPEN.size)LEAF_OPEN=new Set(); return; }
  const p=pos[wip];
  for(let i=0;i<want;i++){
    let el=gn.querySelector(`.gleaf[data-i="${i}"]`);
    if(!el){ el=document.createElement('div'); el.dataset.i=i; el.className='gleaf enter';
      el.addEventListener('click',e=>{ e.stopPropagation();
        if(LEAF_OPEN.has(i))LEAF_OPEN.delete(i); else LEAF_OPEN.add(i); renderGraph(); });
      gn.appendChild(el); requestAnimationFrame(()=>requestAnimationFrame(()=>el.classList.remove('enter'))); }
    const done=LANES_DONE.has(i), open=LEAF_OPEN.has(i);
    const q=LANES[i]||('lane '+(i+1));
    el.classList.toggle('done',done); el.classList.toggle('open',open);
    el.innerHTML=`<span class=lfic aria-hidden=true>🍃</span>`+
      `<span class=lftxt>${esc(open?q:(q.length>30?q.slice(0,30)+'…':q))}</span>`+
      (done?`<span class=lfok aria-hidden=true>✓</span>`:`<span class="spin lfspin" aria-hidden=true></span>`);
    el.title=open?'':q;
    // offshoots, not steps: hug the working node's right flank, stacked + slightly staggered,
    // so they never read as belonging to the children below
    el.style.left=(p.x+NW/2+128+(i%2)*14)+'px';
    el.style.top=(p.y+26+i*36)+'px';
  }
}
function centerOn(p,w,k,yFrac){   // ease the camera so node at p (width w) sits centered, yFrac down
  const r=$('right').getBoundingClientRect();
  VIEW.k=k;
  VIEW.x=r.width/2-(p.x+NW/2)*k;
  VIEW.y=r.height*yFrac-p.y*k;
  applyView(true);
}
let PREFOCUS_VIEW=null;   // the camera as it was BEFORE a doc opened — closing restores it exactly
function focusW(r){ return Math.min(860,Math.max(460,r.width-170)); }   // doc width: panel minus margins
function focusCam(p){   // an open doc reads at NATURAL scale (k=1), panel-fit with margin all around
  if(!PREFOCUS_VIEW)PREFOCUS_VIEW={...VIEW};
  const r=$('right').getBoundingClientRect();
  const el=$('gnodes')&&$('gnodes').querySelector('.gnode.focus');
  const h=(el&&el.offsetHeight)||360;
  VIEW.k=1;                                   // never zoom the content — words stay their real size
  VIEW.x=r.width/2-(p.x+NW/2);
  VIEW.y=(r.height-h)/2-p.y;                  // centered; max-height guarantees the click-out margin
  applyView(true);
}
function applyView(ease){ const v=$('gview'); if(!v)return;
  v.classList.toggle('ease',!!ease); v.style.transform=`translate(${VIEW.x}px,${VIEW.y}px) scale(${VIEW.k})`;
  if(ease)setTimeout(()=>v.classList.remove('ease'),650); }
function initGraphInput(){
  const g=$('graph'); if(!g)return;
  let drag=null,moved=false;
  g.addEventListener('pointerdown',e=>{ if(e.target.closest('.gnode.focus'))return;   // the open doc handles its own input
    drag={x:e.clientX,y:e.clientY,vx:VIEW.x,vy:VIEW.y}; moved=false; });
  g.addEventListener('pointermove',e=>{ if(!drag)return;
    const dx=e.clientX-drag.x, dy=e.clientY-drag.y;
    if(Math.abs(dx)+Math.abs(dy)>6){ moved=true; BROWSING=true; g.classList.add('dragging'); g.setPointerCapture(e.pointerId); }
    if(moved){ VIEW.x=drag.vx+dx; VIEW.y=drag.vy+dy; applyView(false); } });
  const stop=e=>{ if(drag&&!moved&&!e.target.closest('.gnode')){ unfocus(); }   // click outside → the doc collapses into its node
    drag=null; g.classList.remove('dragging'); };
  g.addEventListener('pointerup',stop); g.addEventListener('pointercancel',stop);
  // Trackpad semantics: pinch (ctrlKey wheel) zooms · two-finger swipe PANS · over an open node's
  // scrollable content the wheel scrolls THAT, not the canvas.
  g.addEventListener('wheel',e=>{
    if(!e.ctrlKey&&e.target.closest('.gnode.focus'))return;   // native scroll inside the open doc
    e.preventDefault();
    if(e.ctrlKey){   // pinch → zoom toward the cursor
      BROWSING=true;
      const r=g.getBoundingClientRect(), mx=e.clientX-r.left, my=e.clientY-r.top;
      const k2=Math.min(1.9,Math.max(0.3,VIEW.k*Math.exp(-e.deltaY*0.012)));
      VIEW.x=mx-(mx-VIEW.x)*(k2/VIEW.k); VIEW.y=my-(my-VIEW.y)*(k2/VIEW.k); VIEW.k=k2; applyView(false);
    } else {          // two-finger swipe → pan
      BROWSING=true;
      VIEW.x-=e.deltaX; VIEW.y-=e.deltaY; applyView(false);
    }
  },{passive:false});
}
function gNodeClick(id){
  if(id==='_ghost'){ const b=$('ws-box'); if(b)b.focus(); return; }   // the ghost wants your words
  if(id==='_wip')return;                          // the node being born isn't readable yet
  if(FOCUS===id)return;                           // already reading it
  focusNode(id);                                  // browsing rendered nodes works even while it builds
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
function unfocus(){ if(FOCUS==null&&!BROWSING)return; FOCUS=null; BROWSING=true;
  if(PREFOCUS_VIEW){ VIEW={...PREFOCUS_VIEW}; PREFOCUS_VIEW=null; applyView(true); }   // same alignment as before the doc opened
  renderGraph(); }
function pill(show){ const p=$('gpill'); if(p)p.hidden=!show; }

// ── What lives INSIDE a focused node ─────────────────────────────────────────
function nodeBody(n){
  const {t}=nodesOf(S);
  let body;
  if(n.id===t.active){ const surf=stageSurface(S); body=surf?surf.html:''; }
  else body=pastBody(n)+`<div class=ctarow><button class="stage-cta secondary" onclick="pivotFromHere('${n.id}')">⑂ Pivot from here</button></div>`;
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
  if(d.kind==='brainstorm'){
    const opts=(d.options||[]).map(o=>{const x=o.direction||{};
      return `<div class="opt${o.picked?' sel':''}" style="cursor:default"><div>`+
        `<h3>${o.picked?'✓ ':''}${esc(x.title||'')}</h3><p>${esc(x.one_liner||'')}</p>`+
        `${x.mold?`<span class=mold>${esc(x.mold)}</span>`:''}</div></div>`;}).join('');
    return `<p class=eyebrow>${d.spread==='tight'?'Your idea, sharpened':'The directions offered'}</p>`+
      `<div class=optgrid>${opts}</div>`+
      ((d.options||[]).some(o=>o.picked)?`<p class=thinking>✓ = what you picked and carried forward.</p>`
        :`<p class=thinking>Nothing picked from this fork yet.</p>`);
  }
  if(d.kind==='idea')return `<p>${esc(d.draft||'')}</p><p class=thinking>Where it all started. Every spread and pivot branches from here.</p>`;
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
function toggleSel(id){ if(SEL.has(id))SEL.delete(id); else SEL.add(id); renderView(); }
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

// ── Two projections of the same tree: the decision graph, and a left-to-right document reader ──
let VIEWMODE='graph', DOCTAB=null;
function setView(v){
  VIEWMODE=(v==='docs')?'docs':'graph';
  document.querySelectorAll('#vtabs button').forEach(b=>b.classList.toggle('on',b.dataset.v===VIEWMODE));
  $('graph').hidden=VIEWMODE!=='graph';
  $('docs').hidden=VIEWMODE!=='docs';
  if(VIEWMODE!=='graph')pill(false);
  renderView();
}
function renderView(){ if(VIEWMODE==='docs')renderDocs(); else renderGraph(); }
function pickDoc(id){ DOCTAB=id; renderDocs(); }
function renderDocs(){
  const dt=$('dtabs'), pane=$('docpane'); if(!dt||!S)return;
  const {m,t}=nodesOf(S);
  const chain=[]; let cur=t.active; while(cur&&m[cur]){chain.unshift(m[cur]);cur=m[cur].parent;}
  if(!DOCTAB||!chain.some(n=>n.id===DOCTAB))DOCTAB=t.active;
  const working=WIP_LABEL||S.status==='researching';
  dt.innerHTML=chain.map(n=>
    `<button type=button role=tab aria-selected="${n.id===DOCTAB}" class="dtab${n.id===DOCTAB?' on':''}${n.id===t.active?' cur':''}" onclick="pickDoc('${n.id}')">`+
    `<span aria-hidden=true>${KICON[n.kind]||'▤'}</span>${esc(nodeLabel(n))}</button>`).join('')+
    (working?`<span class="dtab wipdt"><span class=spin aria-hidden=true></span>${esc((WIP_PENDING&&WIP_PENDING.label)||WIP_LABEL||'Working')}</span>`:'');
  const n=m[DOCTAB]; if(!n){pane.innerHTML='';return;}
  let body;
  if(working&&DOCTAB===t.active){   // the step in flight: its receipts, right in the reader
    body=`<p class=eyebrow>⚙ ${esc((WIP_PENDING&&WIP_PENDING.label)||'Working')}</p>`+
      `<div class=nspew style="max-height:220px">`+(WIPLOG.length?WIPLOG.map(l=>`<div>${esc(l)}</div>`).join(''):'<div>warming up…</div>')+`</div>`;
  } else body=nodeBody(n);
  if(n.id!==t.active&&!NODECACHE[n.id])
    api('GET',`/api/plan/${SID}/node/${n.id}`).then(({ok,d})=>{ if(ok){NODECACHE[n.id]=d;
      if(VIEWMODE==='docs'&&DOCTAB===n.id)renderDocs();} });
  pane.innerHTML=`<div class=docsheet>${body}</div>`;
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
  if(researching&&!WIP_T0)WIP_T0=Date.now();          // e.g. a reload mid-build → the timer still ticks
  const {t}=nodesOf(S);
  if(WAS_RESEARCHING&&!researching){          // a build just finished → stash its log on the node it made
    if(WIPLOG.length)NODELOG[t.active]=WIPLOG.slice();
    WIPLOG=[]; LANES=[]; LANES_DONE=new Set(); WIP_LABEL=null; WIP_T0=null; WIP_PENDING=null;
  }
  WAS_RESEARCHING=researching;
  paintMeter(S);
  if(!researching){
    if(t.active!==LAST_ACTIVE){ LAST_ACTIVE=t.active; FOCUS=t.active; BROWSING=false; PREFOCUS_VIEW=null;
      promptFocus(); }   // the next step is ready → zoom in, hands back on the keyboard
    else if(FOCUS==null&&!BROWSING){ FOCUS=t.active; }
  }
  if(!researching&&VIEWMODE==='docs')DOCTAB=t.active;   // the reader follows the build
  renderView();
}
function promptFocus(){   // put the cursor back in the chat box so the user can just start typing
  const a=document.activeElement;
  if(a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA')&&a.id!=='ws-box')return;   // don't steal a real field
  const b=$('ws-box'); if(b&&!$('workspace').hidden)b.focus();
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
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();unfocus();if(PIVOT_FROM)clearGhost();}});
// Enter submits (Shift+Enter for a newline) — both the chat box and the landing box
document.addEventListener('keydown',e=>{
  if(e.key!=='Enter'||e.shiftKey)return;
  if(e.target&&e.target.id==='ws-box'){ e.preventDefault(); sendPrompt(); }
  else if(e.target&&e.target.id==='landing-box'){ e.preventDefault(); startFromLanding(); }
});
initGraphInput();
loadKey();   // paint the key indicator (hosted vs BYOK) on load
renderStackChips();   // the model-crew chips (landing + workspace)
// the working node's elapsed clock — keeps the build feeling alive even between progress lines
setInterval(()=>{const el=$('wiptime');if(el&&WIP_T0)el.textContent=Math.round((Date.now()-WIP_T0)/1000)+'s';},1000);
