# Testing FILG — local harness + config-driven prod checks

**Decision (2026-06-17):** we test gated user-states **locally**, not via a prod backdoor. A "user type"
(fresh-taste / post-taste / kill-gated / budget-exhausted / BYOK / entitlement) is just **config + DB
state**, so locally you can *be* any of them by setting the state directly. Production gets only
config-driven spot checks (no test code, no bypass, no un-dedupe shipped to prod). The harness is
`scripts/dev.py`; point it and the app at the same DB with `FILG_DB`.

## 1. Run locally

```sh
python scripts/dev.py serve          # mock mode, local DB at ./.dev/filg.db, http://localhost:8000
```
or raw, with whatever env you want to exercise:
```sh
FILG_DB=$PWD/.dev/filg.db FILG_MOCK=1 uvicorn app.main:app --reload --app-dir .
```
Mock mode = no API spend, auth/billing off. Turn individual systems ON to test them (below).

## 2. Be each user-type

The same DB is shared by the app and the CLI (both read `FILG_DB`).

| User-type | How to get there locally |
|---|---|
| **Fresh user** (free taste) | `python scripts/dev.py reset` → reload the app |
| **Kill-gated** (coaching ladder, "Build it anyway", waste-of-time mode) | start any plan, then `python scripts/dev.py kill <sid>` and reload (mock vet always returns `pursue`, so force it) |
| **Cleared again** | `python scripts/dev.py unkill <sid>` |
| **Budget exhausted** (kill-switch / degrade-to-key) | `python scripts/dev.py spend 25` (push today's spend over `FILG_DAILY_BUDGET`); `usage` to check |
| **Entitlement** (free / byok / pro limits + concurrency) | `python scripts/dev.py plan <email> <free\|byok\|pro>` |
| **BYOK wall** | run with `FILG_KEY_SECRET=devsecret` set → engine routes require a key; add one in the UI ("Your key") |
| **Mid-build** (test #6 regenerate, #7 comments, tree nav) | just build a few steps in the browser; `state <sid>` to inspect |

`python scripts/dev.py state <sid>` prints the gating-relevant state (status, step, verdict, files,
tree active, account plan, free_used, today_spend/budget).

## 3. Integration paths (still local — no staging needed)

- **Real cited research (OpenRouter / Anthropic):** run *without* `FILG_MOCK`, add a real key. Costs a
  few cents per run. This is the only way to verify the live web-search/grading path.
- **Stripe ($35 one-time, once built):** Stripe **test mode** keys + `stripe listen --forward-to
  localhost:8000/<webhook>` — the Stripe CLI forwards webhooks to your machine, so checkout + webhook
  both work locally with test cards. No public URL required.
- **Google OAuth / magic-link:** add a `http://localhost:8000` redirect URL in Supabase
  (Auth → URL config) and the Google OAuth client (dev). Both support localhost for development.

## 4. Production verification (config-driven, no backdoor)

After deploy, confirm the live site without any test code in prod:

- **Kill-ladder:** type a trash idea ("I want fame and money") — free taste, costs cents, walks the
  whole advisement → snark → waste-of-time ladder live.
- **Degrade-to-key:** set `FILG_DAILY_BUDGET` low in the Render dashboard → submit a new idea → confirm
  it prompts for a key instead of blocking → **restore the budget**. (Config, not code.)
- **Fresh vs returning / dedup:** sign in with 2–3 real emails you control. (Plus-addressing dedupes by
  design; the $10/day cap is the real backstop, so you don't need to defeat dedup to verify it.)
- **$35 purchase (once built):** do one real purchase to yourself and refund it in Stripe.

## 5. The automated suite

```sh
python -m pytest -q          # 94 tests, mock mode (deterministic, no spend)
python app/planner.py        # planner self-test (tree + waste-of-time mode), no API
```
New gated behavior (taste cap, email-dedup, degrade-to-key) gets pytest cases the same way the
kill-gate test does: seed the state, assert the response. Prefer a test over a manual check whenever the
behavior is deterministic.
