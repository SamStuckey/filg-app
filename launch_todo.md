---
status:
  doc: launch_todo
  last_touched: 2026-06-12
---

# Idea → Offer Engine — Concrete Launch Checklist (here → launch)

Sequenced so the cheap, kill-it-early steps come first. **Do not build the app until Phase 2 and
Phase 3 both pass** (no building before signal — launchpad rule). Each `[ ]` is a discrete task.

---

## Phase 0 — Lock the foundation (this week, ~1–2 sessions)
- [ ] Confirm v1 ICP: solo operators starting a **service / consulting / AI-automation** business.
- [ ] Pick a working name + grab the domain (don't overthink; renamable).
- [ ] Lock the v1 artifact set (the 6 outputs in `business_plan.md` §4) — exact list, no scope creep.
- [ ] Lock the free vs. paid line: free = 1 idea → brief + first-pass offer + GTM skeleton + a
      taste of cited research; paid = depth + delivery playbook + roadmap + iteration.
- [ ] Set provisional price ($39/mo, range $29–49) — to be pressure-tested, not final.
- [ ] Run `incubate` flesh-out → **vet (`advising-startups`)** for the pursue/pivot/kill verdict.

## Phase 1 — Dogfood the engine by hand (the make-or-break proof)
Goal: prove the *output* is sellable using tools we already have, before writing product code.
- [ ] Pick one real idea (a bizdev idea or a fresh one) as the test subject.
- [ ] Run it end-to-end through the existing `incubate` + `deep-research` stack by hand → produce
      the full artifact set (brief → research → offer → GTM → delivery playbook → roadmap).
- [ ] **Judge honestly:** is this set genuinely better than a sharp ChatGPT session? Is it
      *sellable*? Are research claims cited and verifiable (no hallucinated TAM)?
- [ ] Capture the exact prompt/stage chain that produced good output → this becomes the product's
      pipeline spec. Save gotchas to a `build_log.md` (git-style HEAD + append-only history).
- [ ] **GATE:** if the artifact set isn't clearly sellable, stop and fix the method (or kill). Do
      not proceed to build on a weak engine.

## Phase 2 — Demand signal before code (see `validation_strategy.md` — supersedes this section)
Full 6-week sprint, channels, messaging, and thresholds live in **`validation_strategy.md`**.
The headline correction: **gate on money, not a waitlist** (a free email is the weakest signal;
~5–10% of a waitlist ever pays). Skeleton:
- [ ] Positioning page (promise + FRI receipt + the real property-mgr sample artifact) + fake-door
      $39 pricing + a free *recurring* lead magnet (weekly cited-offer teardown), not a static form.
- [ ] 15–20 Mom Test interviews; concierge-run the engine by hand for 5–8 hand-raisers.
- [ ] Build-in-public (lead with the receipt) + seed Tier-1 communities (Ottley AAA Hub Skool,
      r/AI_Agents, AI Automation Society) with value-first teardowns.
- [ ] **GATE (PASS):** ≥3 pre-payments **or** ≥1 paid pilot from ~50 conversations, plus cold
      landing conv ≥4–5% and fake-door CTR ≥5%. **KILL** if zero paid commitments after ~50 convos.

## Phase 3 — Thin MVP build (only after Phases 1–2 pass)
Smallest thing that runs the proven pipeline for a stranger.
- [ ] Tech stack decision (suggested: Next.js + a queue/worker for long research runs + Postgres +
      Stripe; host on Railway/Vercel — match Sam's existing stack to reuse infra).
- [ ] Auth + accounts.
- [ ] Idea input (plain-text prompt) → pipeline run (port the Phase 1 stage chain).
- [ ] Deep-research backbone with **cited output + the verify step** (the differentiator).
- [ ] **Cost guardrails:** cheap-model fan-out for search/extract, cache common research, meter
      free-tier runs/depth. Measure $/run before opening the free tap.
- [ ] Artifact workspace: durable, editable, versioned documents (not a chat log).
- [ ] Free-tier gating logic (1 idea, capped depth).
- [ ] Stripe paywall for the paid tier + the "ongoing development" surface (re-run/iterate/refresh).
- [ ] Internal dogfood: re-run the Phase 1 idea through the live app; output must match by-hand quality.

## Phase 4 — Private beta
- [ ] Recruit 10–20 target-ICP founders from the waitlist + communities.
- [ ] Have each run a real idea; watch where they drop off (activation) and where output is weak.
- [ ] Instrument: signup → first run → artifact completed → return. Find the activation cliff.
- [ ] Tighten prompts/pipeline from beta output. Confirm willingness to pay (soft pre-sale).
- [ ] **GATE:** beta users say the output is worth paying for, and complete a full run unaided.

## Phase 5 — Public launch
- [ ] Turn on paid tier; finalize price from beta signal.
- [ ] Polish the programmatic content engine to publish on a cadence (the daily/weekly teardown).
- [ ] **Product Hunt launch** (prep assets, hunter, day-of plan; benchmark ~847 month-1 users).
- [ ] Coordinated push: X build-in-public recap, Reddit/Indie Hackers, YouTube build-guide episode.
- [ ] Track free→paid conversion against the 8–15% top-quartile target; iterate the gate.

## Phase 6 — Post-launch (first 90 days)
- [ ] Watch free-tier COGS vs. conversion; adjust metering.
- [ ] Build the per-business-type **playbook/template library** (the accumulating moat).
- [ ] Decide on the one-time "Spin-up Pack" tier and the FRI done-with-you upsell.
- [ ] If it's working: `incubate` graduate → spin its own repo (`~/<slug>`).

---

### Critical path (the short version)
Lock ICP/scope → **dogfood one idea by hand and prove it's sellable** → landing page + ~100
waitlist → thin MVP that runs the proven pipeline with cited research → 10–20 private beta →
Product Hunt + content engine launch. Two hard gates: **sellable output (Phase 1)** and **demand
signal (Phase 2)** before any meaningful build.

### Immediate next 3 actions
1. Confirm ICP + working name + the 6-artifact scope (Phase 0).
2. Run `incubate` flesh-out → vet for the kill-gate verdict.
3. Pick the Phase 1 dogfood idea and run it end-to-end by hand.
