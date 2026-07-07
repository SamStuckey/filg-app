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
let SB = null, SESSION = null, ME = null;   // Supabase client · live session · /api/me snapshot

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
function authHeaders(){ return SESSION?{'Authorization':'Bearer '+SESSION.access_token}:{}; }
async function api(method,url,body){
  const r=await fetch(url,{method,headers:{'Content-Type':'application/json',...authHeaders()},
    body:body?JSON.stringify(body):undefined});
  let d={}; try{d=await r.json();}catch(e){}
  return {ok:r.ok,status:r.status,d};
}

// ── Auth (Supabase): magic link + Google, same accounts as v1. The wall lives at commit — the free
// taste (brainstorm → merge) runs anonymous; signing in CLAIMS the tasted plan into the account. ──
function signedIn(){ return !!SESSION||!!CFG.devEmail; }
async function initAuth(){
  if(!CFG.authEnabled||!window.supabase){ if(CFG.devEmail)await loadMe(); paintIdentity(); routeV2(); return; }
  SB=window.supabase.createClient(CFG.supabaseUrl,CFG.supabaseAnon);
  SB.auth.onAuthStateChange(async(_e,s)=>{ SESSION=s; await loadMe(); paintIdentity(); claimPending(); });
  const {data}=await SB.auth.getSession(); SESSION=data&&data.session;
  await loadMe(); paintIdentity(); await claimPending(); routeV2();
}
async function loadMe(){
  if(!SESSION&&!CFG.devEmail){ ME=null; await loadKey(); return; }
  const {ok,d}=await api('GET','/api/me'); ME=(ok&&d&&d.signed_in)?d:null;
  await loadKey();
}
function paintIdentity(){
  const chip=$('tierchip'); if(chip){
    const sub=ME&&ME.subscription;
    if(ME&&ME.tier&&sub&&sub.cap_cents){
      const pct=Math.min(100,Math.round(100*sub.spent_cents/sub.cap_cents));
      chip.hidden=false; chip.textContent=(ME.tier_label||ME.tier)+' · '+pct+'%';
      chip.classList.toggle('hot',pct>=100);
    } else chip.hidden=true;
  }
  const pb=$('profilebtn'); if(pb)pb.title=signedIn()?('Account · '+((ME&&ME.email)||SESSION&&SESSION.user&&SESSION.user.email||'')):'Log in / Sign up';
}
function profileClick(){ if(signedIn())openAccount(); else authModal(); }
// after any sign-in: the plan they tasted anonymously joins the new account (the wall's happy path)
async function claimPending(){
  if(!SESSION)return;
  let sid=null; try{sid=localStorage.getItem('filg_claim_sid');}catch(e){}
  const target=sid||((SID&&S&&!S.owned)?SID:null);
  if(!target)return;
  try{localStorage.removeItem('filg_claim_sid');}catch(e){}
  const {ok,d}=await api('POST',`/api/plan/${target}/claim`,{});
  if(ok&&S&&S.id===target){ render(d); chatStatus('✓ Plan saved to your account.'); }
}
function _rememberClaim(){ try{ if(SID)localStorage.setItem('filg_claim_sid',SID); }catch(e){} }
function authModal(note){
  if(!CFG.authEnabled){ toast('Auth is off here (dev) — identity is FILG_DEV_EMAIL.'); return; }
  const goog=CFG.supabaseUrl?`<button type=button class=gbtn onclick="authGo('google')">Continue with Google</button>`:'';
  $('modal-body').innerHTML=
    `<p class=muted>${esc(note||'Sign in so your plan saves to your account. Free to start — build on your own API key, or subscribe to run on ours.')}</p>`+
    `<div class=authgate>${goog}<button type=button class=gbtn onclick="authGo('email')">✉️ Email me a sign-in link</button></div>`+
    `<div id=authemailrow hidden><input id=authemail type=email placeholder="you@email.com" autocomplete=email>`+
    `<div class=err id=autherr></div></div>`;
  $('modal-acts').innerHTML='<button onclick="closeModal()">Not now</button>';
  openModal('Sign in / Sign up');
}
function authGo(kind){
  if(kind==='google')return signinGoogle();
  const row=$('authemailrow');
  if(row&&row.hidden){ row.hidden=false;
    $('modal-acts').innerHTML='<button onclick="closeModal()">Not now</button>'+
      '<button class=primary onclick="signinEmail()">Send the link</button>';
    setTimeout(()=>{const i=$('authemail');if(i)i.focus();},30); }
}
function _authReturnTo(){ return location.origin+(SID?'/plan/'+SID:'/'); }
async function signinGoogle(){
  if(!SB){ toast("Auth isn't configured here.",'err'); return; }
  _rememberClaim();
  const {error}=await SB.auth.signInWithOAuth({provider:'google',options:{redirectTo:_authReturnTo()}});
  if(error)toast(error.message,'err');
}
async function signinEmail(){
  const email=(($('authemail')||{}).value||'').trim(), er=$('autherr');
  if(!email||email.indexOf('@')<0){ if(er)er.textContent='Enter your email.'; return; }
  if(!SB){ toast("Auth isn't configured here.",'err'); return; }
  _rememberClaim();
  const {error}=await SB.auth.signInWithOtp({email,options:{emailRedirectTo:_authReturnTo()}});
  closeModal();
  toast(error?error.message:'Check your inbox for the sign-in link.',error?'err':'');
}
async function signout(){
  if(SB)await SB.auth.signOut();
  SESSION=null; ME=null; closeAccount(); paintIdentity(); toast('Signed out.');
}

// ── The chat log: the conversation IS the left panel; every exchange leaves a bubble ──
function chatSay(role,html){const log=$('chatlog'); if(!log)return null;
  const m=document.createElement('div'); m.className='cmsg '+role; m.innerHTML=html;
  log.appendChild(m); log.scrollTop=log.scrollHeight; return m;}
function chatPush(role,text){ if(SID)api('POST',`/api/plan/${SID}/chatlog`,{role,content:text}); }   // fire-and-forget: the record survives a reload
function chatUser(t){chatPush('user',t);return chatSay('user',esc(t));}
function chatBot(t){chatPush('bot',t);return chatSay('bot',esc(t));}
// transient — errors aren't part of the durable record. Consecutive IDENTICAL errors fold into one
// bubble with a counter (four red copies of the same failure reads as a broken app, not a message).
let LAST_ERR=null;
function chatErr(t){
  if(LAST_ERR&&LAST_ERR.text===t&&LAST_ERR.el&&LAST_ERR.el.isConnected){
    LAST_ERR.n++; LAST_ERR.el.textContent=t+'  (×'+LAST_ERR.n+')';
    const log=$('chatlog'); if(log)log.scrollTop=log.scrollHeight;
    return LAST_ERR.el;
  }
  const el=chatSay('err',esc(t)); LAST_ERR={el,text:t,n:1}; return el;
}
function chatStatus(t){chatPush('status',t);return chatSay('status',esc(t));}
function replayChat(list){ const log=$('chatlog'); if(!log)return; log.innerHTML='';
  (list||[]).forEach(m=>chatSay(m.role==='user'?'user':(m.role==='status'?'status':'bot'),esc(m.content||''))); }
// An in-chat check instead of a native confirm dialog: a bot bubble with Go / Not yet.
function chatConfirm(text,goLabel){return new Promise(res=>{
  chatPush('bot',text);
  const m=chatSay('bot',esc(text)+
    `<div class=cbtns><button type=button class=go>${esc(goLabel||'Go')}</button>`+
    `<button type=button class=nah>Not yet</button></div>`);
  if(!m){res(false);return;}   // no chat mounted → decline safely (never a native dialog)
  const done=yes=>{m.classList.add('asked');
    chatStatus(yes?(goLabel||'Go')+' ✓':'Not yet — carrying on as is.'); res(yes);};
  m.querySelector('.go').onclick=()=>done(true);
  m.querySelector('.nah').onclick=()=>done(false);});}
// Central handler for a gated API error (the prod wall). The ladder, in the order the server sends
// it: needAccount → sign up (the wall at commit) · fairUse → allowance used (upgrade / BYOK fallback)
// · upgrade → a feature that needs a plan · needKey → the fork (BYOK free vs subscribe).
// Returns true if it handled the response.
function gateV2(d){
  if(!d)return false;
  if(d.needAccount){
    authModal("That first stretch was on the house. Create a free account to keep building — this "
      +"plan saves to it (unclaimed plans are cleaned up after ~48h). Then bring your own API key "
      +"(free) or subscribe to run on ours.");
    return true;
  }
  if(d.fairUse){ fairUseModal(d); return true; }
  if(d.upgrade){ pricingModal(d.error||'That one needs a plan, or your own key.'); return true; }
  if(d.needKey){
    if(CFG.authEnabled&&!signedIn()){ authModal(); return true; }
    if((tiersCat().length)&&!HAS_KEY){ pricingModal(d.error||null); return true; }   // the fork: plans + BYOK box
    keyModal(); return true;
  }
  return false;
}

// ── Pricing: the fork (free BYOK vs the monthly plans), the tier cards, and the allowance modal ──
function tiersCat(){ return (ME&&ME.tiers)||CFG.tiers||[]; }
function curTier(){ return (ME&&ME.tier)||null; }
function pdfPriceStr(){ const c=(ME&&ME.pdf_price)||CFG.pdfPrice||1300; return '$'+Math.round(c/100); }
function tierCard(t){
  const cur=curTier()===t.id;
  const allow='~$'+(t.cap_cents/100).toFixed(0)+'/mo of model usage included';
  return `<div class="tiercard${cur?' cur':''}">`+
    `<div style="font-weight:700">${esc(t.label)}${cur?' <span style="color:var(--green)">✓ current</span>':''}</div>`+
    `<div class=tprice>$${t.price}<span>/mo</span></div>`+
    `<ul><li>Every feature, every model crew</li><li>Runs on our key — no API key needed</li>`+
    `<li>Unlimited clean PDFs</li><li>${esc(allow)}</li></ul>`+
    (cur?'<button type=button disabled>Your plan</button>'
        :`<button type=button onclick="subscribe('${t.id}')">Choose ${esc(t.label)}</button>`)+
    `</div>`;
}
function subMeterHtml(){
  const s=ME&&ME.subscription; if(!s||!s.cap_cents)return '';
  const pct=Math.min(100,Math.round(100*s.spent_cents/s.cap_cents));
  const reset=s.reset_at?(' · resets '+new Date(s.reset_at).toLocaleDateString()):'';
  return `<div class=submeter>This month's allowance: <b>${pct}% used</b>${reset}`+
    `<div class=bar><i class="${pct>=100?'hot':''}" style="width:${pct}%"></i></div></div>`;
}
function pricingModal(note){
  if(!tiersCat().length){ keyModal(); return; }
  const cards=tiersCat().map(tierCard).join('');
  const byok=CFG.byokEnabled?
    `<div class=byokbox><b>Bring your own key</b> · <span class=byokfree>free</span>`+
    `<p class=muted style="margin:6px 0 0">Your own OpenRouter or Anthropic key — unlimited, every `+
    `model crew, every feature; you pay your provider (pennies a plan). The clean PDF is a one-time `+
    `${pdfPriceStr()} per plan (re-downloads free; a watermarked copy is always free on your key).</p>`+
    `<button type=button onclick="closeModal();keyModal()">Use my own key</button></div>`:'';
  $('modal-body').innerHTML=(note?`<p class=muted>${esc(note)}</p>`:'')+
    (curTier()?subMeterHtml():'')+`<div class=tiergrid>${cards}</div>`+byok;
  $('modal-acts').innerHTML='<button class=primary onclick="closeModal()">Maybe later</button>';
  openModal(curTier()?'Change your plan':'Keep building',true);
}
async function subscribe(tier){
  if(CFG.authEnabled&&!signedIn()){ authModal('Sign in to subscribe.'); return; }
  if(!CFG.subEnabled){ toast("Billing isn't set up yet.",'err'); return; }
  const {d}=await api('POST','/api/subscribe',{tier});
  if(d&&d.url){ location.href=d.url; return; }          // → Stripe Checkout
  if(d&&d.current){ toast("You're already on that plan."); return; }
  toast((d&&d.error)||'Could not start checkout.','err');
}
function fairUseModal(d){
  const nxt=d&&d.upgradeTier;
  const nxtLabel=(tiersCat().find(t=>t.id===nxt)||{}).label||'the next plan';
  const reset=d&&d.resetAt?(' It resets '+new Date(d.resetAt).toLocaleDateString()+'.'):'';
  $('modal-body').innerHTML=
    `<p class=muted>You've used this month's allowance on our key.${esc(reset)} `+
    (nxt?`Upgrade to ${esc(nxtLabel)} for more monthly credits, or add`:'Add')+
    ` your own API key as a fallback — overflow runs on it, free.</p>`+subMeterHtml();
  $('modal-acts').innerHTML='<button onclick="closeModal()">Wait for the reset</button>'+
    (CFG.byokEnabled?'<button onclick="closeModal();keyModal()">Add my key</button>':'')+
    (nxt?`<button class=primary onclick="closeModal();subscribe('${nxt}')">Upgrade for more credits</button>`:'');
  openModal('Monthly allowance used');
}
async function manageBilling(){
  const {d}=await api('POST','/api/subscription/portal',{});
  if(d&&d.url){ location.href=d.url; return; }
  toast((d&&d.error)||'Could not open billing.','err');
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
function openModal(title,wide){ $('modal-title').textContent=title;
  $('v2modal').classList.toggle('wide',!!wide);
  $('v2modal').hidden=false; $('modalback').classList.add('show'); }
function closeModal(){ $('v2modal').hidden=true; $('v2modal').classList.remove('wide');
  $('modalback').classList.remove('show'); }
async function keyModal(){
  if(CFG.authEnabled&&!signedIn()){ authModal('Your key is stored on your account — sign in first.'); return; }
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
  {k:'the-wonder-kid',n:'Wonder kid',b:"Advanced research with world-class orchestration and synthesis.",rec:true,o:true},
  {k:'trust-fund-baby',n:'Trust fund baby',b:"The absolute best models top to bottom. Not cheap, but hey, neither are you.",o:true},
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
  const locked=u=>!!u.o&&!HAS_KEY&&!curTier();   // Opus crews clamp on the free hosted key
  $('modal-body').innerHTML='<p class=muted>Pick your crew. Sets the models behind research, the credibility gate, and the writing you read.</p>'+
    STACKS_UI.map((u,i)=>`<button type=button class="sktile${i===cur?' on':''}" onclick="pickStack(${i})">`+
      `<span class=sk-top><b>${esc(u.n)}</b>${u.rec?'<span class=mold>Recommended</span>':''}`+
      `${locked(u)?'<span class=mold>Plan or your own key</span>':''}${_pips(i)}</span>`+
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
  $('ws-wrap').classList.toggle('help',LANDING_HELP);
  $('landing-q').classList.toggle('on',LANDING_HELP);
  $('ws-box').placeholder=LANDING_HELP
    ? "'how does this work?', 'what does this cost?'…"
    : "e.g. 'I want to make my dog internet famous', 'I have a truck, some tools, and free time', 'I'm a book worm with a bad back who likes turtles'…";
}
function sendLabel(){ const b=$('ws-send'); if(b)b.textContent=isFull()?'Start →':'Send →';
  const x=$('ws-box'); if(x&&!LANDING_HELP)x.placeholder=isFull()
    ? "e.g. 'I want to make my dog internet famous', 'I have a truck, some tools, and free time', 'I'm a book worm with a bad back who likes turtles'…"
    : 'Tell me what to change, or just talk to it…'; }
function isFull(){ return $('workspace').classList.contains('full'); }
async function startFromLanding(){
  const box=$('ws-box'), idea=box.value.trim();
  $('landing-err').textContent='';
  if(idea.length<12){$('landing-err').textContent='Tell me a bit more about the idea.';return;}
  // ONE surface: the expanded drawer collapses into the sidebar, the box drops to the bottom, and
  // the canvas fades in with a seed node already working — no swap, no fly-away.
  box.value='';
  $('workspace').classList.remove('full'); sendLabel();
  // the operator's words appear in the chat IMMEDIATELY — an empty drawer while the first spread
  // thinks reads as a swallowed input (Sam's QA, 2026-07-06). Display-only for now (no SID yet to
  // persist against); persisted below once the session exists, removed if the submit is rejected.
  const firstMsg=chatSay('user',esc(idea));
  setTimeout(()=>{ if(!SID)seedGraph(idea); },380);   // seed once the panel has size — unless the spread already landed (mock is FAST)
  const {ok,d}=await api('POST','/api/brainstorm',{idea,stack:STACK_CUR});
  if(!ok||(d&&d.gibberish)){   // rejected → expand back out and say why (and unsay the optimistic bubble)
    if(firstMsg)firstMsg.remove();
    $('workspace').classList.add('full'); sendLabel(); box.value=idea; resetGraph();
    if(d&&d.gibberish){ $('landing-joke').innerHTML=`<div class=jokecard><h3>${esc(d.title||"That's not an idea yet.")}</h3><div>${mdToHtml(d.body||'')}</div></div>`; return; }
    if(gateV2(d))return;
    $('landing-err').textContent=(d&&d.error)||'Something went wrong.'; return;
  }
  adoptPlan(d); render(d);
  chatPush('user',idea);   // already rendered above — now it can join the durable record
  chatBot('Spread that into a few directions — pick what clicks on the graph, or just keep typing.');
  // the collapse animation is still moving the panels — re-aim the camera once the layout settles,
  // or the tree stays framed against a mid-transition (half-width) canvas
  setTimeout(()=>{ if(S){BROWSING=false; focusActive(true);} },650);
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
function v2newPlan(){ location.href='/'; }
function adoptPlan(d){ SID=d.id; SEL=new Set(); PENDING_FORK=null; resetGraph();
  mCollapse();   // the first spread draws the graph — the mobile drawer bows out so it takes the stage
  history.replaceState(null,'','/plan/'+SID); }   // the plan gets a real URL — reload restores it
async function restorePlan(id){   // boot straight into an existing plan: graph + docs + conversation
  const {ok,d}=await api('GET',`/api/plan/${id}?touch=1`);
  if(!ok||!d||!d.id){ history.replaceState(null,'','/'); return; }   // unknown → the landing
  $('workspace').classList.remove('full'); sendLabel();
  SID=d.id; SEL=new Set(); PENDING_FORK=null; resetGraph();
  mCollapse();   // a deep link opens onto the PLAN — the graph first, the chat one tap away
  replayChat(d.chat);
  render(d);
  if(SESSION&&!d.owned)claimPending();   // signed in, restoring an anonymous taste → claim it
  loadSel();   // checkbox picks made before the reload come back (per plan + active fork)
  if(SEL.size)renderView();
  if(d.status==='researching')poll();   // a run was mid-flight — pick the poll back up
  focusActive(true);
  // the drawer-collapse transition is still moving the panels — re-aim once the layout settles, or
  // the camera measures stale geometry and pins the focused node UNDER the left drawer, which then
  // intercepts its CTA clicks (the §v2 #15 deep-link watchpoint; reproduced by the monkey, seed 5)
  setTimeout(()=>{ if(S&&!BROWSING)focusActive(true); },700);
}

// ── Prompt box + modes ───────────────────────────────────────────────────────
function setMode(m){
  MODE=m;
  document.querySelectorAll('#modechips .mchip').forEach(b=>b.classList.toggle('on',b.dataset.mode===m));
  $('ws-wrap').className='promptwrap'+(m!=='build'?' '+m:'');
  // research, board, help, AND summary are DISPLAY STATES of the chat drawer (one chat, one display
  // at a time). Entering one quietly drops the others; there are no tool drawers anymore.
  if(m==='research'){ _disarmTraps(); _bDrop(); _hDrop(); _sDrop(); enterResearch(); return; }
  if(m==='board'){ _disarmTraps(); _rDrop(); _hDrop(); _sDrop(); enterBoard(); return; }
  if(m==='help'){ _disarmTraps(); _rDrop(); _bDrop(); _sDrop(); enterHelp(); return; }
  if(m==='summary'){ _disarmTraps(); _rDrop(); _bDrop(); _hDrop(); enterSummary(); return; }
  exitResearch(); exitBoard(); exitHelp(true); exitSummary(true);
}
// an armed pivot ghost / revet box makes the NEXT message a build input — leaving build mode with
// one armed would swallow a research/board/help question into it (the monkey, seed 3, step 9).
// Switching displays disarms both, out loud.
function _disarmTraps(){
  if(PIVOT_FROM){ clearGhost(); chatStatus('⑂ Pivot disarmed.'); }
  if(REVET_ARMED)disarmRevet();
}
// silent display drops for mode switches — no 'Back to build' status, no MODE stomp
function _rDrop(){ if(RMODE){ RMODE=null; RQUERY=''; applyRmode(); } }
function _bDrop(){ if(BMODE){ BMODE=null; BQUERY=''; applyBmode(); } }
function _hDrop(){ if(HMODE){ HMODE=false; applyHmode(); } }
function _sDrop(){ if(SMODE){ SMODE=false; applySmode(); } }
async function sendPrompt(){
  if(isFull())return startFromLanding();   // full mode: the first prompt IS the landing submit
  const box=$('ws-box'), prompt=box.value.trim(); if(!prompt) return;
  $('ws-err').textContent='';
  chatUser(prompt);
  box.value='';   // submitted — the bubble is the record; clearing NOW says "I heard you"
                  // (routing failure below restores it so nothing typed is ever lost)
  // an armed "Pivot from here" ghost: the input IS the pivot feedback — spread from that node directly
  if(PIVOT_FROM){ const from=PIVOT_FROM; box.value=''; clearGhost();
    chatStatus('Pivoting from '+pivotSrcLabel(from)); return pivotSpread(from,prompt); }
  // the kill gate armed "give it substance": the input IS the substance — straight to /revet
  if(REVET_ARMED){ box.value=''; return revetSend(prompt); }
  // research mode: the first question slides the chat up into the split; every message re-focuses
  // the stack on what's relevant (fuzzy-finder feel), and remembers whether anything matched
  let rRelated=null;
  if(RMODE){
    if(RMODE==='full'){ RMODE='split'; applyRmode(); }
    RQUERY=prompt; rRelated=renderResearch();
  }
  // board mode: same contract — the first question slides the chat up into the split, and every
  // message re-focuses the convene stack on what's relevant
  let bRelated=null;
  if(BMODE){
    if(BMODE==='full'){ BMODE='split'; applyBmode(); }
    BQUERY=prompt; bRelated=renderBoard();
  }
  // browsing an earlier node? it rides along as context (a steer pivots from it, a question is about it)
  const {t}=nodesOf(S);
  const fromNode=(FOCUS&&FOCUS!==t.active)?FOCUS:null;
  const btn=$('ws-send'); btn.disabled=true;
  btn.innerHTML='<span class="spin sendspin" aria-hidden=true></span> Routing…';   // instant feedback
  // summary is display-only for routing purposes — the chat under it behaves like build mode
  const body={prompt,mode:(MODE==='summary'?'build':MODE)}; if(fromNode)body.node=fromNode;
  const {ok,d}=await api('POST',`/api/plan/${SID}/route`,body);
  btn.disabled=false; sendLabel();
  if(!ok){ if(!box.value)box.value=prompt;   // give the words back — the send failed
    if(gateV2(d))return; chatErr((d&&d.error)||'Could not route that.'); return; }
  if(d.fork){ PENDING_FORK=d.fork;
    chatBot(d.fork.clash||'That pulls against the committed idea — pick a path on the graph.');
    focusActive(true); return; }
  const dec=d.decision||{};
  // the topic turned back to BUILDING → the research/board display bows out on its own (drawer
  // collapses / chat slides back to full height, the Build pill re-lights), then the action runs
  if(['steer','next','commit','diverge','pick','restart_keep','restart_hard'].includes(dec.intent)){
    if(RMODE)exitResearch();
    if(BMODE)exitBoard();
    if(HMODE)exitHelp();
    if(SMODE)exitSummary();
  }
  // a question that isn't about the live display → answer it, then offer the way out. Two signals:
  // the router aimed it away, or nothing in the on-screen stack matched the question.
  dec._offResearch=!!RMODE&&dec.intent==='ask'&&(
    (dec.target!=='research'&&dec.target!=='board')||
    (rRelated===false&&researchItems().length>0));
  dec._offBoard=!!BMODE&&dec.intent==='ask'&&(
    (dec.target!=='board'&&dec.target!=='research')||
    (bRelated===false&&boardItems().length>0));
  // when an in-chat check follows immediately, the check IS the reply — skip the say bubble
  const checks=(dec.intent==='commit'&&dec.confirm)||dec.intent==='restart_hard';
  if(!checks)chatBot(dec.say||'On it.');
  await dispatch(dec,fromNode,prompt);
}
async function dispatch(dec,fromNode,prompt){
  switch(dec.intent){
    case 'commit':
      if(dec.confirm&&!(await chatConfirm(BIG_STEP_ASK,"Let's go")))return;
      return commit(null,fromNode);
    case 'next': {   // the funnel's ONE next step — what it means depends on where you are
      if(WIP_LABEL||S.status==='researching'){ chatBot('Already on it — the next part is being written now.'); return; }
      if(S.done||S.stage==='done'){ chatBot("The plan's complete — pivot from any node to take it somewhere new."); return; }
      if(S.stage==='building')return keepGoing();
      if(S.stage==='refined'){
        if(!(await chatConfirm(BIG_STEP_ASK,"Let's go")))return;
        return commit();   // 'next' rolls the ACTIVE refined node — a browsed node never hijacks it
      }
      chatBot('Pick a direction first — check the boxes, or just name them ("the first two") and I merge them.');
      return;
    }
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
    case 'ask': return askInChat(prompt||dec.steer||'',dec.target,fromNode,dec._offResearch||dec._offBoard);   // a question gets an ANSWER, in the chat
    case 'steer': default:
      if(fromNode){ chatStatus('Pivoting from '+pivotSrcLabel(fromNode));
        return pivotSpread(fromNode,dec.steer||''); }   // feedback on an earlier node = pivot from it
      return steer(dec.steer||'');
  }
}
// The one costly step, named honestly — used by both an explicit commit and a refined-stage "next"
const BIG_STEP_ASK='Next up is the big step: deep research + building out the full plan. Go?';
// A routed question: product questions go to /api/help, everything else to the plan-grounded
// advisor. The reply lands as a chat bubble — a question must never die in a tool drawer.
async function askInChat(q,target,fromNode,offerExit){
  if(!q)return;
  if(target==='research')return lookupInChat(q,offerExit);   // research mode = a real web lookup, graded by the gate
  if(target==='board')return boardInChat(q);       // board mode = convene the actual multi-persona board
  const th=chatSay('status','thinking…');
  const url=(target==='help')?'/api/help':`/api/plan/${SID}/chat`;
  // the advisor answers WHERE the user is: the node they're reading + any build in flight
  const body=(target==='help')?{message:q}:{message:q,log_user:false,
    node:fromNode||undefined,working:WIP_LABEL||undefined};
  const {ok,d}=await api('POST',url,body);
  if(th)th.remove();
  if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'Could not answer that.'); return; }
  const reply=(d&&d.reply)||'';
  chatSay('bot',mdToHtml(reply));
  if(target==='help'){ chatPush('bot',reply);   // the advisor endpoint logs its own turn; /api/help doesn't
    faqPush(q,reply); }                         // …and the Q+A joins the personal FAQ banner
  if(offerExit)offerExitMode();   // answered in place — now offer the way back to building
}
async function boardInChat(q){
  const th=chatSay('status','convening your board…');
  const body={question:q};
  if(BOARD_PICK)body.directors=[...BOARD_PICK];   // a re-picked bench rides the convene + persists
  const {ok,d}=await api('POST',`/api/plan/${SID}/board`,body);
  if(th)th.remove();
  if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'Could not convene the board.'); return; }
  const rows=(d.directors||[]).map(x=>`<div class=bdrow><b>${esc(x.name||x.key)}</b> ${esc(x.take||'')}</div>`).join('');
  const sk=d.skeptic?`<div class="bdrow skept">🧐 <b>${esc(d.skeptic.name||'The Skeptic')}</b> `+
    `${esc(d.skeptic.take||d.skeptic.rationale||'')}${d.skeptic.verdict?` <span class=lktier>${esc(d.skeptic.verdict)}</span>`:''}</div>`:'';
  const tail=(d.consensus?`<div class=bdrow><b>Consensus</b> ${esc(d.consensus)}</div>`:'')+
    (d.conflicts?`<div class=bdrow><b>Where they clash</b> ${esc(d.conflicts)}</div>`:'')+
    (d.verdict?`<div class=bdrow><b>Net verdict</b> ${esc(d.verdict)}</div>`:'');
  chatSay('bot',`<p>Your board weighed in:</p>${rows}${sk}${tail}`);
  chatPush('bot','Board:\n'+(d.directors||[]).map(x=>`${x.name||x.key}: ${x.take||''}`).join('\n')+
    (d.skeptic?`\n🧐 Skeptic: ${d.skeptic.take||d.skeptic.rationale||''} [${d.skeptic.verdict||''}]`:'')+
    (d.consensus?`\nConsensus: ${d.consensus}`:'')+(d.verdict?`\nNet verdict: ${d.verdict}`:''));
  // the convene is persisted on the active node server-side — mirror it locally so the board stack
  // and the node's board notes update without a refetch
  if(S){ S.board=(S.board||[]).concat([{section:'convene',title:'Board convened: “'+q.slice(0,90)+'”',
    directors:d.directors,skeptic:d.skeptic,consensus:d.consensus,conflicts:d.conflicts,
    verdict:d.verdict}]);
    if(BMODE)renderBoard();
    if(FOCUS)renderView(); }
}
async function lookupInChat(q,offerExit){
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
  // the pulled claims JOIN the research stack (display-side, deduped) and the list re-focuses
  if(RMODE){
    const seen=new Set(researchItems().map(x=>x.text+'|'+(x.url||'')));
    LOOKUPS.push(...cs.filter(c=>!seen.has(c.text+'|'+(c.url||''))));
    renderResearch();
  }
  if(offerExit)offerExitMode();   // it answered, but nothing local matched — offer the way back
}
async function steer(note){
  if(!note) return;
  const {m,t}=nodesOf(S);
  if(S&&(S.stage==='building'||S.stage==='done')){
    // inline comments on the current doc ride the rework (backlog #8)
    if(await run(`/api/plan/${SID}/redraft`,{feedback:note+cmtSteer(t.active)},'Reworking this part'))
      delete DOCCMTS[t.active];
  } else if(((m[t.active]||{}).kind)==='refined'){
    // the prelaunch gate: a steer (or an answer to the gate's questions) SHARPENS the refined idea
    // in place — it must never blow the funnel back up into a fresh spread
    await refineIdea(note);
  } else {
    await reBrainstorm(note+' — '+(S&&S.idea||''));   // upstream: fold the note into a fresh spread
  }
}
async function refineIdea(note){
  const {t}=nodesOf(S);
  const {ok,d}=await api('POST',`/api/plan/${SID}/refine`,{note});
  if(ok&&d&&d.gibberish){ render(S); roastChat(d); return; }   // a mash answer → the roast, no run
  if(!ok){ render(S); if(gateV2(d))return; chatErr((d&&d.error)||'Could not fold that in.'); return; }
  beginWip('Folding that into the refined idea',{parent:t.active,poll:true}); poll();
}

// ── Inline doc comments (backlog #8): select text in any rendered doc → leave a note. Comments key
// on the node they're about, render under that doc, and FOLD INTO the next build verb that touches
// the node (keep going / steer / pivot) — v1's #7 contract, restated in v2's verbs. Client-held;
// they're consumed by the build that uses them.
let DOCCMTS={}, CMT_NODE=null, CMT_QUOTE='';
function cmtsFor(id){return (id&&DOCCMTS[id])||[];}
function cmtSteer(id){ const cs=cmtsFor(id); if(!cs.length)return '';
  return '\n\nInline comments on this document (address each, anchored to the quoted text):\n'+
    cs.map(c=>`- On “${c.quote}”: ${c.note}`).join('\n'); }
function cmtsHtml(id){ const cs=cmtsFor(id); if(!cs.length)return '';
  return `<div class=cmts><p class=eyebrow>Your comments (they ride the next build of this part)</p>`+
    cs.map((c,i)=>`<div class=cmtrow>💬 <span class=cmtq>“${esc(c.quote.length>90?c.quote.slice(0,90)+'…':c.quote)}”</span> `+
      `<span class=cmtn>${esc(c.note)}</span><button type=button class=cmtx aria-label="Remove comment" onclick="rmCmt('${id}',${i})">×</button></div>`).join('')+`</div>`; }
function rmCmt(id,i){ cmtsFor(id).splice(i,1); renderView(); }
function cmtPop(){ let p=$('cmtpop'); if(p)return p;
  p=document.createElement('div'); p.id='cmtpop';
  p.innerHTML=`<div class=cmtq id=cmtpop-q></div>`+
    `<textarea id=cmtpop-note rows=2 placeholder="Your note on that…"></textarea>`+
    `<div class=cmtacts><button type=button onclick="hideCmtPop()">Cancel</button>`+
    `<button type=button class=primary onclick="saveCmt()">Comment</button></div>`;
  document.body.appendChild(p); return p; }
function hideCmtPop(){ const p=$('cmtpop'); if(p)p.classList.remove('show');
  const s=window.getSelection&&window.getSelection(); if(s&&s.removeAllRanges)s.removeAllRanges(); }
function saveCmt(){ const note=(($('cmtpop-note')||{}).value||'').trim(); hideCmtPop();
  if(!note||!CMT_NODE)return;
  (DOCCMTS[CMT_NODE]=DOCCMTS[CMT_NODE]||[]).push({quote:CMT_QUOTE,note});
  renderView(); }
document.addEventListener('mouseup',e=>{
  if(e.target.closest('#cmtpop'))return;
  if(!e.target.closest('.draft'))return;                       // comments live on rendered docs only
  const sel=window.getSelection(); const quote=(sel&&sel.toString()||'').trim();
  if(!quote)return;
  const id=(VIEWMODE==='docs')?DOCTAB:FOCUS;                   // whichever node's doc is on screen
  CMT_NODE=(id&&id!=='_wip')?id:(nodesOf(S).t||{}).active; if(!CMT_NODE)return;
  CMT_QUOTE=quote.slice(0,180);
  const p=cmtPop();
  $('cmtpop-q').textContent='“'+(CMT_QUOTE.length>90?CMT_QUOTE.slice(0,90)+'…':CMT_QUOTE)+'”';
  $('cmtpop-note').value='';
  p.classList.add('show');                                     // show first so it can be measured
  const pw=p.offsetWidth||280, ph=p.offsetHeight||130;
  p.style.left=Math.max(8,Math.min(e.clientX-40,window.innerWidth-pw-8))+'px';
  p.style.top=Math.max(8,Math.min(e.clientY+14,window.innerHeight-ph-8))+'px';
  setTimeout(()=>{const n=$('cmtpop-note');if(n)n.focus();},30);
});
document.addEventListener('keydown',e=>{ if(e.key==='Escape')hideCmtPop(); });
document.addEventListener('mousedown',e=>{ const p=$('cmtpop');
  if(p&&p.classList.contains('show')&&!e.target.closest('#cmtpop'))p.classList.remove('show'); });

// ── Funnel actions ───────────────────────────────────────────────────────────
// A keyboard-mash pivot gets the same free roast as a mash at the landing box — in the chat,
// where the conversation lives (the graph is untouched; a roast is not a spread).
function roastChat(d){
  chatSay('bot',`<b>${esc(d.title||"That's not an idea yet.")}</b>${mdToHtml(d.body||'')}`);
  chatPush('bot',(d.title||'')+'\n'+(d.body||''));
}
async function reBrainstorm(idea){
  if(SID){   // same tree: the new spread branches off the pivot point, the old branch stays visible
    beginWip('Spreading new directions',{parent:pivotParent()});
    const {ok,d}=await api('POST',`/api/plan/${SID}/rebrainstorm`,{idea});
    endWip();
    if(ok&&d&&d.gibberish){ render(S); roastChat(d); return; }
    if(!ok){ render(S); if(gateV2(d))return; chatErr((d&&d.error)||'Could not re-spread.'); return; }
    SEL=new Set(); PENDING_FORK=null; render(d); return;
  }
  const {ok,d}=await api('POST','/api/brainstorm',{idea,stack:STACK_CUR});
  if(ok&&d&&d.gibberish){ roastChat(d); return; }
  if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'Could not re-spread.'); return; }
  adoptPlan(d); render(d);
}
async function doMerge(){
  if(!SEL.size){toast('Pick at least one direction.','err');return;}
  const picks=[...SEL], selKey=_selKey();
  const {ok,d}=await api('POST',`/api/plan/${SID}/merge`,{options:picks});
  if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'Could not merge.'); return; }
  try{localStorage.removeItem(selKey);}catch(e){}   // consumed — the refined node records the picks
  beginWip('Merging your picks + a light research pass',{join:picks,poll:true}); poll();
}
let COMMIT_BUSY=false;   // the big-step button is click-spammable while a request is in flight
async function commit(thesis,fromNode,picks){
  if(COMMIT_BUSY)return;
  COMMIT_BUSY=true;
  try{
    const body={}; if(thesis)body.thesis=thesis; if(fromNode)body.node=fromNode;   // build out of THAT node
    if(picks&&picks.length)body.options=picks;   // a direct brainstorm-commit RECORDS its choice
    const {ok,d}=await api('POST',`/api/plan/${SID}/commit`,body);
    if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'Could not start the build.'); return; }
    beginWip('Deep research: pulling + grading sources',{parent:fromNode||(nodesOf(S).t||{}).active,poll:true}); poll();
  }finally{ COMMIT_BUSY=false; }
}
// ── Pivot-from-a-node: an armed ghost child ("enter feedback to pivot…") + the direct spread ──
let PIVOT_FROM=null;
function pivotFromHere(id){ PIVOT_FROM=id; REVET_ARMED=false; FOCUS=null; BROWSING=true;
  // arming a pivot is a BUILD act — EVERY display bows out and the chat says what's next
  // (Sam's QA: pivot from research stayed in research; the monkey then caught the same
  // hole for help mode on its first walk — seed 1, step 26)
  exitResearch(true); exitBoard(true); exitHelp(true); exitSummary(true);
  renderGraph();
  chatStatus('⑂ Pivot armed from '+pivotSrcLabel(id)+' — enter your pivot: what should change?');
  const b=$('ws-box'); if(b){b.placeholder='Your pivot: what should change from here?';b.focus();} }
function pivotActive(){ const {t}=nodesOf(S); pivotFromHere(t.active); }   // the step view's Pivot button: arm the ghost off THIS step
function pivotSrcLabel(id){   // name the pivot's source node in the chat — "which node am I forking?" must never be a guess
  const {m}=nodesOf(S); const n=m[id]; if(!n)return 'that node';
  const t=(n.direction&&n.direction.title)||nodeLabel(n);
  return '“'+String(t).slice(0,60)+'”'; }
function clearGhost(){ PIVOT_FROM=null; const b=$('ws-box'); if(b)b.placeholder='Tell me what to change, or just talk to it…'; renderGraph(); }
async function pivotSpread(fromNode,feedback){
  const src=pivotParent(fromNode);
  const full=(feedback||'')+cmtSteer(src);   // comments on the pivot node steer the spread (backlog #8)
  beginWip('Spreading new directions',{parent:src});
  const {ok,d}=await api('POST',`/api/plan/${SID}/rebrainstorm`,{feedback:full,node:fromNode});
  endWip();
  if(ok&&d&&d.gibberish){ render(S); roastChat(d); return; }   // a mash pivot → the roast, no spread
  if(ok)delete DOCCMTS[src];
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
  // the PICKS ride along, not just their text — without them the graph drew the checked option as
  // passed-over and a pivot-commit read as "nevermind" even though the build honored it (Sam's QA)
  commit(chosen.join(' + '),null,[...SEL]);
}
async function keepGoing(){
  const {t}=nodesOf(S);   // inline comments on this doc ride the roll-forward (backlog #8)
  if(await run(`/api/plan/${SID}/next`,{feedback:cmtSteer(t.active).trim()},'Writing the next part'))
    delete DOCCMTS[t.active];
}
async function keepGoingForced(){ await run(`/api/plan/${SID}/next`,{feedback:'',force:true},'Building it anyway'); }
async function run(url,body,label){
  beginWip(label,{parent:(nodesOf(S).t||{}).active});
  const {ok,d}=await api('POST',url,body);
  endWip();
  if(!ok){ render(S); if(d&&d.needSubstance){killGateChat(d);return false;}
    if(gateV2(d))return false; chatErr((d&&d.error)||'Something went wrong.'); return false; }
  render(d); return true;
}
// ── The kill gate, v2-native (backlog #13): coach in the chat, off-ramps are the product's verbs ──
// A kill-graded idea won't roll into a full plan. The chat carries the diagnostic; the ways out are
// (a) answer with real substance → /revet, (b) pivot-fork off this node, (c) force → the joke plan.
let REVET_ARMED=false;
function killGateChat(d){
  const m=chatSay('bot',
    `<p>${esc(d.reaction||"Not yet — the gate found nothing real to build on.")}</p>`+
    (d.risk?`<p>Biggest risk: ${esc(d.risk)}</p>`:'')+
    `<p><b>${esc(d.question||'')}</b></p>`+
    `<div class=cbtns><button type=button class=go>Give it substance</button>`+
    `<button type=button class=nah>⑂ Pivot</button>`+
    `<button type=button class=wod>Build it anyway 🤡</button></div>`);
  chatPush('bot',(d.reaction?d.reaction+' ':'')+(d.question||''));
  if(!m)return;
  m.querySelector('.go').onclick=()=>{m.classList.add('asked');armRevet();};
  m.querySelector('.nah').onclick=()=>{m.classList.add('asked');pivotActive();};
  m.querySelector('.wod').onclick=()=>{m.classList.add('asked');
    chatStatus('Waste-of-time mode: you asked for it.'); keepGoingForced();};
}
function armRevet(){
  REVET_ARMED=true; PIVOT_FROM=null;
  const b=$('ws-box'); if(b){ b.placeholder='A real skill, asset, or audience — and who would pay…'; b.focus(); }
  chatStatus('Tell me one real thing you bring — I re-grade with it.');
}
function disarmRevet(){ REVET_ARMED=false;
  const b=$('ws-box'); if(b)b.placeholder='Tell me what to change, or just talk to it…'; }
async function revetSend(more){
  disarmRevet();
  const th=chatSay('status','re-grading with that…');
  const {ok,d}=await api('POST',`/api/plan/${SID}/revet`,{more});
  if(th)th.remove();
  if(!ok){ if(gateV2(d))return; chatErr((d&&d.error)||'Could not re-grade that.'); REVET_ARMED=true; return; }
  const v=(d&&d.vetting)||{};
  if(v.verdict==='kill'){
    killGateChat({reaction:v.reaction,risk:v.biggest_risk,
      question:(d.shaped&&d.shaped.clarifying_question)||'Still too thin — what do you actually have?'});
  } else chatBot('That cleared the gate — part 1 redrafted from the stronger idea.');
  render(d);
}
// THE POLL, hardened (systemic pass, 2026-07-06). Two failure classes lived here:
// (1) a dropped GET killed the chain forever — the WIP box spun with no completion (the "freeze");
// (2) two overlapping ops could both be polling, and a STALE chain's render stomped the newer op's
//     WIP state (the "WIP box confused with the previous box" family).
// Epoch token = only the newest chain may render; transient failures retry with backoff, then say so.
let POLL_EPOCH=0;
function poll(){
  const tok=++POLL_EPOCH, sid=SID; let miss=0;
  const tick=async()=>{
    if(tok!==POLL_EPOCH||sid!==SID)return;      // a newer op (or another plan) owns the poll now
    const {ok,d:s}=await api('GET',`/api/plan/${sid}`);
    if(tok!==POLL_EPOCH||sid!==SID)return;
    if(!ok||!s||!s.id){
      if(++miss<=5){ setTimeout(tick,1200*miss); return; }   // a blip is not a verdict — retry
      endWip(); if(S)render(S);
      chatErr('Lost contact with the build — it may still be running. Reload to catch up.');
      return;
    }
    miss=0; render(s);
    if(s.status==='researching')setTimeout(tick,1100);
  };
  tick();
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
// the server persists each node's build receipts (backlog #10) — hydrate the stash from any payload
// that carries them; the longer record wins (the live stash is capped tighter than the server's)
function hydrateLog(id,log){ if(id&&log&&log.length&&(!NODELOG[id]||NODELOG[id].length<log.length))NODELOG[id]=log.slice(); }
let HIST_OPEN=new Set();   // which nodes' "how this was built" is expanded (survives re-renders)
function histKeep(id,el){ if(el.open)HIST_OPEN.add(id); else HIST_OPEN.delete(id); }
function resetGraph(){ GSEEN=new Set(); FOCUS=null; BROWSING=false; LAST_ACTIVE=null; WAS_RESEARCHING=false;
  PIVOT_FROM=null; REVET_ARMED=false;
  GEXPANDED=new Set(); GQUERY=''; NAVCUR=null; LAYOUT=null;
  const gs=$('gsearch'); if(gs)gs.value=''; const gn2=$('gsearch-n'); if(gn2)gn2.hidden=true;
  const mm=$('minimap'); if(mm){mm.hidden=true;mm.innerHTML='';}
  LOOKUPS=[]; RQUERY=''; if(RMODE)exitResearch(true);   // research display is per-plan state
  BQUERY=''; B_OFFERED=false; BOARD_PICK=null; STRESS_ON=false; FORGED=null;   // board display too
  if(BMODE)exitBoard(true);
  if(HMODE)exitHelp(true);   // display only — the FAQ itself is browser-level, it survives
  if(SMODE)exitSummary(true);
  SA_SAID=new Set();   // set-aside announcements are per-plan
  WIP_LABEL=null; WIP_PENDING=null; WIPLOG=[]; LANES=[]; LANES_DONE=new Set(); PROG_N=0;
  Object.keys(NODECACHE).forEach(k=>delete NODECACHE[k]); Object.keys(NODELOG).forEach(k=>delete NODELOG[k]);
  HIST_OPEN=new Set();
  const gn=$('gnodes'); if(gn)gn.innerHTML=''; const ge=$('gedges'); if(ge)ge.innerHTML=''; }
let WIP_SYNC=false;   // sync ops (await-style: /next, /redraft, pivot spreads) OWN their label —
                      // only endWip may clear it. Poll-driven ops defer to the server's status.
function beginWip(label,opts){ WIP_LABEL=label; WIP_T0=Date.now(); WIPLOG=[]; LANES=[]; LANES_DONE=new Set();
  WIP_SYNC=!(opts&&opts.poll);
  mCollapse();   // a graph-drawing submission collapses the mobile drawer — the tree takes the stage
  WIP_PENDING={label,parent:(opts&&opts.parent)||null,join:(opts&&opts.join)||null};
  if(VIEWMODE==='docs')DOCTAB='_wip';   // docs view rolls forward like the tree: the new step gets its own tab
  LEAF_OPEN=new Set(); FOCUS=null; BROWSING=false; PREFOCUS_VIEW=null; renderView(); }   // content collapses back, the pending node takes the stage
function endWip(){ WIP_LABEL=null; WIP_T0=null; WIP_PENDING=null; WIP_SYNC=false; }
function pivotParent(id){   // THE PIVOT CONTRACT: a pivot branches off the pivot node ITSELF, always
  const {t}=nodesOf(S); return id||t.active; }
function nodesOf(s){const t=(s&&s.tree)||{};const m={};(t.nodes||[]).forEach(n=>m[n.id]=n);return {m,t};}
function pathSetOf(m,active){const set={};let cur=active;
  while(cur!=null&&m[cur]){set[cur]=1;
    const n=m[cur];
    // ANY join node credits its selected options — a refined merge OR a direct brainstorm-commit's
    // section both carry `selected`; the picked options are part of the taken path, not passed-over
    if(Array.isArray(n.selected))n.selected.forEach(s=>{if(m[s])set[s]=1;});
    cur=n.parent;}
  return set;}
// ── Collapse passed-over subtrees to stubs (the wide-tree lever; Sublime Merge's commit-folding
// pattern). An OFF-PATH branch root whose parent is on the committed path, carrying ≥2 nodes,
// folds into one "⊞ N steps" stub — click to unfold, ⊟ on its root to tuck it back. A subtree
// holding the focused / pivot-armed / keyboard-cursor node never folds (you're IN it).
let GEXPANDED=new Set();
function collapseMap(m,onPath){
  const tp={},kids={};
  Object.keys(m).forEach(id=>kids[id]=[]);
  Object.values(m).forEach(n=>{ tp[n.id]=(n.parent!=null&&m[n.parent])?n.parent
    :((Array.isArray(n.selected)&&n.selected.find(s=>m[s]))||null);
    if(tp[n.id]!=null)kids[tp[n.id]].push(n.id); });
  const size=id=>1+(kids[id]||[]).reduce((a,c)=>a+size(c),0);
  const hot=new Set([FOCUS,PIVOT_FROM,NAVCUR].filter(Boolean));
  const warm=id=>hot.has(id)||(kids[id]||[]).some(warm);
  const out={};
  const emit=id=>{
    if(!onPath[id]&&tp[id]!=null&&onPath[tp[id]]&&!GEXPANDED.has(id)&&size(id)>=2&&!warm(id)){
      out['_stub:'+id]={id:'_stub:'+id,kind:'stub',parent:tp[id],root:id,count:size(id),
        title:size(id)+' passed-over steps'};
      return; }
    out[id]=m[id]; (kids[id]||[]).forEach(emit); };
  Object.keys(m).forEach(id=>{ if(tp[id]==null)emit(id); });
  return out;
}
function stubExpand(root){ GEXPANDED.add(root); renderView(); }
function stubCollapse(root){ GEXPANDED.delete(root); renderView(); }
// Is this node the root of an expanded passed-over subtree? (It gets the ⊟ tuck-away control.)
function stubRootOf(n,onPath,m){
  return GEXPANDED.has(n.id)&&!onPath[n.id]&&n.parent!=null&&m[n.parent]&&onPath[n.parent];
}
function nodeLabel(n){
  if(n.kind==='stub')return n.title||'passed over';
  if(n.kind==='idea')return n.title||'Your idea';
  if(n.kind==='brainstorm')return 'A few directions';
  if(n.kind==='refined')return 'Refined idea';
  if(n.kind==='fork')return 'Fork';
  if(n.kind==='pivot')return n.title||'enter feedback to pivot…';
  return n.title||('Part '+((n.step||0)+1));
}
const KICON={idea:'◉',brainstorm:'✳',option:'◇',refined:'◆',fork:'⑂',pivot:'⑂',wip:'⚙',section:'▤',stub:'⊞'};
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
    let ks=kids[id];
    if(!ks.length){ L[id]=nodeW(id)/2; R[id]=nodeW(id)/2; return; }
    ks.forEach(measure);
    // NORMALIZED SPINE PACK (2026-07-05): the spine child sits mid-pack, siblings split around it
    // at the point that best balances the reserved extents (sibling order preserved). Without this,
    // a spine child packed first/last shoved EVERY sibling to one side — the parent reserved that
    // whole one-sided span, the tree hugged an edge, and edges drew as long S-curves over dead
    // space. Balanced, a mostly-linear tree stays compact + centered; real competing subtrees
    // still reserve their full extents (rule 1 holds).
    let si=ks.findIndex(c=>spine.has(c));
    if(spine.has(id)&&si>=0&&ks.length>1){
      const sp=ks[si], rest=ks.filter(c=>c!==sp);
      const w=c=>L[c]+R[c]+GUT;
      const total=rest.reduce((a,c)=>a+w(c),0);
      let acc=0,cut=0,best=Infinity;
      for(let i=0;i<=rest.length;i++){
        const bal=Math.abs(acc-(total-acc));
        if(bal<best){best=bal;cut=i;}
        if(i<rest.length)acc+=w(rest[i]);
      }
      ks=[...rest.slice(0,cut),sp,...rest.slice(cut)];
      si=cut;
    }
    let x=0; const o=[];
    ks.forEach((c,i)=>{ x+=(i?R[ks[i-1]]+GUT+L[c]:L[c]); o.push(x); });
    const packL=o[0]-L[ks[0]], packR=o[ks.length-1]+R[ks[ks.length-1]];
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
  return {pos,kids,joins,tp};
}
function renderGraph(){
  const s=S; const gn=$('gnodes'), ge=$('gedges'); if(!s||!gn)return;
  const {m:m0,t}=nodesOf(s); const active=t.active;
  const onPath=pathSetOf(m0,active);   // computed on the FULL tree — stubs never change the path
  const m=collapseMap(m0,onPath);      // passed-over subtrees fold to ⊞ stubs (expand on click)
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
  const {pos,kids,joins,tp}=layoutGraph(m,wip,active,FOCUS?{fw:FW,fh:FH}:null);
  LAYOUT={pos,kids,tp,m};   // the minimap, fit-view, and keyboard nav all read the LAST layout
  const qhits=GQUERY?new Set(Object.keys(m).filter(id=>nodeMatches(m[id],GQUERY))):null;
  const gsn=$('gsearch-n'); if(gsn){gsn.hidden=!GQUERY; if(GQUERY)gsn.textContent=(qhits?qhits.size:0)+' hit'+((qhits&&qhits.size===1)?'':'s');}
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
    const hit=qhits&&qhits.has(n.id), dimq=qhits&&qhits.size>0&&!hit;
    el.className='gnode'+(n.id==='_ghost'?' ghost':'')+(fresh?' enter':'')+(n.id===active?' on':'')+(onPath[n.id]?' path':'')+(sel?' sel':'')+(rej?' rej':'')+(isWip?' wip':'')+(isFocus?' focus':'')
      +(n.kind==='stub'?' stub':'')+(n.id===NAVCUR?' kbd':'')+(hit?' ghit':'')+(dimq?' gdim':'');
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
    } else if(n.kind==='stub'){   // a folded passed-over subtree: one row, click to unfold
      inner=`<div class=nk><span aria-hidden=true>⊞</span>passed over</div>`+
        `<div class=nt>${esc(n.count)} steps tucked away — click to unfold</div>`;
    } else {
      const tuck=stubRootOf(n,onPath,m0)
        ?`<button type=button class=tuck title="Tuck this passed-over branch away" `+
         `onclick="event.stopPropagation();stubCollapse('${n.id}')">⊟</button>`:'';
      inner=`<div class=nk><span aria-hidden=true>${KICON[n.kind]||'▤'}</span>${esc(n.kind||'part')}${tuck}</div>`+
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
  renderMinimap();
}
// ── Graph chrome: minimap · fit view · zoom · keyboard nav · search-glow · hover trail ─────────
let LAYOUT=null, GQUERY='', NAVCUR=null, MM_DRAG=false;
function graphBounds(){   // the laid-out tree's extent in graph coords
  if(!LAYOUT||!LAYOUT.pos)return null;
  const ids=Object.keys(LAYOUT.pos); if(!ids.length)return null;
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
  ids.forEach(id=>{const p=LAYOUT.pos[id];
    x0=Math.min(x0,p.x);y0=Math.min(y0,p.y);x1=Math.max(x1,p.x+NW);y1=Math.max(y1,p.y+NH);});
  return {x:x0,y:y0,w:Math.max(1,x1-x0),h:Math.max(1,y1-y0)};
}
function fitView(){   // frame the whole tree (F) — zoom-by-pixels; stubs are the zoom-by-meaning lever
  const b=graphBounds(); if(!b)return;
  const r=$('right').getBoundingClientRect();
  const k=Math.min(1,Math.max(0.25,Math.min((r.width-90)/b.w,(r.height-140)/b.h)));
  BROWSING=true; VIEW.k=k;
  VIEW.x=(r.width-b.w*k)/2-b.x*k;
  VIEW.y=(r.height-b.h*k)/2-b.y*k+16;
  applyView(true); renderMinimap();
}
function zoomStep(f){   // +/− buttons and keys zoom on the panel center
  const r=$('right').getBoundingClientRect(), cx=r.width/2, cy=r.height/2;
  const k2=Math.min(1.9,Math.max(0.25,VIEW.k*f));
  BROWSING=true;
  VIEW.x=cx-(cx-VIEW.x)*(k2/VIEW.k); VIEW.y=cy-(cy-VIEW.y)*(k2/VIEW.k); VIEW.k=k2;
  applyView(false); renderMinimap();
}
const MMW=156, MMH=104, MMPAD=6;
function _mmScale(b){ return Math.min((MMW-MMPAD*2)/b.w,(MMH-MMPAD*2)/b.h); }
function renderMinimap(){   // branch structure + viewport rect; click/drag to jump
  const mm=$('minimap'); if(!mm)return;
  const b=graphBounds();
  if(!b||!LAYOUT||Object.keys(LAYOUT.m||{}).length<3||VIEWMODE!=='graph'){mm.hidden=true;return;}
  mm.hidden=false;
  const sc=_mmScale(b), r=$('right').getBoundingClientRect();
  const {t}=nodesOf(S); const onPath=S?pathSetOf(nodesOf(S).m,t.active):{};
  let html='';
  Object.keys(LAYOUT.pos).forEach(id=>{
    const p=LAYOUT.pos[id], n=LAYOUT.m[id]||{};
    const cls='mmdot'+(id===t.active?' act':(onPath[id]?' path':''))+(n.kind==='stub'?' stub':'')+(id==='_wip'?' wip':'');
    html+=`<i class="${cls}" style="left:${((p.x-b.x)*sc+MMPAD).toFixed(1)}px;`+
      `top:${((p.y-b.y)*sc+MMPAD).toFixed(1)}px;width:${Math.max(4,NW*sc).toFixed(1)}px;`+
      `height:${Math.max(2.5,NH*sc*0.8).toFixed(1)}px"></i>`;
  });
  // the viewport rectangle: what the camera currently shows, in tree coords → minimap coords
  const vx=((-VIEW.x/VIEW.k)-b.x)*sc+MMPAD, vy=((-VIEW.y/VIEW.k)-b.y)*sc+MMPAD;
  const vw=(r.width/VIEW.k)*sc, vh=(r.height/VIEW.k)*sc;
  html+=`<b class=mmview style="left:${vx.toFixed(1)}px;top:${vy.toFixed(1)}px;`+
    `width:${vw.toFixed(1)}px;height:${vh.toFixed(1)}px"></b>`;
  mm.innerHTML=html;
}
function mmJump(e){   // center the camera on the clicked tree point
  const b=graphBounds(); if(!b)return;
  const mm=$('minimap'), mr=mm.getBoundingClientRect(), sc=_mmScale(b);
  const gx=(e.clientX-mr.left-MMPAD)/sc+b.x, gy=(e.clientY-mr.top-MMPAD)/sc+b.y;
  const r=$('right').getBoundingClientRect();
  BROWSING=true;
  VIEW.x=r.width/2-gx*VIEW.k; VIEW.y=r.height/2-gy*VIEW.k;
  applyView(false); renderMinimap();
}
function initMinimap(){
  const mm=$('minimap'); if(!mm)return;
  mm.addEventListener('pointerdown',e=>{MM_DRAG=true;mm.setPointerCapture(e.pointerId);mmJump(e);});
  mm.addEventListener('pointermove',e=>{if(MM_DRAG)mmJump(e);});
  const up=()=>{MM_DRAG=false;};
  mm.addEventListener('pointerup',up); mm.addEventListener('pointercancel',up);
}
// Search-glow (the PoE pattern): every node whose label / cached content / research mentions the
// term lights up; the rest dim. Deterministic substring — same spirit as the research stack's focus.
function cacheText(id){
  const d=NODECACHE[id]; if(!d)return '';
  const opts=(d.options||[]).map(o=>{const x=o.direction||{};return (x.title||'')+' '+(x.one_liner||'');}).join(' ');
  return [d.content,d.draft,d.thesis,opts].filter(Boolean).join(' ');
}
function nodeMatches(n,q){
  if(!q||n.kind==='stub')return false;
  const hay=(nodeLabel(n)+' '+(n.title||'')+' '+(n.feedback||'')+' '+cacheText(n.id)).toLowerCase();
  return hay.indexOf(q.toLowerCase())>=0;
}
function gSearch(q){ GQUERY=(q||'').trim(); if(VIEWMODE==='graph')renderGraph(); }
// Keyboard nav: arrows walk the laid-out tree (↑ parent · ↓ child, spine first · ←/→ siblings),
// Enter opens the cursor node (or unfolds a stub), F fits, +/− zoom, / jumps to search.
function navSibs(id){
  const p=LAYOUT&&LAYOUT.tp&&LAYOUT.tp[id];
  if(p==null||!LAYOUT.kids[p])return [id];
  return LAYOUT.kids[p].slice().sort((a,b)=>LAYOUT.pos[a].x-LAYOUT.pos[b].x);
}
function navMove(dir){
  if(!LAYOUT||!S)return;
  const {t}=nodesOf(S);
  let cur=NAVCUR&&LAYOUT.m[NAVCUR]?NAVCUR:(FOCUS&&LAYOUT.m[FOCUS]?FOCUS:t.active);
  if(!LAYOUT.m[cur])return;
  if(dir==='up'){ const p=LAYOUT.tp[cur]; if(p!=null&&LAYOUT.m[p])cur=p; }
  else if(dir==='down'){
    const ks=(LAYOUT.kids[cur]||[]).slice();
    if(ks.length){ const onPath=pathSetOf(nodesOf(S).m,t.active);
      ks.sort((a,b)=>(onPath[b]?1:0)-(onPath[a]?1:0)||LAYOUT.pos[a].x-LAYOUT.pos[b].x); cur=ks[0]; }
  } else {
    const sibs=navSibs(cur), i=sibs.indexOf(cur);
    if(i>=0)cur=sibs[Math.max(0,Math.min(sibs.length-1,i+(dir==='right'?1:-1)))];
  }
  NAVCUR=cur; BROWSING=true;
  renderGraph();
  if(LAYOUT.pos[cur]){ const r=$('right').getBoundingClientRect();
    VIEW.x=r.width/2-(LAYOUT.pos[cur].x+NW/2)*VIEW.k; VIEW.y=r.height*0.42-LAYOUT.pos[cur].y*VIEW.k;
    applyView(true); renderMinimap(); }
}
function graphKeys(e){
  const tgt=e.target||{};
  if(tgt.tagName==='INPUT'||tgt.tagName==='TEXTAREA'||tgt.isContentEditable)return;
  if(!$('v2modal')||!$('v2modal').hidden)return;
  if(VIEWMODE!=='graph'||!S)return;
  if(e.key==='/'){ e.preventDefault(); const g=$('gsearch'); if(g)g.focus(); return; }
  if(e.key==='f'||e.key==='F'){ e.preventDefault(); return fitView(); }
  if(e.key==='+'||e.key==='='){ e.preventDefault(); return zoomStep(1.25); }
  if(e.key==='-'){ e.preventDefault(); return zoomStep(0.8); }
  if(e.key==='ArrowUp'){ e.preventDefault(); return navMove('up'); }
  if(e.key==='ArrowDown'){ e.preventDefault(); return navMove('down'); }
  if(e.key==='ArrowLeft'){ e.preventDefault(); return navMove('left'); }
  if(e.key==='ArrowRight'){ e.preventDefault(); return navMove('right'); }
  if(e.key==='Enter'&&NAVCUR){ e.preventDefault();
    const n=LAYOUT&&LAYOUT.m&&LAYOUT.m[NAVCUR];
    if(n&&n.kind==='stub'){ NAVCUR=n.root; return stubExpand(n.root); }
    const id=NAVCUR; NAVCUR=null; return gNodeClick(id); }
}
// Hover trail (PoE's path-to-target): hovering any node glows the root→node chain and counts steps.
function initTrail(){
  const gn=$('gnodes'); if(!gn)return;
  gn.addEventListener('mouseover',e=>{
    const el=e.target.closest('.gnode'); if(!el||!LAYOUT)return;
    if(el.classList.contains('focus'))return;   // an open doc isn't a wayfinding target
    let id=el.dataset.id, steps=0;
    const chain=new Set();
    for(let cur=id;cur!=null&&LAYOUT.m[cur];cur=LAYOUT.tp[cur]){chain.add(cur);steps++;}
    gn.querySelectorAll('.gnode').forEach(x=>x.classList.toggle('trail',chain.has(x.dataset.id)));
    if(!el.classList.contains('stub')){
      const base=(el.title||'').replace(/\n?· \d+ steps? from the start$/,'');
      const n=Math.max(1,steps-1);
      el.title=base+(base?'\n':'')+`· ${n} step${n===1?'':'s'} from the start`;
    }
  });
  gn.addEventListener('mouseout',e=>{
    if(e.relatedTarget&&e.relatedTarget.closest&&e.relatedTarget.closest('#gnodes'))return;
    gn.querySelectorAll('.gnode.trail').forEach(x=>x.classList.remove('trail'));
  });
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
function focusW(r){ // doc width: panel minus margins — but never wider than the screen (phones < the 460 floor)
  return Math.min(Math.min(860,Math.max(460,r.width-170)), Math.max(200,r.width-24)); }
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
  if(ease)setTimeout(()=>v.classList.remove('ease'),650);
  if(!MM_DRAG)renderMinimap(); }   // the viewport rect tracks every pan/zoom (drag drives itself)
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
  if(id.indexOf('_stub:')===0)return stubExpand(id.slice(6));   // a folded branch: click unfolds it
  if(FOCUS===id)return;                           // already reading it
  focusNode(id);                                  // browsing rendered nodes works even while it builds
}
function focusActive(force){ const {t}=nodesOf(S); BROWSING=false; if(force)FOCUS=null; focusNode(t.active); }
function focusNode(id){
  const {m,t}=nodesOf(S); if(!m[id])return;
  FOCUS=id; BROWSING=false; NAVCUR=null;
  const n=m[id];
  if(id!==t.active&&!NODECACHE[id]){   // a past node: fetch its full content, then re-render
    api('GET',`/api/plan/${SID}/node/${id}`).then(({ok,d})=>{ if(ok){NODECACHE[id]=d; hydrateLog(id,d.log); if(FOCUS===id)renderGraph();} });
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
  // during a transitional stage (merging / researching) stageSurface has no verb to offer — but the
  // node's CONTENT is already here; an empty wide box mid-WIP read as a swallow (Sam's QA,
  // 2026-07-06). Show what the node IS, read-only; the WIP node owns the action.
  if(n.id===t.active){ const surf=stageSurface(S); body=surf?surf.html:transitionalBody(); }
  else body=pastBody(n)+`<div class=ctarow><button class="stage-cta secondary" onclick="pivotFromHere('${n.id}')">⑂ Pivot from here</button></div>`;
  body+=cmtsHtml(n.id);   // inline comments on this doc, awaiting the next build verb (backlog #8)
  body+=boardNotesHtml(n.id);   // this step's convenes — the persisted history, visible on the node
  const log=NODELOG[n.id];
  // the open state survives re-renders — the WIP poll rebuilds this HTML every tick, and an
  // unremembered <details> flashes open then collapses
  if(log&&log.length)body+=`<details class=nhist${HIST_OPEN.has(n.id)?' open':''} `+
    `ontoggle="histKeep('${n.id}',this)"><summary>⚙ how this was built</summary><div class=nspew>`+
    log.map(l=>`<div>${esc(l)}</div>`).join('')+`</div></details>`;
  return body;
}
// The board history pinned to a node (server persists it per step; funnel nodes inherit forward).
// The ACTIVE node's history is the flat mirror (S.board); a browsed past node's comes with its fetch.
function boardNotesHtml(id){
  const {t}=nodesOf(S);
  const list=(id===t.active)?((S&&S.board)||[]):(((NODECACHE[id]||{}).board)||[]);
  if(!list.length)return '';
  const rows=list.slice().reverse().map(e=>
    `<div class=bdrow><b>${esc(e.title||'Board review')}</b>${_vchip(e.verdict)}`+
    (e.skeptic?`<div class="bdrow skept" style="border:none;padding-top:2px;margin-top:2px">🧐 ${esc((e.skeptic.take||e.skeptic.rationale||'').slice(0,180))}</div>`:'')+
    `</div>`).join('');
  return `<details class=nhist${HIST_OPEN.has(id+':b')?' open':''} ontoggle="histKeep('${id}:b',this)">`+
    `<summary>🪑 board notes on this step (${list.length})</summary>${rows}</details>`;
}
// The active node's content while a background op runs on it — no CTAs (double-firing a merge or
// commit mid-run is the failure this read-only view prevents), but everything readable, right away.
function transitionalBody(){
  const a=S&&S.activeNode; if(!a)return '';
  if(a.kind==='brainstorm'){
    const opts=(a.options||[]).map(o=>{const x=o.direction||{};const on=SEL.has(o.id);
      return `<div class="opt${on?' sel':''}" style="cursor:default"><div>`+
        `<h3>${on?'✓ ':''}${esc(x.title||'')}</h3><p>${esc(x.one_liner||'')}</p>`+
        `${x.mold?`<span class=mold>${esc(x.mold)}</span>`:''}</div></div>`;}).join('');
    const piv=a.feedback?`<p class=react style="font-size:14px">↳ Pivoting on: “${esc(a.feedback)}”</p>`:'';
    return piv+setAsideHtml(a.set_aside)+`<p class=eyebrow>The directions offered</p>`+
      `<div class=optgrid>${opts}</div><p class=thinking>✓ = your picks — merging them now.</p>`;
  }
  if(a.kind==='refined'){
    const kept=(a.kept||[]).map(k=>`<li>${esc(k)}</li>`).join('');
    const rows=((a.research&&a.research.rows)||[]).slice(0,4).map(x=>
      `<li>${x.mark==='ok'?'✅':'⚠️'} ${esc(x.text)} <span class="badge ${x.mark==='ok'?'b-ok':'b-warn'}">${x.mark==='ok'?'cited':'vendor'}</span></li>`).join('');
    return `<p class=react>${esc(a.thesis||'')}</p>`+(a.mold?`<span class=mold>${esc(a.mold)}</span>`:'')+
      (kept?`<p class=eyebrow style="margin-top:14px">Kept</p><ul class=kept>${kept}</ul>`:'')+
      (rows?`<ul class=ev>${rows}</ul>`:'')+
      `<p class=thinking>Deep research + the full build are running on this idea now.</p>`;
  }
  return '';
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
    return piv+setAsideHtml(d.set_aside)+`<p class=eyebrow>${d.spread==='tight'?'Your idea, sharpened':'The directions offered'}</p>`+
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
  // THE SURFACE MUST MATCH THE NODE IT RENDERS INSIDE. `stage` and `tree.active` can disagree (a
  // stranded pointer from the old commit bug corrupted live sessions; any future drift lands the
  // same way) — trusting stage alone painted refined CTAs on a brainstorm fork with no checkboxes
  // (Sam's refresh bug, 2026-07-06). When idle, the ACTIVE NODE'S KIND picks the surface; stage
  // stays the tiebreak for section nodes. Mid-run keeps the stage flow → transitionalBody, no CTAs.
  const busy=!!(WIP_LABEL||s.status==='researching');
  if(!busy&&!PENDING_FORK&&s.status!=='error'&&!(s.done||s.stage==='done')){
    const ak=s.activeNode&&s.activeNode.kind;
    if(ak==='brainstorm')return {html:brainstormHtml(s)};
    if(ak==='refined')return {html:refinedHtml(s)};
  }
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
  if(s.done||s.stage==='done')return {html:doneHtml(s)};   // done wins over building — no ghost 'Part 8 of 7'
  if(s.stage==='building')return {html:(s.step||0)===0?firstPageHtml(s):chapterHtml(s)};
  return null;
}
// The frontier gets "Keep going"; a DECIDED step (its next already exists) or a step with a build
// already in flight offers Pivot only — rolling forward again would just duplicate the child.
function stepCtas(){
  const {m,t}=nodesOf(S);
  const decided=Object.values(m).some(n=>n.parent===t.active);
  const busy=!!WIP_LABEL||S.status==='researching';
  if(decided||busy)return `<div class=ctarow><button class=stage-cta onclick=pivotActive()>⑂ Pivot</button></div>`+
    `<p class=thinking>${busy&&!decided?'The next part is already being written — pivot to change course.'
      :'This step is already decided — pivot to take it somewhere else.'}</p>`;
  // name the actual next part on the button — "what happens on click" at every hop (§v2 #17)
  const nx=(S.sections||[])[(S.step||0)+1];
  const nxt=nx?`Next up: Part ${(S.step||0)+2} of ${S.total} — ${nx.title}`:'Next up: draft the next section';
  return `<div class=ctarow><button class=stage-cta onclick=keepGoing()>Keep going →`+
    `<span class=ctasub>${esc(nxt)}</span></button>`+
    `<button class="stage-cta secondary" onclick=pivotActive()>⑂ Pivot</button></div>`+
    `<p class=thinking>Comment or steer in the box anytime, it wins.</p>`;
}
// A declared set-aside: part of the ask the engine declined OUT LOUD (with its reason) — rendered
// as a warning card wherever the spread shows, never buried. The off-ramps are the operator's.
function setAsideHtml(sa){
  if(!sa||!sa.what)return '';
  return `<div class=saside>⚠ <b>Set aside, not silently dropped:</b> ${esc(sa.what)}`+
    `<span class=why> — ${esc(sa.why||'')}</span>`+
    `<span class=why> The directions run with the rest. To force it back in, rephrase the pivot; `+
    `or branch from an earlier node and rebuild around it.</span></div>`;
}
function brainstormHtml(s){
  const cards=stageOptions().map(o=>{const d=o.direction||{};const on=SEL.has(o.id);
    return `<label class="opt${on?' sel':''}"><input type=checkbox ${on?'checked':''} onchange="toggleSel('${o.id}')">`+
      `<div><h3>${esc(d.title||'Direction')}</h3><p>${esc(d.one_liner||'')}</p>`+
      `${d.mold?`<span class=mold>${esc(d.mold)}</span>`:''}</div></label>`;}).join('');
  const piv=(s.activeNode&&s.activeNode.feedback)?`<p class=react style="font-size:14px">↳ Pivoting on: “${esc(s.activeNode.feedback)}”</p>`:'';
  const sa=setAsideHtml(s.activeNode&&s.activeNode.set_aside);
  return piv+sa+`<p class=eyebrow>Pick what clicks</p><div class=optgrid>${cards}</div>`+
    `<div class=ctarow><button class=stage-cta onclick=doMerge()>Let's try it →`+
    `<span class=ctasub>Next up: refine + a light research pass</span></button>`+
    `<button class="stage-cta secondary" onclick=commitFromBrainstorm()>I'm sold, build the plan`+
    `<span class=ctasub>Skips refining — straight to deep research</span></button></div>`+
    `<p class=thinking>Or just type in the box, it always wins.</p>`;
}
// checkbox picks survive a refresh — they were client-memory only, so a reload silently dropped
// the selection and the fork re-rendered pickless (Sam's QA, 2026-07-06)
function _selKey(){ const {t}=nodesOf(S); return 'filg_sel_'+SID+':'+(t.active||''); }
function saveSel(){ try{localStorage.setItem(_selKey(),JSON.stringify([...SEL]));}catch(e){} }
function loadSel(){ try{
    const v=JSON.parse(localStorage.getItem(_selKey())||'[]');
    const opts=new Set((stageOptions()||[]).map(o=>o.id));
    SEL=new Set((v||[]).filter(id=>opts.has(id)));
  }catch(e){ SEL=new Set(); } }
function toggleSel(id){ if(SEL.has(id))SEL.delete(id); else SEL.add(id); saveSel(); renderView(); }
function refinedHtml(s){
  const a=s.activeNode||{};
  const fb=a.feedback?`<p class=react style="font-size:14px">↳ Folded in: “${esc(a.feedback)}”</p>`:'';
  const kept=(a.kept||[]).map(k=>`<li>${esc(k)}</li>`).join('');
  const dropped=(a.dropped||[]).map(d=>`<li>${esc(d.thread)} <span class=why>— ${esc(d.why)}</span></li>`).join('');
  const R=(a.research&&a.research.prose)||{};
  const rows=((a.research&&a.research.rows)||[]).slice(0,4).map(x=>
    `<li>${x.mark==='ok'?'✅':'⚠️'} ${esc(x.text)} <span class="badge ${x.mark==='ok'?'b-ok':'b-warn'}">${x.mark==='ok'?'cited':'vendor'}</span></li>`).join('');
  const qs=(a.questions||[]).map(q=>`<li>${esc(q)}</li>`).join('');
  return fb+`<p class=react>${esc(a.thesis||'')}</p>`+
    (a.mold?`<span class=mold>${esc(a.mold)}</span>`:'')+
    (kept?`<p class=eyebrow style="margin-top:14px">Kept</p><ul class=kept>${kept}</ul>`:'')+
    (dropped?`<p class=eyebrow>Cut (and why)</p><ul class=dropped>${dropped}</ul>`:'')+
    (R.offer?`<p class=eyebrow>First-pass read (light pass)</p><p><b>${esc(R.title||'')}</b> — ${esc(R.offer)}</p>`:'')+
    (rows?`<ul class=ev>${rows}</ul>`:'')+
    (qs?`<p class=eyebrow>Worth answering first</p><ul class=qs>${qs}</ul>`+
      `<p class=thinking>Answer any of these in the box — they sharpen the idea before the deep dive. Or skip ahead.</p>`:'')+
    `<div class=gate>⛳ <b>Prelaunch gate.</b> That read was a quick skim. The next step is the deep one: `+
    `full graded research on every claim, then the plan drafts section by section.</div>`+
    `<div class=ctarow><button class=stage-cta onclick="commit()">I'm sold, build the plan →`+
    `<span class=ctasub>Next up: deep research — the graded vetting pass</span></button></div>`+
    `<p class=thinking>Not quite? Steer it in the box — it refines this idea in place.</p>`;
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
    `<ul class=keypoints>${points}</ul>`+(v.verdict==='kill'?killCtas(s):stepCtas());
}
// A kill-graded first page: the diagnostic question IS the step (backlog #13). Rolling forward
// unforced won't draft; the CTAs are the three real ways out.
function killCtas(s){
  const q=((s.shaped||{}).clarifying_question)
    ||'Name one real skill, asset, or audience you already have, and who would pay for it.';
  return `<p class=react><b>${esc(q)}</b></p>`+
    `<div class=ctarow><button class=stage-cta onclick=armRevet()>Give it substance</button>`+
    `<button class="stage-cta secondary" onclick=pivotActive()>⑂ Pivot</button>`+
    `<button class="stage-cta secondary" onclick=keepGoingForced()>Build it anyway 🤡</button></div>`+
    `<p class=thinking>It grades kill until there's something real to build on — answer in the box, pivot, or take the joke plan.</p>`;
}
// ── #12: 4 chapters over the 7 engine sections — display grouping ONLY, the engine's section
// keys/steps/files are untouched. Step index → chapter.
const CHAPTERS=[
  {name:'The idea',    steps:[0]},        // the setup
  {name:'The offer',   steps:[1,2,3]},    // what you sell · why you win · what you charge
  {name:'The machine', steps:[4,5]},      // how you get customers · how you deliver
  {name:'The launch',  steps:[6]},        // your first 30 days
];
function chapterOf(step){
  for(let i=0;i<CHAPTERS.length;i++)if(CHAPTERS[i].steps.indexOf(step)>=0)return {i,name:CHAPTERS[i].name};
  return null;
}
function chapterHtml(s){
  const p=s.proposal||{};
  const c=chapterOf(s.step||0);
  const eyebrow=(c?`Chapter ${c.i+1}: ${c.name} · `:'')+`Part ${(s.step||0)+1} of ${s.total}`;
  return `<p class=eyebrow>${esc(eyebrow)}</p><div class=draft>${mdToHtml(p.draft||'')}</div>`+
    stepCtas();
}
function doneHtml(s){
  const files=(s.files||[]).map(f=>`<li>${esc(f.path)}</li>`).join('');
  return `<p class=eyebrow>Plan complete</p><p class=react>🎉 All ${s.total} parts, built with you.</p>`+
    `<ul>${files}</ul>`+
    `<div class=ctarow><button class=stage-cta onclick=downloadZip()>⬇ Raw files (.zip)</button>`+
    `<button class="stage-cta secondary" onclick=downloadPdf()>📄 Plan PDF</button>`+
    `<button class="stage-cta secondary" onclick=shareModal()>🔗 Share</button>`+
    `<button class="stage-cta secondary" onclick=pivotActive()>⑂ Pivot</button></div>`+
    `<p class=thinking>Raw export is always free. The PDF is free with a watermark on your own key `+
    `(a one-time ${pdfPriceStr()} unlocks this plan's clean copy, re-downloads free) and clean on `+
    `any subscription.</p>`;
}
// ── Share: a public read-only /p/{id} page (receipts + decision path + the plan), private by
// default. The artifact IS the funnel — the maker line on the page is the growth loop.
async function shareModal(){
  if(!SID){ toast('Start a plan first — then you can share it.','err'); return; }
  const on=!!(S&&S.shared);
  const url=location.origin+'/p/'+SID;
  $('modal-body').innerHTML=
    `<p class=muted>A public, read-only page of this plan: the graded receipts, the decision path, `+
    `and every part — shareable with a client, cofounder, or the internet. Private by default; `+
    `flip it off any time.</p>`+
    `<div class=sharerow><b>${on?'Sharing is ON':'Sharing is OFF'}</b>`+
    `<button type=button class=${on?'':'primary'} onclick="setShared(${on?'false':'true'})">${on?'Make it private':'Turn on sharing'}</button></div>`+
    (on?`<input id=shareurl readonly value="${esc(url)}" onclick="this.select()">`+
        `<div class=cmtacts><button type=button class=primary onclick="copyShare()">Copy link</button>`+
        `<a class=ghost style="padding:6px 10px" href="${esc(url)}" target=_blank rel=noopener>Open ↗</a></div>`:'');
  $('modal-acts').innerHTML='<button class=primary onclick="closeModal()">Done</button>';
  openModal('Share this plan');
}
async function setShared(v){
  const {ok,d}=await api('POST',`/api/plan/${SID}/share`,{shared:v});
  if(!ok){ toast((d&&d.error)||'Could not change sharing.','err'); return; }
  if(S)S.shared=!!v&&!!(d&&d.shared);
  toast(S.shared?'Share link is live.':'Back to private.');
  shareModal();   // re-render the modal in its new state
}
function copyShare(){ const i=$('shareurl'); if(!i)return; i.select();
  try{ navigator.clipboard?navigator.clipboard.writeText(i.value):document.execCommand('copy');
       toast('Link copied. ✓'); }catch(e){ toast('Copy failed — select + copy by hand.','err'); } }
// The PDF fetch goes through JS so a 402 (unlock needed) or 429 lands as guidance, not a broken tab.
async function downloadPdf(){
  if(!SID)return;
  const th=chatSay('status','synthesizing the PDF…');
  let r;
  try{ r=await fetch(`/api/plan/${SID}/plan.pdf`,{headers:authHeaders()}); }catch(e){ if(th)th.remove(); toast('Network hiccup — try again.','err'); return; }
  if(th)th.remove();
  if(!r.ok){
    let d={}; try{d=await r.json();}catch(e){}
    if(d.needPurchase){ pdfBuyPrompt(d); return; }
    if(gateV2(d))return;
    toast((d&&d.error)||'Could not build the PDF.','err'); return;
  }
  const wm=r.headers.get('X-FILG-Watermark')==='1';
  const blob=await r.blob();
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  const fm=(r.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/);
  a.download=(fm&&fm[1])||'business-plan.pdf';
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href),4000);
  toast(wm?`PDF downloaded — free copy, watermarked. A one-time ${pdfPriceStr()} unlocks this plan's clean one.`:'PDF downloaded. ✓');
}
// 402 needPurchase → an in-chat offer with the real checkout button (never a dead end).
function pdfBuyPrompt(d){
  const txt=(d&&d.error)||`The clean PDF for this plan is a one-time ${pdfPriceStr()} — re-downloads are free.`;
  chatPush('bot',txt);
  const m=chatSay('bot',esc(txt)+
    `<div class=cbtns><button type=button class=go>🔓 Unlock clean PDF, ${pdfPriceStr()}</button>`+
    `<button type=button class=nah>Not now</button></div>`);
  if(!m)return;
  m.querySelector('.go').onclick=()=>{ m.classList.add('asked'); buyPdf(); };
  m.querySelector('.nah').onclick=()=>{ m.classList.add('asked');
    chatStatus('No problem — the watermarked copy and the raw export stay free.'); };
}
async function buyPdf(){
  if(CFG.authEnabled&&!signedIn()){ authModal('Sign in to unlock your PDF.'); return; }
  if(!CFG.pdfBilling){ toast("Billing isn't set up yet.",'err'); return; }
  const {d}=await api('POST',`/api/plan/${SID}/buy-pdf`,{});
  if(d&&d.url){ location.href=d.url; return; }          // → Stripe Checkout
  if(d&&d.unlocked){ downloadPdf(); return; }           // already claimable → just grab it
  toast((d&&d.error)||'Could not start checkout.','err');
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
  let lastCh=-1;   // #12: a chapter label leads its first section tab (display grouping only)
  dt.innerHTML=chain.map(n=>{
    let pre='';
    if(n.kind==='section'){
      const c=chapterOf(n.step||0);
      if(c&&c.i!==lastCh){ lastCh=c.i; pre=`<span class=dchap>${esc(c.name)}</span>`; }
    }
    return pre+`<button type=button role=tab aria-selected="${n.id===DOCTAB}" class="dtab${n.id===DOCTAB?' on':''}${n.id===t.active?' cur':''}" onclick="pickDoc('${n.id}')">`+
    `<span aria-hidden=true>${KICON[n.kind]||'▤'}</span>${esc(nodeLabel(n))}</button>`;}).join('')+
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
    api('GET',`/api/plan/${SID}/node/${n.id}`).then(({ok,d})=>{ if(ok){NODECACHE[n.id]=d; hydrateLog(n.id,d.log);
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
  // belt-and-braces: never let a stale POLL-DRIVEN label stick. A SYNC op's label is its own —
  // a stray render mid-await (an old poll tick, a board reply) must not blank a live WIP box.
  if(!researching&&!WAS_RESEARCHING&&!WIP_SYNC)WIP_LABEL=null;
  if(researching&&!WIP_T0)WIP_T0=Date.now();          // e.g. a reload mid-build → the timer still ticks
  const {t}=nodesOf(S);
  if(WAS_RESEARCHING&&!researching){          // a build just finished → stash its log on the node it made
    if(WIPLOG.length)NODELOG[t.active]=WIPLOG.slice();
    WIPLOG=[]; LANES=[]; LANES_DONE=new Set(); WIP_LABEL=null; WIP_T0=null; WIP_PENDING=null;
  }
  if(S.activeNode)hydrateLog(S.activeNode.id,S.activeNode.log);   // server-persisted receipts survive a reload
  // chat-lookup claims persist server-side now — the server's list is the superset, adopt it
  if(S.lookups&&S.lookups.length>LOOKUPS.length)LOOKUPS=S.lookups.slice();
  WAS_RESEARCHING=researching;
  paintMeter(S);
  if(!researching){
    if(t.active!==LAST_ACTIVE){ LAST_ACTIVE=t.active; FOCUS=t.active; BROWSING=false; PREFOCUS_VIEW=null;
      promptFocus(); announceSetAside(); }   // the next step is ready → zoom in, hands back on the keyboard
    else if(FOCUS==null&&!BROWSING){ FOCUS=t.active; }
  }
  if(!researching&&VIEWMODE==='docs')DOCTAB=t.active;   // the reader follows the build
  renderView();
  if(RMODE)renderResearch();   // fresh graded rows (a build just landed) show up in the stack live
  if(SMODE)renderSummary();    // the in-voice summary tracks the newest graded read
}
// A set-aside must reach the CHAT too (the record the operator reads back), once per node —
// "quietly ignore and reroute" is the failure this whole channel exists to kill.
let SA_SAID=new Set();
function announceSetAside(){
  const a=S&&S.activeNode; if(!a||!a.set_aside||!a.set_aside.what)return;
  if(SA_SAID.has(a.id))return; SA_SAID.add(a.id);
  chatBot(`⚠ Heads up — part of your ask was set aside, not built in: “${a.set_aside.what}” `+
    `(${a.set_aside.why||'no reason given'}). The directions run with the rest. Rephrase to force `+
    `it back in, or pivot from an earlier node to rebuild around it.`);
}
function promptFocus(){   // put the cursor back in the chat box so the user can just start typing
  const a=document.activeElement;
  if(a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA')&&a.id!=='ws-box')return;   // don't steal a real field
  const b=$('ws-box'); if(b&&!$('workspace').hidden)b.focus();
}

// ═══ RESEARCH MODE — one chat, three displays ═══════════════════════════════
// The conversation is ALWAYS the same single chat (#chatlog is never cleared); research mode only
// changes what the left drawer shows. States:
//   'full'     — the research stack covers the chat history (the box stays; click Research pill)
//   'split'    — first question slides the chat up: top = research (relevance-focused), bottom = chat
//   'expanded' — the stack slides out into its own left-anchored drawer, chat gets the full column
// The stack = the plan's graded rows + any claims pulled by chat lookups this session. Relevance is
// deterministic term overlap (fuzzy-finder FEEL, not fuzzy matching). Build-intent prompts exit the
// mode automatically; an off-research question gets an in-chat offer to exit.
let RMODE=null, RQUERY='', LOOKUPS=[], R_OFFERED=false;
function enterResearch(){ if(!RMODE)RMODE='full'; R_OFFERED=false; applyRmode(); renderResearch(); }
function exitResearch(quiet){
  if(!RMODE)return;
  RMODE=null; RQUERY=''; applyRmode();
  MODE='build';
  document.querySelectorAll('#modechips .mchip').forEach(b=>b.classList.toggle('on',b.dataset.mode==='build'));
  $('ws-wrap').className='promptwrap';
  if(!quiet)chatStatus('Back to build mode.');
}
function expandResearch(){ RMODE='expanded'; mOpen(); applyRmode(); renderResearch(); }
function collapseResearch(){ RMODE='split'; mOpen(); applyRmode(); renderResearch(); }
// ── The split boundary is DRAGGABLE (Sam, 2026-07-06): one remembered height shared by every
// in-drawer pane (research/board/help). Dragging sets an inline flex-basis; leaving the split
// clears it so the full/expanded displays keep their class-driven sizing. ──
let SPLIT_PX=(function(){try{const v=parseInt(localStorage.getItem('filg_split_px'),10);
  return (v>=120&&v<=2000)?v:null;}catch(e){return null;}})();
function applySplitSize(pane,on){
  if(!pane)return;
  if(on&&SPLIT_PX){ pane.style.flex=`0 0 ${SPLIT_PX}px`; pane.style.maxHeight='none'; }
  else { pane.style.flex=''; pane.style.maxHeight=''; }
}
function initGrips(){
  document.querySelectorAll('.vgrip').forEach(g=>{
    const pane=$(g.dataset.pane); if(!pane)return;
    g.addEventListener('pointerdown',e=>{
      e.preventDefault(); g.setPointerCapture(e.pointerId); g.classList.add('dragging');
      pane.classList.add('resizing');   // the .45s flex ease fights a live drag — off while held
      const move=ev=>{
        const L=$('left'), lr=L.getBoundingClientRect(), pr=pane.getBoundingClientRect();
        const h=Math.max(110,Math.min(ev.clientY-pr.top,lr.height-230));   // chat + box stay usable
        SPLIT_PX=Math.round(h); applySplitSize(pane,true);
      };
      const up=ev=>{ g.classList.remove('dragging'); pane.classList.remove('resizing');
        g.removeEventListener('pointermove',move); g.removeEventListener('pointerup',up);
        g.removeEventListener('pointercancel',up);
        try{localStorage.setItem('filg_split_px',String(SPLIT_PX));}catch(x){} };
      g.addEventListener('pointermove',move);
      g.addEventListener('pointerup',up); g.addEventListener('pointercancel',up);
    });
  });
}
// ── Mobile drawer model (Sam, 2026-07-06): the graph owns the screen; the chat is a bottom
// sheet with a grab handle. An expanded section (research/board) REPLACES the chat sheet
// (workspace.rx hides .left); Tuck in returns to the split chat drawer; the handle collapses
// whichever sheet is up (workspace.mclosed) so the decision tree shows. Desktop CSS ignores
// all three classes — the handles only render under the mobile media query. ──
function mToggle(){ const w=$('workspace'); if(w)w.classList.toggle('mclosed'); }
function mCollapse(){ // any submission that DRAWS THE GRAPH collapses the drawer fully (mobile only)
  if(window.matchMedia&&window.matchMedia('(max-width:820px)').matches){
    const w=$('workspace'); if(w)w.classList.add('mclosed'); } }
function syncRx(){ const w=$('workspace'); if(!w)return;
  const rx=(typeof RMODE!=='undefined'&&RMODE==='expanded')||(typeof BMODE!=='undefined'&&BMODE==='expanded');
  w.classList.toggle('rx',rx); }
function mOpen(){ const w=$('workspace'); if(w)w.classList.remove('mclosed'); }
function applyRmode(){
  const L=$('left'), pane=$('rpane'), dr=$('rdrawer');
  // a stale shell (served before the research pane existed) must fail LOUD, not half-collapse the
  // drawer — the JS/CSS come fresh from /static while the HTML can lag a server restart / hard cache
  if(!L||!pane||!dr){
    if(RMODE){ RMODE=null; toast('This page is stale — hard-refresh (⌘⇧R) to load the research surface.','err'); }
    if(L){ L.classList.remove('rfull'); L.classList.remove('rsplit'); }
    return;
  }
  L.classList.toggle('rfull',RMODE==='full');
  L.classList.toggle('rsplit',RMODE==='split');
  pane.setAttribute('aria-hidden',String(!(RMODE==='full'||RMODE==='split')));
  dr.classList.toggle('open',RMODE==='expanded');
  dr.setAttribute('aria-hidden',String(RMODE!=='expanded'));
  syncRx();
  applySplitSize(pane,RMODE==='split');   // the remembered drag height applies to the split only
  if(RMODE!=='full'){ const log=$('chatlog'); if(log)log.scrollTop=log.scrollHeight; }
}
function researchItems(){
  // EVERY graded source joins the stack, deduped by text|url. Before the deep build the rows live
  // on the REFINED NODE (the first-pass read), not the session — the stack reading only S.research
  // meant a refined-stage Research pill showed empty while graded rows sat in the node (found in
  // Sam's 2026-07-06 QA). Sources: the plan's deep-build rows · the active node's first-pass rows ·
  // any browsed node's rows (NODECACHE) · this session's chat lookups.
  const out=[], seen=new Set();
  const add=(list,fresh)=>(list||[]).forEach(r=>{
    const key=(r.text||'')+'|'+(r.url||'');
    if(!r.text||seen.has(key))return;
    seen.add(key);
    const ok=('mark' in r)?r.mark==='ok':!r.flagged;
    out.push({text:r.text,url:r.url,ok,tier:r.tier&&!('mark' in r)?r.tier:(ok?'cited':'vendor'),
      fresh:!!fresh});
  });
  add(S&&S.research&&S.research.rows);
  add(S&&S.activeNode&&S.activeNode.research&&S.activeNode.research.rows);
  Object.keys(NODECACHE).forEach(id=>{const d=NODECACHE[id];
    add(d&&d.research&&d.research.rows);});
  add(LOOKUPS,true);
  return out;
}
const _RSTOP=new Set(('the,a,an,and,or,but,of,to,in,on,for,with,is,are,was,were,be,been,do,does,did,'+
  'how,what,why,when,where,who,which,i,my,me,you,your,we,our,us,it,its,this,that,these,those,about,'+
  'should,would,could,can,will,there,here,from,into,than,then,them,they,have,has,had,not,no,yes,'+
  'much,many,more,most,some,any,all,per,get,got,make,made,want,like,just,really,going').split(','));
function _rterms(s){ return String(s||'').toLowerCase().replace(/[^a-z0-9$%\s]/g,' ')
  .split(/\s+/).filter(w=>w.length>2&&!_RSTOP.has(w)); }
function _rscore(qterms,item){
  const t=new Set(_rterms(item.text+' '+(item.url||'')));
  let n=0; qterms.forEach(w=>{ if(t.has(w))n++; });
  return n;
}
// Paint the stack into whichever surface is live (in-drawer pane and expanded drawer share markup).
// Returns true if the current RQUERY matched anything — the caller uses that as the relevance signal.
function renderResearch(){
  const items=researchItems();
  const qt=_rterms(RQUERY);
  const scored=items.map((it,i)=>({it,i,s:qt.length?_rscore(qt,it):0}));
  if(qt.length)scored.sort((a,b)=>b.s-a.s||a.i-b.i);   // focused items float up, the rest dim below
  const anyHit=scored.some(x=>x.s>0);
  const html=items.length?scored.map(({it,s})=>
    `<div class="rrow${s>0?' hit':(qt.length&&anyHit?' dim':'')}${it.ok?'':' flagged'}">`+
    `${it.ok?'✅':'⚠️'} ${esc(it.text)}`+
    (it.url?` <a href="${esc(it.url)}" target=_blank rel=noopener>src</a>`:'')+
    `<span class=rtier>${esc(it.tier)}${it.fresh?' · lookup':''}</span></div>`).join('')
    :'<p class=thinking>No graded research yet — the first rows land with the first-pass read (merge a direction), more with the deep build. Ask a question and I\'ll dig (every claim goes through the gate).</p>';
  ['rlist','rlist2'].forEach(id=>{ const el=$(id); if(el)el.innerHTML=html; if(el)el.scrollTop=0; });
  return anyHit;
}
// An off-topic question while a display is up: answer it, then offer the way back to building.
// One chat — exiting is a display change, never a history change. Works for research AND board.
function offerExitMode(){
  const mode=RMODE?'research':(BMODE?'board':null);
  if(!mode)return;
  if(mode==='research'&&R_OFFERED)return;
  if(mode==='board'&&B_OFFERED)return;
  if(mode==='research')R_OFFERED=true; else B_OFFERED=true;
  const txt=`That one isn't really about the ${mode} — want to switch back to building?`;
  chatPush('bot',txt);
  const m=chatSay('bot',esc(txt)+
    '<div class=cbtns><button type=button class=go>Back to build</button>'+
    `<button type=button class=nah>Stay in ${mode}</button></div>`);
  if(!m)return;
  m.querySelector('.go').onclick=()=>{ m.classList.add('asked'); exitResearch(); exitBoard(); };
  m.querySelector('.nah').onclick=()=>{ m.classList.add('asked');
    if(mode==='research')R_OFFERED=false; else B_OFFERED=false;
    chatStatus(`Staying in ${mode} mode.`); };
}

// ═══ BOARD MODE — one chat, three displays (the research contract, applied to the board room) ═══
// 'full' = the convene stack covers the chat history; 'split' = first question slides the chat up;
// 'expanded' = the stack slides into its own left drawer. The stack = the active path's persisted
// board history (S.board mirrors the active node, convenes inherit forward) + the stress-test
// result. Seats row re-picks the bench; ⚒ Forge + 🧪 Stress-test live in the header.
let BMODE=null, BQUERY='', B_OFFERED=false, BOARD_PICK=null, FORGED=null, STRESS_ON=false;
function enterBoard(){ if(!BMODE)BMODE='full'; B_OFFERED=false; applyBmode(); renderBoard(); }
function exitBoard(quiet){
  if(!BMODE)return;
  BMODE=null; BQUERY=''; applyBmode();
  MODE='build';
  document.querySelectorAll('#modechips .mchip').forEach(b=>b.classList.toggle('on',b.dataset.mode==='build'));
  $('ws-wrap').className='promptwrap';
  if(!quiet)chatStatus('Back to build mode.');
}
function expandBoard(){ BMODE='expanded'; mOpen(); applyBmode(); renderBoard(); }
function collapseBoard(){ BMODE='split'; mOpen(); applyBmode(); renderBoard(); }
function applyBmode(){
  const L=$('left'), pane=$('bpane'), dr=$('bdrawer');
  if(!L||!pane||!dr){   // stale shell: fail LOUD, same contract as research
    if(BMODE){ BMODE=null; toast('This page is stale — hard-refresh (⌘⇧R) to load the board surface.','err'); }
    if(L){ L.classList.remove('bfull'); L.classList.remove('bsplit'); }
    return;
  }
  L.classList.toggle('bfull',BMODE==='full');
  L.classList.toggle('bsplit',BMODE==='split');
  pane.setAttribute('aria-hidden',String(!(BMODE==='full'||BMODE==='split')));
  dr.classList.toggle('open',BMODE==='expanded');
  dr.setAttribute('aria-hidden',String(BMODE!=='expanded'));
  syncRx();
  applySplitSize(pane,BMODE==='split');
  if(BMODE!=='full'){ const log=$('chatlog'); if(log)log.scrollTop=log.scrollHeight; }
}
// The stack: newest first — the stress-test result (if any) rides on top, then every convene.
function boardItems(){
  const out=[];
  const sk=S&&S.skeptic;
  if(sk&&sk.status==='done'&&sk.result)out.push({stress:sk.result});
  const hist=(S&&S.board)||[];
  for(let i=hist.length-1;i>=0;i--)out.push({entry:hist[i]});
  return out;
}
function _btext(it){   // the searchable text of a stack item, for the relevance focus
  if(it.stress){ const r=it.stress;
    return 'stress test assumptions '+((r.assessments||[]).map(a=>(a.assumption||'')+' '+(a.why||'')).join(' ')); }
  const e=it.entry||{};
  return [e.title,e.consensus,e.conflicts,e.verdict,
    (e.skeptic&&(e.skeptic.take||e.skeptic.rationale))||'',
    ...(e.directors||[]).map(x=>(x.name||'')+' '+(x.take||''))].join(' ');
}
function _vchip(v){ if(!v)return '';
  const cls=/non.?starter|kill|broken/i.test(v)?'bad':(/concern|dissent|weak/i.test(v)?'warn':(/agree|pursue|surviv/i.test(v)?'ok':''));
  return `<span class="bverdict ${cls}">${esc(v)}</span>`; }
function _bcard(it,open){
  if(it.stress)return _stressCard(it.stress);
  const e=it.entry||{};
  const dirs=(e.directors||[]).map(x=>`<div class=bdrow><b>${esc(x.name||x.key)}</b> ${esc(x.take||'')}</div>`).join('');
  const sk=e.skeptic?`<div class="bdrow skept">🧐 <b>${esc(e.skeptic.name||'The Skeptic')}</b> `+
    `${esc(e.skeptic.take||e.skeptic.rationale||'')} ${_vchip(e.skeptic.verdict)}</div>`:'';
  const tail=(e.consensus?`<div class=bdrow><b>Consensus</b> ${esc(e.consensus)}</div>`:'')+
    (e.conflicts?`<div class=bdrow><b>Clash</b> ${esc(e.conflicts)}</div>`:'');
  return `<span class=bt>${esc(e.title||'Board review')}</span>${_vchip(e.verdict)}`+
    `<details${open?' open':''}><summary>${(e.directors||[]).length||''} takes + the skeptic</summary>${dirs}${sk}${tail}</details>`;
}
function _stressCard(r){
  const sm=r.summary||{};
  const rows=(r.assessments||[]).map(a=>{
    const ev=(a.evidence||[]).map(x=>`<a href="${esc(x.url||'')}" target=_blank rel=noopener>${esc((x.tier||'src').toLowerCase())}</a>`).join(' ');
    return `<div class="sline ${esc(a.verdict||'')}"><b>${esc(a.verdict||'')}</b> ${esc(a.assumption||'')}`+
      (a.why?`<div class=why>${esc(a.why)}</div>`:'')+(ev?`<div class=why>${ev}</div>`:'')+`</div>`;}).join('');
  return `<span class=bt>🧪 Assumption stress-test</span>`+
    `<div class=stressbar>${sm.broken||0} broken · ${sm.weakened||0} weakened · ${sm.survives||0} survived</div>`+
    `<details><summary>the assessments</summary>${rows}</details>`;
}
// Paint the stack + the seats row into whichever board surface is live. Returns true if BQUERY
// matched anything — same relevance signal contract as renderResearch.
function renderBoard(){
  const items=boardItems();
  const qt=_rterms(BQUERY);
  const scored=items.map((it,i)=>({it,i,s:qt.length?_rscore(qt,{text:_btext(it)}):0}));
  if(qt.length)scored.sort((a,b)=>b.s-a.s||a.i-b.i);
  const anyHit=scored.some(x=>x.s>0);
  const html=items.length?scored.map(({it,s},idx)=>
    `<div class="brow${s>0?' hit':(qt.length&&anyHit?' dim':'')}">${_bcard(it,idx===0&&!qt.length)}</div>`).join('')
    :'<p class=thinking>No convenes yet — ask the board anything and the room fills up. '+
     'Every review sticks to the step it judged.</p>';
  ['blist','blist2'].forEach(id=>{ const el=$(id); if(el)el.innerHTML=html; if(el)el.scrollTop=0; });
  renderSeats();
  return anyHit;
}
// ── The bench: seated directors as toggle chips (standing archetypes + forged customs) ──
function _benchCatalog(){
  const customs=(S&&S.customDirectors)||[];
  return (CFG.archetypes||[]).map(a=>({key:a.key,name:a.name||a.key,custom:false}))
    .concat(customs.map(c=>({key:c.key,name:c.name||c.key,custom:true})));
}
function _benchInit(){ if(BOARD_PICK)return;
  BOARD_PICK=new Set((S&&S.directors&&S.directors.length?S.directors:CFG.defaultBoard)||[]); }
function renderSeats(){
  _benchInit();
  const chips=_benchCatalog().map(p=>
    `<button type=button class="bseat${BOARD_PICK.has(p.key)?' on':''}${p.custom?' custom':''}" `+
    `onclick="seatToggle('${esc(p.key)}')" title="${p.custom?'Custom-forged director':'Standing archetype'}">`+
    `${esc(p.name)}</button>`).join('');
  ['bseats','bseats2'].forEach(id=>{ const el=$(id); if(el)el.innerHTML=chips; });
}
function seatToggle(key){
  _benchInit();
  if(BOARD_PICK.has(key)){
    if(BOARD_PICK.size<=1){ toast('The board needs at least one seat.','err'); return; }
    BOARD_PICK.delete(key);
  } else BOARD_PICK.add(key);
  renderSeats();
  toast('Bench updated — it rides the next convene.');
}
// ── ⚒ Director Forge: describe → forge (draft persona) → seat. Pro / own-key feature; the server
// gates it and we surface the upgrade copy instead of a dead button. ──
function forgeModal(){
  if(!SID){ toast('Start a plan first.','err'); return; }
  FORGED=null;
  $('modal-body').innerHTML=
    `<p class=muted>Describe the director you want at the table — a domain, a temperament, who they `+
    `fight for. FILG forges a persona; nothing is seated until you approve.</p>`+
    `<textarea id=forgedesc rows=3 placeholder="e.g. a grizzled dental-practice office manager who has seen every vendor pitch and cares only about no-show rates…"></textarea>`+
    `<div class=err id=forgeerr></div><div id=forgeout></div>`;
  $('modal-acts').innerHTML=`<button onclick="closeModal()">Cancel</button>`+
    `<button class=primary id=forgego onclick="forgeGo()">⚒ Forge</button>`;
  openModal('Forge a director');
  setTimeout(()=>{const i=$('forgedesc');if(i)i.focus();},40);
}
async function forgeGo(){
  const desc=(($('forgedesc')||{}).value||'').trim(), er=$('forgeerr'), btn=$('forgego');
  if(desc.length<4){ er.textContent='Describe the director you want.'; return; }
  er.textContent=''; btn.disabled=true; btn.textContent='Forging…';
  const {ok,d}=await api('POST',`/api/plan/${SID}/director/forge`,{description:desc});
  btn.disabled=false; btn.textContent='⚒ Forge';
  if(!ok){
    if(d&&d.upgrade){ er.textContent=d.error||'Forging is a Pro feature.'; return; }
    if(gateV2(d))return;
    er.textContent=(d&&d.error)||'The forge misfired — try again.'; return;
  }
  FORGED=d.persona||null;
  if(!FORGED){ er.textContent='The forge came back empty — try rewording.'; return; }
  $('forgeout').innerHTML=`<div class=forgecard><b>${esc(FORGED.name||'')}</b>`+
    (FORGED.first?` <span class=muted>· goes by ${esc(FORGED.first)}</span>`:'')+
    `<p style="margin:6px 0 0">${esc(FORGED.blurb||'')}</p>`+
    ((FORGED.domains||[]).length?`<div class=doms>Owns: ${esc((FORGED.domains||[]).join(', '))}</div>`:'')+`</div>`;
  $('modal-acts').innerHTML=`<button onclick="forgeModal()">↻ Different one</button>`+
    `<button class=primary onclick="forgeSeat()">Seat them</button>`;
}
async function forgeSeat(){
  if(!FORGED)return;
  const {ok,d}=await api('POST',`/api/plan/${SID}/director/save`,{persona:FORGED});
  if(!ok){ toast((d&&d.error)||'Could not seat them.','err'); return; }
  const name=FORGED.name; FORGED=null; closeModal();
  if(d&&d.id){ S=d; BOARD_PICK=null; }   // the save returns fresh plan state — adopt it, re-init the bench
  renderBoard();
  toast(name+' has a seat. ✓');
  chatStatus(name+' joined the board.');
}
// ── 🧪 Assumption stress-test: kick, poll, land the card in the stack + a chat summary ──
async function stressGo(){
  if(!SID){ toast('Start a plan first.','err'); return; }
  if(STRESS_ON){ toast('A stress-test is already running.'); return; }
  const {ok,d}=await api('POST',`/api/plan/${SID}/stress-test`,{});
  if(!ok){
    if(d&&d.upgrade){ toast(d.error||'The stress-test is a Pro feature.','err'); return; }
    if(d&&d.running){ STRESS_ON=true; stressPoll(); return; }   // rejoin one already in flight
    if(gateV2(d))return;
    toast((d&&d.error)||'Could not start the stress-test.','err'); return;
  }
  STRESS_ON=true;
  chatStatus('🧪 Stress-testing the assumptions under your plan…');
  stressPoll();
}
async function stressPoll(){
  const sid=SID;
  const {ok,d}=await api('GET',`/api/plan/${sid}/stress-test`);
  if(sid!==SID)return;   // the plan changed under the poll — drop it
  if(!ok||!d){ STRESS_ON=false; return; }
  if(d.status==='running'){ setTimeout(stressPoll,1200); return; }
  STRESS_ON=false;
  if(d.status==='error'){ toast(d.error||'The stress-test failed.','err'); return; }
  if(d.status==='done'&&d.result){
    if(S)S.skeptic={status:'done',result:d.result};
    const sm=(d.result.summary)||{};
    chatBot(`Stress-test done: ${sm.broken||0} broken · ${sm.weakened||0} weakened · `+
      `${sm.survives||0} survived. The full read is in the board room.`);
    if(BMODE)renderBoard();
  }
}

// ── HELP — a banner above the chat (the split mechanism), not a drawer. The static how-to plus
// the user's OWN past help Q&As as a personal FAQ: the question is the row, expand for the answer
// (just that Q+A, never the full chat history). FAQ lives in localStorage — help is product-level,
// not plan-level, so it follows the browser across plans. ──
const HELP_BLURB='Type your idea on the landing page, then watch it spread into a few directions, '+
  'merge the ones you like, and research + build the plan. The prompt box always wins: steer, jump '+
  'ahead, or start over from it anytime. It runs on us to start.';
let HMODE=false;
function enterHelp(){ HMODE=true; applyHmode(); renderHelp(); }
function exitHelp(quiet){
  if(!HMODE)return;
  HMODE=false; applyHmode();
  MODE='build';
  document.querySelectorAll('#modechips .mchip').forEach(b=>b.classList.toggle('on',b.dataset.mode==='build'));
  $('ws-wrap').className='promptwrap';
  if(!quiet)chatStatus('Back to build mode.');
}
function applyHmode(){
  const L=$('left'), pane=$('hpane');
  if(!L||!pane){ if(HMODE){ HMODE=false; toast('This page is stale — hard-refresh (⌘⇧R) to load the help surface.','err'); }
    if(L)L.classList.remove('hsplit'); return; }
  L.classList.toggle('hsplit',HMODE);
  pane.setAttribute('aria-hidden',String(!HMODE));
  applySplitSize(pane,HMODE);
  if(!HMODE){ const log=$('chatlog'); if(log)log.scrollTop=log.scrollHeight; }
}
function helpFaq(){ try{return JSON.parse(localStorage.getItem('filg_help_faq')||'[]');}catch(e){return [];} }
function faqPush(q,a){
  if(!q||!a)return;
  const f=helpFaq().filter(x=>x.q!==q);   // re-asking replaces the old answer
  f.push({q:q.slice(0,200),a:a.slice(0,1200),t:Date.now()});
  try{localStorage.setItem('filg_help_faq',JSON.stringify(f.slice(-20)));}catch(e){}
  if(HMODE)renderHelp();
}
function faqClear(){ try{localStorage.removeItem('filg_help_faq');}catch(e){} renderHelp(); }
// The disclaimer (ported from v1): one modal, linked from the landing + the help banner.
function disclaimerModal(){
  $('modal-body').innerHTML=
    `<p>This is just for fun. Do your research, and talk to your lawyer, your family, or your local `+
    `deity before you put any real time or money into a new business.</p>`+
    `<p><b>AI is great at being confidently wrong.</b> It will hand you a polished, sure-sounding `+
    `plan whether or not the idea holds up. Treat everything here as a starting point to `+
    `pressure-test, not as advice.</p>`+
    `<p>People have talked themselves into real trouble taking a chatbot too seriously. A few reads:</p>`+
    `<ul><li><a href="https://www.google.com/search?q=%22AI+psychosis%22+chatbot+case+studies" `+
    `target=_blank rel=noopener>Reported cases of “AI psychosis”</a></li>`+
    `<li><a href="https://www.google.com/search?q=chatbot+reinforcing+delusions+mental+health" `+
    `target=_blank rel=noopener>How chatbots can reinforce delusions</a></li></ul>`;
  $('modal-acts').innerHTML='<button class=primary onclick="closeModal()">Got it</button>';
  openModal("Just so we're clear");
}
function renderHelp(){
  const el=$('hlist'); if(!el)return;
  const faq=helpFaq().slice().reverse();   // newest question first
  el.innerHTML=`<div class=hblurb>${esc(HELP_BLURB)} `+
    `<button type=button class=fineprint style="display:inline;padding:0" onclick="disclaimerModal()">Just so we're clear ›</button></div>`+
    (faq.length?`<div class=hfaqhead><span class=eyebrow>Your questions</span>`+
      `<button type=button class="ghost small" onclick="faqClear()">Clear</button></div>`+
      faq.map(x=>`<details class=hfaq><summary>${esc(x.q)}</summary>`+
        `<div class=hfaqa>${mdToHtml(x.a)}</div></details>`).join('')
      :`<p class=thinking>Ask anything about using FILG — your questions collect here as a personal FAQ.</p>`);
}

// ── SUMMARY — the in-voice idea summary (v1's top-of-page block), as a display state. The pane
// leads with the FILG-voice read (the vet's spoken reaction + verdict), then the offer prose. The
// chat underneath is the same one chat — ask anything; build intents exit the display like the
// other modes. ──
let SMODE=false;
function enterSummary(){ SMODE=true; applySmode(); renderSummary(); }
function exitSummary(quiet){
  if(!SMODE)return;
  SMODE=false; applySmode();
  MODE='build';
  document.querySelectorAll('#modechips .mchip').forEach(b=>b.classList.toggle('on',b.dataset.mode==='build'));
  $('ws-wrap').className='promptwrap';
  if(!quiet)chatStatus('Back to build mode.');
}
function applySmode(){
  const L=$('left'), pane=$('spane');
  if(!L||!pane){ if(SMODE){ SMODE=false; toast('This page is stale — hard-refresh (⌘⇧R) to load the summary surface.','err'); }
    if(L)L.classList.remove('ssplit'); return; }
  L.classList.toggle('ssplit',SMODE);
  pane.setAttribute('aria-hidden',String(!SMODE));
  applySplitSize(pane,SMODE);
  if(!SMODE){ const log=$('chatlog'); if(log)log.scrollTop=log.scrollHeight; }
}
function summaryHtml(){
  if(!S)return '<p class=thinking>Start a plan and your idea\'s summary lives here.</p>';
  const v=S.vetting||{}, sh=S.shaped||{};
  const prose=(S.research&&S.research.prose)
    ||(S.activeNode&&S.activeNode.research&&S.activeNode.research.prose)||{};
  const MT={'full-time':'Full time','side-hustle':'Side hustle','seasonal':'Seasonal',
            'one-shot':'One shot','gig':'Gig','scalable':'Scalable'};
  const chips=(v.verdict?`<span class="verdict ${esc(v.verdict)}">${esc(v.verdict)}</span>`:'')+
    (v.model_type&&MT[v.model_type]?`<span class=mold>${esc(MT[v.model_type])}</span>`:'');
  const react=v.reaction?`<p class=sumreact>${esc(v.reaction)}</p>`:'';   // the FILG voice leads
  const rows=[["What you'd sell",prose.offer],["How you'd sell it",prose.gtm],
              ['Your edge',sh.founder_edge],['Biggest risk',v.biggest_risk],
              ['Cheapest first test',v.first_test]]
    .filter(r=>r[1]).map(r=>`<p><b>${esc(r[0])}:</b> ${esc(r[1])}</p>`).join('');
  const title=prose.title||sh.thesis||(S.activeNode&&S.activeNode.thesis)||S.idea||'';
  if(!v.reaction&&!prose.offer&&!title)
    return '<p class=thinking>No summary yet — it lands with the first graded read (merge a direction, or build the plan).</p>';
  return `<div class=sumcard>${chips}${title?`<h3 style="margin:6px 0 0">${esc(title)}</h3>`:''}${react}`+
    `<p class=thinking style="margin:2px 0 8px">Your offer with the research graded — vendor spin labeled, not laundered.</p>`+
    `${rows}</div>`;
}
function renderSummary(){ const el=$('slist'); if(el)el.innerHTML=summaryHtml(); }

// ═══ THE ACCOUNT SURFACE — projects / files / API key / account, ported from v1 ═══
// A full overlay over the workspace with real routes (/v2/account/<tab>) so refresh, bookmarks, and
// Stripe returns land on the right tab. Same endpoints as v1 (/api/plans, /api/key, /api/me).
const ACCT_TABS=[['projects','Projects'],['files','My files'],['api','API key'],['account','Account']];
const ACCT_SLUG={projects:'plans',files:'files',api:'api-config',account:'settings'};
const ACCT_FROM_SLUG={plans:'projects',files:'files','api-config':'api',settings:'account'};
let ACCT_TAB='projects', ACCT_PD=null;
async function openAccount(tab,replace){
  if(CFG.authEnabled&&!signedIn()){ authModal(); return; }
  if(tab)ACCT_TAB=tab;
  const url='/account/'+(ACCT_SLUG[ACCT_TAB]||'plans');
  if(location.pathname!==url){ const st={acct:ACCT_TAB};
    if(replace)history.replaceState(st,'',url); else history.pushState(st,'',url); }
  const {ok,d}=await api('GET','/api/plans');
  ACCT_PD=ok?d:{plans:[],total:7,email:(ME&&ME.email)||''};
  await loadMe(); paintIdentity();
  renderAccount();
  $('account').hidden=false;
}
function closeAccount(){
  const a=$('account'); if(!a||a.hidden)return;
  a.hidden=true;
  if(/^\/(v2\/)?account/.test(location.pathname))
    history.pushState({},'',SID?'/plan/'+SID:'/');
}
function acctTab(t){ ACCT_TAB=t;
  const url='/account/'+(ACCT_SLUG[t]||'plans');
  if(location.pathname!==url)history.pushState({acct:t},'',url);
  renderAccount(); }
function _planCard(p,total){
  const meta=p.done?`Finished · ${total} parts`:(p.status==='researching'?'Researching…':`In progress · part ${(p.step||0)+1} of ${total}`);
  const pill=p.done?'done':(p.status==='researching'?'WIP':((p.step||0)+1)+'/'+total);
  return `<div class=pcard><div><div class=idea>${esc((p.idea||'Untitled').slice(0,90))}</div>`+
    `<div class=meta>${meta} · ${esc(new Date(p.created_at).toLocaleDateString())}</div></div>`+
    `<div class=act><span class="pill${p.done?' done':''}">${pill}</span>`+
    `<button class=primary onclick="acctOpen('${p.id}')">${p.done?'Open / iterate':'Resume'}</button>`+
    `<button onclick="acctShare('${p.id}')">${p.shared?'🔗 Shared':'Share'}</button>`+
    `<button onclick="acctDelete('${p.id}')">Delete</button></div></div>`;
}
function _fileCard(p){
  const acts=[];
  if(p.done&&p.pdf_unlocked)acts.push(`<button onclick="acctPdf('${p.id}')">⬇ Clean PDF</button>`);
  if(p.done)acts.push(`<button onclick="acctZip('${p.id}')">⬇ Raw files (.zip)</button>`);
  // get-your-data-out, at ANY stage: the plain-text dump + the paste-into-any-LLM handoff prompt
  acts.push(`<button onclick="acctExport('${p.id}')">⬇ Export (.txt)</button>`);
  acts.push(`<button onclick="acctPrompt('${p.id}')">📋 LLM prompt</button>`);
  return `<div class=pcard><div><div class=idea>${esc((p.idea||'Untitled').slice(0,90))}</div>`+
    `<div class=meta>${p.done?'Finished':'In progress'} · ${esc(new Date(p.created_at).toLocaleDateString())}</div></div>`+
    `<div class=act>${acts.join('')}</div></div>`;
}
async function acctExport(id){   // the free plain-text dump of everything so far — works mid-build
  let r; try{ r=await fetch(`/api/plan/${id}/export.txt`,{headers:authHeaders()}); }catch(e){ toast('Network hiccup.','err'); return; }
  if(!r.ok){ toast('Nothing to export yet — run the first step first.','err'); return; }
  const blob=await r.blob(); const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download='filg-export.txt';
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href),4000);
}
async function acctPrompt(id){   // the LLM-handoff prompt: paste into any model, continue from here
  let r; try{ r=await fetch(`/api/plan/${id}/handoff.txt`,{headers:authHeaders()}); }catch(e){ toast('Network hiccup.','err'); return; }
  if(!r.ok){ toast('Nothing to hand off yet — run the first step first.','err'); return; }
  const t=await r.text();
  try{ await navigator.clipboard.writeText(t); toast('Prompt copied — paste it into any AI to continue. ✓'); }
  catch(e){ toast('Could not copy — downloading instead.','err');
    const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([t])); a.download='filg-handoff-prompt.txt';
    document.body.appendChild(a); a.click(); a.remove(); }
}
function _acctPlanBlock(){
  if(curTier()){
    const manage=CFG.subEnabled?'<div class=prow><button onclick=pricingModal()>Change plan</button><button onclick=manageBilling()>Manage / cancel</button></div>':'';
    return `<p class=pnote>You're on <b>${esc((ME&&ME.tier_label)||'your plan')}</b> — every feature, runs on our key, unlimited clean PDFs.</p>`+subMeterHtml()+manage;
  }
  const byok=HAS_KEY?`<p class=pnote>You're on <b>BYOK</b> (free) — your own <b>${esc((KEY_META&&KEY_META.provider)||'')}</b> key, unlimited. The clean PDF is ${pdfPriceStr()} per plan.</p>`
    :`<p class=pnote>Free on your own API key (BYOK), or subscribe monthly to run on our key — no API key needed, clean PDFs included.</p>`;
  return byok+(CFG.subEnabled||tiersCat().length?'<div class=prow><button onclick=pricingModal()>See plans</button></div>':'');
}
function renderAccount(){
  const pd=ACCT_PD||{plans:[],total:7,email:''}, total=pd.total||7;
  const tabbar=ACCT_TABS.map(([k,l])=>`<button type=button class="${ACCT_TAB===k?'on':''}" onclick="acctTab('${k}')">${l}</button>`).join('');
  let body='';
  if(ACCT_TAB==='projects'){
    body=(pd.plans||[]).length?pd.plans.map(p=>_planCard(p,total)).join('')
      :'<p class=acctempty>No projects yet — build your first one.</p>';
  }else if(ACCT_TAB==='files'){
    body=`<p class=pnote style="margin:0 0 10px">Everything you've built is yours, free, at any point: the plain-text export and the LLM handoff prompt work mid-build; the raw .zip lands when a plan finishes; the clean PDF shows once a plan is unlocked (or on any subscription).</p>`+
      ((pd.plans||[]).length?pd.plans.map(_fileCard).join(''):'<p class=acctempty>Nothing here yet.</p>');
  }else if(ACCT_TAB==='api'){
    body=!BYOK_ON?'<p class=pnote>Bring-your-own-key isn\'t enabled here.</p>'
      :(HAS_KEY?`<div class=acct-block><div class=acct-lbl>Your key</div><p class=pnote>Running on your own <b>${esc(KEY_META.provider)}</b> key (••••${esc(KEY_META.last4||'')}).</p>`+
          `<div class=prow><button onclick=keyForm()>Replace key</button><button onclick=removeKey()>Remove key</button></div></div>`
        :`<div class=acct-block><div class=acct-lbl>Your key</div><p class=pnote>No key yet. Add your own OpenRouter or Anthropic key to build free, every model crew, every feature.</p>`+
          `<div class=prow><button onclick=keyForm()>Add a key</button></div></div>`);
  }else{
    body=`<div class=acct-block><div class=acct-lbl>Contact</div><p class=pnote>${esc(pd.email||(ME&&ME.email)||'')}</p></div>`+
      `<div class=acct-block><div class=acct-lbl>Plan</div>${_acctPlanBlock()}</div>`+
      `<div class=acct-block><div class=acct-lbl>Session</div><div class=prow><button onclick=signout()>Sign out</button></div></div>`+
      `<div class=acct-block><div class=acct-lbl>Danger zone</div><p class=pnote>Permanently delete your account, all projects, your key, and purchase history.</p>`+
      `<div class=prow><button class=danger onclick=acctDeleteAccount()>Delete account</button></div></div>`;
  }
  $('account').innerHTML=`<div class=acctwrap>`+
    `<div class=accttop><h2>Account</h2><button class=link onclick=closeAccount()>← Back to building</button></div>`+
    `<div class=accttabs role=tablist>${tabbar}</div>${body}</div>`;
}
// A styled modal confirm — no native dialogs, the standing UX rule. Resolves true on the go label.
function uiConfirm(title,text,goLabel){return new Promise(res=>{
  $('modal-body').innerHTML=`<p class=muted>${esc(text)}</p>`;
  $('modal-acts').innerHTML='<button id=uic-no>Cancel</button>'+
    `<button class=primary id=uic-go>${esc(goLabel||'Go')}</button>`;
  openModal(title);
  $('uic-no').onclick=()=>{ closeModal(); res(false); };
  $('uic-go').onclick=()=>{ closeModal(); res(true); };
});}
function acctOpen(id){ location.href='/plan/'+id; }   // full boot restores graph + docs + chat
async function acctShare(id){
  const {ok,d}=await api('POST',`/api/plan/${id}/share`,{shared:true});
  if(!ok){ toast((d&&d.error)||'Could not share.','err'); return; }
  try{ await navigator.clipboard.writeText(d.url); toast('🔗 Share link copied.'); }
  catch(e){ toast('Share link: '+d.url); }
  openAccount();   // refresh the chip
}
async function acctDelete(id){
  if(!await uiConfirm('Delete this plan?','This permanently removes the plan. It cannot be undone.','Delete'))return;
  const {ok,d}=await api('POST',`/api/plan/${id}/delete`,{});
  if(!ok){ toast((d&&d.error)||'Could not delete.','err'); return; }
  toast('Plan deleted.'); openAccount();
}
async function acctDeleteAccount(){
  if(!await uiConfirm('Delete your account?','This permanently deletes your account, all projects, your saved key, and purchase history. It cannot be undone.','Delete everything'))return;
  const {ok}=await api('DELETE','/api/account');
  if(!ok){ toast('Could not delete your account.','err'); return; }
  toast('Your account and all its data were deleted.');
  await signout(); location.href='/v2';
}
async function acctPdf(id){
  toast('Building the PDF…');
  let r; try{ r=await fetch(`/api/plan/${id}/plan.pdf`,{headers:authHeaders()}); }catch(e){ toast('Network hiccup.','err'); return; }
  if(!r.ok){ let d={}; try{d=await r.json();}catch(e){} toast((d&&d.error)||'Could not build the PDF.','err'); return; }
  const blob=await r.blob(); const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download='filg-business-plan.pdf';
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href),4000);
}
async function acctZip(id){
  let r; try{ r=await fetch(`/api/plan/${id}/download`,{headers:authHeaders()}); }catch(e){ toast('Network hiccup.','err'); return; }
  if(!r.ok){ toast('Could not download.','err'); return; }
  const blob=await r.blob(); const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download='filg-business-plan.zip';
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href),4000);
}
function downloadZip(){ if(SID)acctZip(SID); }   // the done off-ramp uses the same authed fetch

// ── v2 routing: /v2 · /v2/plan/{id} · /v2/account/<tab>, back/forward safe ──
function routeV2(){
  const path=location.pathname||'';
  let m=path.match(/^\/(?:v2\/)?account(?:\/([a-z-]+))?\/?$/i);   // legacy /v2 paths still route
  if(m){ openAccount(ACCT_FROM_SLUG[(m[1]||'').toLowerCase()]||'projects',true); return; }
  const a=$('account'); if(a)a.hidden=true;
  m=path.match(/^\/(?:v2\/)?plan\/([A-Za-z0-9]+)/);
  if(m&&m[1]&&(!S||S.id!==m[1]))restorePlan(m[1]).then(_afterBootQuery);
  else _afterBootQuery();
}
function _afterBootQuery(){   // back from Stripe: ?pdf=1 (clean PDF unlocked) / ?sub=1 (tier live)
  const q=new URLSearchParams(location.search);
  if(q.get('pdf')){ history.replaceState(history.state,'',location.pathname);
    chatBot('🎉 Payment received — the clean PDF for this plan is unlocked. Downloading it now; re-downloads are free.');
    if(SID)downloadPdf(); }
  if(q.get('sub')){ history.replaceState(history.state,'',location.pathname);
    loadMe().then(paintIdentity); toast('🎉 Your subscription is live.'); }
  if(q.get('sub_canceled')||q.get('pdf_canceled')){ history.replaceState(history.state,'',location.pathname);
    toast('Checkout canceled — no charge.'); }
}
window.addEventListener('popstate',routeV2);

// ── boot ─────────────────────────────────────────────────────────────────────
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();unfocus();if(PIVOT_FROM)clearGhost();
  if(NAVCUR){NAVCUR=null;if(VIEWMODE==='graph')renderGraph();}}});
document.addEventListener('keydown',graphKeys);   // graph wayfinding: arrows/Enter/F/+/−//
// Enter submits (Shift+Enter for a newline) — sendPrompt handles full mode as the landing submit
document.addEventListener('keydown',e=>{
  if(e.key!=='Enter'||e.shiftKey)return;
  if(e.target&&e.target.id==='ws-box'){ e.preventDefault(); sendPrompt(); }
});
initGraphInput();
initMinimap(); initTrail();   // graph chrome: click-to-jump minimap + hover wayfinding trail
initGrips();   // the split boundary drags; the height is remembered across modes + reloads
renderStackChips();   // the model-crew chip
sendLabel();   // 'Start →' in full mode, 'Send →' once a plan exists
initAuth();   // session + /api/me + key state, then routeV2 (deep links + /v2/account tabs)
// the working node's elapsed clock — keeps the build feeling alive even between progress lines
setInterval(()=>{const el=$('wiptime');if(el&&WIP_T0)el.textContent=Math.round((Date.now()-WIP_T0)/1000)+'s';},1000);
