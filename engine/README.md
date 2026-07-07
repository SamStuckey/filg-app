# The decision engine

Use-case-agnostic machinery for **graded-evidence decision building**: fan out research on a
subject, grade every quantitative claim's source credibility, assemble labeled evidence, and
grow a branching **decision tree** of typed nodes from what the operator decides. The host app
supplies what a run is ABOUT; this package supplies how it runs.

## Modules

| Module | Owns |
|---|---|
| `spine.py` | The deterministic conductor: a fixed phase DAG (plan → research → grade → re-search → assemble), the model only at typed seams, every phase logged (`PhaseEvent`). Also `run_author` (generate → validate → reprompt, bounded) and `regrade_engine` (re-grade carried claims without re-fetching). |
| `pipeline.py` | The LLM call layer (`call`, provider routing, web_search variants), the per-run cost `LEDGER`, `Claim` + the research seam (`plan`, `research_lane`, schema-validated), and the GATE (`gate_claims`: voted self-interest judge + deterministic staleness, boolean-algebra flagging). `ResearchFraming` is the subject-wording seam. |
| `evidence.py` | `build_evidence` — the public graded-evidence run; forwards the phase log as readable activity lines. |
| `tree.py` | The decision tree: node wiring (id/parent/children), a KIND registry (the host names its node kinds + display labels), bounded/inheritable ATTACHMENTS (board trail, build log — future: decisions, blockers, sub-trees), root→node walks, run epochs. Dict-in/dict-out on purpose: the persisted blob and the frontend read the same shape. |
| `provider.py` | Per-run provider + model-stack binding (BYOK seam). Contextvars + `bound()` to carry them into fan-out threads. |
| `model_catalog.py` | Model ids / prices / OpenRouter slugs, env-overridable — repoint a slot with no deploy. |
| `source_credibility_gate.py` | Domain-tier classification (PRIMARY/RESEARCH/VENDOR/FORUM/UNKNOWN) + conflict-of-interest heuristics. |
| `voice_lint.py` | The deterministic no-AI-tells copy linter — the author-seam validator (reprompts the author; never post-processes). |
| `usage.py` | Metering: per-user run caps, the daily kill switch, monthly cost caps. SQLite-backed. |

## The boundary

The engine carries **no product vocabulary** — no sections, no personas, no business copy.
Hosts plug in through three seams:

1. **`ResearchFraming`** (pipeline) — the subject wording of the research prompts.
2. **`tree.register_kind` / `register_attachment`** — the host's node vocabulary and per-node
   histories (FILG's live in `app/domain/nodes.py`).
3. **Prose/synthesis** — the host writes its own summaries over the graded rows
   (FILG's in `app/teardown.py`).

`tests/test_engine_neutrality.py` greps this package for product vocabulary and fails the
build on a leak. When a sibling product forks the app layer, the engine should need zero edits.

Modules use relative imports (the package is relocatable); self-tests run via
`python -m engine.<module>`.
