# Test #1 (live) — post-gate survival quality + measured $/run

Ran the thin unattended pipeline (Haiku research fan-out → Sonnet synthesis → source-credibility gate) end-to-end on one fresh prompt, with real `web_search`. This closes the open number from `test_01_results.md`.

**Prompt (fresh niche — not seen in either dogfood run):** I'm good with automation and AI tools. I've noticed that small, independent property-management companies (the ones managing a few hundred rental units) are slow to respond to tenant maintenance requests and leasing inquiries, and they lose tenants and prospective renters because of it. I think I could sell them something AI-powered, but I don't know exactly what the offer is or how I'd sell it.

**Research lanes (auto-planned):**
- What is the market size and number of target buyers?
- Who are the competitors and what are the pricing norms?
- What is the buyer's most acute, expensive pain point?

## (a) Post-gate survival quality — THE open number

- Quantitative claims gated: **14**
- Clean on first pass: **3/14 (21%)**
- Flagged (self-interested / non-primary) → forced re-search: **11**
- Rescued to a primary/neutral cite: **5/11** (rescue rate **45%**)
- **Survival after the gate: 8/14 = 57%** of cited quantitative claims land on a primary/neutral source.

### Per-claim

| verdict | source | claim | judge |
|---|---|---|---|
| clean | ibisworld.com | There are approximately 335,293 property management businesses in the United Sta | CROSS_CHECK |
| FLAG | apmrealestate.com | 35% of property managers handle 101 to 500 rental units, which aligns with the s | CROSS_CHECK |
| clean | grandviewresearch.com | The U.S. property management services market size was $122.02 billion in 2025 an | CROSS_CHECK |
| FLAG | revenuememo.com | AI adoption among property managers surged from 21% in 2024 to 34% in 2025, with | CROSS_CHECK |
| clean | grandviewresearch.com | The property management software market size was $3.61 billion in 2025 and is pr | CROSS_CHECK |
| FLAG | tenantcloud.com | Mid-sized portfolios (50-200 units) typically pay $150-$600 per month for proper | FLAG_SELF_INTERESTED |
| FLAG | rentvine.com | Standard property management software costs $1 to $5 per unit per month, with mi | CROSS_CHECK |
| FLAG | eliseai.com | AI-driven maintenance coordination has generated $12+ savings per door through o | FLAG_SELF_INTERESTED |
| FLAG | lula.life | 39% of property managers spend more than 20 hours per month on maintenance reque | FLAG_SELF_INTERESTED |
| FLAG | eliseai.com | EliseAI's platform achieves 90% automation of leasing conversations, dramaticall | FLAG_SELF_INTERESTED |
| FLAG | allbetterapp.com | Tenant turnover costs average $4,000 per unit per turnover event, including vaca | FLAG_SELF_INTERESTED |
| FLAG | allbetterapp.com | Operational costs consume 35-45% of gross rental income when combining maintenan | FLAG_SELF_INTERESTED |
| FLAG | joinbeagle.com | Tenant turnover costs have more than doubled since the COVID-19 pandemic, often  | FLAG_SELF_INTERESTED |
| FLAG | markets.financialcontent.com | 45% of landlords find reducing operating costs challenging, with outsourcing tas | FLAG_SELF_INTERESTED |

### Re-search outcomes

| original claim | new source | tier | judge | rescued |
|---|---|---|---|---|
| 35% of property managers handle 101 to 500 rental units, whi | info.buildium.com | UNKNOWN | CROSS_CHECK | yes |
| AI adoption among property managers surged from 21% in 2024  | naahq.org | UNKNOWN | CROSS_CHECK | yes |
| Mid-sized portfolios (50-200 units) typically pay $150-$600  | itqlick.com | UNKNOWN | CROSS_CHECK | yes |
| Standard property management software costs $1 to $5 per uni | constructioncoverage.com | UNKNOWN | CROSS_CHECK | yes |
| AI-driven maintenance coordination has generated $12+ saving | (none) | NONE | ? | no |
| 39% of property managers spend more than 20 hours per month  | (none) | NONE | ? | no |
| EliseAI's platform achieves 90% automation of leasing conver | (none) | NONE | ? | no |
| Tenant turnover costs average $4,000 per unit per turnover e | butterflymx.com | UNKNOWN | FLAG_SELF_INTERESTED | no |
| Operational costs consume 35-45% of gross rental income when | mrisoftware.rentpayment.com | UNKNOWN | FLAG_SELF_INTERESTED | no |
| Tenant turnover costs have more than doubled since the COVID | gitnux.org | UNKNOWN | CROSS_CHECK | yes |
| 45% of landlords find reducing operating costs challenging,  | (none) | NONE | ? | no |

## (b) Measured $/run vs the $0.21 estimate

| stage | cost |
|---|--:|
| research2 | $0.7598 |
| research | $0.2376 |
| synth | $0.0558 |
| judge | $0.0051 |
| plan | $0.0009 |
| **total** | **$1.0592** |

- Model estimate (`pipeline_economics.py`, lean routing): **$0.21/run**.
- Measured live: **$1.0592/run** (47 web searches @ $0.01 included).
- Wall-clock: 135s.

## Caveats
- One run, one niche — survival % is indicative, not a distribution. Re-run across niches to get a confidence interval.
- Web-search server-tool cost ($10/1k searches) is included here but was NOT in the $0.21 token-only model — see the breakdown above for the split.
- Re-search success depends on whether a neutral source actually publishes the fact; a 'still weak' outcome can mean the number only exists in vendor marketing (itself a useful signal to down-rank the claim).

## Read (what this closes, and what it changes)

**(a) Survival quality — the gate works, with a real ceiling.** On a third, fully-unseen
niche the gate again flagged the overwhelming majority of raw research (**11/14, 79%**
self-interested/non-primary) — the "research is mostly laundered vendor stats" finding from
Test #1 holds outside the dogfood niches. Forced re-search then **roughly tripled clean-cite
coverage (21% → 57%)**. The ceiling is honest: ~30% of these niche stats (`$12/door savings`,
`90% leasing automation`, `39% spend >20 hrs`) have **no neutral source** — they exist only in
vendor marketing, and the right product behavior is to down-rank/label them "unverified vendor
claim", not print them as fact. The gate is the difference between a tool that launders those
and one that doesn't. (Numbers are one sample; across the 3 live runs survival landed 57–79%
once the methodology was judge-led — see notes.)

**Methodology note (why judge-led, not registry-led):** survival is scored by the Haiku
*judge* + known-vendor registry, NOT by whether a domain sits in the hand-built tier registry.
The registry is explicitly a stub; on a fresh niche it can't know `naahq.org`, `ibisworld.com`,
or `constructioncoverage.com` are neutral. Scoring survival off the registry alone under-counts
real rescues by ~4× (a registry-led pass of this same run read 7%). For production this is the
core lesson: **the judge/allowlist must replace the hand registry** — the registry doesn't
generalize past the niches you hand-curate.

**(b) Cost — the $0.21 estimate was 5× low, and it matters.** Measured **~$1.05/run** vs the
$0.21 token-only model. Decomposition:
- *Token-only research + synth + plan + judge ≈ $0.30* — close-ish to the $0.21 estimate (a bit
  high; real research fan-out used more tokens than modeled).
- *Re-search ≈ $0.76* — **the surprise.** It was not in the original model at all, and it's the
  single largest line because re-searching every one of ~11 flagged claims means ~3 more
  Haiku+web_search calls each.
- *Web-search server-tool fees ≈ $0.47* (47 searches @ $0.01) — also absent from the token-only
  model.

**What this does to the kill-gate:** at $1.05/run a $39/mo sub covers **~37 idea-runs/mo** (not
186), and a free-tier signup now costs **~$1**, not $0.21. Margin is still positive, but the
**free tier is no longer trivially cheap** — that's a real product constraint, and it changes the
abuse surface (1,000 free signups = ~$1k, not ~$200).

**The cheap lever is obvious:** re-search is 72% of the cost and is the naive choice — re-sourcing
*every* flagged claim live. Cheaper, equally-defensible production behavior: **label/down-rank
flagged claims by default and only re-search the 2–3 load-bearing headline stats** (TAM, the ROI
number in the pitch). That alone pulls a run back toward **~$0.35–0.45** while keeping the moat.
The kill-gate verdict stays **PASS** — but on "label-don't-chase" economics, not "$0.21".
