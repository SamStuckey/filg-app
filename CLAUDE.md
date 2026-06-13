# FILG — Claude Code guide

**FILG** ("fuck it, let's go") turns a plain-text business idea into a **sellable offer + GTM +
delivery playbook**, backed by research where **every number is graded by a source-credibility gate
and vendor marketing is labeled, not laundered**. ICP: solo operators starting a service /
AI-automation business. Free tier + $39/mo "Operator" tier. Domains: filg.ai, fuckitletsgo.ai.

> Graduated from the `bizdev` launchpad (2026-06-13). Status: engine + marketing site working;
> building the thin MVP and launching the free tier (validation-by-launch, not a waitlist).

## Layout
- `prototype/` — the engine. `pipeline.py` (full unattended run), `source_credibility_gate.py`
  (the moat), `teardown.py` (lead-magnet generator + the `generate()`/`generate_full()` library the
  app calls), `usage.py` (cost guardrail), `pipeline_economics.py` + `test_01_*.md` (the cost work).
- `app/` — the MVP backend (FastAPI). Idea → async run → graded artifact, gated by `usage.py`.
- `landing/` — the static marketing site + the hosted teardown archive (`landing/teardowns/`).
- `teardowns/` — markdown + structured-rows source for each weekly issue.
- Planning: `business_plan.md`, `validation_strategy.md`, `vet.md`, `launch_todo.md`,
  `research_findings.md`, `00_brief.md`. `GRADUATION.md` is the migration record.

## Invariants — do not break these
1. **The source-credibility gate is the product's moat.** Never print a vendor-sourced stat as fact;
   grade it and label it. The gate's value is that it has no priors.
2. **Cost discipline = "label-don't-chase".** Default behavior labels flagged claims and re-sources
   only the 2–3 headline stats → ~$0.40/run. Full re-search (`pipeline.py`) is ~$1/run; use it
   deliberately. See `prototype/test_01_live_results.md`.
3. **Meter before you open the tap.** The free tier MUST stay behind `usage.py` (per-user run cap +
   daily kill switch). An unmetered free run is real money.
4. **Models:** research fan-out on **Haiku** (`claude-haiku-4-5`), synthesis on **Sonnet**
   (`claude-sonnet-4-6`); web research uses the `web_search` server tool with
   `allowed_callers=["direct"]` (Haiku can't do programmatic calling). You cannot fine-tune Claude.

## Run
```bash
pip install anthropic fastapi uvicorn markdown
export ANTHROPIC_API_KEY=...
python3 prototype/teardown.py "an idea"          # generate a weekly teardown (~$0.40)
FILG_MOCK=1 uvicorn app.main:app --app-dir .     # the app, free/no-API mock mode → localhost:8000
```

## Next (launch path)
Real auth (Clerk/Supabase) → Stripe $39/mo (set `is_paid` from the subscription, not the allowlist)
→ Postgres + a real job queue → artifact workspace. Then point the domains at it and launch via
build-in-public + the weekly teardown. Checklist: `launch_todo.md` Phase 3.
