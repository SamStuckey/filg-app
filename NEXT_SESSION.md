# NEXT SESSION — pick up here

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

## Relevant files
- `app/main.py` (endpoints + free/paid gate + frontend), `app/billing.py` (Stripe), `app/auth.py`
  (Supabase JWT), `app/store.py` (SQLite: jobs + subscriptions), `prototype/usage.py` (caps).
- `prototype/teardown.py` `generate()` (free teardown) / `generate_full()` (paid full set).
- Plan: `business_plan.md` §9 (tiers), §12 (unit economics), `validation_strategy.md` (gate on money).
- Ops: `DEPLOY.md`, `.env.example`, `render.yaml`.
