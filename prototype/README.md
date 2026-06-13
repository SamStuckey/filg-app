# Prototype 01 — source-credibility + verify gate

**Why this exists.** Both dogfood runs proved the engine produces sellable, whole-arc output.
The failure mode they exposed: on research, **self-interested vendor-blog stats get laundered
into the artifact set as fact**. That's the single thing that would destroy the cited-research
moat. This is the smallest real version of the gate that prevents it.

## What it does
For each research claim (text + source URL):
1. Classify the source domain — PRIMARY (gov/AMA/MGMA) / RESEARCH (market-research firm) /
   VENDOR (sells something) / FORUM / UNKNOWN.
2. Detect **conflict of interest** — a quantitative claim that makes a category look good,
   sourced from a domain that *sells in that category*.
3. Verdict per claim; **FAIL the gate** if any quantitative claim is self-interested and not
   backed by a primary cite.

Run:
```
python3 source_credibility_gate.py          # heuristic, no API key needed
python3 source_credibility_gate.py --judge   # + Haiku cross-check (needs ANTHROPIC_API_KEY)
```

## Result on the real dogfood claims
Seeded with the actual claims from `dogfood_run_01.md` and `dogfood_run_02.md`. The gate
**failed** (exit 1) and flagged **6 self-interested** stats, including:
- "$201,600/yr from switching billers" — listerventures.com (an RCM vendor) ✓ the one I caught by hand
- in-house billing "$55-80K/yr" — carecloud.com (sells outsourced billing)
- denial rate "12% / 15-18%" — aptarro.com (sells denial management) — **a number I had rated
  "med-high confidence" by hand; the gate is stricter than I was, correctly**

**The finding I did NOT catch by hand:** run 01's two marquee stats — "62% of calls unanswered"
and "78% buy from the first responder" — both came from **getaira.io, an AI-receptionist vendor**.
I leaned on them as credible in run 01. The gate flags both. So the laundering problem wasn't
unique to the unfamiliar niche; it was in the *familiar* run too, and I missed it because the
numbers matched my priors. **That is exactly the value of an automated gate: it doesn't have priors.**

## What this proves / what's still stubbed
- **Proves:** a cheap deterministic layer (domain registry + COI rule) catches the moat-killing
  failure on real data, with no LLM call. This is shippable as the v1 spine.
- **Stubbed for production:**
  - Domain → tier/seller-category is a hand-built registry. Production needs (a) a maintained
    allowlist of primary sources, (b) an LLM classifier for unknown domains (the `--judge` path
    is the seed of this — routed to Haiku, a classify task), (c) detecting the *category a claim
    promotes* automatically rather than from hand-tags.
  - "Demand a primary cite" is a verdict, not yet an action. Production should trigger a
    re-search for a primary source, or down-rank the claim to "marketing claim, unverified" in
    the artifact.
- **Next:** wire this gate as a stage in the unattended prompt→artifact pipeline, then measure
  what fraction of a fresh run's claims survive it (the real quality metric for the product).

## Update — the unattended pipeline now exists (`pipeline.py`)
`pipeline.py` is the live, no-human-in-the-loop stage chain: **Haiku research fan-out (real
`web_search`) → Sonnet synthesis → this gate → Haiku re-search of flagged claims.** It meters
every API call so it prints a real $/run. Run on a fresh niche (independent property managers):
- Produced a complete sellable artifact set unattended (`live_run_artifacts.md`).
- Gate flagged **79%** of raw research as self-interested/non-primary (the moat finding holds on
  a 3rd niche); re-search lifted clean cites **21% → 57%**.
- **Measured ~$1.05/run** vs the $0.21 token-only estimate — re-search (72% of cost) and
  web-search fees were unmodeled. See `test_01_live_results.md`.
- Confirmed the "stubbed for production" warning above: the hand registry doesn't generalize to
  fresh niches, so survival is scored **judge-led**, not registry-led. The judge/allowlist must
  replace the hand registry in production.
