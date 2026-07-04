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

// ── The chat log: the conversation IS the left panel; every exchange leaves a bubble ──
function chatSay(role,html){const log=$('chatlog'); if(!log)return null;
  const m=document.createElement('div'); m.className='cmsg '+role; m.innerHTML=html;
  log.appendChild(m); log.scrollTop=log.scrollHeight; return m;}
function chatPush(role,text){ if(SID)api('POST',`/api/plan/${SID}/chatlog`,{role,content:text}); }   // fire-and-forget: the record survives a reload
function chatUser(t){chatPush('user',t);return chatSay('user',esc(t));}
function chatBot(t){chatPush('bot',t);return chatSay('bot',esc(t));}
function chatErr(t){return chatSay('err',esc(t));}   // transient — errors aren't part of the durable record
function chatStatus(t){chatPush('status',t);return chatSay('status',esc(t));}
function replayChat(list){ const log=$('chatlog'); if(!log)return; log.innerHTML='';
  (list||[]).forEach(m=>chatSay(m.role==='user'?'user':(m.role==='status'?'status':'bot'),esc(m.content||''))); }
// An in-chat check instead of a native confirm(): a bot bubble with Go / Not yet.
function chatConfirm(text,goLabel){return new Promise(res=>{
  chatPush('bot',text);
  const m=chatSay('bot',esc(text)+
    `<div class=cbtns><button type=button class=go>${esc(goLabel||'Go')}</button>`+
    `<button type=button class=nah>Not yet</button></div>`);
  if(!m){res(confirm(text));return;}   // no log mounted → native fallback
  const done=yes=>{m.classList.add('asked');
    chatStatus(yes?(goLabel||'Go')+' ✓':'Not yet — carrying on as is.'); res(yes);};
  m.querySelector('.go').onclick=()=>done(true);
  m.querySelector('.nah').onclick=()=>done(false);});}
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
  chatUser(idea);
  chatBot('Spread that into a few directions — pick what clicks on the graph, or just keep typing.');
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
function adoptPlan(d){ SID=d.id; SEL=new Set(); PENDING_FORK=null; resetGraph();
  history.replaceState(null,'','/v2/plan/'+SID); }   // the plan gets a real URL — reload restores it
async function restorePlan(id){   // boot straight into an existing plan: graph + docs + conversation
  const {ok,d}=await api('GET',`/api/plan/${id}?touch=1`);
  if(!ok||!d||!d.id){ history.replaceState(null,'','/v2'); return; }   // unknown → the landing
  $('landing').hidden=true; $('workspace').hidden=false;
  SID=d.id; SEL=new Set(); PENDING_FORK=null; resetGraph();
  replayChat(d.chat);
  render(d);
  if(d.status==='researching')poll();   // a run was mid-flight — pick the poll back up
  focusActive(true);
}

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
  chatUser(prompt);
  // an armed "Pivot from here" ghost: the input IS the pivot feedback — spread from that node directly
  if(PIVOT_FROM){ const from=PIVOT_FROM; box.value=''; clearGhost();
    chatStatus('Pivoting from '+pivotSrcLabel(from)); return pivotSpread(from,prompt); }
  // browsing an earlier node? it rides along as context (a steer pivots from it, a question is about it)
  const {t}=nodesOf(S);
  const fromNode=(FOCUS&&FOCUS!==t.active)?FOCUS:null;
  const btn=$('ws-send'); btn.disabled=true;
  btn.innerHTML='<span class="spin sendspin" aria-hidden=true></span> Routing…';   // instant feedback
  const body={prompt,mode:MODE}; if(fromNode)body.node=fromNode;
  const {ok,d}=await api('POST',`/api/plan/${SID}/route`,body);
  btn.disabled=false; btn.textContent='Send →';
  if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'Could not route that.'); return; }
  box.value='';
  if(d.fork){ PENDING_FORK=d.fork;
    chatBot(d.fork.clash||'That pulls against the committed idea — pick a path on the graph.');
    focusActive(true); return; }
  const dec=d.decision||{};
  // when an in-chat check follows immediately, the check IS the reply — skip the say bubble
  const checks=(dec.intent==='commit'&&dec.confirm)||dec.intent==='restart_hard';
  if(!checks)chatBot(dec.say||'On it.');
  await dispatch(dec,fromNode,prompt);
}
async function dispatch(dec,fromNode,prompt){
  switch(dec.intent){
    case 'commit':
      if(dec.confirm&&!(await chatConfirm('Commit to the full research + build?',"Let's go")))return;
      return commit(null,fromNode);
    case 'diverge': return fromNode?pivotSpread(fromNode,S.idea):reBrainstorm(S.idea);
    case 'restart_keep':
      return fromNode?pivotSpread(fromNode,(dec.keep?dec.keep+' — ':'')+S.idea)
                     :reBrainstorm((dec.keep?dec.keep+' — ':'')+S.idea);
    case 'restart_hard':
      if(await chatConfirm('Throw it all out and start fresh?','Start fresh')) v2newPlan();
      return;
    case 'pick': {   // the chat chose among the on-screen directions → check them + merge
      const opts=stageOptions();
      const ids=(dec.picks||[]).map(i=>opts[i-1]&&opts[i-1].id).filter(Boolean);
      if(!ids.length){ chatErr("I couldn't match that to the directions on screen — name them by number, or click the boxes."); return; }
      SEL=new Set(ids); renderView();
      return doMerge();
    }
    case 'ask': return askInChat(prompt||dec.steer||'',dec.target);   // a question gets an ANSWER, in the chat
    case 'steer': default:
      if(fromNode){ chatStatus('Pivoting from '+pivotSrcLabel(fromNode));
        return pivotSpread(fromNode,dec.steer||''); }   // feedback on an earlier node = pivot from it
      return steer(dec.steer||'');
  }
}
// A routed question: product questions go to /api/help, everything else to the plan-grounded
// advisor. The reply lands as a chat bubble — a question must never die in a tool drawer.
async function askInChat(q,target){
  if(!q)return;
  if(target==='research')return lookupInChat(q);   // research mode = a real web lookup, graded by the gate
  const th=chatSay('status','thinking…');
  const url=(target==='help')?'/api/help':`/api/plan/${SID}/chat`;
  const body=(target==='help')?{message:q}:{message:q,log_user:false};
  const {ok,d}=await api('POST',url,body);
  if(th)th.remove();
  if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'Could not answer that.'); return; }
  const reply=(d&&d.reply)||'';
  chatSay('bot',mdToHtml(reply));
  if(target==='help')chatPush('bot',reply);   // the advisor endpoint logs its own turn; /api/help doesn't
}
async function lookupInChat(q){
  const th=chatSay('status','searching + grading sources…');
  const {ok,d}=await api('POST',`/api/plan/${SID}/lookup`,{message:q});
  if(th)th.remove();
  if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'The lookup came back empty — try rewording it.'); return; }
  const cs=(d&&d.claims)||[];
  if(!cs.length){ chatErr('The lookup found nothing gradeable — try rewording it.'); return; }
  const rows=cs.map(c=>`<div class=lkclaim>${c.flagged?'⚠':'✓'} ${esc(c.text)} `+
    `<a href="${esc(c.url)}" target=_blank rel=noopener>src</a>`+
    `<span class="lktier${c.flagged?' bad':''}" title="${esc(c.reason||'')}">${esc(c.tier)}</span></div>`).join('');
  chatSay('bot',`<p>Graded lookup — every stat labeled, vendor numbers flagged:</p>${rows}`);
  chatPush('bot','Graded lookup:\n'+cs.map(c=>`${c.flagged?'⚠':'✓'} ${c.text} [${c.tier}] ${c.url}`).join('\n'));
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
    if(!ok){ render(S); if(gateV2(d))return; chatErr((d&&d.error)||'Could not re-spread.'); return; }
    SEL=new Set(); PENDING_FORK=null; render(d); return;
  }
  const {ok,d}=await api('POST','/api/brainstorm',{idea,stack:STACK_CUR});
  if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'Could not re-spread.'); return; }
  adoptPlan(d); render(d);
}
async function doMerge(){
  if(!SEL.size){toast('Pick at least one direction.','err');return;}
  const picks=[...SEL];
  const {ok,d}=await api('POST',`/api/plan/${SID}/merge`,{options:picks});
  if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'Could not merge.'); return; }
  beginWip('Merging your picks + first-pass research',{join:picks}); poll();
}
async function commit(thesis,fromNode){
  const body={}; if(thesis)body.thesis=thesis; if(fromNode)body.node=fromNode;   // build out of THAT node
  const {ok,d}=await api('POST',`/api/plan/${SID}/commit`,body);
  if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'Could not start the build.'); return; }
  beginWip('Deep research: pulling + grading sources',{parent:fromNode||(nodesOf(S).t||{}).active}); poll();
}
// ── Pivot-from-a-node: an armed ghost child ("enter feedback to pivot…") + the direct spread ──
let PIVOT_FROM=null;
function pivotFromHere(id){ PIVOT_FROM=id; FOCUS=null; BROWSING=true; renderGraph();
  const b=$('ws-box'); if(b){b.placeholder='Your pivot: what should change from here?';b.focus();} }
function pivotActive(){ const {t}=nodesOf(S); pivotFromHere(t.active); }   // the step view's Pivot button: arm the ghost off THIS step
function pivotSrcLabel(id){   // name the pivot's source node in the chat — "which node am I forking?" must never be a guess
  const {m}=nodesOf(S); const n=m[id]; if(!n)return 'that node';
  const t=(n.direction&&n.direction.title)||nodeLabel(n);
  return '“'+String(t).slice(0,60)+'”'; }
function clearGhost(){ PIVOT_FROM=null; const b=$('ws-box'); if(b)b.placeholder='Tell me what to change, or just talk to it…'; renderGraph(); }
async function pivotSpread(fromNode,feedback){
  beginWip('Spreading new directions',{parent:pivotParent(fromNode)});
  const {ok,d}=await api('POST',`/api/plan/${SID}/rebrainstorm`,{feedback,node:fromNode});
  endWip();
  if(!ok){   // fail LOUD: restore the feedback + re-arm the ghost, never quietly show the old fork
    render(S);
    const b=$('ws-box'); if(b&&!b.value)b.value=feedback;
    if(fromNode){PIVOT_FROM=fromNode;renderView();}
    chatErr((d&&d.error)||'The pivot failed — feedback restored, try again.');
    if(gateV2(d))return; return; }
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
  if(!ok){ render(S); if(gateV2(d))return; chatErr((d&&d.error)||'Something went wrong.'); return; }
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
const NW=190, NFOCUS=560, NH=58, ROWH=150, PADX=70, PADY=48, GUT=64;   // whitespace is free — spread, don't squeeze
const WIP_BOX=210;   // an expanded working node's height — its row grows so children never sit under it
let VIEW={x:0,y:0,k:1}, GSEEN=new Set(), FOCUS=null, BROWSING=false, LAST_ACTIVE=null, WAS_RESEARCHING=false;
let WIP_LABEL=null, WIP_T0=null, WIPLOG=[], LANES=[], LANES_DONE=new Set(), PROG_N=0;
let WIP_PENDING=null;   // {label,parent,join} — the node BEING BORN; rendered nodes never show loading
const NODECACHE={}, NODELOG={};   // fetched past-node content · per-node build-log stash
let HIST_OPEN=new Set();   // which nodes' "how this was built" is expanded (survives re-renders)
function histKeep(id,el){ if(el.open)HIST_OPEN.add(id); else HIST_OPEN.delete(id); }
function resetGraph(){ GSEEN=new Set(); FOCUS=null; BROWSING=false; LAST_ACTIVE=null; WAS_RESEARCHING=false;
  PIVOT_FROM=null;
  WIP_LABEL=null; WIP_PENDING=null; WIPLOG=[]; LANES=[]; LANES_DONE=new Set(); PROG_N=0;
  Object.keys(NODECACHE).forEach(k=>delete NODECACHE[k]); Object.keys(NODELOG).forEach(k=>delete NODELOG[k]);
  HIST_OPEN=new Set();
  const gn=$('gnodes'); if(gn)gn.innerHTML=''; const ge=$('gedges'); if(ge)ge.innerHTML=''; }
function beginWip(label,opts){ WIP_LABEL=label; WIP_T0=Date.now(); WIPLOG=[]; LANES=[]; LANES_DONE=new Set();
  WIP_PENDING={label,parent:(opts&&opts.parent)||null,join:(opts&&opts.join)||null};
  if(VIEWMODE==='docs')DOCTAB='_wip';   // docs view rolls forward like the tree: the new step gets its own tab
  LEAF_OPEN=new Set(); FOCUS=null; BROWSING=false; PREFOCUS_VIEW=null; renderView(); }   // content collapses back, the pending node takes the stage
function endWip(){ WIP_LABEL=null; WIP_T0=null; WIP_PENDING=null; }
function pivotParent(id){   // THE PIVOT CONTRACT: a pivot branches off the pivot node ITSELF, always
  const {t}=nodesOf(S); return id||t.active; }
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
// ── THE LAYOUT RULES (rebuilt 2026-07-04, backlog #1) ────────────────────────
// 1. NO OVERLAP, EVER: every subtree reserves the full horizontal extent of ALL its descendants,
//    siblings pack with a guaranteed gutter. Whitespace is free — we spread, we don't squeeze.
// 2. THE SPINE IS STRAIGHT: the committed path (root → active → the node being born) is pinned to
//    one X. You're working this branch, so it reads top-to-bottom; everything old fans aside.
// 3. AN OPEN DOC PUSHES, NOT COVERS: a focused node reserves its true rendered footprint (width AND
//    row height), shoving neighbors and lower rows out of the way instead of sitting on them.
function layoutGraph(m,wip,active,fit){
  const FWpx=(fit&&fit.fw)||NFOCUS, FHpx=(fit&&fit.fh)||0;
  // tree wiring: a parentless JOIN (the merge-wip) rides the tree under its first merged option, so
  // it's part of the reserved layout — the old "re-plant at a fractional slot" was the overlap source
  const tp={};
  Object.values(m).forEach(n=>{ tp[n.id]=(n.parent!=null&&m[n.parent])?n.parent
      :((Array.isArray(n.selected)&&n.selected.find(s=>m[s]))||null); });
  const kids={},roots=[];
  Object.keys(m).forEach(id=>kids[id]=[]);
  Object.keys(m).forEach(id=>{ const p=tp[id]; if(p!=null)kids[p].push(id); else roots.push(id); });
  const joins={};
  Object.values(m).forEach(n=>{ if(Array.isArray(n.selected)){
    const sel=n.selected.filter(id=>m[id]); if(sel.length)joins[n.id]=sel; }});
  // depth; a join (and its whole subtree) sinks below the DEEPEST branch it merged
  const depth={};
  const dq=[...roots]; roots.forEach(r=>depth[r]=0);
  while(dq.length){const id=dq.shift();(kids[id]||[]).forEach(c=>{depth[c]=(depth[id]||0)+1;dq.push(c);});}
  Object.keys(joins).forEach(id=>{
    const want=Math.max(...joins[id].map(s=>depth[s]||0))+1;
    if((depth[id]||0)<want){ const delta=want-(depth[id]||0);
      const q=[id]; while(q.length){const x=q.shift();depth[x]=(depth[x]||0)+delta;(kids[x]||[]).forEach(c=>q.push(c));} }
  });
  // the SPINE: root → active, extended by the node being born when it grows from the active tip
  const spine=new Set();
  let tip=(active&&m[active])?active:null;
  if(wip&&m[wip]&&tp[wip]===active)tip=wip;
  for(let cur=tip;cur!=null&&m[cur];cur=tp[cur])spine.add(cur);
  // horizontal extents (px, relative to each node's center): reserve EVERYTHING
  const nodeW=id=>(id===FOCUS&&FHpx)?FWpx:(id===wip?250:NW);
  const L={},R={},off={};
  function measure(id){
    const ks=kids[id];
    if(!ks.length){ L[id]=nodeW(id)/2; R[id]=nodeW(id)/2; return; }
    ks.forEach(measure);
    let x=0; const o=[];
    ks.forEach((c,i)=>{ x+=(i?R[ks[i-1]]+GUT+L[c]:L[c]); o.push(x); });
    const packL=o[0]-L[ks[0]], packR=o[ks.length-1]+R[ks[ks.length-1]];
    const si=ks.findIndex(c=>spine.has(c));
    // a spine node pins its spine child DIRECTLY beneath it; everything else centers over its pack
    const anchor=(spine.has(id)&&si>=0)?o[si]:(packL+packR)/2;
    ks.forEach((c,i)=>off[c]=o[i]-anchor);
    L[id]=Math.max(nodeW(id)/2,anchor-packL);
    R[id]=Math.max(nodeW(id)/2,packR-anchor);
  }
  const cx={};
  let cursor=PADX;
  roots.forEach(r=>{ measure(r); cursor+=L[r]; cx[r]=cursor; cursor+=R[r]+GUT; });
  const bq=[...roots];
  while(bq.length){const id=bq.shift();(kids[id]||[]).forEach(c=>{cx[c]=cx[id]+off[c];bq.push(c);});}
  const minLeft=Math.min(...Object.keys(m).map(id=>cx[id]-nodeW(id)/2));
  const nudge=PADX-minLeft;
  // per-row y: the WIP row grows for the leaf stack; the FOCUSED row reserves the open doc's height
  const maxDepth=Math.max(0,...Object.values(depth));
  const rowY={}; let y=PADY;
  for(let d=0;d<=maxDepth;d++){ rowY[d]=y;
    let h=ROWH;
    if(wip!=null&&depth[wip]===d)h=Math.max(h,WIP_BOX+LANES.length*30+24);
    if(FOCUS!=null&&FHpx&&depth[FOCUS]===d)h=Math.max(h,FHpx+48);
    y+=h; }
  const pos={};
  Object.keys(m).forEach(id=>{pos[id]={x:cx[id]+nudge-NW/2, y:rowY[depth[id]||0]};});
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
  const rrect=$('right').getBoundingClientRect();
  const FW=focusW(rrect), FH=rrect.height-120;   // open-doc box: panel-fit, margin on every side
  const {pos,kids,joins}=layoutGraph(m,wip,active,FOCUS?{fw:FW,fh:FH}:null);
  const onPath=pathSetOf(m,active);
  // nodes: keyed divs, moved (CSS transition) or created (.enter → fade in)
  const live=new Set(Object.keys(m));
  gn.querySelectorAll('.gnode').forEach(el=>{ if(!live.has(el.dataset.id)) el.remove(); });
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
    if(isWip){   // the node being born: spinner + label + elapsed + the leaf stack + the live receipt tail
      const secs=WIP_T0?Math.round((Date.now()-WIP_T0)/1000)+'s':'';
      inner=`<div class="nk wipl"><span class=spin aria-hidden=true></span>`+
        `${esc(WIP_PENDING.label||'Working')} · <span id=wiptime>${secs}</span></div>`;
      inner+=leafStackHtml();   // research lanes live INSIDE the node, stacked; green as each resolves
      inner+=`<div class=nspew>`+(WIPLOG.length?WIPLOG.map(l=>`<div>${esc(l)}</div>`).join(''):'<div>warming up…</div>')+`</div>`;
    } else {
      inner=`<div class=nk><span aria-hidden=true>${KICON[n.kind]||'▤'}</span>${esc(n.kind||'part')}</div>`+
        `<div class=nt>${esc(nodeLabel(n))}</div>`;
    }
    if(isFocus)inner+=`<div class=nbody>${nodeBody(n)}</div>`;
    el.innerHTML=inner;
    if(fresh)requestAnimationFrame(()=>requestAnimationFrame(()=>el.classList.remove('enter')));
  });
  gn.querySelectorAll(':scope > .gleaf').forEach(el=>el.remove());   // legacy fanned leaves (now in-node)
  if(!wip&&LEAF_OPEN.size)LEAF_OPEN=new Set();
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
function leafToggle(i){ if(LEAF_OPEN.has(i))LEAF_OPEN.delete(i); else LEAF_OPEN.add(i); renderView(); }
function leafStackHtml(){   // research lanes as a stack INSIDE the working node: click to expand, ✓ green when done
  if(!LANES.length)return '';
  return `<div class=leafstack>`+LANES.map((q0,i)=>{
    const done=LANES_DONE.has(i), open=LEAF_OPEN.has(i);
    const q=q0||('lane '+(i+1));
    return `<div class="gleaf inrow${done?' done':''}${open?' open':''}" title="${open?'':esc(q)}" `+
      `onclick="event.stopPropagation();leafToggle(${i})">`+
      `<span class=lfic aria-hidden=true>🍃</span>`+
      `<span class=lftxt>${esc(open?q:(q.length>34?q.slice(0,34)+'…':q))}</span>`+
      (done?`<span class=lfok aria-hidden=true>✓</span>`:`<span class="spin lfspin" aria-hidden=true></span>`)+
      `</div>`;}).join('')+`</div>`;
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
  const stop=e=>{ if(drag&&!moved){
      if(LEAF_OPEN.size&&!e.target.closest('.gleaf')){ LEAF_OPEN=new Set(); renderView(); }   // click out of an open leaf → fold it
      if(!e.target.closest('.gnode')&&!e.target.closest('.gleaf')){ unfocus(); } }            // click outside → the doc collapses into its node
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
  // the open state survives re-renders — the WIP poll rebuilds this HTML every tick, and an
  // unremembered <details> flashes open then collapses
  if(log&&log.length)body+=`<details class=nhist${HIST_OPEN.has(n.id)?' open':''} `+
    `ontoggle="histKeep('${n.id}',this)"><summary>⚙ how this was built</summary><div class=nspew>`+
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
    const piv=d.feedback?`<p class=react style="font-size:14px">↳ Pivoting on: “${esc(d.feedback)}”</p>`:'';
    return piv+`<p class=eyebrow>${d.spread==='tight'?'Your idea, sharpened':'The directions offered'}</p>`+
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
  const piv=(s.activeNode&&s.activeNode.feedback)?`<p class=react style="font-size:14px">↳ Pivoting on: “${esc(s.activeNode.feedback)}”</p>`:'';
  return piv+`<p class=eyebrow>Pick what clicks</p><div class=optgrid>${cards}</div>`+
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
    `<div class=ctarow><button class=stage-cta onclick=keepGoing()>Keep going →</button>`+
    `<button class="stage-cta secondary" onclick=pivotActive()>⑂ Pivot</button></div>`+
    `<p class=thinking>Comment or steer in the box anytime, it wins.</p>`;
}
function chapterHtml(s){
  const p=s.proposal||{};
  return `<p class=eyebrow>Part ${(s.step||0)+1} of ${s.total}</p><div class=draft>${mdToHtml(p.draft||'')}</div>`+
    `<div class=ctarow><button class=stage-cta onclick=keepGoing()>Keep going →</button>`+
    `<button class="stage-cta secondary" onclick=pivotActive()>⑂ Pivot</button></div>`+
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
  const working=WIP_LABEL||S.status==='researching';
  if(DOCTAB==='_wip'&&!working)DOCTAB=t.active;   // the step landed → roll onto the new doc
  if(!DOCTAB||(DOCTAB!=='_wip'&&!chain.some(n=>n.id===DOCTAB)))DOCTAB=t.active;
  dt.innerHTML=chain.map(n=>
    `<button type=button role=tab aria-selected="${n.id===DOCTAB}" class="dtab${n.id===DOCTAB?' on':''}${n.id===t.active?' cur':''}" onclick="pickDoc('${n.id}')">`+
    `<span aria-hidden=true>${KICON[n.kind]||'▤'}</span>${esc(nodeLabel(n))}</button>`).join('')+
    (working?`<button type=button role=tab aria-selected="${DOCTAB==='_wip'}" class="dtab wipdt${DOCTAB==='_wip'?' on':''}" onclick="pickDoc('_wip')">`+
      `<span class=spin aria-hidden=true></span>${esc((WIP_PENDING&&WIP_PENDING.label)||WIP_LABEL||'Working')}</button>`:'');
  if(DOCTAB==='_wip'){   // the step in flight rides its OWN tab — settled docs keep their pages
    pane.innerHTML=`<div class=docsheet><p class=eyebrow>⚙ ${esc((WIP_PENDING&&WIP_PENDING.label)||'Working')}</p>`+
      `<div class=nspew style="max-height:220px">`+(WIPLOG.length?WIPLOG.map(l=>`<div>${esc(l)}</div>`).join(''):'<div>warming up…</div>')+`</div></div>`;
    return;
  }
  const n=m[DOCTAB]; if(!n){pane.innerHTML='';return;}
  const body=nodeBody(n);
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
{ const m=location.pathname.match(/^\/v2\/plan\/([A-Za-z0-9]+)/); if(m)restorePlan(m[1]); }   // deep link → skip the landing
// the working node's elapsed clock — keeps the build feeling alive even between progress lines
setInterval(()=>{const el=$('wiptime');if(el&&WIP_T0)el.textContent=Math.round((Date.now()-WIP_T0)/1000)+'s';},1000);
