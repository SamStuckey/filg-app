# FILG — deploy quick-start

Ship the **free tier** first (the launch goal); flip on auth + billing later with no code change.
The app degrades gracefully — with only `ANTHROPIC_API_KEY` set it runs free-tier-only.

> Prereqs: repo on GitHub (`SamStuckey/fuckitletsgo`), an Anthropic API key. Render deploys from
> `main` via [`render.yaml`](./render.yaml) (Python service, `starter` plan, 1 GB persistent disk
> at `/var/data` for the SQLite DB).

## 1. Create the service
1. [Render dashboard](https://dashboard.render.com) → **New → Blueprint** → pick `SamStuckey/fuckitletsgo`.
2. Render reads `render.yaml` and proposes the `filg` service. It prompts for the `sync: false` secrets —
   for a free-tier launch set **only**:
   - `ANTHROPIC_API_KEY` → your real key. **This is what flips the app from mock to live.**
   - Leave `FILG_PAID_EMAILS` and all `SUPABASE_*` / `STRIPE_*` **blank**.
   - Pre-set in the blueprint: `FILG_DB=/var/data/filg.db`, `FILG_FREE_RUNS=1`,
     `FILG_DAILY_BUDGET=5` (conservative; raise once validated), `FILG_PUBLIC_URL=https://fuckitletsgo.ai`.
3. **Apply**. Build = `pip install -r requirements.txt`; start = `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

## 2. Smoke-test
On the Render URL (`https://filg-xxxx.onrender.com`):
- `GET /healthz` → expect `{"ok":true,"mock":false,"auth_enabled":false,"billing_enabled":false,...}`.
  **`mock:false` confirms the key is wired.**
- Open `/`, run one real idea (~$0.40), watch `today_spend` rise in `/healthz`. A 2nd run on the same
  email → "free limit reached" (the guardrail works).

## 3. Point the domain
1. Service → **Settings → Custom Domains → Add** `fuckitletsgo.ai` and `www.fuckitletsgo.ai`.
2. At your DNS registrar, per the records Render shows:
   - apex `fuckitletsgo.ai` → **ALIAS/ANAME** → `filg-xxxx.onrender.com` (or Render's A record if no ALIAS support).
   - `www` → **CNAME** → `filg-xxxx.onrender.com`.
3. Render auto-provisions TLS once DNS resolves (minutes–1hr). Verify `https://fuckitletsgo.ai/healthz`.

**→ Free tier is live.**

## 4. (Later) turn on auth + billing
Just fill env in **Environment** and save — no redeploy logic changes. See `.env.example` for the full list.
- **Supabase** (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_JWT_SECRET` — Settings → API).
  Add `https://fuckitletsgo.ai` as an Auth redirect URL.
- **Stripe** (`STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID` for the $39/mo recurring price,
  `STRIPE_WEBHOOK_SECRET`). Webhook endpoint: `https://fuckitletsgo.ai/api/stripe/webhook`,
  events `checkout.session.completed` + `customer.subscription.*`.
- `/healthz` then reports `auth_enabled:true, billing_enabled:true`.

## Notes
- The persistent disk pins the service to a **single instance** (fine — runs are serialized in-process).
  Scaling out is the trigger for the Postgres migration (next milestone, `launch_todo.md`).
- Local dev: `FILG_MOCK=1 uvicorn app.main:app --app-dir .` → free, no API/auth/billing.
