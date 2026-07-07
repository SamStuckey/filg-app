#!/usr/bin/env python3
"""
Standalone live pipeline run — the Test #1 measurement harness (moved out of the engine).

Runs the unattended chain (plan -> research fan-out -> synthesis -> gate -> re-search) on one
plain-text idea with a real ANTHROPIC_API_KEY, prints post-gate survival quality + measured
$/run, and writes test_01_live_results_auto.md. Dev tool only — the app never imports this.

Run:  python3 scripts/pipeline_live_run.py "your idea in plain text"
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> `engine` package

from engine.pipeline import (  # noqa: E402
    LEDGER, WEB_SEARCH_PRICE, Claim, Rescue, SONNET, bound, call, gate_claims,
    plan, research_lane, research_primary,
)

# --- Stage 3: SYNTH ----------------------------------------------------------
def synthesize(idea: str, claims: list[Claim]) -> str:
    research_block = "\n".join(
        f"- {c.text}  [{c.source_url}]" for c in claims)
    return call("synth", SONNET, max_tokens=3500, prompt=(
        "You are the synthesis stage of an Idea→Offer engine. From the operator's idea "
        "and the cited research below, produce a sellable artifact set in markdown: "
        "(1) structured brief, (2) offer definition, (3) packaging + pricing, "
        "(4) go-to-market, (5) delivery playbook, (6) 30-day roadmap. Cite the research "
        "inline with its URL. Where a number comes from a source that profits if you "
        "believe it, mark it '(unverified vendor claim)'. Be concrete and specific.\n\n"
        f"IDEA:\n{idea}\n\nCITED RESEARCH:\n{research_block}"
    ))




# --- Orchestrator ------------------------------------------------------------
DEFAULT_IDEA = (
    "I'm good with automation and AI tools. I've noticed that small, independent "
    "property-management companies (the ones managing a few hundred rental units) are "
    "slow to respond to tenant maintenance requests and leasing inquiries, and they "
    "lose tenants and prospective renters because of it. I think I could sell them "
    "something AI-powered, but I don't know exactly what the offer is or how I'd sell it."
)


def main() -> int:
    idea = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IDEA
    t0 = time.time()
    print("\n=== LIVE PIPELINE: Idea → Offer (unattended) ===")
    print(f"\nPROMPT (fresh niche):\n{idea}\n")

    # 1. PLAN
    lanes = plan(idea)
    print("PLAN — research lanes:")
    for ln in lanes:
        print(f"  • {ln}")

    # 2. RESEARCH fan-out (parallel)
    print("\nRESEARCH — Haiku fan-out (live web_search, parallel)…")
    with ThreadPoolExecutor(max_workers=3) as ex:
        # bound() re-binds the active provider inside each worker (threads don't inherit contextvars),
        # so a BYOK run's fan-out still runs on the user's key.
        lane_claims = list(ex.map(bound(lambda ln: research_lane(idea, ln)), lanes))
    claims = [c for lane in lane_claims for c in lane]
    quant = [c for c in claims if c.quantitative]
    print(f"  {len(claims)} claims gathered ({len(quant)} quantitative).")

    # 3. SYNTH
    print("\nSYNTH — Sonnet artifact set…")
    artifacts = synthesize(idea, claims)
    with open("live_run_artifacts.md", "w") as f:
        f.write(f"# Live run — artifact set\n\n**Prompt:** {idea}\n\n---\n\n{artifacts}\n")
    print(f"  artifact set written ({len(artifacts)} chars) → live_run_artifacts.md")

    # 4. GATE
    print("\nGATE — credibility check on quantitative claims (heuristic + Haiku judge)…")
    verdicts = gate_claims(quant)  # one batched judge call for all claims (token win)
    flagged = [v for v in verdicts if v.flagged]
    clean = [v for v in verdicts if not v.flagged]
    for v in verdicts:
        mark = "FLAG" if v.flagged else ("OK  " if v in clean else "weak")
        host = urlparse(v.claim.source_url).netloc.removeprefix("www.")
        print(f"  [{mark}] {host:<28} {v.claim.text[:70]}")
    n = len(verdicts) or 1
    print(f"\n  pre-re-search: {len(clean)}/{n} clean ({len(clean)/n:.0%}), "
          f"{len(flagged)} flagged ({len(flagged)/n:.0%})")

    # 5. RE-SEARCH flagged → survival
    print("\nRE-SEARCH — forcing a primary/neutral cite for each flagged claim…")
    rescues: list[Rescue] = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(bound(lambda v: research_primary(v.claim)), flagged))
    for v, r in zip(flagged, results):
        r.original = v
        rescues.append(r)
        host = urlparse(r.new_url).netloc.removeprefix("www.") if r.new_url else "(none found)"
        print(f"  [{'RESCUED' if r.rescued else 'still weak'}] "
              f"{v.claim.text[:55]} → {host} (tier={r.new_tier}, judge={r.new_judge})")

    rescued = [r for r in rescues if r.rescued]
    survived = len(clean) + len(rescued)
    survival_q = survived / n
    rescue_rate = (len(rescued) / len(flagged)) if flagged else 0.0

    # --- Report ---
    cost = LEDGER.cost()
    elapsed = time.time() - t0
    print("\n=== RESULTS ===")
    print(f"(a) POST-GATE SURVIVAL QUALITY")
    print(f"    initial clean         : {len(clean)}/{n} ({len(clean)/n:.0%})")
    print(f"    flagged → re-searched : {len(flagged)}")
    print(f"    rescued to primary    : {len(rescued)}/{len(flagged) or 0} "
          f"(rescue rate {rescue_rate:.0%})")
    print(f"    >> SURVIVAL after gate: {survived}/{n} = {survival_q:.0%} "
          f"land on a primary/neutral cite")
    print(f"\n(b) MEASURED $/RUN")
    for stage, c in sorted(LEDGER.breakdown().items(), key=lambda x: -x[1]):
        print(f"    {stage:<10} ${c:.4f}")
    print(f"    web searches: {LEDGER.searches()}  (${LEDGER.searches()*WEB_SEARCH_PRICE:.4f})")
    print(f"    >> TOTAL    : ${cost:.4f}/run   (model estimate was $0.21)")
    print(f"\n    wall-clock: {elapsed:.0f}s")

    write_results_md(idea, lanes, verdicts, clean, flagged, rescues,
                     survived, n, survival_q, rescue_rate, cost, elapsed)
    print("\n  results written → test_01_live_results_auto.md\n")
    return 0


def write_results_md(idea, lanes, verdicts, clean, flagged, rescues,
                     survived, n, survival_q, rescue_rate, cost, elapsed) -> None:
    bd = LEDGER.breakdown()
    n_rescued = len([r for r in rescues if r.rescued])
    lines = [
        "# Test #1 (live) — post-gate survival quality + measured $/run",
        "",
        "Ran the thin unattended pipeline (Haiku research fan-out → Sonnet synthesis → "
        "source-credibility gate) end-to-end on one fresh prompt, with real `web_search`. "
        "This closes the open number from `test_01_results.md`.",
        "",
        f"**Prompt (fresh niche — not seen in either dogfood run):** {idea}",
        "",
        "**Research lanes (auto-planned):**",
        *[f"- {ln}" for ln in lanes],
        "",
        "## (a) Post-gate survival quality — THE open number",
        "",
        f"- Quantitative claims gated: **{n}**",
        f"- Clean on first pass: **{len(clean)}/{n} ({len(clean)/n:.0%})**",
        f"- Flagged (self-interested / non-primary) → forced re-search: **{len(flagged)}**",
        f"- Rescued to a primary/neutral cite: **{n_rescued}/{len(flagged)}** "
        f"(rescue rate **{rescue_rate:.0%}**)",
        f"- **Survival after the gate: {survived}/{n} = {survival_q:.0%}** of cited "
        "quantitative claims land on a primary/neutral source.",
        "",
        "### Per-claim",
        "",
        "| verdict | source | claim | judge |",
        "|---|---|---|---|",
    ]
    for v in verdicts:
        host = urlparse(v.claim.source_url).netloc.removeprefix("www.")
        mark = "FLAG" if v.flagged else ("clean" if v in clean else "weak")
        lines.append(f"| {mark} | {host} | {v.claim.text[:80]} | {v.judge} |")
    lines += ["", "### Re-search outcomes", "",
              "| original claim | new source | tier | judge | rescued |",
              "|---|---|---|---|---|"]
    for r in rescues:
        host = urlparse(r.new_url).netloc.removeprefix("www.") if r.new_url else "(none)"
        lines.append(f"| {r.original.claim.text[:60]} | {host} | {r.new_tier} | "
                     f"{r.new_judge} | {'yes' if r.rescued else 'no'} |")
    lines += [
        "",
        "## (b) Measured $/run vs the $0.21 estimate",
        "",
        "| stage | cost |",
        "|---|--:|",
        *[f"| {s} | ${c:.4f} |" for s, c in sorted(bd.items(), key=lambda x: -x[1])],
        f"| **total** | **${cost:.4f}** |",
        "",
        f"- Model estimate (`pipeline_economics.py`, lean routing): **$0.21/run**.",
        f"- Measured live: **${cost:.4f}/run** ({LEDGER.searches()} web searches "
        f"@ $0.01 included).",
        f"- Wall-clock: {elapsed:.0f}s.",
        "",
        "## Caveats",
        "- One run, one niche — survival % is indicative, not a distribution. Re-run "
        "across niches to get a confidence interval.",
        "- Web-search server-tool cost ($10/1k searches) is included here but was NOT in "
        "the $0.21 token-only model — see the breakdown above for the split.",
        "- Re-search success depends on whether a neutral source actually publishes the "
        "fact; a 'still weak' outcome can mean the number only exists in vendor marketing "
        "(itself a useful signal to down-rank the claim).",
    ]
    # Auto-generated machine output. The curated file of record (with interpretation)
    # is test_01_live_results.md — kept separate so a re-run doesn't wipe the analysis.
    with open("test_01_live_results_auto.md", "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
