---
status:
  doc: vet
  verdict: PURSUE (conditional)
  last_touched: 2026-06-12
---

# Idea → Offer Engine — Vet (kill-gate)

Done in the spirit of `advising-startups` (skill not loadable in this cloud clone — empty profile
submodule). Grounded in the brief, research, two dogfood runs, and the working prototype. The job
here is to be the adversary, not the cheerleader.

## Verdict: PURSUE — conditional, with explicit kill criteria

Not an unconditional green light. The idea clears the bar to keep going, but the thing that would
kill it is still unproven, so investment past a thin validation is gated on the tests below.

> **Update 2026-06-12 — cost kill-gate RESOLVED (PASS).** Test #1
> (`prototype/pipeline_economics.py`, `prototype/test_01_results.md`) modeled $/run off real token
> usage: **~$0.21/full run** on lean routing (Haiku fan-out + Sonnet synthesis); a $39/mo sub
> covers ~186 idea-runs/mo; free-tier signup ~$0.21. Margin is comfortable and is **not** what
> kills this. The remaining open number: **post-gate survival quality** (after re-sourcing the
> flagged 67%), which needs a live pipeline run.

## Scorecard
| Lens | Read | Confidence |
|---|---|---|
| Problem real & painful | Yes — founders get stuck idea→sellable-offer; existing tools stop at validation | High |
| Wedge clear | Yes — own the offer→GTM→delivery layer + cited/verified research | High |
| Moat durable | **Partial** — see below; the cited-research feature is copyable, the method+flywheel+distribution less so | Medium |
| Market | Adequate — business-planning SW $2.25B→$5.5B; solo-founder buyer doubling; not a hypergrowth TAM | Medium |
| Competition | Crowded but bimodal — shallow validators below us, funded "AI runs your company" (Audos $11.5M, Cofounder $8.7M) above; our lane is the gap | Medium |
| GTM | Strong — product *is* the content engine; Sam has build-in-public + YouTube already | High |
| Unit economics | **Unproven** — deep research is token-heavy; can a $39/mo self-serve tier carry it? | Low |
| Founder-fit | Strong — Sam built FRI with this exact method; has the niche, the content motion, the dogfood flywheel | High |

## Where it's strong
- **Proven by construction.** The method shipped a real business (FRI) and is incubating these
  ideas. That's a receipt nobody in the category has.
- **The wedge is the part everyone skips**, confirmed by research: validators stop at "is this a
  good idea"; this gets to "here's what you sell and how."
- **The moat has a working demonstrator.** The prototype catches self-interested sources a smart
  founder + ChatGPT would miss (it caught stats *I* missed). That's a concrete, demonstrable edge.
- **Product = distribution.** The deep-research engine is also the programmatic-content factory;
  GTM and product share one engine. Rare and efficient.

## Where it's weak (the honest part)
1. **The moat's most marketable feature is copyable.** "Cited + verified research" is a strong
   wedge *today*, but a funded competitor can build a source-credibility gate too. The *durable*
   moat is the softer stuff — the proven method, the accumulating per-vertical playbook library,
   the dogfood flywheel, and Sam's distribution. Underwrite the durable moat, don't bet on the
   feature staying unique.
2. **Unit economics are the real kill risk.** Both dogfoods were human-steered and used ~3 quality
   searches + synthesis each. Unattended, at quality, with the verify gate, on a generous free
   tier — is the per-run token cost low enough that $39/mo has margin? **Unknown. This is the
   thing that kills it if it's going to die.**
3. **Self-serve completion.** Founders abandon. The genuine value may live in the higher-touch
   "ongoing development" tier — which pulls toward services, the exact thing v1 deprioritized.
   Risk: the self-serve free tier becomes expensive lead-gen for a service we said we wouldn't run.
4. **Low-trust category.** "AI builds your business" reads as snake oil. Mitigated by the FRI
   receipt + visible sources, but it raises CAC and lengthens trust-building.

## Kill / pivot criteria (decide against these, not vibes)
- **KILL if** the unattended pipeline can't produce gated, sellable-quality artifacts at a per-run
  cost that leaves margin on a ~$39/mo tier (test #1 below). This is the load-bearing assumption.
- **KILL if** the FRI-receipt promise + concierge offer can't pull **≥3 paid commitments (pre-pay
  or a paid pilot) from ~50 real ICP conversations** in the validation sprint — means the promise
  doesn't convert to money even with the receipt. *(Revised 2026-06-13: a raw "~100 waitlist" is a
  vanity metric — ~5–10% of a free waitlist ever pays. Gate on money. Full plan + thresholds in
  `validation_strategy.md`.)*
- **PIVOT to software-assisted service if** self-serve activation is poor but the artifact quality
  is clearly worth paying for — i.e. the value is real but needs a human in the loop. (Re-opens the
  shape decision; acceptable outcome, not a failure.)

## Conditions to proceed (the thin validation, in order)
1. **Pipeline cost/quality test (the kill-gate within the kill-gate):** wire the proven stage chain
   + the gate to run unattended on one fresh prompt; measure (a) what % of claims survive the gate,
   (b) $/run. This converts the #1 unknown into a number.
2. **Demand-validation sprint:** the money-gated 6-week plan in `validation_strategy.md` (concierge
   + pre-sales, not just a waitlist). PASS = ≥3 pre-payments / a paid pilot.
3. Only then: thin MVP (Phase 3 of `launch_todo.md`).

## One-line
Real problem, real wedge, a moat with a working demo, and ideal founder-fit — gated on one
unproven number (unattended cost-at-quality). Worth the thin validation; not worth an app build
until test #1 returns a margin-positive number.
