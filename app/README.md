# FILG — MVP backend

The thin app that runs the engine for a stranger: plain-text idea → async label-don't-chase run →
graded artifact, gated by the free-tier cap + daily kill switch. Reuses `../prototype/teardown.py`
(the engine) and `../prototype/usage.py` (the guardrail) — nothing is reimplemented.

Tiers: a **free** run returns the Cited Offer Teardown; a **paid** email (in `FILG_PAID_EMAILS`)
returns the **full artifact set** (brief → offer → pricing → GTM → delivery → roadmap). Every finished
run has a shareable server-rendered permalink at **`/r/{job_id}`** (in-memory in the skeleton —
persist for durable links).

## Run
```bash
pip install fastapi uvicorn anthropic markdown

# free dev mode — canned result, no API calls, no spend (use for frontend/iteration)
FILG_MOCK=1 uvicorn app.main:app --reload --app-dir .

# real runs (~$0.40 each), metered by the cost guardrail
export ANTHROPIC_API_KEY=...
uvicorn app.main:app --app-dir .
```
Open http://127.0.0.1:8000 — type an idea + email, get a graded offer. `GET /healthz` shows today's
spend vs the budget.

## Cost guardrail (wired, non-negotiable for launch)
`../prototype/usage.py`:
- **`FILG_FREE_RUNS`** (default 1) — free runs per user (email).
- **`FILG_DAILY_BUDGET`** (default $20) — global $/day kill switch; trips for everyone, even paid.
- **`FILG_PAID_EMAILS`** — comma-sep allowlist that skips the free cap (the Stripe stub).

## What's real vs stubbed
| Real | Stubbed (clearly marked TODO) |
|---|---|
| The engine (research → gate → re-search), label-don't-chase | **Auth** — "user" = the email entered; no login |
| Per-run cost metering + free cap + daily kill switch | **Billing** — `is_paid` is a static allowlist, not Stripe |
| Async job run + status polling, single-page UI | **Persistence** — jobs/usage are in-memory/JSON, not Postgres |

## Deploy (when ready)
Any container host runs it: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Render / Fly / Modal
are the easy paths (no GPU needed — it's just API calls). Set `ANTHROPIC_API_KEY` + the `FILG_*` env.
Then point `filg.ai` (app) and serve `../landing/` as the marketing site, or host the landing page
separately and link to the app subdomain.

## Next build steps (in order)
1. Real auth (Clerk/Supabase) → drop the email-as-identity stub.
2. Stripe `$39/mo` checkout → set `is_paid` from the subscription, not the allowlist.
3. Postgres for users/runs/artifacts; a real queue for jobs.
4. Artifact workspace (durable, editable docs) + the full pipeline mode alongside teardown.
