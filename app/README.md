# FILG — the business-planning app

The product built on the decision engine (`../engine/`): a plain-text idea walks a
diverge → converge → commit funnel into a branching decision tree, every research pass graded
by the source-credibility gate, and the winning branch grows into a 7-section business plan
with receipts. FastAPI + SQLite, deployed on Render's native Python runtime.

## Layout — one owner per layer

| Module | Owns |
|---|---|
| `main.py` | The route surface: the funnel (`/api/brainstorm` → `/merge` → `/refine` → `/commit` → `/next`/`/back`/`/redraft`/`/goto`), chat routing, board, exports, billing, pages. Routes + walls + background workers only — logic lives in the layers below. |
| `ops.py` | How an engine op RUNS: `run_slot` (concurrency + provider/stack/ledger binding + money guards), `bind_session_run` (worker binding), the kill-switch predicate, error surfacing, usage folding. |
| `access.py` | Entitlements: tier/BYOK/subscriber resolution, provider selection, budget math, metering. |
| `views.py` | Session → frontend shaping: `plan_state` (the poll payload), the tree views, the legacy flat mirror, the share-page decision path. |
| `exports.py` | The free take-it-with-you surfaces: whole-plan .txt + the LLM handoff prompt. |
| `domain/` | **The use-case plug**: `sections.py` (the 7-section build spec + QA checklist), `copy.py` (mock/WOD copy), `nodes.py` (the tree vocabulary registered on the engine), `research.py` (the research framing), `help.py` (generated pricing facts). A sibling product swaps this package + `skills/` + `personas.py`; the engine needs zero edits. |
| `planner.py` | The staged build: per-section synthesis over gate-graded research, the branch verbs (forward/rebranch/wod), the final voted QA pass. |
| `brainstorm.py` · `intake.py` | The funnel's top: diverge (spread directions), merge (reconcile + light skim), shape/vet (the kill gate), premortem. |
| `personas.py` · `board.py` · `director_forge.py` · `advisor.py` · `skeptic.py` | The advisory layer: composite archetypes (never real people), the board convene, custom directors, plan chat, the adversarial stress test. |
| `context.py` | THE CONTEXT ENGINE — every model-facing view of session state (one renderer per node kind; contract tests pin it). |
| `router.py` | The single prompt box: free text → one intent (steer/commit/diverge/ask/pick/next/…). |
| `store.py` · `keys.py` · `auth.py` · `billing.py` · `tiers.py` | SQLite persistence (the tree rides one JSON column), Fernet-encrypted BYOK keys, Supabase JWT auth, Stripe (one-shot PDF + subscriptions), the tier ladder. |
| `teardown.py` | The research-run product layer: one graded pass + the offer summary (`generate`), the full artifact set (`generate_full`). |
| `plan_pdf.py` · `render.py` | The polished PDF (fpdf2, pure Python — Render can't apt) and the server-rendered share pages. |
| `skills/` · `rag/` | The prompt bodies (SKILL.md format, one loader: `skill_registry.py`) and the optional method-grounding corpus. |
| `web/` | The SPA: `index.html` (templated) + `static/app.js` + `static/styles.css`. |

## Run

```bash
FILG_MOCK=1 uvicorn app.main:app --reload   # dev: canned results, no spend
uvicorn app.main:app                        # real runs, metered
python -m pytest                            # the suite (mock, fast)
```

`GET /healthz` shows wiring + today's spend. `scripts/dev.py` is the local harness (be any
user-type against a local DB); `scripts/monkey.py` is the seeded random-walk UI test.

## Guardrails (non-negotiable)

- The free taste is metered: per-user run caps + the daily kill switch (`engine/usage.py`),
  read at every spend point (`ops.free_pool_tapped`).
- Subscribers run on the hosted key bounded by a monthly cost cap; BYOK runs bill the user.
- The gate's verdicts ship labeled, never laundered (invariant #1 — the moat).
