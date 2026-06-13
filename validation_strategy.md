---
status:
  doc: validation_strategy
  last_touched: 2026-06-13
  supersedes: launch_todo.md Phase 2 (the thin "~100 waitlist" gate)
---

# Idea → Offer Engine — Demand-Validation Strategy

How we prove (or kill) demand **before building the app**. Driven by three deep-research passes
(June 2026): where the ICP congregates, what validation methods give real signal, and how analogous
founder-tools (IdeaBrowser, Audos, the AAA-creator ecosystem) got their first users. Sources are
cited inline; the full briefs are in this session's research.

> **The one correction this makes to the prior plan:** the vet's "pull ~100 waitlist" gate is a
> *vanity metric*. Research is unambiguous — a free-waitlist email is the weakest credible signal
> (~5–10% of a waitlist ever pays, and that drops below 10% if you make them wait 3+ months
> [getwaitlist, scalemath]). **We gate on money, not emails.** ~100 list subscribers is a *pipeline*,
> not a *pass condition*.

---

## TL;DR — the decision and the one number

- **What decides PASS/KILL:** **≥3 pre-payments OR ≥1 paid pilot** pulled from ~50 real ICP
  conversations. Everything else (signups, CTR, interviews) is supporting evidence that tells us
  *why*, not *whether*.
- **Timeline:** a focused **6-week sprint.** Realistic for ~100 qualified list subscribers in this
  niche is 6–10 weeks at $0–150 [analog teardown synthesis].
- **Budget:** **$0–150** (domain + an optional $100–150 paid micro-test). No app build.
- **Our unfair advantage:** Sam's **receipt** — he ran this exact method by hand to spin up FRI.
  Build-in-public takes most founders 6–12 months to manufacture the trust the receipt gives us on
  day one [Distribution Base, 68-app study]. Lead with it everywhere.

---

## What we're actually testing (three falsifiable hypotheses)

1. **Problem is real, urgent, budgeted.** Solo operators get genuinely stuck idea→sellable-offer and
   are *already paying* (courses, ChatGPT Plus, agencies, their own time) to get unstuck.
   → *Killed if* interviews show the pain is real but nobody's spending on it today.
2. **The wedge pulls.** "Cited research + the offer→GTM→delivery layer competitors skip" is the
   thing that makes them choose us over a free ChatGPT session or a $7k AAA course.
   → *Killed if* the receipt + cited-research promise gets shrugs, not signups.
3. **They'll pay ~$39/mo (or a pilot fee).** Self-serve, recurring.
   → *Killed if* they like the output but won't pay unless Sam's in the loop → **pivot to
   software-assisted service** (the vet's allowed pivot, not a failure).

---

## The signal ladder (we climb it; we gate at the top)

Signal strength = what the respondent pays to give it. Push prospects up this ladder fast; gate at
the money rung, not the email rung [Mom Test; getsubmarine].

| Rung | Method | Signal | Our gate uses it as… |
|---|---|---|---|
| Vibes | "cool idea!" | none | ignore |
| **Free email** | landing page / lead magnet | weak (curiosity) | pipeline, not proof |
| **Click intent** | fake-door "$39 Start" button | medium (revealed intent) | leading indicator |
| **Time** | Mom Test interview, concierge run | medium-strong | the "why" |
| **Money / usage** | pre-payment, paid pilot | **strongest** | **the PASS/KILL gate** |

---

## Explicit PASS / KILL thresholds (grounded in benchmarks)

Set so the call is made against numbers, not vibes. Benchmarks from the methods brief
[landerlab, amplitude, getwaitlist, saastr].

**PASS (proceed to thin MVP) if all of:**
- **Money:** ≥3 pre-payments **or** ≥1 paid pilot from ~50 conversations. *(This is load-bearing.)*
- Landing page: cold-traffic email conv **≥4–5%** (or **≥6%** from warm/community traffic).
- Fake-door "$39 Start" → capture CTR **≥5%** (≥10% in a targeted segment = strong green).
- Interviews: **≥40%** of qualified ICP name a *current paid workaround or active search* for this.

**KILL (or pivot) if:**
- Zero paid commitments after ~50 real conversations, **and**
- Cold landing conv <2% **and** fake-door CTR <2%, **and**
- Interviews show no urgent, budgeted pain.
- **PIVOT to software-assisted service** instead of kill if: concierge output is clearly valued and
  people *will* pay — but only with Sam in the loop (weak self-serve intent). Matches `vet.md`.

> Why money over emails: a "1,000-signup waitlist" is often only ~50–100 buyers. The 3 pre-payers
> outweigh the other 97 hand-raisers [getwaitlist, lenny].

---

## The 6-week sprint

### Week 0 — assets (2–3 days, no app)
- [ ] **Landing page** (Framer/Carrd or one Next page on Vercel): the promise + **the receipt** ("I
      used this exact method to spin up FRI — here's the output") + **a real sample artifact** (use
      the property-manager live run: `prototype/live_run_artifacts.md`).
- [ ] **Fake-door pricing:** a "Start — $39/mo" button that routes to a "Founding members — get in
      first" capture (honest "building now", not a dead end) [amplitude ethics].
- [ ] **Lead magnet = a free *recurring-value* tool, not a waitlist form.** A **weekly "cited offer
      teardown"**: each week, run `pipeline.py` on one reader-submitted or trending idea and email the
      gated, sourced offer. This is the IdeaBrowser engine (daily idea → 5,000 emails) done by hand
      [Startup Spells teardown]. The recurring reason-to-stay beats "join the waitlist."
- [ ] **Concierge offer page:** "Tell me your idea — I'll build your offer + GTM by hand, free, this
      week." (We literally run the pipeline; near-zero cost, strongest pre-product signal.)
- [ ] **Instrument:** Plausible or PostHog — visitor→capture, fake-door CTR, by source. Set up the
      build-in-public log (the `build_log.md` per-lane convention; doubles as content).

### Weeks 1–2 — problem + resonance
- [ ] **15–20 Mom Test interviews.** Ask about *past behavior and current spend*, never pitch
      [atlantaventures, koji]. Qualifier: "How do you solve this today, and what have you paid to try?"
- [ ] **Publish 2–3 cited offer teardowns** across 2–3 different niches (proves the engine
      generalizes, not just property managers). Seed them where the ICP is (below).
- [ ] **Start daily build-in-public** on X, leading with the receipt.
- [ ] Watch landing conv + fake-door CTR as the first traffic arrives.

### Weeks 3–4 — concierge + climb the ladder
- [ ] **Run the engine by hand for 5–8 hand-raisers** (concierge / Wizard-of-Oz). This is the
      strongest pre-build signal — proves people *use and value* the output [userpilot]. Cap at ~10.
- [ ] After each concierge run, **ask the money question**: "If this were a tool you ran yourself,
      $39/mo — would you start today?" Push toward a founding pre-pay.
- [ ] Coordinated traffic push into the primary channels; keep publishing.

### Weeks 5–6 — force the money
- [ ] **Open the founding-member pre-sale:** locked $39/mo founder rate (or annual pre-pay at a
      discount). Money now is the truest signal [getsubmarine, saastr].
- [ ] **Offer 1–2 paid pilots:** a one-time "done-with-you offer build" fee (concierge, priced). A
      *paid* pilot converts to product at 70%+ when it's real [saastr].
- [ ] **Optional $100–150 paid micro-test** (Audos pattern): run a cheap ad/DM test *for one of the
      generated offers* — it validates acquisition **and** doubles as proof the product works
      [PRNewswire/Audos].
- [ ] **Decision point:** evaluate against the PASS/KILL thresholds → MVP / iterate / pivot / kill.

---

## Channel playbook (ranked: reachability × ICP density)

Spend **80% of effort on Tier 1.** Promo rules matter — one mis-posted link burns the account, so
**value-first, link in comments/profile.**

**Tier 1 — dense ICP, free, reachable:**
- **Liam Ottley "AI Automation Agency Hub" (Skool, ~280k, free)** — *literally our ICP at peak pain
  ("I don't know what to sell")*. Promo is banned; earn reach by posting a free offer teardown for
  someone in the comments, then DM the engaged ones. [skool.com/ai-automation-agency-hub]
- **r/AI_Agents (~296k)** — open posting, build-in-public framing tolerated. Demo: "I turned a vague
  idea into a cited offer in 10 min — here's the output," link in a comment. *(Verify sidebar promo
  rule first.)*
- **AI Automation Society (Skool, ~120k+, free)** — same playbook, more builder-leaning.
- **r/Entrepreneur (4.8M) + r/sidehustle (3.1M)** — top-of-funnel, low density, strict promo. Pure
  value text posts only; capture via profile/comments.

**Tier 2 — high density, harder reach:** Nick Saraev's free YouTube (~400k) + r/n8n (~100k) + n8n
Discord (~80k). Answer the recurring "what do I sell / what do I charge" questions with a free
teardown.

**Tier 3 — amplifiers, not discovery:** X build-in-public (slow; our *owned* log, leans on the
receipt) and Product Hunt (saved for the MVP launch, not now — ~10% featured, near-zero without a
warm audience; needs 20–50 primed supporters first).

**The creators are also a distribution partner, not just a template** — our ICP *is* Ottley's and
Saraev's audience. A guest demo / tool feature is a high-leverage swing once we have the page up.

---

## Messaging to test

- **Lead with the receipt, every time.** "I used this to build FRI" is the trust shortcut.
- **The wedge:** *cited, verified research* (not hallucinated TAM) + the offer→GTM→delivery layer —
  show the source-credibility gate catching a vendor stat live. That's the demo that converts.
- **Founding-member framing:** scarcity + locked price, not "join a waitlist."
- **Avoid the "AI co-founder" label** — it's saturated and reads as snake oil [Cofounder teardown].
  We're "the engine that turns your idea into a *sellable offer you can act on Monday*, with receipts."

---

## What to build for validation (and what NOT to)

**Build (~$0–150):**
- Landing page (Framer/Carrd/Vercel) with receipt, real sample artifact, fake-door pricing, capture.
- The weekly-teardown mechanism — **manual**: run `pipeline.py`, send the email. (No automation yet.)
- Analytics (Plausible/PostHog) + the build-in-public log.

**Do NOT build:** the app, auth, billing, product UI, the async job infra. Concierge delivery = Sam
running `pipeline.py` by hand. We only build the app *after* this sprint passes the money gate — and
when we do, the gate runs **label-don't-chase** to keep $/run ~$0.35–0.45 (see `test_01_live_results.md`).

---

## How this could mislead us (and the guardrails)

- **Free emails over-promise** → gate on money, treat every email as a lead to convert, not a vote.
- **Concierge can mask whether the *automated* version is wanted** → after a concierge run, still
  require them to pay for *the product*, and probe self-serve intent. Cap concierge at ~10.
- **Build-in-public is slow** (6–12 mo for others) → it's the owned log + receipt amplifier, not a
  volume channel in 6 weeks. Pair it with a Tier-1 community push.
- **Single-niche demo** (property managers) → publish teardowns across 2–3 niches to show generality.
- **Community promo bans** → value-first or get nuked; verify each sidebar before linking.

---

## Upstream changes this triggers
- `launch_todo.md` Phase 2: replace the "~100 waitlist" gate with the money gate above.
- `vet.md` kill criteria: the landing-page kill criterion is re-stated as *can't get ≥3 paid
  commitments / a paid pilot*, not *can't pull 100 waitlist*.
- After PASS: graduate to its own repo + start the thin MVP (`launch_todo.md` Phase 3).
