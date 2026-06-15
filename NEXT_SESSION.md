# NEXT SESSION — pick up here

## ⏭️ Quick restart (read first)
**Do first:** load the personal `personal-architecture` skill (it wasn't available in the last
session — that environment only had the project skill + defaults).

**Immediate pending task — split FILG into app + docs repos** per your paired-repo convention
(`<project>-app`/`-service`/`-site` + `<project>-docs`). Decision was left to you (3 options offered,
dismissed — waiting on you):
- A) keep `fuckitletsgo` as the app repo + add `fuckitletsgo-docs` (no rename → Render link untouched). *recommended/lowest-risk*
- B) rename → `filg-app` + create `filg-docs` (re-points the Render deploy link; verify after)
- C) rename → `fuckitletsgo-app` + `fuckitletsgo-docs` (same Render caveat)

Proposed file mapping:
- **app repo:** `app/` · `prototype/` · `landing/` · `teardowns/` · `Dockerfile` · `render.yaml` ·
  `requirements.txt` · `.env.example` · `.gitignore` · `CLAUDE.md` · `DEPLOY.md` · `README.md`
- **docs repo:** `00_brief.md` · `business_plan.md` · `validation_strategy.md` · `vet.md` ·
  `launch_todo.md` · `sprint_todos.md` · `research_findings.md` · `GRADUATION.md` · `NEXT_SESSION.md` ·
  `copy_bank.md` · `dogfood_run_01.md` · `dogfood_run_02.md`
- Constraints: Render deploys from `fuckitletsgo` (rename usually survives via repo-ID + GitHub
  redirect, but verify); a history-preserving split needs local `git` (the last env's git remote was
  scoped to one repo); the new `-docs` repo must be added to session scope before writing to it.

**State of the product (all on `main`, pushed):** app is live on Render (`filg.onrender.com`);
`fuckitletsgo.ai` live w/ TLS; `filg.ai` DNS pointed (A `@`→216.24.57.1, CNAME `www`→filg.onrender.com),
verifying. Auth+profiles, Stripe billing, and the plan-builder overhaul are BUILT but auth/billing are
OFF until Supabase/Stripe env is set (runbook: `DEPLOY.md` §4–5). Everything mock-verified; real-LLM
path untested in the cloud env (no key there). Details below.

---

> Handoff note. Last session: shipped persistence + auth/billing, **deployed the free tier live on
> Render**, pointed the domain, and scoped the monetization work. Next: build the monetization flow.

## Where things stand (deploy = DONE)
- **Live in prod:** `https://filg.onrender.com` — Render Starter web service `filg` (Oregon,
  Python 3, Blueprint-managed from `main`), 1 GB persistent disk at `/var/data`, `FILG_DB=/var/data/filg.db`.
  `/healthz` → `{"ok":true,"mock":false,"auth_enabled":false,"billing_enabled":false,"daily_budget":5.0,...}`.
- **Real runs verified in prod** (the gate works live — flagged a vendor stat, cited the neutral ones,
  ~$0.40/run). Cost is ~$7.25/mo Render hosting + ~$0.40/run Anthropic.
- **Domain:** `fuckitletsgo.ai` on Squarespace → repointed via Custom Records (`A @ → 216.24.57.1`,
  `CNAME www → filg.onrender.com`; deleted the "Squarespace Defaults" preset). Render shows
  **Verified**; TLS cert was **Issuing/Pending** when we stopped — confirm it went **Issued** and that
  `https://fuckitletsgo.ai` serves FILG (test in Incognito).
- **`filg.ai`** is added in Render but unpointed (Verification Error) — second domain, do later.
- **Auth + billing are built but OFF** (env unset). Flip on later with `SUPABASE_*` / `STRIPE_*`
  (see `.env.example`, `DEPLOY.md` §4). `FILG_PAID_EMAILS` = manual comp only.

## NEW: interactive plan-builder (built, mock-verified — needs prod test + real-LLM test)
The core loop is now a **3-pane decision-tree builder** (replaces the one-shot teardown page as `/`):
- Enter idea → left sidebar fills with **graded research** + a **live file tree** (`01_brief.md …
  06_roadmap.md`). Center shows the offer answer, then walks the plan **section by section**.
- Each node proposes a draft; operator picks **Yes, and / Okay, but / Not quite** (+ optional note).
  `not_quite` re-drafts that node; the others finalize the file and advance — files appear live.
- **Download the file tree (zip) = the paid artifact** (pay gate at the end, per Sam's spec).
- Files: `app/planner.py` (sections + mock drafts + real-LLM-per-section + decision-tree `advance()`),
  `app/store.py` `plan_sessions` table + `plan_create/get/save`, endpoints `POST /api/plan/start`,
  `GET /api/plan/{id}`, `POST /api/plan/{id}/respond`, `GET /api/plan/{id}/download` (zip).
- **Verified in mock:** full walk (research → 6 files → done), `not_quite` re-draft, download 402
  (free) vs 200 zip (signed-in paid). **NOT yet tested:** real per-section LLM synthesis (no key
  here) and the flow in prod — smoke-test on Render after deploy.
- **Follow-ups:** (a) download gate is currently subscription/allowlist — wire the **$99 one-time
  Spin-up Pack** as the real unlock (Stripe `mode=payment`); (b) `/api/plan/{id}/download` doesn't
  check session ownership (any paid user could pull any finished plan by id) — add an owner check;
  (c) per-section cost adds up in real mode — meter it (currently only research cost is recorded).
- Old teardown endpoints (`/api/run`, `/r/{id}`) kept for back-compat / existing share links.

## UX OVERHAUL v1 (built, mock-verified — needs prod + real-LLM test)
Research-backed redesign (6 web-research agents; brand picked by Sam = **"The Optimist"** — warm,
rounded, encouraging, coral `#FF6B4A` + sky `#2E7CF6` on cream). Shipped:
- **Optimist rebrand** of the whole builder; intake headline "You've got a business in you…".
- **Skeleton-first file tree** (Gamma pattern): all 6 sections show immediately as `○ pending →
  ✍︎ active → ✓ done`, fill in live. **Plain-language section names** (fixes the "structured brief"
  confusion): The setup / What you sell / What you charge / How you get customers / How you deliver /
  Your first 30 days (`planner.SECTIONS`).
- **Branch buttons open an inline input on click** (Sam's ask) with branch-specific placeholder +
  quick chips; backend already steers the LLM per branch (`planner._steer`).
- **"Ask an expert" add-on** = FILG-owned COMPOSITE ARCHETYPES only (The Closer/Bootstrapper/Brand
  Builder/Skeptical CFO), always-on "AI, not professional advice" disclaimer. **Legal: never ship
  real named people** — right-of-publicity / ELVIS Act / NO FAKES Act (Senate vote ~June 18 2026,
  ~$750k/work platform liability). Real personas only via signed license/opt-in (Delphi model).
  `planner.ask_expert` + `POST /api/plan/{id}/ask`.
- **Cost metering hardened:** `usage.record_spend()` now sends per-section drafts + add-on calls to
  the daily kill switch (per-user free cap unchanged).
- Verified in mock end-to-end (page, steered respond, expert, finish, download 402). **NOT tested:**
  real LLM (no key here) + prod. Auto-deploys to Render on push.

### Overhaul — deferred to next passes (research has the patterns)
- Real token streaming per section; select-text-in-a-section → ask (scoped edits); "Re-source this
  stat / show grade" one-click; sibling drafts on "Not quite"; per-claim grade streaming.
- Add-ons: locked-preview teasers + the **$99 one-time Spin-up Pack as the download unlock** (Stripe
  `mode=payment`); compliance-report add-on (the recorded idea); ask-expert UI is `prompt()`-based
  (replace with inline composer). Reverse-trial-flavored gate (research: post-results paywall ~3–5x).
- Download ownership check still TODO.

## PROFILES + OAUTH (built, mock-verified — needs Supabase config to go live)
Replaced "enter your email" with real accounts when Supabase is configured (email fallback stays
when it isn't, so prod keeps working until you flip it on):
- **Login**: Google OAuth (`signInWithOAuth`) + email magic-link. When `auth_enabled`, the intake is
  gated behind sign-in (typed idea persists across the OAuth redirect via localStorage).
- **Profile / "My plans"** (auth bar → "My plans"): lists the user's plans — WIP (Resume) and finished
  (Open/iterate + Download). `GET /api/plans` (auth-required) + `store.plan_list(user)`. Plans are
  keyed by the verified email.
- **Paid integrations**: stub "CRM kickstarts & more — coming soon" slot on the profile (real ones TBD).
- Verified in mock with minted JWTs (ownership filtering, 401 unauth, page wiring).
- **To activate:** set `SUPABASE_*` env in Render AND enable **Google** provider in the Supabase
  dashboard (Auth → Providers; needs a Google OAuth client + redirect `https://fuckitletsgo.ai`).
- **Note / tension:** free cap is 1 run lifetime (`FILG_FREE_RUNS=1`), so a free user's profile shows
  ≤1 plan. Revisit the free allowance alongside the monetization work (maybe free = build WIP freely,
  gate finish/download) — but watch cost (~$1–2/plan real).
- Follow-up: `/api/plan/{id}` + download still lack an ownership check (any signed-in user could load
  another's plan by id); add `WHERE user=?` enforcement.

## The monetization decision (what to build next)
Model is already locked in `business_plan.md` §9: Free (1 teardown) → **$39/mo Operator** (full
artifacts + ongoing dev + unlimited) → **$99–149 one-time Spin-up Pack** → FRI done-with-you upsell.
Stripe + the free/full split are already wired.

**Core problem named:** it's a subscription selling a one-time artifact. The offer is generated once,
so (a) there's no reason to pay month 2 until the "ongoing development" layer exists, and (b) the
shipped free tier is *more generous* than §9 intended (gives offer + GTM + full graded evidence), so
paid must sell a different axis (execution + it-evolves-with-you), not "a longer PDF."

### Prioritized plan (agreed)
1. **Close the free-tier leak (do first — small, protects unit economics).** Observed live:
   `sam.n.stuckey+1@gmail.com` reset the 1-run cap. `+aliases` and throwaway emails bypass the
   per-email cap, and **each free run is ~$0.40 of real spend** (business_plan §12: loss leader we can
   afford, not an unbounded bill). Fix: **normalize `+alias`/dots in gmail** in `usage.py`/`main.py`
   identity, and **gate the free run behind the Supabase magic-link we already built** (verified email
   before the run) — doubles as email capture. Add IP rate-limit; keep the daily kill switch.
2. **Ship the $99 one-time "Spin-up Pack" (fast revenue).** Stripe is wired — add a one-time price +
   "unlock the full artifact set once" (mode=payment checkout, grant a one-shot `full` run). Captures
   the one-and-done majority who won't subscribe. Likely out-earns the sub early.
3. **Build the recurring hook (the real SaaS arc, bigger — scope separately).** Durable, **editable
   artifact workspace** + a **"refresh research / iterate offer" button**. This is what makes $39/mo
   defensible (LTV; moat in §11). Until this exists, the subscription is a one-time with extra steps.

**Recommendation given:** do **#1 + #2 as one next PR**, then **#3** as its own arc.

## Open question to answer next session
Which to build first — `#1+#2` together (recommended), or jump to `#3`? (User was going to decide.)

## Ideas backlog (recorded for tomorrow — Sam, end of session)
Raw ideas to explore next session; not yet prioritized or designed.

1. **Live decision tree.** When you get feedback on your idea, you get a **"yes, and"** vs.
   **"okay, but"** choice, and you build a decision tree **live with Claude** — branch the idea
   interactively instead of a one-shot artifact. (Note: this is also a candidate answer to the
   "why pay month 2?" recurring-value problem — it makes the product a session you return to.)
2. **"Ask an expert."** Personas modeled on business people, podcasts, etc. — get advice in a
   specific operator's voice. (Constraint to honor: per CLAUDE.md invariant #4 we **can't
   fine-tune Claude** — so this is persona prompting + RAG over expert/podcast content, not an
   actually "trained" model. Sourcing/licensing of that content is an open question.)
3. **Compliance pushback.** Surface a **warning about compliance** (or a separate dialogue) when an
   idea has regulatory exposure, with an option to **pay for a compliance report**. (Strong fit with
   the moat — the product already *pushes back* via the source-credibility gate; "pushback as a
   feature" extends that, and the paid report is a natural monetization surface like the Spin-up Pack.)
4. **Instant fixed-price "Build my business plan" lever.** Right after the initial prompt, a quick
   one-click **fixed-price, no-subscription** purchase. (This is essentially the **$99 Spin-up Pack
   (#2 above) surfaced at the moment of intent** — high-leverage; merge this into the Spin-up Pack
   design: show the one-time buy CTA immediately, not buried.)
5. **"The worst idea that worked."** A featured gallery of **silly/stupid ideas that made a lot of
   money** (and/or things we've built) — social proof to **encourage users** that their "dumb" idea
   is worth running. (Fit: feeds the §10 programmatic-content / "idea of the day" GTM engine, lowers
   the activation barrier — "if *that* worked, mine might too" — and each entry is a shareable demo
   of the engine. Could be a curated landing/teardown surface in `landing/`.)
6. **"Uncertainty multiplier."** A **marketing-leverage index/score** based on how scared the world
   is of something going wrong — high societal fear around a domain = high opportunity score for
   building there. E.g. *"AI is taking people's jobs"* → high uncertainty → an AI app scores high.
   Backed by **historical research** (start with the Industrial Revolution → present) to build
   **cited context/proof points** that periods of fear/disruption are when entrepreneurship pays off.
   (Fit: this is on-moat — a *researched, cited* score, "every number graded," not vibes; it's the
   gate pointed at optimism instead of skepticism. Heavy research lift — flag scope. Purpose:
   **encourage entrepreneurship**, which is the brand → see Branding note below.)
7. **Learning loop — improve from user interactions.** Use real usage to evolve the product: "good
   pushback" (`not_quite`/`okay_but` notes) and "positively-weighted" signal (accepted drafts,
   downloads, expert asks) feed back in. (Constraint: per CLAUDE.md #4 we **can't fine-tune Claude** —
   so this is a *feedback flywheel*, not model training: mine winning patterns into the prompt /
   few-shot library, tune the source-credibility gate's priors, and grow the per-business-type
   playbook library (business_plan §11 moat (c)). Needs user-consent/privacy handling for using their
   data. The decision-tree already captures the exact signal — log it.)
8. **BYO API key + token-usage meter (big unit-economics lever).** Show "remaining tokens" and let a
   user paste their own API secret; their account then runs on *their* key under the hood — FILG
   becomes "a useful skin / doc store" over their own LLM spend. Applies to BOTH tiers (each has a
   token limit that BYO-key unblocks); paid may get a higher monthly allotment. (Strategic: this flips
   COGS — the token-heavy research (§12 COGS risk) — off our books onto the user, changing the model
   from reselling compute to selling the product layer. Security is load-bearing: never log keys,
   encrypt at rest per-account, scope, easy revoke. Simple paste-in UI.)
9. **Bizdev marketplace → agentic business-management platform.** Monetize by selling add-on tools
   (often white-labeled) and straight integrations — e.g. "use HubSpot CRM right here with your data
   + model." FILG is the simple hook; it sprawls (future) into an agentic platform to run the
   business, not just plan it. (Fit: extends the add-ons sidebar (#3 compliance, ask-an-expert) and
   the paid "CRM kickstarts" integrations stub already on the profile; the add-on/upsell UX research
   applies. Marketplace = rev-share/white-label on third-party tools + native integrations.)

## Branding note (Sam, end of session)
**The brand is encouraging entrepreneurship — "Entrepreneurship is for everyone."** Work this into
the positioning/voice. Today the tone is skeptical-but-fair (the gate labels vendor spin); the brand
should pair that rigor with *encouragement* — we de-risk and embolden, not just debunk. The
"uncertainty multiplier" (#6) and "worst idea that worked" (#5) are both expressions of this. Revisit
`business_plan.md` §2 positioning and `landing/` copy through this lens next session.

## Relevant files
- `app/main.py` (endpoints + free/paid gate + frontend), `app/billing.py` (Stripe), `app/auth.py`
  (Supabase JWT), `app/store.py` (SQLite: jobs + subscriptions), `prototype/usage.py` (caps).
- `prototype/teardown.py` `generate()` (free teardown) / `generate_full()` (paid full set).
- Plan: `business_plan.md` §9 (tiers), §12 (unit economics), `validation_strategy.md` (gate on money).
- Ops: `DEPLOY.md`, `.env.example`, `render.yaml`.
