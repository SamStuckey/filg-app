# FILG — MVP backend

The thin app that runs the engine for a stranger: plain-text idea → async label-don't-chase run →
graded artifact, gated by the free-tier cap + daily kill switch. Reuses `../prototype/teardown.py`
(the engine) and `../prototype/usage.py` (the guardrail) — nothing is reimplemented.

Tiers: a **free** run returns the Cited Offer Teardown; a **paid** email (in `FILG_PAID_EMAILS`)
returns the **full artifact set** (brief → offer → pricing → GTM → delivery → roadmap). Every finished
run has a shareable server-rendered permalink at **`/r/{job_id}`**, backed by SQLite (`store.py`)
so links survive restarts.

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
- **`FILG_PAID_EMAILS`** — comma-sep manual comp allowlist (real paid status comes from Stripe).

## Auth + billing (real, degrade gracefully)
Both are stdlib-only — no PyJWT/`cryptography`/`stripe` SDK — so the app keeps "running anywhere",
and both are **off by default**: with their env unset the app is free-tier-only on the email typed in.
- **Auth** (`auth.py`, Supabase): verifies the Supabase HS256 user JWT with `hmac`. Enable with
  `SUPABASE_URL` + `SUPABASE_ANON_KEY` (frontend) + `SUPABASE_JWT_SECRET` (server verify). Free tier
  needs only an email (no login); paid is always gated on a verified user.
- **Billing** (`billing.py`, Stripe $39/mo): Checkout Sessions via the REST API (`urllib`), webhook
  signatures verified with `hmac`. Enable with `STRIPE_SECRET_KEY` + `STRIPE_PRICE_ID` +
  `STRIPE_WEBHOOK_SECRET` (+ `FILG_PUBLIC_URL`). `is_paid` is derived from the live subscription
  (`store.py` `subscriptions`), per the CLAUDE.md invariant — never the allowlist.
- Extra endpoints: `GET /api/me`, `POST /api/checkout`, `POST /api/stripe/webhook`.

## What's real vs stubbed
| Real | Stubbed (clearly marked TODO) |
|---|---|
| The engine (research → gate → re-search), label-don't-chase | **Datastore** — SQLite (`store.py`), not yet Postgres |
| Per-run cost metering + free cap + daily kill switch | **Job queue** — in-process threads, not a real queue |
| Async run + status polling + share links, single-page UI | **Artifact workspace** — results are read-only, not editable |
| Supabase auth (JWT verify) + Stripe $39/mo (subscription-derived `is_paid`) | — |

## Deploy (when ready)
Any container host runs it: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Render is the easy
path (`../render.yaml` — persistent disk for the SQLite DB, all env wired). Set `ANTHROPIC_API_KEY`,
keep `SUPABASE_*`/`STRIPE_*` blank for a free-tier launch, fill them to turn on login + billing.
Then point `fuckitletsgo.ai` at it and serve `../landing/` as the marketing site.

## Next build steps (in order)
1. Postgres for users/runs/artifacts; a real queue for jobs (SQLite via `store.py` is the launch step).
2. Artifact workspace (durable, editable docs) + the full pipeline mode alongside teardown.
3. Stripe customer portal (self-serve cancel/update) + dunning emails on `past_due`.
