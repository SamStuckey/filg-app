"""The decision engine — use-case-agnostic research, grading, and orchestration machinery.

This package knows about CLAIMS, SOURCES, GRADES, RESEARCH LANES, AUTHOR LOOPS, PHASES,
PROVIDERS, and COST. It deliberately knows nothing about any one product built on it
(business plans, wedding playbooks, …): domain vocabulary, section lists, personas, and
prompt copy live in the host app's domain layer and are passed in through typed seams.

Modules:
  spine                    the deterministic conductor — walks the phase DAG, model only at seams
  pipeline                 the LLM call layer: providers, cost ledger, claims, the credibility gate
  provider                 per-run provider/stack binding (BYOK seam, contextvars + bound())
  model_catalog            model ids / prices / slugs, env-overridable (no-deploy repoints)
  source_credibility_gate  domain-tier classification + conflict-of-interest heuristics
  voice_lint               the deterministic no-AI-tells copy linter (the author-seam validator)
  evidence                 the graded-evidence run: research fan-out -> gate -> graded rows
  tree                     the decision tree: nodes, kinds, attachments, and tree operations
  usage                    metering: per-user run caps, daily kill switch, monthly cost caps

Import as a package (`from engine import pipeline`); modules use relative imports internally
so the package stays relocatable. Self-tests run via `python -m engine.<module>`.
"""
