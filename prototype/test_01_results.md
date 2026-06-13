# Test #1 results — gate survival + $/run

> **Update (2026-06-12): the open number is now closed live.** The thin unattended pipeline
> (`pipeline.py`) was built and run end-to-end on a fresh niche. Post-gate survival ≈ **57%**
> (21% clean pre-re-search → 57% after re-search), and **measured $/run ≈ $1.05** — 5× the
> $0.21 token-only estimate below, because re-search and web-search server-tool fees were not
> in the model. See **`test_01_live_results.md`** for the live numbers, decomposition, and the
> revised economics. The model below stands as the token-only baseline.


Ran `pipeline_economics.py` (real token usage from this session's research subagents; published
per-MTok prices; the credibility gate on the real dogfood claims).

## The two numbers
**(a) Gate survival — raw research, pre-re-search:** 2/9 claims clean (22%), 1 needs a cross-cite,
**6/9 self-interested (67%)**. Raw vendor-heavy research is mostly self-interested → the gate is
load-bearing, not optional. (The *product-quality* metric — survival *after* the gate forces
re-search — still needs a live run to measure.)

**(b) $/run — one full idea→artifact run:**
| routing | $/run | ×2 stress | runs to burn a $39 sub | free-tier $/signup |
|---|--:|--:|--:|--:|
| cheap (haiku synth) | $0.14 | $0.28 | 279 | $0.14 |
| **lean (sonnet synth)** | **$0.21** | $0.42 | **186** | $0.21 |
| premium (opus synth) | $0.28 | $0.56 | 139 | $0.28 |

## Verdict on the cost kill-gate: PASS
A full run is **~$0.21** on lean routing. A $39/mo subscriber would have to run **~186 ideas/month**
before COGS eats the sub — far above any realistic usage. A free-tier signup (1 capped idea) costs
**~$0.21**, ~$0.42 even at 2× stress. **Cost is not what kills this.** The reason is structural: the
research fan-out runs on Haiku (cheap), and only the single synthesis call touches a pricier model.

## What this changes
- The vet's #1 load-bearing unknown ("can a $39/mo tier have margin?") is **resolved: yes.** Margin
  is comfortable; even a generous free tier is affordable.
- The remaining open number is now narrower and clearer: **post-gate survival quality** — after the
  gate forces re-sourcing of the 67% flagged claims, how many land on a primary/neutral cite? That
  is the real product-quality metric and the next thing a live pipeline must measure.

## Caveats
Token splits are estimated off observed totals; synthesis tokens are modeled, not measured; excludes
infra/egress and prompt-cache savings (which only help). Re-run with live numbers once the
unattended pipeline exists.
