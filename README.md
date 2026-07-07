# FILG

**Turn a plain-text business idea into a sellable offer — with receipts.**

Idea in → the offer, the go-to-market, and the delivery playbook out, backed by research where
**every number is graded by a source-credibility gate and vendor marketing is labeled, not
laundered.** No hallucinated TAM. Built for solo operators starting a service / AI-automation
business.

> Brand: **FILG** ("fuck it, let's go"). Domains: **filg.ai** + **fuckitletsgo.ai**.
> Live at https://fuckitletsgo.ai (Render, auto-deploys from `main`).

---

## Architecture

Two packages with a hard boundary, plus deploy artifacts:

| Path | What it is |
|---|---|
| `engine/` | **The decision engine — use-case-agnostic.** Research fan-out + the source-credibility gate (`pipeline.py`), the deterministic conductor (`spine.py`), the graded-evidence entry point (`evidence.py`), the decision tree — nodes, kinds, attachments (`tree.py`), providers/BYOK (`provider.py`), the model catalog, the VOICE linter, metering (`usage.py`). Knows nothing about business plans; `tests/test_engine_neutrality.py` enforces it. |
| `app/` | **The business-planning product.** FastAPI routes (`main.py`) over layered modules: `ops.py` (how an op runs: slots, guards, metering), `views.py` (session → frontend shaping), `exports.py` (free text exports), `access.py` (entitlements), plus the funnel (`brainstorm`/`intake`/`planner`), advisors (`personas`/`board`/`advisor`), persistence (`store.py`) and billing. `app/domain/` is the use-case plug: the section spec, canned copy, node vocabulary, research framing, help facts. `app/web/` is the SPA. |
| `scripts/` | Dev CLI (`dev.py`), the seeded Playwright monkey (`monkey.py`), the weekly teardown publisher (`teardown_publish.py`), the live BYOK path check (`verify_openrouter.py`). |
| `landing/` · `teardowns/` | The static marketing site + the published teardown issues. |
| `tests/` | 350+ pytest tests (mock mode, no spend) — wire-shape goldens, the engine-neutrality beacon, contract tests. |

> **Planning + research docs** live in the sibling [filg-docs](https://github.com/SamStuckey/filg-docs)
> repo. This repo is code + deploy artifacts only.

## Quickstart

```bash
pip install -r requirements.txt

# dev mode — canned results, no API calls, no spend
FILG_MOCK=1 uvicorn app.main:app --reload

# real runs (BYOK or hosted key), metered
export ANTHROPIC_API_KEY=...
uvicorn app.main:app
```

```bash
python -m pytest                                        # the suite (mock, fast)
python scripts/monkey.py --base http://127.0.0.1:8600   # seeded random-walk UI drive
python scripts/dev.py engine "your idea"                # real engine smoke test
python scripts/teardown_publish.py "your idea"          # publish a lead-magnet issue
```

## The two things that define this product

1. **The source gate is the moat.** Every quantitative claim is graded against its source
   (voted self-interest judge + deterministic staleness); vendor stats are labeled, never
   laundered. The gate has no priors — that's the point.
2. **Cost discipline = "label-don't-chase."** Label flagged claims by default, re-source only
   the 2–3 headline stats. The free taste stays metered (per-user caps + daily kill switch);
   subscribers are bounded by a monthly cost cap.

## Building another product on the engine

Swap the `app/domain/` package (sections, copy, node vocabulary, research framing), the
`app/skills/` prompt files, and `app/personas.py` — the engine needs zero edits. The
wedding-planner fork is the working proof.

---
© Sam Stuckey. Private — not for redistribution.
