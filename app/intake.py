#!/usr/bin/env python3
"""
Intake + vet — the two stages that run BEFORE the engine builds anything.

Why this exists: the engine assumed its input was already one coherent business
idea (see prototype/pipeline.py DEFAULT_IDEA — a full paragraph). Hand it a pile
of hobbies ("basketball, Magic: The Gathering, food, and I'm good at sales") and
it Frankensteins them into one nonsense offer. There was no stage that turned
vague input into a focused thesis, and no stage willing to say an idea is weak.

  shape(idea) -> focus a grab-bag into ONE researchable thesis (intake skill).
  vet(idea, shaped, research) -> pursue / pivot / kill verdict (vet skill).

Both run on Sonnet over a cached skill system block (app/skills/intake, /vet) and
have mock paths so the app's mock mode and these self-tests need no API key.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # app/ on path → bare sibling imports
import skill_registry as skills  # noqa: E402

_MOCK_SHAPED = {
    "coherent": True,
    "thesis": "A done-for-you sales service for local businesses that are good at their craft but "
              "bad at following up on leads — you run the outreach and closing they won't.",
    "founder_edge": "You're good at sales — that's the transferable advantage, not the hobbies.",
    "wedges_considered": ["sales-as-a-service for local businesses",
                          "coaching others to sell", "a sales-focused newsletter"],
    "clarifying_question": None,
}

_MOCK_VET = {
    "verdict": "pursue",
    "scores": {"demand": 4, "market": 4, "willingness_to_pay": 4, "founder_fit": 5, "execution_risk": 3},
    "reason": "Real, proven demand for outsourced sales and a clear founder edge; the risk is "
              "operational, not whether anyone wants it.",
    "biggest_risk": "Whether you can get results for a client without their product/brand doing the work.",
    "first_test": "DM 20 local owner-operators offering to book 5 sales calls in a week for a flat fee; "
                  "count how many say yes.",
    "ninety_day_win": "3 paying clients on a monthly retainer with at least one renewal.",
}


def shape(idea: str, mock: bool = False) -> tuple[dict, float]:
    """Intake: turn whatever the operator typed into ONE focused thesis. Returns (shaped, cost).
    Never blends unrelated interests — picks the strongest wedge and names the rest."""
    if mock:
        return dict(_MOCK_SHAPED), 0.0
    from pipeline import LEDGER, call, extract_json, SONNET  # heavy; real mode only
    start = len(LEDGER.rows)
    out = call("intake", SONNET, max_tokens=600, system=skills.system("intake"), cache=True,
               prompt=f"The operator typed this in plain text:\n\n{idea}\n\nShape it now.")
    data = extract_json(out) or {}
    shaped = {
        "coherent": bool(data.get("coherent", True)),
        "thesis": (data.get("thesis") or idea).strip(),
        "founder_edge": (data.get("founder_edge") or "").strip(),
        "wedges_considered": [w for w in (data.get("wedges_considered") or []) if w][:4],
        "clarifying_question": (data.get("clarifying_question") or None),
    }
    return shaped, round(LEDGER.cost_slice(start), 4)


def vet(idea: str, shaped: dict, research: dict | None = None, mock: bool = False) -> tuple[dict, float]:
    """Kill-gate: score the shaped idea and return pursue/pivot/kill. `research` (optional) is the
    teardown result — its cleared evidence grounds the verdict. Returns (vetting, cost)."""
    if mock:
        return dict(_MOCK_VET), 0.0
    from pipeline import LEDGER, call, extract_json, SONNET
    start = len(LEDGER.rows)
    cited = ""
    if research and research.get("rows"):
        cleared = [r["text"] for r in research["rows"] if r.get("mark") == "ok"]
        cited = "\n".join(f"- {t}" for t in cleared) or "- (none cleared)"
    out = call("vet", SONNET, max_tokens=700, system=skills.system("vet"), cache=True, prompt=(
        f"OPERATOR'S RAW INPUT:\n{idea}\n\nFOCUSED THESIS (from intake):\n{shaped.get('thesis', '')}\n\n"
        f"FOUNDER EDGE:\n{shaped.get('founder_edge', '(none named)')}\n\n"
        f"GATE-CLEARED EVIDENCE (context only):\n{cited or '- (no research yet)'}\n\nVet it now."))
    data = extract_json(out) or {}
    verdict = str(data.get("verdict", "pursue")).lower()
    if verdict not in ("pursue", "pivot", "kill"):
        verdict = "pursue"
    return {
        "verdict": verdict,
        "scores": data.get("scores") or {},
        "reason": (data.get("reason") or "").strip(),
        "biggest_risk": (data.get("biggest_risk") or "").strip(),
        "first_test": (data.get("first_test") or "").strip(),
        "ninety_day_win": (data.get("ninety_day_win") or "").strip(),
    }, round(LEDGER.cost_slice(start), 4)


if __name__ == "__main__":  # self-test (mock, no API)
    grab_bag = "I like basketball, Magic the Gathering, and food, and I'm good at sales"
    shaped, c = shape(grab_bag, mock=True)
    assert c == 0.0 and shaped["coherent"] and shaped["thesis"]
    assert "sales" in shaped["founder_edge"].lower()           # leads with the real edge, not hobbies
    assert len(shaped["wedges_considered"]) >= 2               # shows it chose, didn't blend
    vetting, c2 = vet(grab_bag, shaped, None, mock=True)
    assert vetting["verdict"] in ("pursue", "pivot", "kill")
    assert vetting["scores"]["founder_fit"] == 5 and vetting["first_test"]
    # skill bodies are real and used as the system blocks
    assert "Frankenstein" in skills.system("intake") and "kill-gate" in skills.system("vet")
    print("intake.py self-test OK — shape + vet (mock); verdict:", vetting["verdict"])
