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
async function requireKey(){         // gate any API-calling button: only block when there's truly no key
  if(!CFG.byokEnabled)return true;
  if(!HAS_KEY)await loadKey();        // HAS_KEY can be stale — re-verify against the server before walling,
  if(HAS_KEY)return true;             // so a keyed-up user is never interrupted by the key modal
  keyForm();return false;            // genuinely no key → the add-key form (not the manage-key modal)
}
// The bail-out button: send the procrastinator to a random snarky Google search.
const GOOFS=["is a hotdog a sandwich", "are birds real", "how many golf balls fit in a school bus", "why do cats knock things off tables", "goat screaming like a human", "is cereal a soup", "do fish get thirsty", "how long can a snail nap", "world's largest ball of twine", "can you outrun a goose", "how to fold a fitted sheet", "why do we say um", "who invented the wheel and why", "how many licks to the center of a tootsie pop", "do penguins have knees", "why is yawning contagious", "capybara compilation", "competitive cup stacking finals", "extreme ironing world championship", "octopus solving a puzzle", "why do feet smell like corn chips", "is water wet", "do trees talk to each other", "how to win a staring contest against a pigeon", "longest recorded sneeze", "how do they get the caramel in the candy bar", "what does a quokka sound like", "why do dogs tilt their heads", "the history of the high five", "can a duck climb a ladder", "videos of cats being unimpressed", "how to sell pogs in 2026", "ways to waste time on the internet", "what is the speed of dark", "do cows have best friends", "cheese rolling gloucester injuries", "competitive wife carrying championship", "why do we get goosebumps", "how to skip a rock 50 times", "is a tomato a fruit lawsuit", "man vs raccoon who would win", "bigfoot caught on ring camera", "how to whistle with two fingers", "why do escalators feel weird when stopped", "how do they paint the lines on the road", "why does the alphabet song end so suddenly", "competitive thumb wrestling rules", "what would happen if everyone jumped at once", "how to look busy at work", "why do we park in driveways and drive on parkways", "medieval people reacting to a zipper", "how long could you survive in a ball pit", "world record for most t-shirts worn at once", "do ants have rush hour", "how to convincingly fake a sneeze", "why is it called a building if it is already built", "can you hear a hug", "how to win an argument with a cat", "is it weird to name your roomba", "why do snacks taste better when stolen", "do penguins get cold feet", "how to moonwalk badly", "why is the loch ness monster still missing", "quokka selfie compilation", "how to yodel quietly"];
function goofOff(){
  const q=GOOFS[Math.floor(Math.random()*GOOFS.length)];
  location.href='https://www.google.com/search?q='+encodeURIComponent(q);
}
async function start(){
  const idea=document.getElementById('idea').value.trim(), email=document.getElementById('email').value.trim();
  const go=document.getElementById('go'), err=document.getElementById('err');
  err.textContent='';document.getElementById('joke').innerHTML='';
  if(CFG.authEnabled&&!session){authModal();return;}   // signed-out → prompt them with the sign-in modal
  // First query is on us when a free taste is offered → let the server gate (it 402s needKey once the
  // taste is used). Otherwise (no hosted key) require a key up front.
  if(!CFG.freeTaste){if(!await requireKey())return;}
  const body={idea, stack:STACK_CUR}; if(!session) body.email=email;   // signed in → identity from the token
  if(BOARD.length) body.directors=BOARD;               // optional Board of Directors → vets each step
  go.disabled=true; go.textContent='Researching…'; ACT_RESEARCH=false;
  try{
    const r=await fetch('/api/plan/start',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.gibberish){showJoke(d);go.disabled=false;go.textContent='Build my plan →';return;}  // nonsense → roast, no run
    if(!r.ok){err.textContent=d.error||'Something went wrong.';go.disabled=false;go.textContent='Build my plan →';gate(d);return;}
    SID=d.id;meterBaseline(d.id);   // baseline at 0 so this run's tokens fully count as research streams in
    clearWorkspace();   // new idea → never flash the previous plan's PURSUE block / tabs / research
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
    document.getElementById('node').innerHTML='<div class=node><span class=eyebrow>Working</span><h3>Researching + grading your market…</h3><p class=lead>Pulling sources and grading every number, so vendor spin gets labeled, not laundered. About 1 to 2 minutes. Open <b>The machine</b> tool to watch the leaves green up and the receipts spew in.</p></div>';
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
    if(ln.indexOf('\u00A7LANES\u00A7')===0){ try{Activity.leaves(ACT_ID,JSON.parse(ln.slice(7)));}catch(e){} }
    else if(ln.indexOf('\u00A7LANEDONE\u00A7')===0){ Activity.leafDone(parseInt(ln.slice(10),10)); }
    else if(ln.indexOf('\uD83D\uDD0E')===0){ /* "🔎 X is digging into: <lane>" — now shown as the nested leaf, skip */ }
    else Activity.push(ACT_ID,ln);
  }
  ACT_PROG_N=Math.max(ACT_PROG_N,prog.length);
  if(s.research&&s.research.owned_lanes)Activity.relabelLeaves(s.research.owned_lanes);  // Lane N → owner name
  if(s.research)Activity.leafDetails(s.research.owned_lanes,s.research.rows);             // fill each leaf's internals
}
// ── Stress-test: adversarial assumption-checking, streamed into the runner like the research fan-out ──
let STRESS_BUSY=false;
function runStressTest(){
  if(STRESS_BUSY)return; STRESS_BUSY=true;
  const out=document.getElementById('stressout');
  if(out)out.innerHTML='<div class=think>Attacking your assumptions\u2026</div>';
  fetch('/api/plan/'+SID+'/stress-test',{method:'POST',headers:authHeaders()}).then(r=>r.json().then(d=>({ok:r.ok,d}))).then(({ok,d})=>{
    if(!ok){ if(out)out.innerHTML='<div class=ferr>'+esc(d.error||'Could not start.')+'</div>'; STRESS_BUSY=false; return; }
    const aid=Activity.open('Stress-testing your assumptions'); Activity.resetLeaves(); Activity.push(aid,'Naming the load-bearing assumptions');
    let cur=0;
    function tick(){
      fetch('/api/plan/'+SID+'/stress-test',{headers:authHeaders()}).then(r=>r.json()).then(st=>{
        const prog=st.progress||[];
        for(let i=cur;i<prog.length;i++){ const ln=prog[i]||'';
          if(ln.indexOf('\u00A7LANES\u00A7')===0){ try{Activity.leaves(aid,JSON.parse(ln.slice(7)));}catch(e){} }
          else if(ln.indexOf('\u00A7LANEDONE\u00A7')===0){ Activity.leafDone(parseInt(ln.slice(10),10)); }
          else Activity.push(aid,ln);
        }
        cur=Math.max(cur,prog.length);
        if(st.status==='running'){ setTimeout(tick,1200); return; }
        if(st.status==='error'){ Activity.done(aid,'Hit a snag.'); if(out)out.innerHTML='<div class=ferr>'+esc(st.error||'Stress-test failed.')+'</div>'; STRESS_BUSY=false; return; }
        Activity.done(aid,'Assumptions stress-tested.'); meterTick({id:SID,cost:st.cost,tokens:st.tokens});
        if(out)out.innerHTML=renderStress(st.result); STRESS_BUSY=false;
      }).catch(e=>{ Activity.stop(aid); if(out)out.innerHTML='<div class=ferr>Network error.</div>'; STRESS_BUSY=false; });
    }
    tick();
  }).catch(e=>{ if(out)out.innerHTML='<div class=ferr>Network error.</div>'; STRESS_BUSY=false; });
}
function renderStress(res){
  if(!res||!res.assessments||!res.assessments.length)return '<div class=think>No load-bearing assumptions found to test.</div>';
  const chip={survives:'\u2705 survives',weakened:'\u26A0\uFE0F weakened',broken:'\u274C broken'};
  const col={survives:'#2e7d32',weakened:'#b8860b',broken:'#c62828'};
  const sm=res.summary||{}; let h='<div class=stresssum>'+(sm.broken||0)+' broken \u00B7 '+(sm.weakened||0)+' weakened \u00B7 '+(sm.survives||0)+' survived</div>';
  res.assessments.forEach(a=>{
    h+='<div style="border-left:3px solid '+(col[a.verdict]||'#888')+';padding:4px 0 4px 10px;margin:10px 0">';
    h+='<div><b>'+(chip[a.verdict]||esc(a.verdict))+'</b> &mdash; '+esc(a.assumption)+'</div>';
    if(a.why)h+='<div class=stresswhy style="opacity:.8;margin:3px 0">'+esc(a.why)+'</div>';
    if(a.evidence&&a.evidence.length){ h+='<ul style="margin:4px 0 0;padding-left:18px">'; a.evidence.forEach(e=>{ h+='<li>'+esc(e.text)+' <a href="'+esc(e.url||'')+'" target=_blank rel=noopener>['+esc((e.tier||'').toLowerCase())+'/'+esc((e.judge||'').toLowerCase())+']</a></li>'; }); h+='</ul>'; }
    else h+='<div style="opacity:.6;font-size:.9em">no credible disconfirming evidence found</div>';
    h+='</div>';
  });
  return h;
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
  if(lbl)lbl.innerHTML=esc(u.n)+(u.rec?' <span class=sk-star aria-hidden=true>\u2605</span>':'');
  const c=document.getElementById('stackcost'); if(c)c.innerHTML=_stackCost(i);
  const pop=document.getElementById('stackpop'); if(pop&&!pop.hidden)renderStackTiles();
}
function renderStackTiles(){
  const pop=document.getElementById('stackpop'); if(!pop)return;
  const cur=_stackIdx(STACK_CUR);
  pop.innerHTML='<div class=stackpop-h>Pick your crew. Sets the models behind research, the credibility gate, and the writing you read.</div>'+
    STACKS_UI.map((u,i)=>{
      const locked=stackLocked(u.k);
      const badges=(u.rec?'<span class="st-badge rec">Recommended</span>':'')+(locked?'<span class="st-badge" style="opacity:.7">\uD83D\uDD12 upgrade</span>':'');
      return '<button type=button role=menuitemradio aria-checked='+(i===cur)+' class="stacktile'+(i===cur?' sel':'')+'"'+(locked?' style="opacity:.6"':'')+' onclick="pickStack('+i+')">'+
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
function pickStack(i){const u=STACKS_UI[i];closeStackPop();if(!u)return;
  if(stackLocked(u.k)){const t=tierForStack(u.k);pricingModal(t?('\u201c'+u.n+'\u201d is on the '+t.label+' plan and up. Upgrade to run it on our key, or bring your own key.'):null);return;}
  if(u.k!==STACK_CUR)commitStack(i);}
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
function fmtTokens(n){n=Math.round(n);return n>=1000?(n/1000).toFixed(n>=10000?0:1).replace(/\.0$/,'')+'k':String(n);}
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
let PENDING_PDF=false;   // set on return from Stripe (?pdf=1): auto-download once the credits land
// Back from Stripe checkout: poll for the 3 credits to land (the webhook is async), re-render so the
// button flips to Download, then auto-grab the PDF (which spends one credit). Stays on the plan.
function pdfReady(){ return pdfUnlocked()||pdfCredits()>0; }
async function autoGrabPdf(){
  for(let i=0;i<6;i++){
    if(pdfReady()){download();return;}
    await loadMe();
    try{const r=await fetch('/api/plan/'+SID,{headers:authHeaders()});if(r.ok)render(await r.json());}catch(e){}
    if(pdfReady()){download();return;}
    await new Promise(res=>setTimeout(res,1300));
  }
  if(pdfReady())download(); else toast('Payment received — tap "Download polished PDF".','ok');
}
function _bootDone(){const h=document.documentElement;if(h)h.classList.remove('route-plan');}   // clear the deep-link boot loader
function render(s){
  _bootDone();    // content is painting now → drop the boot loader
  if(s&&s.id&&Activity._restoredSid!==s.id)Activity.restoreFor(s.id);   // lay in this plan's saved machine history (once)
  LAST_S=s;       // stash for the feedback modal (suggested questions, current step)
  meterTick(s);   // tick the session usage meter off this plan's cumulative cost/tokens
  const dlbar=document.getElementById('dlbar');   // "download everything" appears the moment there's data
  if(dlbar)dlbar.style.display=(s.research||(s.files&&s.files.length)||s.vetting||s.shaped)?'':'none';
  CUR_NODE=(s.tree&&s.tree.active)||null;   // #7 key inline comments to the active node
  hideCmtPop();
  if(s.status==='error'){
    const keyErr=isKeyErr(s.error);   // a rejected/expired key → offer to fix the key, not just restart
    const fix=keyErr?'<button type=button class=b-but onclick=keyModal()>Update your key</button> ':'';
    document.getElementById('node').innerHTML='<div class=node><h3>Hit a snag</h3><p class=lead>'+esc(s.error||'Something went wrong.')+'</p>'+fix+'<button type=button class=ghost onclick=newPlan()>Start over</button></div>';
    say('Something went wrong: '+(s.error||'')); return;
  }
  renderResearch(s);renderAnswer(s);renderVet(s);renderNode(s);renderPlanTabs(s);renderAddons(s);renderBoard(s);renderBoardRound(s);renderDecisionTree(s);renderChat(s);renderStack(s);syncSidebar(s);maybeGreetStraightRead(s);
  if(s.done&&SID&&location.pathname!=='/plan/'+SID)history.pushState({plan:SID},'','/plan/'+SID);   // finished plan gets a clean URL (revisit + bookmark)
  if(s.done&&PENDING_PDF){PENDING_PDF=false;autoGrabPdf();}   // returned from Stripe → grab the PDF now
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
  setOpen('straightsec',phase==='intro');    // the straight read leads the first view
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
  if(!await requireKey())return;
  const log=document.getElementById('chatlog'),btn=document.getElementById('chatsend'),st=document.getElementById('chatstart');
  CHAT_BUSY=true;btn.disabled=true;t.value='';if(st)st.innerHTML='';
  log.insertAdjacentHTML('beforeend',`<div class="cmsg user">${esc(msg)}</div><div class="cmsg bot md" id=chatthinking><span class=think>Thinking…</span></div>`);
  log.scrollTop=log.scrollHeight;
  const aid=Activity.start(["Reading your plan","Checking the graded evidence","Thinking it through"],1200,'You asked: '+(msg.length>40?msg.slice(0,40)+'\u2026':msg));
  tabNotify('chatsec','running');
  try{
    const [r]=await Promise.all([fetch('/api/plan/'+SID+'/chat',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({message:msg})}),new Promise(res=>setTimeout(res,850))]);
    const d=await r.json();const th=document.getElementById('chatthinking');
    if(!r.ok){Activity.stop(aid);tabNotify('chatsec','fail');if(th){th.removeAttribute('id');th.innerHTML='<span class=think>'+esc(d.error||'Could not reach the advisor.')+'</span>';}}
    else{Activity.done(aid,'Answered.');tabNotify('chatsec','done');meterTick({id:SID,cost:d.cost,tokens:d.tokens});if(th){th.removeAttribute('id');th.innerHTML=mdToHtml(d.reply);}}
  }catch(e){Activity.stop(aid);tabNotify('chatsec','fail');const th=document.getElementById('chatthinking');if(th)th.innerHTML='<span class=think>Network error.</span>';}
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
  // The auto per-section board review now lives in the SAME "Your board weighed in" section at the top
  // of the Board of Directors drawer as the on-demand convene result (one home for board output).
  const el=document.getElementById('conveneresult'); if(!el)return;
  const wi=document.getElementById('ds-weighedin');
  const reviews=s.board||[];
  if(!reviews.length){return;}   // leave whatever's there (e.g. a convene result); nothing to add
  if(wi){wi.hidden=false;wi.classList.add('open');}
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
    const src=x.url?`<a href="${esc(x.url)}" target=_blank rel=noopener>${esc(host(x.url))}</a>, `:'';
    return `<li>${x.mark==='ok'?'✅':'⚠️'} ${esc(x.text)} <span class="badge ${x.mark==='ok'?'b-ok':'b-warn'}">${x.mark==='ok'?'cited':'vendor'}</span><br><span class=note>${src}${esc(x.note)}${esc(stale)}${by}</span>${gate}</li>`;
  }).join('')+'</ul>'):'';
  el.innerHTML=lanesHtml+rowsHtml;
}
// Query your research: 'quick' reads the gathered research (ok to be unsure); 'deep' spawns fresh research.
let RQ_BUSY=false;
async function runResearchQuery(mode){
  if(!await requireKey())return;
  if(RQ_BUSY)return;
  const q=((document.getElementById('rqinput')||{}).value||'').trim();
  if(q.length<3){toast('Ask a question about your research.','err');const t=document.getElementById('rqinput');if(t)t.focus();return;}
  RQ_BUSY=true;
  const out=document.getElementById('rqout');
  const deep=mode==='deep';
  const steps=deep?["Planning fresh research","Pulling + grading new sources","Answering from what cleared"]:["Reading your graded research","Checking what it actually says"];
  // terminal-style spew, inline in the drawer, until the answer comes back
  if(out)out.innerHTML='<div class=rqspew id=rqspew></div>';
  const spew=document.getElementById('rqspew');
  const raid=Activity.open((deep?'Researching: ':'Quick check: ')+(q.length>42?q.slice(0,42)+'\u2026':q||'your question'));   // tight headline + machine-tab mirror
  tabNotify('researchsec','running');
  let si=0; const pushLine=()=>{if(si<steps.length){if(spew){const d=document.createElement('div');d.className='rqline';d.textContent='\u203a '+steps[si];spew.appendChild(d);spew.scrollTop=spew.scrollHeight;}Activity.push(raid,steps[si]);si++;}};
  pushLine(); const tmr=setInterval(pushLine,1100);
  try{
    const r=await _aiRun('/api/plan/'+SID+'/research/query',{question:q,mode:deep?'deep':'quick'});
    const d=await r.json(); clearInterval(tmr);
    if(!r.ok){Activity.stop(raid);tabNotify('researchsec','fail');if(out)out.innerHTML='<div class=ferr>'+esc(d.error||'Could not run that.')+'</div>';RQ_BUSY=false;return;}
    Activity.done(raid,'Answered from the graded research.');tabNotify('researchsec','done');
    if(d.cost!=null)meterTick({id:SID,cost:d.cost,tokens:d.tokens});
    let html='<div class="rqans md">'+mdToHtml(d.answer||'')+'</div>';
    if(d.rows&&d.rows.length){html+='<div class=rqrows>'+d.rows.map(x=>`<div class=rqrow>${x.mark==='ok'?'\u2705':'\u26a0\ufe0f'} ${esc(x.text)} <span class=rqsrc>${esc(host(x.url))}</span></div>`).join('')+'</div>';}
    if(out)out.innerHTML=html;
  }catch(e){clearInterval(tmr);Activity.stop(raid);tabNotify('researchsec','fail');if(out)out.innerHTML='<div class=ferr>Network error.</div>';}
  RQ_BUSY=false;
}
let SUM_OPEN=true;
function toggleSummary(){SUM_OPEN=!SUM_OPEN;const a=document.getElementById('answer');if(!a)return;
  a.classList.toggle('collapsed',!SUM_OPEN);const h=a.querySelector('.sum-head');if(h)h.setAttribute('aria-expanded',String(SUM_OPEN));}
function renderAnswer(s){
  const p=s.research&&s.research.prose; if(!p)return;
  const a=document.getElementById('answer'); a.classList.toggle('collapsed',!SUM_OPEN);
  const v=(s.vetting||{}).verdict||'';
  const stamp=v?`<span class="verdict ${esc(v)}">${esc(v)}</span>`:'';   // PURSUE/PIVOT/KILL sits next to the headline
  const mt=(s.vetting||{}).model_type||'';                              // its realistic shape, next to the verdict
  const MT_LABELS={'full-time':'Full time','side-hustle':'Side hustle','seasonal':'Seasonal','one-shot':'One shot','gig':'Gig','scalable':'Scalable'};
  const mtb=(mt&&MT_LABELS[mt])?`<span class="modelbadge mt-${esc(mt)}">${esc(MT_LABELS[mt])}</span>`:'';
  // the cheeky spoken reaction (vet voice) — a plain-spoken sub-header under the title
  const react=(s.vetting&&s.vetting.reaction)?`<p class=sum-react>${esc(s.vetting.reaction)}</p>`:'';
  a.innerHTML=`<button type=button class=sum-head aria-expanded="${SUM_OPEN}" onclick=toggleSummary()><span class=sum-headl>${stamp}${mtb}<h2>${esc(p.title)}</h2></span><span class=sum-caret aria-hidden=true>\u25be</span></button>`+
    `<div class=sum-body>${react}<p class=tag>Your offer, with the research graded, vendor spin labeled, not laundered.</p>`+
    `<p><b>What you'd sell:</b> ${esc(p.offer)}</p><p><b>How you'd sell it:</b> ${esc(p.gtm)}</p></div>`;
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
    // Build-from-here works AFTER completion too: jump to an earlier node and roll a NEW branch from
    // clean context at that point (its own files/research only). The new branch is its own finished plan
    // → its own $7 PDF unlock, priced on the new data alone.
    const build=nodeId?`<button type=button class=pbuild onclick="gotoNode('${nodeId}')">↩ Jump back and build from here</button>`:'';
    const back=`<button type=button class=ghost onclick=backToCurrent()>${s.done?'Back to overview':"Back to the part you're on"} →</button>`;
    view.innerHTML=`<div class=node><span class=eyebrow>From your plan</span><h3>${esc(sec.title||'Part')}</h3><p class=h3sub>${esc(sec.sub||'')}</p><div class="draft md">${mdToHtml(content)}</div><div class=planacts>${build}${back}</div></div>`;
    view.style.display='block'; if(node)node.style.display='none';
    const bar=document.getElementById('actionbar'); if(bar)bar.classList.remove('show'); document.body.classList.remove('hasbar');
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
    n.innerHTML='<div class=node><div class=done>🎉 <b>Your plan is ready</b>, all '+s.total+' parts. This is your plan\'s home: grab the <b>polished PDF</b> (or the free raw files), <b>chat with your plan</b> in the sidebar to pressure-test it, or share it.</div>'+
    qaHtml(s.qa)+
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
  bar.classList.remove('working');                       // clear any leftover spinner state (the run finished → render)
  const w=bar.querySelector('.ab-working'); if(w)w.remove();
  const killed=(s.vetting||{}).verdict==='kill';
  const show=!!(s&&s.proposal&&!s.done&&!killed&&(s.status==='building'||s.status==null));
  bar.classList.toggle('show',show);
  document.body.classList.toggle('hasbar',show);
  if(show)bar.innerHTML=
    `<span class=ab-hint><b>I'm with you</b> locks this part and builds the next<br><b>Not feeling it</b> redraws it (add a note to steer)</span>`+
    `<button type=button class="ab-btn ab-back" onclick="openFeedbackModal('regen')" title="Redo this part \u2014 you can add a note to steer the rewrite">\u21bb Not feeling it</button>`+
    `<button type=button class="ab-btn ab-next" onclick="openFeedbackModal('next')" title="Lock this part in and build the next one">I'm with you \u2192</button>`;
  if(show)maybeStepHint(); else dismissStepHint(true);
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
  const head=WOD_PUSHES>0?'\u26d4 Still nothing to sell':'\u26d4 Not buildable yet';
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
  if(!await requireKey())return;
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
function useFb(t){const f=document.getElementById('feedback'); if(!f)return; f.value=(f.value?f.value.replace(/\s*$/,'')+' ':'')+t; f.focus();}

// ── #2 inline comments: highlight the targeted block, leave a 💬/✕ marker you can edit or remove ──
let COMMENTS={};     // {nodeId:[{quote,note,blk}]}  (blk = index among the draft's block elements)
let CUR_NODE=null;   // active node id (keys the comments)
let CMT_QUOTE='', CMT_BLK=-1, CMT_EDIT=-1;
function nodeComments(){return (CUR_NODE&&COMMENTS[CUR_NODE])||[];}
function commentsSteer(){   // fold inline comments into the feedback string the model receives
  const cs=nodeComments(); if(!cs.length)return '';
  return "\n\nInline comments on the current draft (address each, anchored to the quoted text):\n"+
    cs.map(c=>`- On \u201c${c.quote}\u201d: ${c.note}`).join("\n");
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
    mk.innerHTML=`<button type=button class=cmtmark-e title="Edit note: ${esc(c.note)}" onclick="editComment(${i})">💬</button><button type=button class=cmtmark-x aria-label="Remove note" title="Remove note" onclick="removeComment(${i})">\u00d7</button>`;
    b.appendChild(mk);
  });
}
function renderComments(){const box=document.getElementById('cmtlist'); if(box)box.innerHTML=''; decorateComments();}
// The inline comments, enumerated inside the feedback modal so you can SEE what's being sent with your
// note (they're folded into the prompt via commentsSteer). Each is removable from here.
function renderModalComments(){
  const host=document.getElementById('mfbcmtshost'); if(!host)return;
  const cs=nodeComments();
  host.innerHTML = cs.length ? (`<div class=mfb-h>Your comments on this part (sent with your note)</div><ol class=mfb-clist>`+
    cs.map((c,i)=>{const q=c.quote.length>90?c.quote.slice(0,90)+'\u2026':c.quote;
      return `<li><span class=mfb-cq>\u201c${esc(q)}\u201d</span> <span class=mfb-cn>${esc(c.note)}</span><button type=button class=mfb-cx aria-label="Remove this comment" title="Remove" onclick="removeModalComment(${i})">\u00d7</button></li>`;
    }).join('')+`</ol>`) : '';
}
function removeModalComment(i){ removeComment(i); renderModalComments(); }
function removeComment(i){const cs=nodeComments();cs.splice(i,1);decorateComments();}
function editComment(i){const c=nodeComments()[i]; if(!c)return; CMT_EDIT=i; CMT_QUOTE=c.quote||''; CMT_BLK=c.blk;
  openCmtPop(draftBlocks()[c.blk], c.note);}
function openCmtPop(anchorEl, prefill){
  const pop=document.getElementById('cmtpop'); if(!pop)return;
  clearCmtTarget(); if(anchorEl)anchorEl.classList.add('cmt-target');
  document.getElementById('cmtquote').textContent='\u201c'+(CMT_QUOTE.length>90?CMT_QUOTE.slice(0,90)+'\u2026':CMT_QUOTE)+'\u201d';
  document.getElementById('cmtnote').value=prefill||'';
  pop.classList.add('show'); pop.setAttribute('aria-hidden','false');   // show first so we can measure it
  const r=anchorEl?anchorEl.getBoundingClientRect():{left:40,bottom:80,top:60};
  const pw=pop.offsetWidth||288, ph=pop.offsetHeight||170;
  pop.style.left=Math.max(8,Math.min(window.scrollX+r.left,window.scrollX+window.innerWidth-pw-8))+'px';
  let top=window.scrollY+r.bottom+8;
  if(r.bottom+8+ph>window.innerHeight){top=window.scrollY+r.top-ph-8;if(top<window.scrollY+8)top=window.scrollY+8;}   // flip up if it would run off the bottom
  pop.style.top=top+'px';
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
function addChip(txt){const t=document.getElementById('feedback'); if(!t)return; t.value=(t.value?t.value.replace(/\s*$/,'')+', ':'')+txt; t.focus();}
function _navBusy(){const n=document.getElementById('node');if(n)n.querySelectorAll('button').forEach(b=>b.disabled=true);
  const ab=document.getElementById('actionbar');
  if(ab){ab.querySelectorAll('button').forEach(b=>b.disabled=true);ab.classList.add('working');
    if(!ab.querySelector('.ab-working'))ab.insertAdjacentHTML('beforeend','<div class=ab-working><span class=spin aria-hidden=true></span><span>Working<span class=dots><i>.</i><i>.</i><i>.</i></span></span></div>');}
  const f=document.getElementById('ferr');if(f)f.textContent='';document.getElementById('err2').textContent='';}
function _navFree(){const n=document.getElementById('node');if(n)n.querySelectorAll('button').forEach(b=>b.disabled=false);
  const ab=document.getElementById('actionbar');if(ab){ab.classList.remove('working');const w=ab.querySelector('.ab-working');if(w)w.remove();ab.querySelectorAll('button').forEach(b=>b.disabled=false);}}
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
  if(!await requireKey())return;
  const fb=_fbRead();
  const full=(fb+commentsSteer()).trim();   // #7 fold inline comments into the roll-forward
  _navBusy();
  const steps=[]; if(full)steps.push("Folding in your notes");
  steps.push("Drafting the next part of your plan","Checking it against your graded research");
  const _sx=(LAST_S&&LAST_S.sections)||[], _nt=(_sx[((LAST_S&&LAST_S.step)||0)+1]||{}).title;   // tight headline
  const aid=Activity.start(steps,1200,_nt?("Writing \u2018"+_nt+"\u2019"):'Building the next part');
  try{
    const r=await _aiRun('/api/plan/'+SID+'/next',{feedback:full});
    const s=await r.json();
    if(!r.ok){Activity.stop(aid);fbErr(s.error||'Something went wrong.');_navFree();return;}
    REDRAFTS=0;   // advanced past this part — reset the rework counter
    Activity.done(aid,'Next part ready.');render(s);
  }catch(e){Activity.stop(aid);fbErr('Network error.');_navFree();}
}
async function reCheck(){       // kill-gate rescue: re-vet with the substance the operator just added
  if(!await requireKey())return;
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
  if(!await requireKey())return;
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
  if(!await requireKey())return;
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
async function openFeedbackModal(mode){
  if(!await requireKey())return;
  FB_MODE=mode; const s=LAST_S||{};
  const qs=suggestedFb(s);
  const sfb=qs.length?(`<div class=mfb-sg><div class=mfb-h>The engine's open questions</div>`+
    qs.map(q=>`<button type=button class=mfb-q data-q="${esc(q)}" onclick="useFb(this.dataset.q)">${esc(q)}</button>`).join('')+`</div>`):'';
  const regen=mode==='regen';
  document.getElementById('modal-title').textContent=regen?"What's not landing?":'Roll forward, any notes?';
  document.getElementById('modal-body').innerHTML=
    `<p class=mfb-hint>${regen?"Tell me what to change and I'll rework this part.":'Add an optional note to steer the next part, or just go.'}</p>`+sfb+
    `<div class=mfb-cmts id=mfbcmtshost></div>`+
    `<div class=mfb-chips id=fbchips><span class=mfb-load>thinking up quick edits\u2026</span></div>`+
    `<label for=feedback class=sr-only>Your feedback</label>`+
    `<textarea id=feedback rows=3 placeholder="${regen?'e.g. simpler pricing, drop the second tier':'Optional note\u2026'}"></textarea>`+
    `<div class=ferr id=ferr></div>`;
  document.getElementById('modal-actions').innerHTML=
    `<button type=button class=ghost onclick="_closeModal()">Cancel</button>`+
    `<button type=button class=mfb-go onclick="commitFeedback()">${regen?'Rework it':'Go'} \u2192</button>`;
  renderModalComments(); _openModal('#feedback'); loadNudges();
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
// The decision tree, back as a sidebar tab: an interactive, descriptive map of every node you've built.
// The highlighted path is the active plan; click any node to jump there (gotoNode), then back up + branch.
function renderDecisionTree(s){
  const sec=document.getElementById('dtreesec'),box=document.getElementById('dtree');
  if(!sec||!box)return;
  const t=s.tree;
  if(!t||!t.show){sec.style.display='none';return;}
  sec.style.display='';
  const nodes=t.nodes||[],byId={},kids={};
  nodes.forEach(n=>{byId[n.id]=n;kids[n.id]=[];});
  nodes.forEach(n=>{if(n.parent!=null&&kids[n.parent])kids[n.parent].push(n.id);});
  const path={}; let cur=t.active; while(cur!=null&&byId[cur]){path[cur]=1;cur=byId[cur].parent;}
  const roots=nodes.filter(n=>n.parent==null).map(n=>n.id);
  const subOf={}; (s.sections||[]).forEach((x,i)=>{subOf[i]=x.sub;});
  function row(id,depth){
    const n=byId[id];
    const cls='dnode'+(id===t.active?' on':'')+(path[id]?' path':'');
    const sub=subOf[n.step]?`<span class=dsub>${esc(subOf[n.step])}</span>`:'';
    const tag=n.feedback?`<span class=ds>\u21b3 ${esc(n.feedback.slice(0,60))}</span>`:'';
    let h=`<button type=button class="${cls}" style="padding-left:${8+depth*14}px" onclick="gotoNode('${id}')" aria-current="${id===t.active?'true':'false'}"><span class=dtitle>${esc(n.title||('Part '+(n.step+1)))}</span>${sub}${tag}</button>`;
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
  const sec=document.getElementById('straightsec');
  const v=s.vetting, sh=s.shaped;
  if(!v&&!sh){el.innerHTML='';if(sec)sec.style.display='none';return;}
  if(sec)sec.style.display='';   // the straight read now lives in the left sidebar (poppable to a modal)
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
  el.innerHTML=`<div class="vet sr"><div class=vetbody>${react}${thesis}${edge}${alts}${reason}${risk}${test}${cq}${pm}</div></div>`;
}
// Board selection state (keys); seeded from the default board, editable in intake + sidebar.
let BOARD=(CFG.defaultBoard||[]).slice();
function personaName(key){const p=(CFG.archetypes||[]).find(a=>a.key===key);return p?(p.first||p.name):key;}
let BOARDPICK_OPEN=false;   // optional, so collapsed by default
function toggleBoardPick(){BOARDPICK_OPEN=!BOARDPICK_OPEN;const el=document.getElementById('boardpick');if(!el)return;
  el.classList.toggle('open',BOARDPICK_OPEN);const h=el.querySelector('.bp-head');if(h)h.setAttribute('aria-expanded',String(BOARDPICK_OPEN));}
function renderBoardPick(){
  const el=document.getElementById('boardpick'); if(!el)return;
  const ax=CFG.archetypes||[]; if(!ax.length){el.innerHTML='';return;}
  el.classList.toggle('open',BOARDPICK_OPEN);
  const n=BOARD.length;
  el.innerHTML=`<button type=button class=bp-head aria-expanded="${BOARDPICK_OPEN}" onclick=toggleBoardPick()><span class=lab id=boardpicklab>Pick your Board of Directors<span id=bp-n>${n?` (${n} picked)`:''}</span>, they'll vet every step (optional)</span><span class=bp-caret aria-hidden=true>\u25b8</span></button>`+
    `<div class=opts role=group aria-labelledby=boardpicklab>`+ax.map(a=>`<button type=button class="bchip${BOARD.includes(a.key)?' on':''}" aria-pressed=${BOARD.includes(a.key)} onclick="toggleBoard('${a.key}',this)" title="${esc(a.first?a.first+', ':'')}${esc(a.blurb)}">${esc(a.name)}</button>`).join('')+`</div>`;
}
function toggleBoard(key,btn){
  const i=BOARD.indexOf(key), on=i<0;
  if(i>=0){BOARD.splice(i,1);}else{BOARD.push(key);}
  if(btn){btn.classList.toggle('on',on);btn.setAttribute('aria-pressed',String(on));}
  const c=document.getElementById('bp-n'); if(c)c.textContent=BOARD.length?` (${BOARD.length} picked)`:'';   // live count in the collapsed header
}
let CUSTOM_DIRECTORS=[];
function renderBoard(s){
  const sec=document.getElementById('boardsec'); if(!sec)return;
  if(s.status==='researching'){sec.style.display='none';return;}
  sec.style.display='';
  CUSTOM_DIRECTORS=s.customDirectors||[];
  // Chips reflect the active board; tap to add/drop a director for on-demand convening. Custom-forged
  // directors are listed alongside the built-ins (flagged with a ✦).
  if(SESSION_BOARD===null) SESSION_BOARD=(s.directors&&s.directors.length?s.directors.slice():BOARD.slice());
  const all=(CFG.archetypes||[]).concat(CUSTOM_DIRECTORS);
  document.getElementById('boarddirs').innerHTML=all.map(a=>{
    const custom=CUSTOM_DIRECTORS.some(c=>c.key===a.key);
    return `<button type=button class="bchip${custom?' custom':''}${SESSION_BOARD.includes(a.key)?' on':''}" aria-pressed=${SESSION_BOARD.includes(a.key)} onclick="toggleSessionBoard('${a.key}',this)" title="${esc(a.first?a.first+', ':'')}${esc(a.blurb||'')}">${custom?'\u2726 ':''}${esc(a.name)}</button>`;
  }).join('');
}
let SESSION_BOARD=null;
function toggleSessionBoard(key,el){
  if(SESSION_BOARD===null)SESSION_BOARD=[];
  const i=SESSION_BOARD.indexOf(key), on=i<0;
  if(i>=0){SESSION_BOARD.splice(i,1);}else{SESSION_BOARD.push(key);}
  el.classList.toggle('on',on);el.setAttribute('aria-pressed',String(on));
}
// ── Forge a custom director: distill → draft → QA spew runs INLINE in the tools drawer (the board's
// main content collapses), then the drafted director renders in place to approve / retry / cancel. ──
let FORGE_DRAFT=null, FORGE_DESC='', FORGE_TIMER=null, FORGE_BUSY=false;
const FORGE_STEPS=[{k:'distill',l:'Distilling the archetype'},{k:'draft',l:'Drafting the director'},{k:'qa',l:"QA: checking they're distinct + useful"}];
async function openForge(){
  if(!await requireKey())return;
  const desc=((document.getElementById('forgeinput')||{}).value||'').trim();
  if(desc.length<4){toast('Describe the director you want first.','err');const t=document.getElementById('forgeinput');if(t)t.focus();return;}
  FORGE_DESC=desc;
  const fm=document.getElementById('forgemain'); if(fm)fm.style.display='none';   // hide the input while it runs
  ['ds-directors','ds-convene'].forEach(id=>{const e=document.getElementById(id);if(e)e.classList.remove('open');});   // collapse siblings (don't remove)
  const fs=document.getElementById('ds-forge'); if(fs)fs.classList.add('open','running');
  const panel=document.getElementById('forgepanel'); if(!panel)return;
  panel.hidden=false;
  panel.innerHTML=`<div class=dpanel-h><span>Forging your director</span><button type=button class=dpanel-x onclick=cancelForge() aria-label="Close">\u00d7</button></div>`+
    `<p class=mfb-hint>Running a quick research + QA pass on: <i>${esc(desc.length>120?desc.slice(0,120)+'\u2026':desc)}</i></p>`+
    `<div class=forgetree id=forgetree>`+FORGE_STEPS.map(st=>`<div class=ftstep data-k=${st.k}><span class=ftleaf aria-hidden=true>\uD83C\uDF43</span><span class=ftlabel>${esc(st.l)}</span><span class=ftnote></span></div>`).join('')+`</div>`+
    `<div class=forgeout id=forgeout></div><div class=dpanel-acts id=forgeacts></div>`;
  runForge();
}
function _forgeClose(){
  if(FORGE_TIMER){clearInterval(FORGE_TIMER);FORGE_TIMER=null;}
  const panel=document.getElementById('forgepanel'); if(panel){panel.hidden=true;panel.innerHTML='';}
  const fm=document.getElementById('forgemain'); if(fm)fm.style.display='';
  const fs=document.getElementById('ds-forge'); if(fs)fs.classList.remove('running','done');
}
function _forgeStep(k,state,note){const row=document.querySelector('#forgetree .ftstep[data-k="'+k+'"]');if(!row)return;
  row.classList.remove('running','done');if(state)row.classList.add(state);if(note!=null){const n=row.querySelector('.ftnote');if(n)n.textContent=note;}}
function runForge(){
  FORGE_BUSY=true; FORGE_DRAFT=null;
  const out=document.getElementById('forgeout'); if(out)out.innerHTML='';
  FORGE_STEPS.forEach(st=>_forgeStep(st.k,''));
  // animate the tree greening up while the request is in flight (snaps to done on response)
  let i=0; _forgeStep(FORGE_STEPS[0].k,'running');
  const faid=Activity.open('Forging a director');   // mirror into the machine tab
  tabNotify('boardsec','running');
  FORGE_STEPS.forEach(st=>Activity.push(faid,st.l));
  if(FORGE_TIMER)clearInterval(FORGE_TIMER);
  FORGE_TIMER=setInterval(()=>{ if(i<FORGE_STEPS.length){_forgeStep(FORGE_STEPS[i].k,'done');i++; if(i<FORGE_STEPS.length)_forgeStep(FORGE_STEPS[i].k,'running');} },1400);
  _aiRun('/api/plan/'+SID+'/director/forge',{description:FORGE_DESC}).then(async r=>{
    const d=await r.json(); clearInterval(FORGE_TIMER); FORGE_TIMER=null; FORGE_BUSY=false;
    if(!r.ok){ Activity.stop(faid); tabNotify('boardsec','fail'); FORGE_STEPS.forEach(st=>_forgeStep(st.k,'')); showForgeError(d.error||'Could not forge a director.'); return; }
    Activity.done(faid,'Director forged.');tabNotify('boardsec','done');
    if(d.cost!=null)meterTick({id:SID,cost:d.cost,tokens:d.tokens});
    FORGE_DRAFT=d.persona||null;
    const trace={}; ((FORGE_DRAFT&&FORGE_DRAFT.trace)||[]).forEach(t=>{trace[t.step]=t.note;});
    FORGE_STEPS.forEach(st=>_forgeStep(st.k,'done',trace[st.k]||''));
    showForgeResult();
  }).catch(()=>{ if(FORGE_TIMER)clearInterval(FORGE_TIMER); FORGE_TIMER=null; FORGE_BUSY=false; Activity.stop(faid); tabNotify('boardsec','fail'); showForgeError('Network error.'); });
}
function showForgeResult(){
  const p=FORGE_DRAFT, out=document.getElementById('forgeout'); if(!out)return;
  const fs=document.getElementById('ds-forge'); if(fs){fs.classList.remove('running');fs.classList.add('done');}
  if(!p){showForgeError('No director came back. Try again.');return;}
  const doms=(p.domains||[]).slice(0,6).map(d=>`<span class=fdom>${esc(d)}</span>`).join('');
  out.innerHTML=`<div class=forgecard><div class=fc-name>\u2726 ${esc(p.name)}${p.first?` <span class=fc-first>(${esc(p.first)})</span>`:''}</div>`+
    `<div class=fc-blurb>${esc(p.blurb||'')}</div>`+
    `<div class="fc-voice md">${mdToHtml(p.voice||'')}</div>`+
    (doms?`<div class=fc-doms>${doms}</div>`:'')+`</div>`;
  const acts=document.getElementById('forgeacts'); if(acts)acts.innerHTML=
    `<button type=button class=ghost onclick=cancelForge()>Cancel</button>`+
    `<button type=button class=ghost onclick=runForge()>\u21bb Redo</button>`+
    `<button type=button class=mfb-go onclick=approveForge()>\u2713 Seat on my board</button>`;
}
function showForgeError(msg){
  const out=document.getElementById('forgeout'); if(out)out.innerHTML=`<div class=ferr>${esc(msg)}</div>`;
  const acts=document.getElementById('forgeacts'); if(acts)acts.innerHTML=
    `<button type=button class=ghost onclick=cancelForge()>Cancel</button>`+
    `<button type=button class=mfb-go onclick=runForge()>\u21bb Retry</button>`;
}
async function approveForge(){
  if(!FORGE_DRAFT)return;
  try{
    const r=await fetch('/api/plan/'+SID+'/director/save',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({persona:FORGE_DRAFT})});
    const s=await r.json();
    if(!r.ok){showForgeError(s.error||'Could not seat the director.');return;}
    SESSION_BOARD=null;                 // re-seed the board chips (the new director is now seated)
    const fi=document.getElementById('forgeinput'); if(fi)fi.value='';
    const nm=FORGE_DRAFT.name||'Director'; FORGE_DRAFT=null;
    _forgeClose(); render(s); toast('\u2726 '+nm+' seated on your board.','ok');
  }catch(e){showForgeError('Network error.');}
}
function cancelForge(){ FORGE_DRAFT=null; FORGE_BUSY=false; _forgeClose(); }
// Ask-an-expert + convene open the advisor drawer (a styled flyout, not a browser dialog).
function ask(key){openDrawer('expert',key);}
// Convene the board INLINE in the tools drawer: open the convene sub-section with a question box, run
// it with terminal spew, then render the board's take (skeptic + directors + takeaway) in place.
async function convene(){
  if(!await requireKey())return;
  const dd=document.getElementById('ds-directors'); if(dd)dd.classList.add('open');   // keep the directors section open; the input renders inline below the button
  const body=document.getElementById('convenebody'); if(!body)return;
  body.innerHTML=`<p class=forge-sub>Convene your board on the plan so far. Leave it blank for a general read, or aim them at one thing.</p>`+
    `<label for=conveneq class=sr-only>What should the board weigh in on?</label>`+
    `<textarea id=conveneq rows=2 placeholder="e.g. is the pricing right?"></textarea>`+
    `<button type=button class=mfb-go onclick=runConvene()>Convene the board \u2192</button>`+
    `<div class=dpanel id=convenepanel></div>`;
  const t=document.getElementById('conveneq'); if(t)t.focus();
}
let CONVENE_BUSY=false;
async function runConvene(){
  if(!await requireKey())return; if(CONVENE_BUSY)return;
  const q=((document.getElementById('conveneq')||{}).value||'').trim();
  const panel=document.getElementById('convenepanel'); if(!panel)return;
  const ds=document.getElementById('ds-convene'); if(ds){ds.classList.remove('done');ds.classList.add('running');}
  CONVENE_BUSY=true;
  const steps=["Briefing your board on the plan","Each director weighs in","The skeptic pushes back","Synthesizing their verdict"];
  const aid=Activity.open('Convening your board');   // mirror the run into the machine tab (terminal history)
  tabNotify('boardsec','running');
  panel.innerHTML='<div class=rqspew id=convspew></div>';
  const spew=document.getElementById('convspew'); let si=0;
  const push=()=>{if(si<steps.length){if(spew){const d=document.createElement('div');d.className='rqline';d.textContent='\u203a '+steps[si];spew.appendChild(d);spew.scrollTop=spew.scrollHeight;}Activity.push(aid,steps[si]);si++;}};
  push(); const tmr=setInterval(push,1100);
  try{
    const body={question:q}; if(SESSION_BOARD&&SESSION_BOARD.length)body.directors=SESSION_BOARD;
    const r=await _aiRun('/api/plan/'+SID+'/board',body);
    const d=await r.json(); clearInterval(tmr);
    if(ds){ds.classList.remove('running');ds.classList.add('done');}
    if(!r.ok){Activity.stop(aid);tabNotify('boardsec','fail');panel.innerHTML='<div class=ferr>'+esc(d.error||'Could not convene the board.')+'</div>';CONVENE_BUSY=false;return;}
    Activity.done(aid,'Your board weighed in.');tabNotify('boardsec','done');
    if(d.cost!=null)meterTick({id:SID,cost:d.cost,tokens:d.tokens});
    const split=(d.conflicts&&d.conflicts.toLowerCase()!=='none')?`<span class=split>Where they split: ${esc(d.conflicts)}</span>`:'';
    const balloons=(d.directors||[]).map((x,i)=>`<div class=balloon id=cbal_${i}><button type=button class=bh onclick="document.getElementById('cbal_${i}').classList.toggle('open')">\uD83D\uDCAC ${esc(x.first||x.name)}<span class=caret>\u25b8</span></button><div class="bb md">${mdToHtml(x.take)}</div></div>`).join('');
    const resHtml=`<div class=bround>${skepticCardHtml(d.skeptic||{})}<div class=balloons>${balloons}</div>`+
      `<div class=takeaway><div class=tl>Board takeaway</div>${esc(d.verdict||'')}${split}</div></div>`+
      `<div class=dpanel-acts><button type=button class=ghost onclick=convene()>Convene again</button></div>`;
    // The verdict surfaces as the collapsible "Your board weighed in" section at the TOP of the drawer.
    const res=document.getElementById('conveneresult'); if(res)res.innerHTML=resHtml;
    const wi=document.getElementById('ds-weighedin'); if(wi){wi.hidden=false;wi.classList.add('open');}
    if(ds)ds.classList.remove('open');                                  // collapse the convene input, the result is up top
    panel.innerHTML='';                                                 // clear the spew (it lives in the machine tab now)
    const sb=document.getElementById('sd-body')||document.querySelector('#boardsec'); if(sb)sb.scrollTop=0;
  }catch(e){clearInterval(tmr);Activity.stop(aid);tabNotify('boardsec','fail');if(ds)ds.classList.remove('running');panel.innerHTML='<div class=ferr>Network error.</div>';}
  CONVENE_BUSY=false;
}
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
    title.textContent=p.name||'Expert take';
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
  setTimeout(()=>{if(!focusSel)return;const el=m.querySelector(focusSel);if(el)el.focus();},60);
}
function _closeModal(val){
  const m=document.getElementById('modal');
  if(!m.classList.contains('open'))return;
  m.classList.remove('open');m.setAttribute('aria-hidden','true');
  document.getElementById('modalback').classList.remove('show');
  if(MODAL_TRIGGER&&MODAL_TRIGGER.focus){MODAL_TRIGGER.focus();MODAL_TRIGGER=null;}
  const r=MODAL_RESOLVE;MODAL_RESOLVE=null;if(r)r(val);
}
// ── Sidebar tools are TABS. Clicking a tab slides a full-height drawer out of the toolbar's right edge
// (anchored to the left of the main content, overlapping it) with that section's content; the tab
// highlights. Clicking it again — or to the right of the drawer — closes it. We MOVE the live .secbody
// node (not a clone) so handlers + ids stay intact, then move it back on close.
let POPPED=null;
function _returnPopped(){
  if(!POPPED)return;
  const body=document.querySelector('#sd-body > .secbody');
  if(body&&POPPED.ph&&POPPED.ph.parentNode)POPPED.ph.parentNode.insertBefore(body,POPPED.ph);
  if(POPPED.ph&&POPPED.ph.parentNode)POPPED.ph.parentNode.removeChild(POPPED.ph);
  POPPED=null;
}
function _setTabActive(id){document.querySelectorAll('.side .sec.collap').forEach(s=>s.classList.toggle('tabactive',s.id===id));}
// Tool-tab status notifier: 'running' pulses the tab; 'done'/'fail' apply a green/red highlight that
// persists until they open it (no highlight if they're already viewing that tab). 'clear' resets it.
function tabNotify(secId,state){
  const sec=document.getElementById(secId); if(!sec)return;
  sec.classList.remove('tabrun','tabnew','tabfail');
  if(state==='running'){sec.classList.add('tabrun');return;}
  if(state==='clear')return;
  if(POPPED&&POPPED.id===secId)return;              // already open → nothing new to flag
  sec.classList.add(state==='fail'?'tabfail':'tabnew');
}
function tabOpen(id){
  if(POPPED&&POPPED.id===id){closeSecDrawer();return;}   // clicking the active tab closes it
  if(POPPED)_returnPopped();
  const sec=document.getElementById(id); if(!sec)return;
  sec.classList.remove('tabnew','tabfail');   // opening it clears the new/failed highlight (keep any running pulse)
  const body=sec.querySelector('.secbody'); if(!body)return;
  const h3=sec.querySelector('h3');
  const ph=document.createComment('tab:'+id); body.parentNode.insertBefore(ph,body); POPPED={id,ph};
  document.getElementById('sd-title').textContent=h3?h3.textContent:'';
  const sb=document.getElementById('sd-body'); sb.innerHTML=''; sb.appendChild(body);
  const dr=document.getElementById('secdrawer'); dr.classList.add('open'); dr.setAttribute('aria-hidden','false');
  document.body.classList.add('sd-open'); _setTabActive(id);
  if(window.innerWidth<=1024)document.body.classList.add('drawer-collapsed');   // small screens: the toolbar slides back, the tab content replaces it
}
function closeSecDrawer(){
  const dr=document.getElementById('secdrawer'); if(!dr||!dr.classList.contains('open'))return;
  _returnPopped();
  dr.classList.remove('open'); dr.setAttribute('aria-hidden','true');
  document.body.classList.remove('sd-open'); _setTabActive(null);
}
// Small screens: collapse BOTH the tab drawer and the toolbar (sidebar slides off, the "Tools" rail
// reopens it). Wired to the sidebar's collapse rail and the backdrop (tap outside to dismiss).
function collapseAll(){closeSecDrawer();document.body.classList.add('drawer-collapsed');}
// "Back to tools": close the open tab drawer and reveal the tool menu (the sidebar). On small screens
// the sidebar was collapsed when the drawer opened, so bring it back.
function backToTools(){closeSecDrawer();document.body.classList.remove('drawer-collapsed');}
const TAB_ICONS={spewsec:'\u2699\ufe0f',straightsec:'\uD83D\uDCCB',dtreesec:'\uD83C\uDF3F',chatsec:'\uD83D\uDCAC',boardsec:'\uD83D\uDC65',researchsec:'\uD83D\uDD0D'};
function setupTabs(){   // turn every collapsible sidebar section into a modern nav tab (icon + label, no caret)
  document.querySelectorAll('.side .sec.collap').forEach(sec=>{
    if(!sec.id)return;
    const head=sec.querySelector('.sechead'); if(head)head.onclick=function(){tabOpen(sec.id);};
    const caret=sec.querySelector('.sechead .caret'); if(caret)caret.remove();   // no drop-down arrows
    const pop=sec.querySelector('.sec-pop'); if(pop)pop.remove();
    if(head&&!head.querySelector('.ticon')&&TAB_ICONS[sec.id]){
      const ic=document.createElement('span'); ic.className='ticon'; ic.setAttribute('aria-hidden','true'); ic.textContent=TAB_ICONS[sec.id];
      head.insertBefore(ic,head.firstChild);
    }
  });
}
let GREETED_SID=null;
function maybeGreetStraightRead(s){   // first build view → greet with the straight read in the drawer
  if(!s||s.done||s.status!=='building'||(s.step||0)!==0)return;
  if(!(s.vetting||s.shaped)||GREETED_SID===s.id)return;
  GREETED_SID=s.id;
  setTimeout(()=>{const sec=document.getElementById('straightsec');if(sec&&sec.style.display!=='none')tabOpen('straightsec');},450);
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
  if(!await requireKey())return;
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
  _seq:0, tracks:{}, n:0, _live:0, _everRan:false,
  _el(){return document.getElementById('runner');},
  _log(){return document.getElementById('runner-log');},
  _sec(){return document.getElementById('spewsec');},   // the runner now lives in the "The machine" tab
  _show(){const a=this._el();if(a){a.classList.add('show');a.classList.remove('min');} const sec=this._sec(); if(sec){sec.style.display='';sec.classList.remove('tabfail');}},   // a fresh run clears the prior red
  _trim(){ const log=this._log(); if(!log)return; const done=log.querySelectorAll('.atask.done');
    for(let i=0;i<done.length-7;i++)done[i].parentNode.removeChild(done[i]); },   // keep ~7 history cards
  _mkTask(label){
    const log=this._log(); if(!log)return null;
    const empty=document.getElementById('run-empty'); if(empty)empty.style.display='none';   // first op hides the idle hint
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
    body.scrollTop=body.scrollHeight;   // each spew chain scrolls within itself to the latest line
    return li;
  },
  _advance(t,text){ if(t.line)t.line.classList.replace('active','done'); t.line=this._line(t.body,text,false); },
  _busy(){ const a=this._el(); if(a)a.classList.toggle('busy',this._live>0);
    const sec=this._sec(); if(sec){ if(this._live>0){sec.classList.add('running');sec.classList.remove('done');} else {sec.classList.remove('running'); if(this._everRan)sec.classList.add('done');} } },
  start(steps,interval,label){
    const id=++this._seq; this.n++; this._live++; this._everRan=true; this._show();
    const wrap=this._mkTask(label); const body=wrap?wrap.querySelector('.abody'):null;
    const s=(steps||[]).slice(); let i=0; const t={wrap,body,line:null,timer:null}; this.tracks[id]=t;
    if(s.length)this._advance(t,s[0]);
    t.timer=setInterval(()=>{ if(i<s.length-1){i++;this._advance(t,s[i]);} else {clearInterval(t.timer);t.timer=null;} }, interval||1600);
    this._busy(); return id;
  },
  open(label){ const id=++this._seq; this.n++; this._live++; this._everRan=true; this._show(); const wrap=this._mkTask(label); this.tracks[id]={wrap,body:wrap?wrap.querySelector('.abody'):null,line:null,timer:null}; this._busy(); return id; },
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
    if(immediate)tabNotify('spewsec','fail');   // an op errored → the machine flags red (its only highlight)
    delete this.tracks[id]; this.n=Math.max(0,this.n-1); this._live=Math.max(0,this._live-1);
    this._trim(); this._busy(); this._persist();   // save the terminal history so it survives a refresh
  },
  // ── persist the machine's terminal history (per plan) so a page refresh keeps the spew ──
  _restoredSid:null,
  _key(){ return SID?('filg_machine_'+SID):null; },
  _persist(){
    const key=this._key(), log=this._log(); if(!key||!log)return;
    const cards=[].slice.call(log.querySelectorAll('.atask.done')).slice(-7);
    const data=cards.map(c=>({label:((c.querySelector('.alabel')||{}).textContent||''),
                              html:((c.querySelector('.abody')||{}).innerHTML||'')}));
    try{localStorage.setItem(key,JSON.stringify(data));}catch(e){}
  },
  restoreFor(sid){   // lay this plan's saved terminal history into a fresh machine tab (once per plan)
    this._restoredSid=sid;
    const log=this._log(); if(!log)return;
    log.innerHTML='<div class=run-empty id=run-empty>Nothing running yet. This is the engine\u2019s terminal: every operation shows here step by step and stays as collapsed history you can reopen.</div>';
    let data; try{data=JSON.parse(localStorage.getItem('filg_machine_'+sid)||'[]');}catch(e){data=[];}
    if(!data.length)return;
    const empty=document.getElementById('run-empty'); if(empty)empty.style.display='none';
    data.forEach(rec=>{
      const wrap=document.createElement('div'); wrap.className='atask done collapsed';
      wrap.innerHTML='<button type=button class=ah><span class=astat aria-hidden=true></span><span class=alabel></span><span class=caret aria-hidden=true>&#9662;</span></button><div class=abody></div>';
      wrap.querySelector('.alabel').textContent=rec.label||'Done';
      wrap.querySelector('.abody').innerHTML=rec.html||'';
      wrap.querySelector('.ah').onclick=()=>wrap.classList.toggle('collapsed');
      // rebind restored research leaves to a self-contained toggle (the live _leafbox is gone after a refresh)
      [].forEach.call(wrap.querySelectorAll('.leafnode'),btn=>{btn.onclick=()=>btn.classList.toggle('open');});
      log.appendChild(wrap);
    });
    this._everRan=true; this._busy();
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
        '<span class=leaf-ico aria-hidden=true>\uD83C\uDF43</span>'+
        '<span class=leaf-lbl>Lane '+(i+1)+'</span>'+
        '<span class=leaf-q>'+esc(ln)+'</span><span class=lcaret aria-hidden=true>\u25b8</span></button>'+
      '<div class=leafbody data-i="'+i+'"><div class=lq>'+esc(ln)+'</div>'+
        '<div class=lsrc data-i="'+i+'"><span class=pending>Researching this lane\u2026</span></div></div>'
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
        const gate=(r.tier||jl)?'<span class=gate>\u2699 gate: '+esc((r.tier||'').toLowerCase())+(jl?' \u00b7 '+esc(jl):'')+'</span>':'';
        return '<div class=src>'+(ok?'\u2705':'\u26a0\ufe0f')+' '+esc(r.text)+
          '<br><span class=note>'+esc(host(r.url))+', '+esc(r.note)+'</span>'+gate+'</div>';
      }).join('');
    });
  },
  resetLeaves(){ this._leafbox=null; this._leaflabels=null; },   // the old leaf tree lives in its card; cleared with the log
  stopAll(){ for(const id in this.tracks){if(this.tracks[id].timer)clearInterval(this.tracks[id].timer);}
    this.tracks={}; this.n=0; this._live=0; this._everRan=false; this._restoredSid=null; this.resetLeaves();
    // The machine tab stays present (a persistent terminal): clear the cards, restore the idle hint,
    // drop the running/done state. Don't hide it.
    const a=this._el(); if(a)a.classList.remove('min','busy'); this._busy();
    const sec=this._sec(); if(sec)sec.classList.remove('running','done');
    const l=this._log(); if(l)l.innerHTML='<div class=run-empty id=run-empty>Nothing running yet. This is the engine\u2019s terminal: every operation, the research fan-out, grading, drafting, the board, shows here step by step and stays as collapsed history you can reopen.</div>'; }
};
const RESEARCH_STEPS=["Focusing your idea into one sharp thesis","Spinning up research across the web","Pulling sources on the market and competition","Grading every source for credibility","Flagging vendor-marketing spin","Re-sourcing the headline stats to primary sources","Scoring demand, market, and willingness to pay","Drafting your first offer"];
const PDF_STEPS=["Applying your board's input","Pulling your graded evidence","Building the decision matrix","Laying out a modern, on-brand design","Typesetting your PDF"];
// ── PDF = plan-unlock credits: $7 buys 3 plans; re-downloading an unlocked plan is free ──
// pdfUnlocked(): THIS plan is already free to grab (already unlocked, a comp grant, or billing-off dev).
function pdfUnlocked(){ return !CFG.pdfBilling || !!(LAST_S&&LAST_S.pdfUnlocked) || !!(me&&me.pdf_unlocked); }
// pdfCredits(): account credits left to spend on a new plan (prefer the plan state, fall back to /me).
function pdfCredits(){ const v=(LAST_S&&LAST_S.pdfCredits); return (v!=null?v:((me&&me.pdf_credits)||0)); }
function pdfPriceStr(){ const c=(me&&me.pdf_price)||CFG.pdfPrice||700; return '$'+Math.round(c/100); }
function pdfBtn(){
  if(pdfUnlocked())return '<button type=button onclick=download()>⬇ Download polished PDF</button>';
  if(pdfCredits()>0)return '<button type=button onclick=download()>⬇ Download polished PDF <span class=credithint>('+pdfCredits()+' plan'+(pdfCredits()>1?'s':'')+' left)</span></button>';
  return '<button type=button onclick=buyPdf()>🔓 Unlock polished PDF, '+pdfPriceStr()+' for 3 plans</button>';
}
// The final QA pass report — surfaced on the finished plan so the editing step is visible (the
// agentic-showcase point: show the machine checking its own work before it ships).
function qaHtml(qa){
  if(!qa||!(qa.notes&&qa.notes.length))return '';
  const notes=qa.notes.map(function(x){return '<li>'+esc(x)+'</li>'}).join('');
  const fixed=(qa.fixed&&qa.fixed.length)?'<div class=qafixed>Revised '+qa.fixed.length+' section'+(qa.fixed.length>1?'s':'')+' for consistency.</div>':'';
  return '<details class=qabox open><summary>✅ Final QA pass — checked before completing</summary><ul>'+notes+'</ul>'+fixed+'</details>';
}
async function buyPdf(){
  if(CFG.authEnabled&&!session){toast('Sign in to unlock your PDF.');signinEmail();return;}
  if(!CFG.pdfBilling){toast('Billing isn\'t set up yet.','err');return;}
  try{
    const r=await fetch('/api/plan/'+SID+'/buy-pdf',{method:'POST',headers:authHeaders()});
    const d=await r.json();
    if(d.url){location.href=d.url;return;}            // → Stripe Checkout
    if(d.unlocked){await loadMe();download();return;}  // already paid → just grab it
    toast(d.error||'Could not start checkout.','err');
  }catch(e){toast('Network error starting checkout.','err');}
}
// ── Subscriptions: monthly tiers that run on FILG's key (Starter/Pro/Studio) ──
function subTiers(){ return (me&&me.tiers)||CFG.tiers||[]; }
function curTier(){ return (me&&me.tier)||null; }
function isSub(){ return !!curTier(); }
// Stack keys this account may run ON OUR KEY: BYOK → any (they pay); subscriber → their tier's ceiling;
// otherwise the Opus-free default (the server clamps to match, so this is just UI truth-in-advertising).
function allowedStackKeys(){
  // Subscriber → their tier's stacks (runs on our key are tier-clamped while under the allowance, which
  // is the normal case; overflow onto their own key lifts it server-side). BYOK-only → any stack.
  const t=curTier();
  if(t){const tier=subTiers().find(x=>x.id===t);return tier?tier.stacks.map(s=>s.key):['the-work-horse'];}
  if(HAS_KEY) return STACKS_UI.map(u=>u.k);
  return ['the-work-horse'];
}
function stackLocked(key){ return allowedStackKeys().indexOf(key)<0; }
function tierForStack(key){ for(const t of subTiers()){ if((t.stacks||[]).some(s=>s.key===key)) return t; } return null; }
const FEAT_LABEL={director_forge:'Forge custom directors',custom_directors:'Custom board',skeptic:'Adversarial stress-test'};

async function subscribe(tier){
  if(CFG.authEnabled&&!session){toast('Sign in to subscribe.');signinEmail();return;}
  if(!CFG.subEnabled){toast('Billing isn\u2019t set up yet.','err');return;}
  try{
    const r=await fetch('/api/subscribe',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({tier})});
    const d=await r.json();
    if(d.url){location.href=d.url;return;}          // → Stripe Checkout
    if(d.current){toast('You\u2019re already on that plan.');return;}
    toast(d.error||'Could not start checkout.','err');
  }catch(e){toast('Network error starting checkout.','err');}
}
function _tierCard(t){
  const cur=curTier()===t.id;
  const names=(t.stacks||[]).map(s=>{const u=STACKS_UI.find(x=>x.k===s.key);return u?u.n:s.key;});
  const top=names[names.length-1]||'';
  const feats=(t.features||[]).map(f=>FEAT_LABEL[f]||f);
  return '<div class=tiercard style="border:1px solid '+(cur?'#2a7':'#ccc')+';border-radius:8px;padding:14px;flex:1;min-width:150px">'+
    '<div style="font-weight:700">'+esc(t.label)+(cur?' <span style="color:#2a7">\u2713 current</span>':'')+'</div>'+
    '<div style="font-size:1.5em;font-weight:700;margin:4px 0">$'+t.price+'<span style="font-size:.5em;opacity:.6">/mo</span></div>'+
    '<div style="font-size:.85em;opacity:.8;margin-bottom:8px">Models up to <b>'+esc(top)+'</b></div>'+
    (feats.length?'<ul style="font-size:.85em;margin:0 0 10px;padding-left:18px">'+feats.map(f=>'<li>'+esc(f)+'</li>').join('')+'</ul>':'<div style="font-size:.85em;opacity:.6;margin:0 0 10px">Core plan builder + PDF</div>')+
    '<div style="font-size:.8em;opacity:.7;margin-bottom:10px">Polished PDF included \u00b7 runs on our key</div>'+
    (cur?'<button type=button disabled>Your plan</button>':'<button type=button onclick="subscribe(\''+t.id+'\')">Choose '+esc(t.label)+'</button>');
}
function subMeterHtml(){
  const s=me&&me.subscription; if(!s||!s.cap_cents)return '';
  const pct=Math.min(100,Math.round(100*s.spent_cents/s.cap_cents));
  const reset=s.reset_at?(' \u00b7 resets '+new Date(s.reset_at).toLocaleDateString()):'';
  return '<div style="margin:0 0 14px;font-size:.85em">This month\u2019s allowance: <b>'+pct+'% used</b>'+reset+
    '<div style="height:6px;background:#eee;border-radius:3px;margin-top:4px;overflow:hidden"><div style="height:100%;width:'+pct+'%;background:'+(pct>=100?'#c33':'#2a7')+'"></div></div></div>';
}
function pricingModal(note){
  if(!subTiers().length){keyForm();return;}          // no tiers configured → fall back to BYOK
  document.getElementById('modal-title').textContent=isSub()?'Change your plan':'Keep building';
  const cards=subTiers().map(_tierCard).join('');
  const byok=CFG.byokEnabled?'<div style="margin-top:14px;font-size:.9em">Prefer your own API key? <a href=# onclick="_closeModal();keyForm();return false">Bring your own key</a> \u2014 free and unlimited, you pay your provider (pennies a plan). The polished PDF is '+pdfPriceStr()+' for 3 plans on that path.</div>':'';
  document.getElementById('modal-body').innerHTML=
    (note?'<p class=or style="margin:0 0 10px">'+esc(note)+'</p>':'')+(isSub()?subMeterHtml():'')+
    '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:6px">'+cards+'</div>'+byok;
  document.getElementById('modal-actions').innerHTML='<button type=button class=ghost onclick="_closeModal()">Maybe later</button>';
  _openModal('.tiercard button:not([disabled])');
}
function fairUseModal(d){
  const reset=d&&d.resetAt?(' It resets '+new Date(d.resetAt).toLocaleDateString()+'.'):'';
  document.getElementById('modal-title').textContent='Monthly allowance used';
  document.getElementById('modal-body').innerHTML='<p class=or style="margin:0 0 12px">You\u2019ve used this month\u2019s plan allowance on our key.'+esc(reset)+' Add your own API key to keep building for free, or wait for the reset.</p>';
  document.getElementById('modal-actions').innerHTML='<button type=button class=ghost onclick="_closeModal()">OK</button>'+(CFG.byokEnabled?'<button type=button onclick="_closeModal();keyForm()">Add my key</button>':'');
  _openModal('#modal-actions button');
}
function subPlanBlock(){   // the Account tab's subscription section
  if(isSub()){   // a live subscription shows regardless of whether Stripe is wired (dev grants via dev.py)
    const lbl=(me&&me.tier_label)||'your plan';
    const manage=CFG.subEnabled?'<div class=prow><button class=gbtn onclick=pricingModal()>Change plan</button><button class=gbtn onclick=manageBilling()>Manage / cancel</button></div>':'';
    return '<p class=pnote>You\u2019re on <b>'+esc(lbl)+'</b> \u2014 runs on our key, polished PDF included.</p>'+subMeterHtml()+manage;
  }
  if(!CFG.subEnabled)return '<p class=pnote>Subscriptions aren\u2019t enabled here.'+(CFG.byokEnabled?' Bring your own key to build for free.':'')+'</p>';
  return '<p class=pnote>Free on your own API key. Or subscribe monthly to run on our key (no key needed), polished PDF included.</p><div class=prow><button onclick=pricingModal()>See plans</button></div>';
}
async function manageBilling(){   // → Stripe billing portal (update card / cancel)
  try{
    const r=await fetch('/api/subscription/portal',{method:'POST',headers:authHeaders()});
    const d=await r.json();
    if(d.url){location.href=d.url;return;}
    toast(d.error||'Could not open billing.','err');
  }catch(e){toast('Network error.','err');}
}
// Central handler for a gated API error payload. Returns true if it opened a prompt.
function gate(d){
  if(!d)return false;
  if(d.fairUse){fairUseModal(d);return true;}
  if(d.upgrade){pricingModal('That\u2019s a Pro feature (forge a custom director, adversarial stress-test). Upgrade, or add your own key.');return true;}
  if(d.needKey){ if(CFG.subEnabled&&!HAS_KEY){pricingModal();} else {keyForm();} return true; }
  return false;
}
async function redeemCoupon(){
  const inp=document.getElementById('coupon'); if(!inp)return;
  const code=(inp.value||'').trim(); if(!code){toast('Enter a code.','err');return;}
  if(CFG.authEnabled&&!session){toast('Sign in to use a code.');signinEmail();return;}
  try{
    const r=await fetch('/api/coupon',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({code})});
    const d=await r.json();
    if(r.ok&&d.unlocked){
      await loadMe();                               // refresh credits
      toast('Code applied, PDF credits added.','ok');
      try{const pr=await fetch('/api/plan/'+SID,{headers:authHeaders()});render(await pr.json());}catch(e){}  // flip the button to Download
    } else { toast(d.error||'That code isn\'t valid.','err'); }
  }catch(e){toast('Network error.','err');}
}
async function download(){
  if(!pdfUnlocked()&&pdfCredits()<=0){buyPdf();return;}   // no access + no credits → checkout
  const willSpend=!pdfUnlocked()&&pdfCredits()>0;          // a new plan, paid from credits
  const aid=Activity.start(PDF_STEPS,1600,'Building your styled PDF');
  const minShow=new Promise(res=>setTimeout(res,2600));   // let the sequence breathe (covers fast mock runs)
  try{
    const [r]=await Promise.all([fetch('/api/plan/'+SID+'/plan.pdf',{headers:authHeaders()}),minShow]);
    if(!r.ok){let d={};try{d=await r.json();}catch(e){} Activity.stop(aid);
      if(d.needPurchase){buyPdf();return;}             // server says out of credits → open checkout
      toast(d.error||'Could not build the PDF.','err');return;}
    const blob=await r.blob();
    meterTick({id:SID,cost:r.headers.get('X-FILG-Cost'),tokens:r.headers.get('X-FILG-Tokens')});
    Activity.done(aid,'Your PDF is ready.');
    const u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download='filg-business-plan.pdf';a.click();URL.revokeObjectURL(u);
    if(willSpend){await loadMe();try{const pr=await fetch('/api/plan/'+SID,{headers:authHeaders()});if(pr.ok)render(await pr.json());}catch(e){}}  // refresh credits + flip to free re-download
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
// Get-your-data-out: everything generated so far. The toolbar button opens a chooser — download the
// plain-text file, or copy an LLM-handoff prompt to continue in any model. Always free, any point.
function openExportModal(){
  if(!SID)return;
  document.getElementById('modal-title').textContent='Take your data with you';
  document.getElementById('modal-body').innerHTML=
    `<p class=mfb-hint>Everything you've built so far is yours, free, at any point. Grab the plain-text file, or copy a prompt that lets any AI pick up exactly where you left off.</p>`+
    `<div class=exp-row><button type=button class=mfb-go onclick="exportTxt();_closeModal()">\u2b07 Download .txt</button>`+
    `<button type=button class=ghost onclick=loadHandoff()>\uD83D\uDCCB As an LLM prompt</button></div>`+
    `<div id=handoffwrap style="display:none"><div class=exp-lbl>Paste this into ChatGPT, Claude, or any model to continue where you left off:</div>`+
    `<textarea id=handofftext class=handoff readonly rows=10>Loading\u2026</textarea>`+
    `<button type=button class=mfb-go onclick=copyHandoff()>\uD83D\uDCCB Copy prompt</button></div>`;
  document.getElementById('modal-actions').innerHTML=`<button type=button class=ghost onclick=_closeModal()>Done</button>`;
  _openModal();
}
function loadHandoff(){
  const wrap=document.getElementById('handoffwrap'), ta=document.getElementById('handofftext');
  if(wrap)wrap.style.display='';
  if(ta){ta.value='Building the prompt\u2026';
    fetch('/api/plan/'+SID+'/handoff.txt',{headers:authHeaders()}).then(r=>r.ok?r.text():Promise.reject()).then(t=>{ta.value=t;ta.focus();ta.select();}).catch(()=>{ta.value='Could not build the prompt. Try the .txt download.';});}
}
function copyHandoff(){
  const ta=document.getElementById('handofftext'); if(!ta)return; ta.focus(); ta.select();
  const ok=()=>toast('Prompt copied. Paste it into any AI.','ok');
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(ta.value).then(ok).catch(()=>{try{document.execCommand('copy');ok();}catch(e){toast('Select the text and copy manually.','err');}});}
  else{try{document.execCommand('copy');ok();}catch(e){toast('Select the text and copy manually.','err');}}
}
async function exportTxt(){
  if(!SID)return;
  try{
    const r=await fetch('/api/plan/'+SID+'/export.txt',{headers:authHeaders()});
    if(!r.ok){toast('Nothing to export yet.','err');return;}
    const blob=await r.blob(),u=URL.createObjectURL(blob);
    const a=document.createElement('a');a.href=u;a.download='filg-export.txt';a.click();URL.revokeObjectURL(u);
  }catch(e){toast('Network error.','err');}
}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
function host(u){try{return new URL(u).hostname.replace(/^www\./,'');}catch(e){return u;}}
function isKeyErr(m){return /key was rejected|expired or invalid|update your key|401|user not found/i.test(m||'');}
function mdToHtml(md){
  let h=esc(md==null?'':md);
  h=h.replace(/`([^`]+)`/g,'<code>$1</code>');
  h=h.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  h=h.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,'<a href="$2" target=_blank rel=noopener>$1</a>');   // [text](url) → embedded
  h=h.replace(/\[(https?:[^\]\s]+)\]/g,function(_,u){return '<a href="'+u+'" target=_blank rel=noopener>'+host(u)+'</a>';});  // [bare url] → linked hostname, not the raw URL
  h=h.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,function(_,pre,u){return pre+'<a href="'+u+'" target=_blank rel=noopener>'+host(u)+'</a>';});  // raw url → linked hostname
  const lines=h.split('\n'); const out=[]; let inList=false; let i=0;
  const cells=function(r){return r.replace(/^\s*\|/,'').replace(/\|\s*$/,'').split('|').map(function(c){return c.trim();});};
  const isRow=function(s){return /^\s*\|.*\|\s*$/.test(s);};
  const isSep=function(s){return /^\s*\|?[\s:|-]*-{2,}[\s:|-]*$/.test(s);};
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
    if(/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(ln)){if(inList){out.push('</ul>');inList=false;}out.push('<hr>');i++;continue;}  // --- → real rule, not text
    if(m=ln.match(/^(#{1,6})\s+(.*)$/)){if(inList){out.push('</ul>');inList=false;}const lvl=Math.min(m[1].length+3,5);out.push('<h'+lvl+'>'+m[2]+'</h'+lvl+'>');i++;continue;}
    if(m=ln.match(/^\s*[-*]\s+(.*)$/)){if(!inList){out.push('<ul>');inList=true;}out.push('<li>'+m[1]+'</li>');i++;continue;}
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
  if((sb&&session)||(CFG.devEmail&&me)){   // real signed-in session, OR the local dev identity (auth off)
    bar.style.display='';
    // portrait icon → the profile page (projects / API config / account); same on every screen, no hamburger
    bar.innerHTML=`<button type=button class=pfp onclick=openProfile() aria-label=Profile title=Profile><svg viewBox="0 0 24 24" aria-hidden=true><circle cx=12 cy=8 r=4 fill=currentColor></circle><path d="M4 20c0-4.4 3.6-7 8-7s8 2.6 8 7" fill=currentColor></path></svg></button>`;
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
  if(!CFG.byokEnabled){toast('Bring-your-own-key isn\u2019t turned on yet.','err');return;}
  if(CFG.authEnabled&&!session){authModal();return;}   // BYOK is account-scoped → sign in first
  let d; try{const r=await fetch('/api/key',{headers:authHeaders()});d=await r.json();}catch(e){d={key:null};}
  if(d&&d.key){
    document.getElementById('modal-title').textContent='Your API key';
    document.getElementById('modal-body').innerHTML=
      `<p class=or style="margin:0 0 12px">You\u2019re running on your own <b>${esc(d.key.provider)}</b> key (\u2022\u2022\u2022\u2022${esc(d.key.last4)}). Swap or remove it any time.</p>`+
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
  const inp=document.getElementById('keyinput'); if(inp)inp.placeholder=(KEY_PROV==='anthropic')?'sk-ant-\u2026':'sk-or-v1-\u2026';
  const help=document.getElementById('keyprovhelp');
  if(help)help.innerHTML=(KEY_PROV==='anthropic')
    ?'Get it at <a href="https://console.anthropic.com/settings/keys" target=_blank rel=noopener>Anthropic \u2192 API keys</a> (Claude direct, all tiers, billed by Anthropic).'
    :'Get it at <a href="https://openrouter.ai/keys" target=_blank rel=noopener>OpenRouter \u2192 Keys</a> (one key fronts every model + cited web search).';
}
function keyPrefixDetect(v){v=(v||'').trim();if(v.indexOf('sk-ant-')===0)setKeyProv('anthropic');else if(v.indexOf('sk-or-')===0)setKeyProv('openrouter');}
function keyForm(){
  document.getElementById('modal-title').textContent='Bring your own key';
  document.getElementById('modal-body').innerHTML=
    `<p class=or style="margin:0 0 10px">Hook up your own key to build your plan and use the full suite of tools: plans, branches, the board, chat, and PDF export. Pick your provider, paste a key, and you pay them directly (usually pennies a plan).</p>`+
    `<div class=modesw id=keyprov role=group aria-label="Key provider" style="margin:0 0 12px"><button type=button data-p=openrouter onclick="setKeyProv('openrouter')">OpenRouter</button><button type=button data-p=anthropic onclick="setKeyProv('anthropic')">Anthropic</button></div>`+
    `<ol class=keysteps><li><span id=keyprovhelp></span></li><li>Create a key and copy it</li><li>Paste it below and save \u2014 we\u2019ll test it before storing</li></ol>`+
    `<label for=keyinput class=sr-only>Your API key</label>`+
    `<input id=keyinput type=password placeholder="sk-or-v1-\u2026" autocomplete=off spellcheck=false oninput="keyPrefixDetect(this.value)" style="margin:4px 0 2px">`+
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
  if(key.length<8){er.textContent='That doesn\u2019t look like a key.';return;}
  btn.disabled=true;btn.textContent='Validating\u2026';
  try{
    const r=await fetch('/api/key',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({provider:KEY_PROV,key})});  // chosen provider (server falls back to prefix detection)
    const d=await r.json();
    if(!r.ok){er.textContent=d.error||'Could not save the key.';btn.disabled=false;btn.textContent='Save & validate';return;}
    HAS_KEY=true;toast('Key saved \u2014 build as many plans as you want. \u2713');_afterKeyChange();
  }catch(e){er.textContent='Network error.';btn.disabled=false;btn.textContent='Save & validate';}
}
async function removeKey(){
  try{const r=await fetch('/api/key/remove',{method:'POST',headers:authHeaders()});
    if(r.ok){HAS_KEY=false;toast('Key removed.');_afterKeyChange();}else toast('Could not remove the key.','err');
  }catch(e){toast('Network error.','err');}
}
function saveIdea(){try{const v=document.getElementById('idea').value;if(v)localStorage.setItem('filg_idea',v);}catch(e){}}
function restoreIdea(){try{const v=localStorage.getItem('filg_idea');if(v){document.getElementById('idea').value=v;localStorage.removeItem('filg_idea');}}catch(e){}}
async function loadMe(){
  if(!session&&!CFG.devEmail){me=null;HAS_KEY=false;return;}   // dev: with CFG.devEmail, fetch /api/me even with auth off
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
  document.body.classList.toggle('ws',id==='workspace');
  if(id!=='workspace'){document.body.classList.remove('hasbar','sd-open');const dr=document.getElementById('secdrawer');if(dr)dr.classList.remove('open');}   // leaving the build → drop the action-bar/drawer state so the landing footer shows
  if(id==='workspace'&&_toolsNarrow())document.body.classList.add('drawer-collapsed');}   // tablet/smaller → tools start collapsed
function _toolsNarrow(){return window.innerWidth<=1024;}   // tablet or smaller
let _wasToolsNarrow=_toolsNarrow();
window.addEventListener('resize',function(){const n=_toolsNarrow();if(n&&!_wasToolsNarrow&&document.body.classList.contains('ws'))document.body.classList.add('drawer-collapsed');_wasToolsNarrow=n;});
function clearWorkspace(){
  // Wipe every workspace render target so a new idea never flashes the previous plan's PURSUE block,
  // plan tabs, research, board, or machine spew. (render() repopulates these for the new plan.)
  LAST_S=null; PLAN_TAB=-99; SUM_OPEN=true; SESSION_BOARD=null; CUR_NODE=null;
  ['answer','node','planview','boardround','vet','rqout','conveneresult','boarddirs','runner-log'].forEach(id=>{const e=document.getElementById(id);if(e)e.innerHTML='';});
  const wi=document.getElementById('ds-weighedin'); if(wi){wi.hidden=true;wi.classList.remove('open');}
  const pw=document.getElementById('planwrap'); if(pw)pw.style.display='none';
  const dl=document.getElementById('dlbar'); if(dl)dl.style.display='none';
  document.querySelectorAll('#workspace .sec.collap').forEach(s=>{if(s.id!=='spewsec')s.style.display='none';});
  closeSecDrawer();
}
function newPlan(){SIDEBAR_PHASE=null;ACT_RESEARCH=false;ACT_PROG_N=0;ACT_ID=null;VET_OPEN=true;VET_STEPPED=false;GREETED_SID=null;Activity.stopAll();closeViewer();clearWorkspace();SID=null;
  // render a FRESH intake — clear any in-flight button/idea/error left over from a prior build or sign-out
  const g=document.getElementById('go'); if(g){g.disabled=false;g.textContent='Build my plan →';}
  const idea=document.getElementById('idea'); if(idea)idea.value='';
  const err=document.getElementById('err'); if(err)err.textContent='';
  const joke=document.getElementById('joke'); if(joke)joke.innerHTML='';
  try{localStorage.removeItem('filg_idea');}catch(e){}
  if(location.pathname!=='/')history.pushState({},'','/');show('intake');renderBoardPick();gateIntake();}
// One profile page: projects, API config, contact, delete account. Replaces the old My-plans /
// Your-key / email / Sign-out header menu (a dropdown-in-a-dropdown on mobile).
// Profile tabs have real, directly-visitable routes (/account/<slug>) so a refresh or a shared link
// lands on the right tab instead of bouncing to the landing page.
const ACCOUNT_TAB_SLUG={projects:'plans',files:'files',api:'api-config',account:'settings'};
const ACCOUNT_SLUG_TAB={plans:'projects',files:'files','api-config':'api',settings:'account'};
function accountUrl(tab){return '/account/'+(ACCOUNT_TAB_SLUG[tab]||'plans');}
function syncAccountUrl(tab,replace){const url=accountUrl(tab);if(location.pathname===url)return;
  const st={account:tab};if(replace)history.replaceState(st,'',url);else history.pushState(st,'',url);}
async function openProfile(tab,replace){
  if(CFG.authEnabled&&!session){authModal();return;}
  if(tab)PROFILE_TAB=tab;
  syncAccountUrl(PROFILE_TAB,replace);   // reflect the open tab in the URL (refresh-safe, bookmarkable)
  let pd={plans:[],total:7,email:(session&&session.user&&session.user.email)||''};
  try{const r=await fetch('/api/plans',{headers:authHeaders()});if(r.ok)pd=await r.json();}catch(e){}
  let key=null;
  if(CFG.byokEnabled){try{const r=await fetch('/api/key',{headers:authHeaders()});if(r.ok)key=(await r.json()).key;}catch(e){}}
  show('profile');renderProfile(pd,key);
}
function showPlans(){openProfile('projects');}   // back-compat: share/delete refreshers route to the profile
function planCardHtml(p,total){
  const meta=p.done?`Finished · ${total} parts`:(p.status==='researching'?'Researching…':`In progress · part ${(p.step||0)+1} of ${total}`);
  const acts=`<button onclick="resume('${p.id}')">${p.done?'Open / iterate':'Resume'}</button>`+
    `<button class=gbtn onclick="sharePlan('${p.id}')">${p.shared?'🔗 Shared':'Share'}</button>`+
    `<button class=gbtn onclick="deletePlan('${p.id}')" aria-label="Delete plan">Delete</button>`;   // downloads now live in the "My files" tab
  // WIP projects show their progress as N/7 (current part); researching = pre-build; done = done.
  const pill=p.done?'done':(p.status==='researching'?'WIP':((p.step||0)+1)+'/'+total);
  return `<div class=pcard><div class=pcard-main><div class=idea>${esc((p.idea||'Untitled').slice(0,90))}</div><div class=meta>${meta} · ${esc(new Date(p.created_at).toLocaleDateString())}</div></div><div class=act><span class="pill ${p.done?'done':''}">${pill}</span>${acts}</div></div>`;
}
// "My files" card: re-download a plan's deliverables. PDF only when unlocked (re-download a purchased
// item); the raw .zip (finished plans) and the LLM hand-off prompt are always free.
function fileCardHtml(p){
  const date=esc(new Date(p.created_at).toLocaleDateString());
  const acts=[];
  if(p.done&&p.pdf_unlocked)acts.push(`<button onclick="resumeDownload('${p.id}')">\u2b07 Polished PDF</button>`);
  if(p.done)acts.push(`<button class=gbtn onclick="resumeZip('${p.id}')">\u2b07 Raw files (.zip)</button>`);
  acts.push(`<button class=gbtn onclick="openExportFor('${p.id}')">\uD83D\uDCCB LLM prompt</button>`);
  return `<div class=pcard><div class=pcard-main><div class=idea>${esc((p.idea||'Untitled').slice(0,90))}</div><div class=meta>${p.done?'Finished':'In progress'} · ${date}</div></div><div class=act>${acts.join('')}</div></div>`;
}
function resumeZip(id){SID=id;downloadZip();}                 // set the active plan, then reuse the existing exporters
function openExportFor(id){SID=id;openExportModal();}
let PROFILE_TAB='projects', PROFILE_PD=null, PROFILE_KEY=null;
function selectProfileTab(t){PROFILE_TAB=t;syncAccountUrl(t);renderProfile(PROFILE_PD,PROFILE_KEY);}
function renderProfile(pd,key){
  PROFILE_PD=pd; PROFILE_KEY=key;
  const total=pd.total||7;
  const TABS=[['projects','Projects'],['files','My files'],['api','API config'],['account','Account']];
  const tabbar=TABS.map(([k,l])=>`<button type=button class="ptab2${PROFILE_TAB===k?' on':''}" onclick="selectProfileTab('${k}')">${l}</button>`).join('');
  let body='';
  if(PROFILE_TAB==='projects'){
    const rows=pd.plans.length?pd.plans.map(p=>planCardHtml(p,total)).join(''):`<p class=empty>No projects yet, build your first one.</p>`;
    body=`<div class=psec-head><h3>Projects</h3><button onclick=newPlan()>+ New plan</button></div>${rows}`;
  }else if(PROFILE_TAB==='files'){
    const rows=pd.plans.length?pd.plans.map(p=>fileCardHtml(p)).join(''):`<p class=empty>Nothing here yet. Build a plan and your files show up here.</p>`;
    body=`<div class=psec-head><h3>My files</h3></div><p class=pnote>Re-download anything you\u2019ve made. The raw export and the LLM hand-off prompt are always free; the polished PDF is here once you\u2019ve unlocked it.</p>${rows}`;
  }else if(PROFILE_TAB==='api'){
    body=!CFG.byokEnabled
      ? `<p class=pnote>Bring-your-own-key isn\u2019t enabled here.</p>`
      : (key?`<p class=pnote>Running on your own <b>${esc(key.provider)}</b> key (\u2022\u2022\u2022\u2022${esc(key.last4)}).</p><div class=prow><button class=gbtn onclick=keyForm()>Replace key</button><button class=gbtn onclick=removeKey()>Remove key</button></div>`
            :`<p class=pnote>No key yet. Add your own OpenRouter or Anthropic key to build plans and use every tool.</p><div class=prow><button onclick=keyForm()>Add a key</button></div>`);
  }else{
    body=`<div class=acct-block><div class=acct-lbl>Contact</div><p class=pcontact>${esc(pd.email||'')}</p></div>`+
      `<div class=acct-block><div class=acct-lbl>Plan</div>${subPlanBlock()}</div>`+
      `<div class=acct-block><div class=acct-lbl>Session</div><div class=prow><button class=gbtn onclick=signout()>Sign out</button></div></div>`+
      `<div class=acct-block><div class=acct-lbl>Danger zone</div><p class=pnote>Permanently delete your account, all projects, your key, and purchase history.</p><div class=prow><button class=danger onclick=deleteAccount()>Delete account</button></div></div>`;
  }
  document.getElementById('profile').innerHTML=
    `<div class=profilewrap>`+
    `<div class=prof-top><h2>Profile</h2><button class=link onclick=newPlan()>\u2190 Back</button></div>`+
    `<div class=ptabs2 role=tablist>${tabbar}</div>`+
    `<section class=psec>${body}</section>`+
    `</div>`;
}
function _afterKeyChange(){   // key add/replace/remove → close the modal and refresh the profile if open
  const pf=document.getElementById('profile'), onProfile=pf&&pf.style.display!=='none';
  _closeModal(); if(onProfile)openProfile();
}
async function deleteAccount(){
  if(!await uiConfirm('Delete your account?','This permanently deletes your account, all your projects, your saved key, and your purchase history. This cannot be undone.','Delete everything'))return;
  try{
    const r=await fetch('/api/account',{method:'DELETE',headers:authHeaders()});
    if(!r.ok){toast('Could not delete your account.','err');return;}
    toast('Your account and all its data were deleted.');
    if(sb)await sb.auth.signOut();
    session=null;me=null;HAS_KEY=false;renderAuth();newPlan();
  }catch(e){toast('Network error.','err');}
}
async function resume(id){
  SID=id;if(location.pathname!=='/plan/'+id)history.pushState({plan:id},'','/plan/'+id);   // clean URL for any entry point
  show('workspace');SESSION_BOARD=null;SIDEBAR_PHASE=null;VET_OPEN=true;VET_STEPPED=false;
  const ab=document.getElementById('addons');if(ab)delete ab.dataset.done;
  closeDrawer();closeViewer();
  try{const r=await fetch('/api/plan/'+SID+'?touch=1',{headers:authHeaders()});const s=await r.json();render(s);if(s.status==='researching')poll();}catch(e){_bootDone();document.getElementById('err2').textContent='Could not load that plan.';}
}
function resumeDownload(id){SID=id;download();}
async function deletePlan(id){
  if(!await uiConfirm('Delete this plan?','This permanently removes the plan. It won\'t free up a free build.','Delete'))return;
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
    // copy only — never redirect. If we're already ON the profile, refresh in place so the chip flips to "Shared".
    const pf=document.getElementById('profile'); if(pf&&pf.style.display!=='none')openProfile();
  }catch(e){toast('Network error.','err');}
}
function banner(msg){const b=document.getElementById('banner');b.textContent=msg;b.style.display='block';}
function openDisclaimer(){
  document.getElementById('modal-title').textContent='Just so we\u2019re clear';
  document.getElementById('modal-body').innerHTML=
    `<p>This is just for fun. Do your research, and talk to your lawyer, your family, or your local deity before you put any real time or money into a new business.</p>`+
    `<p><b>AI is great at being confidently wrong.</b> It will hand you a polished, sure-sounding plan whether or not the idea holds up. Treat everything here as a starting point to pressure-test, not as advice.</p>`+
    `<p>People have talked themselves into real trouble taking a chatbot too seriously. A few reads on that:</p>`+
    `<ul class=disclinks>`+
    `<li><a href="https://www.google.com/search?q=%22AI+psychosis%22+chatbot+case+studies" target=_blank rel=noopener>Reported cases of \u201cAI psychosis\u201d</a></li>`+
    `<li><a href="https://www.google.com/search?q=chatbot+reinforcing+delusions+mental+health" target=_blank rel=noopener>How chatbots can reinforce delusions</a></li>`+
    `</ul>`;
  document.getElementById('modal-actions').innerHTML=`<button type=button onclick="_closeModal()">Got it</button>`;
  _openModal('#modal-actions button');
}
async function initAuth(){
  const q=new URLSearchParams(location.search);
  if(q.get('pdf')){
    PENDING_PDF=true;   // back from Stripe → stay on the plan, auto-download once the unlock lands
    banner('🎉 Payment received. Taking you back to your plan and starting your download…');
    // Drop the ?pdf flag but KEEP the /plan/{id} path so we stay on (and reload to) the finished plan.
    try{history.replaceState(history.state,'',location.pathname);}catch(e){}
  }
  if(q.get('pdf_canceled')){banner('Checkout canceled, no charge. Your raw export is still free.');
    try{history.replaceState(history.state,'',location.pathname);}catch(e){}}
  restoreIdea();renderBoardPick();renderStack();paintMeter();   // show the crew picker + meter from first paint
  if(!CFG.authEnabled||!window.supabase){if(CFG.devEmail)await loadMe();renderAuth();renderStack();routeFromPath();return;}
  sb=window.supabase.createClient(CFG.supabaseUrl,CFG.supabaseAnon);
  sb.auth.onAuthStateChange(async (_e,s)=>{session=s;await loadMe();renderAuth();});
  const {data}=await sb.auth.getSession();session=data.session;await loadMe();renderAuth();routeFromPath();
}
function routeFromPath(){   // deep-link / bookmark / revisit / back-fwd for /plan/{id} and /account/<tab>
  const path=location.pathname||'';
  let m=path.match(/^\/plan\/([a-z0-9]+)/i);
  if(m&&m[1]){resume(m[1]);return;}
  m=path.match(/^\/account(?:\/([a-z-]+))?\/?$/i);
  if(m){
    if(CFG.authEnabled&&!session){_bootDone();show('intake');renderBoardPick();gateIntake();authModal();return;}
    // render the profile first, THEN clear the boot overlay (no landing flash); replace: URL already set
    openProfile(ACCOUNT_SLUG_TAB[(m[1]||'').toLowerCase()]||'projects',true).finally(_bootDone);
    return;
  }
  _bootDone();   // not a known route → drop the boot loader and show home
  if(SID){SID=null;show('intake');renderBoardPick();gateIntake();}
  maybeWelcome();   // first-time "your first plan is on us" popup (once, only when a free taste is offered)
}
window.addEventListener('popstate',routeFromPath);   // browser back/forward drives the SPA
// ── First-run coachmark: explain how to advance the build (once, dismissible) ──
function maybeStepHint(){
  try{if(localStorage.getItem('filg_seen_stephint'))return;}catch(e){}
  const el=document.getElementById('stephint'); if(!el||el.classList.contains('show'))return;
  el.innerHTML=`<div>Two ways forward from here: <b>I'm with you \u2192</b> locks this part in and builds the next one. `+
    `<b>\u21bb Not feeling it</b> redraws this part (add a note to steer it). You can branch back to any earlier part from the plan tree.</div>`+
    `<button type=button class=sh-got onclick=dismissStepHint()>Got it</button>`;
  el.classList.add('show');
}
function dismissStepHint(silent){
  const el=document.getElementById('stephint'); if(el)el.classList.remove('show');
  if(silent!==true){try{localStorage.setItem('filg_seen_stephint','1');}catch(e){}}
}
// ── Welcome popup: "your first plan is on us" (once, only when a free taste is offered) ──
function maybeWelcome(){
  if(!CFG.freeTaste)return;
  try{if(localStorage.getItem('filg_seen_welcome'))return;}catch(e){}
  const el=document.getElementById('welcomepop'); if(el)el.classList.add('show');
}
function dismissWelcome(){const el=document.getElementById('welcomepop');if(el)el.classList.remove('show');
  try{localStorage.setItem('filg_seen_welcome','1');}catch(e){}
  const i=document.getElementById('idea'); if(i)i.focus();}
// ── In-app product help chat (standard website help bubble; runs on the user's key) ──
let HELP_MSGS=[];
function helpBubble(m){return `<div class="help-msg ${m.role==='user'?'u':'a'}">${esc(m.content).replace(/\n/g,'<br>')}</div>`;}
function renderHelp(){const b=document.getElementById('help-body'); if(!b)return; b.innerHTML=HELP_MSGS.map(helpBubble).join(''); b.scrollTop=b.scrollHeight;}
function toggleHelp(){
  const p=document.getElementById('helppanel'); if(!p)return;
  const open=p.classList.toggle('open');
  if(open){
    if(!HELP_MSGS.length){HELP_MSGS.push({role:'assistant',content:"Hi! I can help you use FILG \u2014 building a plan, the buttons, the board, exporting, or your key. What do you need?"});renderHelp();}
    setTimeout(()=>{const i=document.getElementById('help-input'); if(i)i.focus();},30);
  }
}
async function sendHelp(){
  const i=document.getElementById('help-input'); const msg=(i.value||'').trim(); if(!msg)return;
  if(CFG.authEnabled&&!session){authModal();return;}   // login-gated like the rest of the app
  if(!CFG.freeTaste){if(!await requireKey())return;}    // no hosted free path → must use your own key
  i.value=''; HELP_MSGS.push({role:'user',content:msg}); renderHelp();
  const b=document.getElementById('help-body');
  const wait=document.createElement('div'); wait.className='help-msg a'; wait.textContent='\u2026'; if(b){b.appendChild(wait);b.scrollTop=b.scrollHeight;}
  try{
    const r=await fetch('/api/help',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({message:msg,history:HELP_MSGS.slice(-8)})});
    const d=await r.json(); wait.remove();
    if(!r.ok){HELP_MSGS.push({role:'assistant',content:d.error||'Something went wrong.'}); renderHelp(); gate(d); return;}
    HELP_MSGS.push({role:'assistant',content:d.reply||'(no reply)'}); renderHelp();
  }catch(e){wait.remove(); HELP_MSGS.push({role:'assistant',content:'Network error, try again.'}); renderHelp();}
}
function toggleTopMenu(){const r=document.querySelector('.topright'),h=document.getElementById('topham');if(!r)return;const open=r.classList.toggle('open');if(h)h.setAttribute('aria-expanded',String(open));}
document.addEventListener('click',function(e){   // click outside the crew picker closes it
  const sp=document.getElementById('stackpop');
  if(sp&&!sp.hidden&&!e.target.closest('#stackdial'))closeStackPop();
  // click outside the inline-comment popover discards it (same as Cancel) + unhighlights the block
  const cp=document.getElementById('cmtpop');
  if(cp&&cp.classList.contains('show')&&!e.target.closest('#cmtpop')&&!e.target.closest('.draft')&&!e.target.closest('.cmtmark'))hideCmtPop();
  // click outside the collapsed header menu closes it
  const tr=document.querySelector('.topright.open');
  if(tr&&!e.target.closest('.topright')&&!e.target.closest('#topham'))toggleTopMenu();
});
document.addEventListener('keydown',function(e){
  const drawer=document.getElementById('drawer'), modal=document.getElementById('modal');
  const dOpen=drawer&&drawer.classList.contains('open'), mOpen=modal&&modal.classList.contains('open');
  if(e.key==='Escape'){const sp=document.getElementById('stackpop'), sdOpen=document.body.classList.contains('sd-open');
    if(sp&&!sp.hidden){closeStackPop();}else if(mOpen)_closeModal();else if(sdOpen)closeSecDrawer();else if(dOpen)closeDrawer();}
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
setupTabs();   // sidebar sections become tabs that slide out the tools drawer
