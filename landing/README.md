# FILG — validation funnel (landing page + lead magnet)

Brand: **FILG** ("fuck it, let's go"). Domains: **filg.ai** + **fuckitletsgo.ai**.

The standing funnel for the demand-validation sprint (`validation_strategy.md` in the sibling
[filg-docs](https://github.com/SamStuckey/filg-docs) repo). Two pieces:

1. **`index.html`** — a single self-contained landing page. No build step, no dependencies (one
   Google Font). Leads with the receipt, shows a real graded sample, fake-door `$39` pricing, and
   two email captures (weekly teardown + founding members).
2. **The teardown engine** — `../prototype/teardown.py` generates the free weekly "Cited Offer
   Teardown" lead-magnet issue. Run it weekly; that's the recurring value that grows the list.

---

## Deploy the page (pick one, ~2 min, free)
It's a static file — host it anywhere:
- **Cloudflare Pages / Netlify / Vercel:** drag the `landing/` folder in, or point at this repo.
- **GitHub Pages:** push, enable Pages on the branch, set folder to `/landing`.
- **Local preview:** `python3 -m http.server -d landing 8000` → open `http://localhost:8000`.

Then point a domain at it (anything memorable; renamable later).

## Wire up email capture (required — otherwise signups only log to console)
The page posts JSON `{email, list, ts}` to one endpoint. Set it once at the top of the `<script>`
in `index.html`:
```js
const FORM_ENDPOINT = "https://formspree.io/f/xxxxxxx";
```
Easiest options:
- **Formspree** (`formspree.io`) — free tier, instant endpoint, emails you each signup.
- **Buttondown / ConvertKit / beehiiv** — better if you want the weekly teardown to go out as a
  newsletter (recommended, since the lead magnet *is* a newsletter). Use their form/webhook URL.
- **Tally / a Cloudflare Worker** — if you want the rows in a sheet/DB.

`list` is either `teardown` (weekly magnet) or `founding` (the fake-door pricing modal) — tag them
so you can measure each funnel separately.

## Turn on analytics (needed to read the gate metrics)
Uncomment the Plausible line in `<head>` and set your domain (or swap in PostHog):
```html
<script defer data-domain="YOURDOMAIN.com" src="https://plausible.io/js/script.tagged-events.js"></script>
```
The page already fires custom events:
- `fakedoor_start` — every click on a "Start / $39" button (**this is the fake-door CTR** — the
  revealed-intent metric; gate threshold ≥5%).
- `signup` with `{list}` — captures, split by funnel.

With Plausible's goals you can read visitor→signup conversion and fake-door CTR directly against the
PASS/KILL thresholds in `validation_strategy.md` ([filg-docs](https://github.com/SamStuckey/filg-docs)).

---

## Site structure (deploy the whole `landing/` folder)
```
landing/
  index.html                 → filg.ai/
  teardowns/
    index.html               → filg.ai/teardowns/         (the archive)
    issue-01-law-firm-intake.html → filg.ai/teardowns/issue-01-law-firm-intake.html
    _manifest.json           (drives the archive; don't serve as a route)
```
Point both `filg.ai` and `fuckitletsgo.ai` at this folder.

## Generate the weekly lead magnet
```bash
cd ../prototype
python3 teardown.py "a plain-text business idea in your ICP's world"
```
Each run (~$0.40–0.50, **label-don't-chase** mode) writes three things:
- `../teardowns/issue_NN_<slug>.md` — markdown to publish (Reddit/Skool/X/newsletter)
- `../teardowns/issue_NN_<slug>.rows.json` — structured data (for rebuilds)
- `../landing/teardowns/issue-NN-<slug>.html` — the **branded hosted page**, and the archive
  index is rebuilt automatically.

`python3 teardown.py --rebuild` re-renders every page + the index from saved rows (no API calls) —
use it after a style change. Issue #1 (small-law-firm intake) is live and is the landing-page sample.

## What's intentionally NOT here
No app, no auth, no billing, no backend. Concierge delivery during validation = you running
`teardown.py` / `pipeline.py` by hand for hand-raisers. Build the real app only after the sprint
clears the money gate.
