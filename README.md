# FILG

**Turn a plain-text business idea into a sellable offer — with receipts.**

Idea in → the offer, the go-to-market, and the delivery playbook out, backed by research where
**every number is graded by a source-credibility gate and vendor marketing is labeled, not
laundered.** No hallucinated TAM. Built for solo operators starting a service / AI-automation
business.

> Brand: **FILG** ("fuck it, let's go"). Domains: **filg.ai** + **fuckitletsgo.ai**.
> Status: engine + marketing site working; **building the thin MVP, launching the free tier.**

---

## What's here

| Path | What it is |
|---|---|
| `prototype/pipeline.py` | The unattended engine: Haiku research fan-out (real `web_search`) → Sonnet synthesis → source-credibility gate → re-search of flagged claims. Meters its own $/run. |
| `prototype/source_credibility_gate.py` | The differentiator: grades each claim's source, flags self-interested vendor stats, routes a Haiku judge for unknown domains. |
| `prototype/teardown.py` | The weekly "Cited Offer Teardown" lead-magnet generator (label-don't-chase mode, ~$0.40/issue). Emits branded hosted pages into `landing/teardowns/`. |
| `prototype/pipeline_economics.py` · `test_01_*.md` | The cost/quality kill-gate work. |
| `landing/` | The marketing site (self-contained static HTML) + the hosted teardown archive. Deploy the folder; point both domains at it. |
| `teardowns/` | Markdown sources + structured rows for each teardown issue. |

> **Planning + research docs** live in the sibling [filg-docs](https://github.com/SamStuckey/filg-docs)
> repo: business plan, validation strategy, launch checklist, research findings, dogfood runs, brief,
> and the session handoff (`NEXT_SESSION.md`). This repo is code + deploy artifacts only.

## Quickstart (engine)
```bash
pip install anthropic
export ANTHROPIC_API_KEY=...

# full artifact set for one idea (~$1/run, re-sources every flagged claim)
python3 prototype/pipeline.py "your plain-text idea"

# a publishable teardown (~$0.40/run, label-don't-chase — the production behavior)
python3 prototype/teardown.py "your plain-text idea"
```

## Deploy the site
`landing/` is a no-build static site. Drag it into Cloudflare Pages / Netlify / Vercel and point
`filg.ai` + `fuckitletsgo.ai` at it. Wire `FORM_ENDPOINT` (Buttondown/ConvertKit) for email capture.
See `landing/README.md`.

## The two things that define this product
1. **The source gate is the moat.** On a fresh niche it flags ~80% of raw research as
   vendor-laundered; re-search lifts clean cites from ~20% to ~57%. See `prototype/test_01_live_results.md`.
2. **Cost discipline = "label-don't-chase."** Label flagged claims by default, re-source only the 2–3
   headline stats → ~$0.40/run. Free tier must be **metered (per-user run caps + kill switch)** before
   launch — an unmetered free run is ~$0.40–1 of compute.

## Next: the thin MVP
Idea input → async pipeline run (label-don't-chase) → artifact workspace; free tier capped at 1 run;
Stripe paywall for the $39/mo "Operator" tier; light auth. See `launch_todo.md` Phase 3 in
[filg-docs](https://github.com/SamStuckey/filg-docs) for the checklist.

---
© Sam Stuckey. Private — not for redistribution.
