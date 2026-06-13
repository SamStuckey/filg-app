---
status:
  stage: flesh-out
  verdict: pending
  state: active
  shape: self-serve SaaS (decided 2026-06-12)
  last_touched: 2026-06-12
---

# Idea → Offer Engine (working name) — Brief

Created: 2026-06-12. **Decisions:** shape = self-serve SaaS; v1 ICP = solo operators starting a
service / consulting / AI-automation business (narrow lane, locked 2026-06-12). Companion docs:
`research_findings.md` (cited market/competitor research), `business_plan.md` (v0.1 plan),
`launch_todo.md` (here→launch checklist). Working name only; real name TBD.

## Concept
A SaaS that takes a founder from a vague notion ("I think I can make money doing X")
all the way to a sellable product **and a way to sell it** — pitch, offer, audit/delivery
method, target customer, channel. The user types a plain-text prompt about what their
business might be; the system runs deep research on the market and the opportunity, then
generates the plans and artifacts that turn the notion into an actual product.

The output isn't "a business plan PDF." It's the working set an operator needs to start
selling: **what you're selling, who to, where, the pitch, the offer structure, and the
delivery method** — from "I have an idea" to "this is my product, here's how I sell it."

## Origin — why this is real (the dogfood story)
This is not hypothetical. Sam *already built the engine by hand* getting Front Range
Intelligence (`~/fri`) off the ground. He started with only a vague opportunity ("I can
make money as an AI consultant") and didn't know what he was selling, where, how to pitch,
or how to run the audit. He used AI to do deep market/opportunity research and ended up with
a repeatable system that produced FRI's actual product, pitch, and go-to-market.

That same system is **also already running inside this very repo** — the bizdev launchpad,
the `incubate` pipeline (spark → flesh-out → vet → tech design → roadmap), and the
`process-playbook.md`. In other words: a primitive, manual version of this product exists and
has shipped one business (FRI) and is incubating several more. The product is "productize the
thing that made FRI." That's the credibility and the moat: a method proven on a real outcome,
not a prompt wrapper guessing.

## MVP (build for the system we already have, then expose it)
A guided pipeline: plain-text idea in → structured artifact set out. Lean v1 = wrap the
existing `incubate` stages behind a simple interface and a deep-research backbone:
1. **Idea capture** — plain-text prompt → structured brief (problem, wedge, who, why now).
2. **Opportunity research** — deep research on market, competitors, demand signal.
3. **Offer definition** — what to sell, how it's packaged/priced.
4. **Go-to-market** — channel, pitch, first-customer motion.
5. **Delivery method** — the audit/playbook for actually doing the work (FRI's audit is the
   template for service businesses).

Dogfood target: a founder could have run *their own* version of FRI's spin-up through this,
faster than Sam did it by hand.

## Tiering (Sam's framing)
- **Free** — develops the basic structure: idea → brief → first-pass offer + GTM skeleton.
  Enough to be obviously useful and to hook. Generous on purpose (top of funnel).
- **Paid** — **ongoing business-development support**: iterative research, artifact
  refinement, accountability/cadence, and operator-grade deliverables as the business
  evolves. The recurring value is in the "keep developing the business," not the one-shot plan.

## How it could be built (open)
- **Deep-research backbone** is the engine — the `deep-research` harness pattern (fan-out
  search → fetch → adversarially verify → cited synthesis) is the obvious starting point.
- **Staged pipeline = the `incubate` skill, externalized.** Each stage has a contract and a
  named artifact; gates between stages so upstream changes don't waste downstream work
  (mirrors the content-workflow gating lesson from FRI).
- **Artifacts, not chat.** Output durable, editable documents (brief, offer, GTM, delivery
  playbook), versioned — closer to a notebook/workspace than a chatbot.
- **Model routing** — fan-out research/extract/classify on cheap models; reasoning/synthesis
  on a strong model (per token-optimization norms).

## Differentiation / competitive (reason-from-context, verify later)
The "AI business plan generator" space is crowded and mostly shallow — one-shot reports that
look good and do nothing. Even Ideabrowser (which Sam already uses) has known report-quality
gaps. The wedge here is **depth + the whole arc + ongoing operator support**:
- Not idea *validation* — idea → *sellable offer + GTM + delivery method*. Most tools stop at
  "is this a good idea." This one gets you to "here's the thing you sell and how."
- **Proven method** (built FRI, incubating more) vs. generic prompt scaffolding.
- **Paid tier = ongoing development**, not a frozen PDF — the durable, defensible value.
- Open question whether the wedge is self-serve SaaS or an FRI-style done-with-you service
  with software underneath. (Same wedge ambiguity flagged on creator_personas — decide later.)

## Risks & open questions
1. **"Generates plans" is easy; generating plans that *sell* is hard.** The output has to
   beat free ChatGPT prompting by a wide margin, or there's no product. The deep-research
   quality + the artifact structure are the whole game.
2. **Crowded, low-trust category.** "AI builds your business" reads as snake oil. The
   FRI-proof story is the antidote — lead with the receipt.
3. **Generality vs. depth.** FRI's method is tuned for AI/ops-audit service businesses. Does
   the engine generalize to a coffee shop or a SaaS, or is it really "spin up a services
   business like FRI"? Narrowing the ICP (e.g. solo operators starting service/consulting
   businesses) may be the honest v1.
4. **Free tier cost.** Deep research is token-heavy. A generous free tier could be expensive;
   needs a cost model (cheap-model fan-out, caching, limits).
5. **Self-serve completion rate.** Founders abandon. The paid "ongoing support" may be where
   value *and* retention actually live — maybe the free self-serve tier is just the lead gen
   for a higher-touch product.
6. **Wedge ambiguity:** pure SaaS vs. software-assisted service (FRI as the delivery arm).

## Relation to existing work
This is the **most meta idea in the launchpad** — it productizes the launchpad itself plus
FRI's spin-up method. Direct overlap with: the `incubate` skill + `process-playbook.md` (the
pipeline), the `deep-research` skill (the engine), and FRI (`~/fri`) as both the proof case
and a possible done-with-you delivery arm. Strong dogfood path: run the next bizdev idea
through the prototype and see if it beats the hand method.

## Next
Spark only — grow it with Sam. First forks to resolve before fleshing out:
- **Self-serve SaaS vs. software-assisted service?** (sets the whole shape)
- **ICP for v1** — generalize, or narrow to "spin up a solo service/consulting business"?
- **Sharpest first proof** — likely: run one real idea end-to-end through the existing
  `incubate`/`deep-research` stack and judge whether the artifact set is genuinely sellable.
Run `incubate` flesh-out → vet when the wedge + ICP are set.
