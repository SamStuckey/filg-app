#!/usr/bin/env python3
"""
Test #1 — the kill-gate within the kill-gate (see ../vet.md).

Question the vet flagged as load-bearing: can the unattended pipeline produce
gated, sellable artifacts cheaply enough that a ~$39/mo self-serve tier has
margin? Two numbers decide it:
  (a) gate survival rate  — how much of raw research is trustworthy
  (b) $/run               — cost of one full idea→artifact run

This computes both from REAL data (no API key needed):
  - token usage = the actual subagent_tokens the 3-agent research fan-out
    reported on both dogfood runs this session (provenance below)
  - prices      = published per-MTok rates (Haiku/Sonnet/Opus)
  - survival    = the credibility gate run on the real dogfood claims

Run:  python3 pipeline_economics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> `engine` package

from engine.source_credibility_gate import (  # noqa: E402
    CLAIMS, evaluate, PASS_HIGH, PASS_OK, FLAG_WEAK, FLAG_SELF_INTERESTED,
)

# --- Prices: USD per 1M tokens (input, output) -------------------------------
PRICES = {
    "haiku":  (1.0, 5.0),
    "sonnet": (3.0, 15.0),
    "opus":   (5.0, 25.0),
}

# --- Observed research cost (provenance) -------------------------------------
# subagent_tokens reported by the 3-agent research fan-out this session:
#   run 01 dogfood: 25271, 27202, 27877
#   run 02 dogfood: 30127, 26613, 25678
# mean ≈ 27,128 tokens/agent. Web-research agents are input-heavy (fetched
# pages) with a small ~500-word digest out → split ~26k in / 1.5k out per agent.
RESEARCH_AGENTS = 3
RESEARCH_IN_PER_AGENT = 26_000
RESEARCH_OUT_PER_AGENT = 1_500

# Synthesis: one call over the 3 digests → the full artifact set.
SYNTH_IN = 10_000
SYNTH_OUT = 5_000

# Gate: classify unknown domains (production --judge path). Tiny.
GATE_IN = 3_000
GATE_OUT = 300


def cost(model: str, tin: int, tout: int) -> float:
    pin, pout = PRICES[model]
    return tin / 1e6 * pin + tout / 1e6 * pout


def run_cost(research_model: str, synth_model: str, gate_model: str, stress: float = 1.0) -> float:
    research = RESEARCH_AGENTS * cost(
        research_model, int(RESEARCH_IN_PER_AGENT * stress), int(RESEARCH_OUT_PER_AGENT * stress))
    synth = cost(synth_model, int(SYNTH_IN * stress), int(SYNTH_OUT * stress))
    gate = cost(gate_model, int(GATE_IN * stress), int(GATE_OUT * stress))
    return research + synth + gate


SCENARIOS = {
    "cheap   (research=haiku, synth=haiku)":  ("haiku", "haiku", "haiku"),
    "lean    (research=haiku, synth=sonnet)": ("haiku", "sonnet", "haiku"),
    "premium (research=haiku, synth=opus)":   ("haiku", "opus", "haiku"),
}

TIER_PRICE = 39.0   # $/mo self-serve
FREE_RUNS = 1       # free tier = 1 capped idea


def survival():
    results = [evaluate(c) for c in CLAIMS]
    n = len(results)
    clean = sum(r.verdict in (PASS_HIGH, PASS_OK) for r in results)
    weak = sum(r.verdict == FLAG_WEAK for r in results)
    bad = sum(r.verdict == FLAG_SELF_INTERESTED for r in results)
    return n, clean, weak, bad


def main() -> None:
    print("\n=== TEST #1: gate survival + $/run ===\n")

    n, clean, weak, bad = survival()
    print("(a) GATE SURVIVAL — raw research claims, pre-re-search")
    print(f"    {clean}/{n} clean ({clean/n:.0%})  |  {weak} need a cross-cite  |  {bad} self-interested ({bad/n:.0%})")
    print("    → raw vendor-heavy research is mostly self-interested; the gate is load-bearing,")
    print("      not optional. The live test must measure survival AFTER the gate forces re-search.\n")

    print("(b) $/RUN — one full idea→artifact run (real token usage)")
    print(f"    {'scenario':<40} {'$/run':>8} {'×2 stress':>10} {'runs to burn $39':>18} {'free-tier $/signup':>20}")
    for label, (rm, sm, gm) in SCENARIOS.items():
        c = run_cost(rm, sm, gm)
        c2 = run_cost(rm, sm, gm, stress=2.0)
        runs_to_burn = TIER_PRICE / c
        free_cost = c * FREE_RUNS
        print(f"    {label:<40} ${c:>6.3f} ${c2:>8.3f} {runs_to_burn:>16.0f} ${free_cost:>17.3f}")

    print("\n--- READ ---")
    lean = run_cost(*SCENARIOS["lean    (research=haiku, synth=sonnet)"])
    print(f"    A full run on the lean routing costs ~${lean:.2f}. A $39/mo subscriber would need to")
    print(f"    run ~{TIER_PRICE/lean:.0f} ideas/month before COGS eats the sub — far above realistic usage.")
    print(f"    A free-tier signup (1 capped idea) costs ~${lean*FREE_RUNS:.2f}. Even at 2× stress it stays")
    print(f"    under ${run_cost(*SCENARIOS['lean    (research=haiku, synth=sonnet)'], stress=2.0):.2f}.")
    print("\n    KILL-GATE on cost: PASS — deep research is cheap because the fan-out runs on Haiku;")
    print("    only the synthesis touches a pricier model. Margin is not the thing that kills this.")
    print("    The remaining open number is POST-GATE survival quality, which needs a live run.\n")

    print("    CAVEATS: token splits are estimates off observed totals; synthesis tokens are modeled,")
    print("    not measured; excludes infra/egress and prompt-cache savings (which only help). Re-run")
    print("    with live numbers once the unattended pipeline exists.")


if __name__ == "__main__":
    main()
