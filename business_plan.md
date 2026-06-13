---
status:
  doc: business_plan
  stage: flesh-out
  shape: self-serve SaaS (decided 2026-06-12)
  last_touched: 2026-06-12
---

# Idea → Offer Engine — Business Plan (v0.1)

Working name TBD. Shape **locked: self-serve SaaS** (Sam, 2026-06-12). This is the flesh-out
draft, grounded in `research_findings.md`. Lean and revisable — not investor-final.

## 1. One-liner
A self-serve tool that takes a solo operator from a plain-text "I think I can make money doing
X" to the thing they actually sell and how they sell it — a cited, researched **offer + go-to-
market + delivery playbook** — and keeps developing the business with them.

## 2. Positioning
> Most AI tools tell you whether your idea is good. This one gets you to the thing you sell and
> how you sell it — with real, cited research instead of made-up market size, and it keeps
> building the business with you after day one.

Lead with the receipt: **the method already built a real business (Front Range Intelligence)**
and is incubating others. We're not guessing — we productized a method that worked.

## 3. Problem
Aspiring solo founders get stuck in the gap between "I have a notion" and "I have something to
sell." Existing tools either (a) validate the idea and drop you, or (b) generate a generic
business plan / pitch deck. None reliably produce the operator's actual working set: **what you
sell, to whom, where, the pitch, the offer/pricing, and how you deliver it** — and none keep
iterating. Output is widely complained about as shallow, hallucinated, and one-shot (see findings
§2).

## 4. Solution — what it outputs
A guided pipeline: plain-text idea in → a **durable, editable artifact set** out (a workspace,
not a chat transcript):
1. **Structured brief** — problem, wedge, who it's for, why now.
2. **Opportunity research** — market, demand signal, competitors — **every claim cited** (the
   anti-hallucination differentiator).
3. **Offer definition** — what you sell, packaging, pricing.
4. **Go-to-market** — ICP, channel, pitch, first-customer motion.
5. **Delivery playbook** — how you actually do the work (FRI's audit method is the template for
   service businesses).
6. **Execution roadmap** — week-by-week next actions ("who to talk to, what to test first").

## 5. Wedge / why we win (from findings)
- **Whole arc, not just validation** — we own the offer→GTM→delivery layer competitors skip.
- **Cited + verified research** — the deep-research + adversarial-verify harness kills the
  hallucinated-TAM complaint. This is a feature we market, not plumbing.
- **Ongoing development** — the paid tier iterates with you (memory, pivots, refreshes), beating
  one-shot reports and lifetime-deal tools on real value and LTV.
- **Proven method + dogfood** — built FRI; the same engine (`incubate` + `deep-research`) already
  runs in the bizdev launchpad. Credibility competitors can't fake.
- **Modern, service-native templates** — no 1995 manufacturing questionnaires.

## 6. ICP (v1 — LOCKED 2026-06-12, deliberately narrow)
Solo operators starting a **service / consulting / productized-service / AI-automation business**
— the lane where the method is proven and Sam can judge output quality. **Decided: start narrow;**
generalize to other business types only in v2+ once the engine + source/verify gate are solid.
(Rationale in findings §5; both dogfoods landed inside this lane.)

## 7. Market (verify figures before external use)
- Business-planning software ~$2.25–2.5B (2025) → ~$5.5B by 2035, ~8.5% CAGR.
- 44% of profitable SaaS now solo-founder-run (doubled since 2018) — the buyer is multiplying.
- Creator/solopreneur economy ~$250B+, ~20%+ growth — adjacent tailwind.
- Sustained high US new-business application volume (exact annual count TBD-verify).

## 8. Competition
| Tool | Stops at | We add |
|---|---|---|
| Ideabrowser, IdeaBuddy, ValidatorAI | idea validation | offer + GTM + delivery + iteration |
| LivePlan, Upmetrics, Venturekit | business plan / pitch deck | the sell-it layer + cited research |
| FounderPal | positioning/offer pieces | GTM execution, delivery, ongoing dev |
| Audos, Cofounder.ai | AI agents *run* the company ($8–11M funded) | enable the *human* operator (lighter, cheaper, services-native) |

Win condition: be the only self-serve tool that takes a human operator end-to-end with cited
research and keeps going.

## 9. Product tiers
- **Free** — one idea → basic structure (brief + first-pass offer + GTM skeleton), with a taste
  of cited research. Generous on purpose: top-of-funnel + the SEO/shareable artifact. Gate on
  depth + number of runs.
- **Paid (~$39/mo target; test $29–$49)** — unlimited ideas, full-depth cited research, the
  delivery playbook + execution roadmap, artifact versioning, and **ongoing development**
  (refresh research, iterate the offer, pivot support, memory).
- **Optional one-time "Spin-up Pack" (~$99–149)** — one business taken fully through, for people
  who won't subscribe (mirrors FounderPal's $199 lifetime). Test later.
- **Upsell path (not v1):** hand-off to FRI for done-with-you delivery. Keeps services revenue
  available without diluting the self-serve focus.

Pricing rationale: prosumer entry sits $9–29, mid ~$35; ongoing-support justifies the upper end.
Target top-quartile freemium conversion (8–15%); design the free/paid gate around depth + iteration.

## 10. Go-to-market
The engine that builds the product also fills the funnel:
1. **Programmatic content engine** (primary, compounding) — publish researched idea/offer
   teardowns (the "idea of the day" model). Each post is a live product demo + SEO surface +
   shareable artifact.
2. **Build-in-public on X** — Sam's existing motion; document building this *with itself*.
3. **Product Hunt launch** at MVP (benchmark ~847 month-1 users, ~$127 CAC).
4. **Reddit / Indie Hackers** — honest build updates + free-tier value drops.
5. **YouTube** — Sam's "how to automate things" channel: build-guide episodes double as content.

## 11. Moat
Not the LLM (commodity). The moat is: (a) a **proven, codified method** (FRI receipt + the
`incubate`/`process-playbook` IP), (b) the **research→verify→artifact pipeline** tuned for
sellable output, (c) **accumulating template/playbook library** per business type, (d) the
**dogfition flywheel** — every idea Sam runs through it improves the product and generates content.

## 12. Cost / unit economics note
Deep research is token-heavy — the main COGS risk. Controls: cheap-model fan-out for
search/extract (Haiku-tier), caching/reuse of common market research, and a **metered free tier**
(1 idea, capped research depth). Model per-run token cost before opening the free tap; the free
tier must be a loss leader we can afford, not an unbounded bill.

## 13. Risks & mitigations
1. **Output must beat free ChatGPT by a wide margin** → cited+verified research, durable artifacts,
   and the delivery/roadmap layer are the margin. Validate this in the dogfood gate *before*
   building.
2. **Low-trust category ("AI builds your business" = snake oil)** → lead with the FRI receipt and
   show sources on every claim.
3. **Generality vs. depth** → narrow v1 ICP to services/AI-automation; don't promise universality.
4. **Free-tier cost** → meter hard; cheap-model fan-out; cache.
5. **Self-serve completion/abandonment** → the paid "ongoing development" may be where value and
   retention actually live; the free self-serve tier may primarily be lead-gen. Watch activation.
6. **Funded competitors (Audos/Cofounder)** → they're going heavy/agent-run-company; we stay light
   and operator-enabling. Different buyer, different promise.

## 14. Validate before building (hard gate)
Per the launchpad playbook: **no MVP build before signal.** Two gates first — (a) **dogfood**:
run one real idea end-to-end through the existing `incubate`+`deep-research` stack and judge
whether the artifact set is genuinely sellable; (b) **landing-page signal**: a positioning page +
waitlist, marketed via build-in-public, to test that the promise pulls. Build the app only if both
sing. Full sequence in `launch_todo.md`.

## 15. Near-term targets (illustrative, not committed)
- Dogfood proof: 1 idea fully run, Sam judges "yes, sellable."
- Landing page: 100+ waitlist from build-in-public before writing app code.
- Private beta: 10–20 target-ICP founders running real ideas.
- Paid launch: first 10 paying subs; watch free→paid toward the 8–15% top quartile.
