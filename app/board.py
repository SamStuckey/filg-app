#!/usr/bin/env python3
"""
Board of Directors — multi-advisor vetting + a collaboration matrix.

"Ask an expert" is one persona, one-off (planner.ask_expert). The Board is the
standing version: the operator picks a SET of directors once, and the board vets
the plan — either a whole step or a specific question. This is the orchestration
layer the broader goal needs: each director is consulted (in parallel — the
subagent/fan-out pattern), then a synthesis pass turns the separate takes into
one collaboration matrix (where they agree, where they conflict, the net call).

Design notes for the roadmap this seeds:
  - Directors are personas (app/personas.py); their domains drive `route()`, so
    a future "who should weigh in on pricing?" router already has its input.
  - The directors run independently and a final synthesis reconciles them — that
    two-layer shape (consult → reconcile) is the same shape a deeper hierarchy of
    skill-backed experts will use; only the population grows.
  - Real mode fans the director calls out in parallel and meters each; mock mode
    returns canned takes so the app's mock mode and these self-tests cost nothing.

Pay-gating (e.g. Board is an Operator-tier feature) is a policy decision for the
caller (app/main.py) — this module just runs the board.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # app/ on path → bare sibling imports
import personas  # noqa: E402
import skill_registry as skills  # noqa: E402


def _director_take(idea: str, plan_text: str, focus: str, key: str) -> tuple[dict, float]:
    """One director's take. Real mode: one Sonnet call under that persona's system block."""
    from pipeline import LEDGER, call, SONNET
    start = len(LEDGER.rows)
    p = personas.get(key)
    ans = call(f"board_{key}", SONNET, max_tokens=450, system=personas.system_for(key), cache=True,
               prompt=(f"IDEA:\n{idea}\n\nPLAN SO FAR:\n{plan_text or '(early — little built yet)'}\n\n"
                       f"WHAT TO WEIGH IN ON:\n{focus}\n\n"
                       "Give your take in 2-4 punchy sentences from your persona's focus. Lead with the "
                       "one thing you'd change. If this is outside your lane, say so in one line."))
    return {"key": key, "name": p["name"], "first": p.get("first"), "take": ans.strip()}, \
        round(LEDGER.cost_slice(start), 4)


def _synthesize(idea: str, focus: str, takes: list[dict]) -> tuple[dict, float]:
    """Reconcile the directors' takes into a collaboration matrix (agreement / conflict / net call)."""
    from pipeline import LEDGER, call, extract_json, SONNET
    start = len(LEDGER.rows)
    board_block = "\n\n".join(f"### {t['name']}\n{t['take']}" for t in takes)
    out = call("board_synth", SONNET, max_tokens=600, prompt=(
        "You are the chair synthesizing a board of composite advisors. Below are each director's take "
        "on the same question. Produce strictly this JSON, no preamble:\n"
        '{"consensus": "what they agree on (1-2 sentences)", '
        '"conflicts": "where they disagree and the tradeoff (1-2 sentences, or \'none\' )", '
        '"verdict": "the net recommendation to the operator (1-2 sentences, decisive)"}\n\n'
        f"QUESTION:\n{focus}\n\nIDEA:\n{idea}\n\nDIRECTORS' TAKES:\n{board_block}"))
    data = extract_json(out)
    data = data if isinstance(data, dict) else {}
    return ({"consensus": (data.get("consensus") or "").strip(),
             "conflicts": (data.get("conflicts") or "none").strip(),
             "verdict": (data.get("verdict") or "").strip()},
            round(LEDGER.cost_slice(start), 4))


_MOCK_BOARD = {
    "closer": "Price it higher and lead with the outcome, not the hours. You're leaving money on the "
              "table with a flat low fee.",
    "bootstrapper": "Sell it before you build any of it — get one paying client this week, then make "
                    "the delivery repeatable.",
    "cfo": "The margin only works if you can deliver in under ~5 hours a client. Model that before you "
           "promise a flat price.",
}


def convene(idea: str, plan_text: str, focus: str, director_keys: list[str] | None = None,
            mock: bool = False) -> tuple[dict, float]:
    """Run the board on `focus`. `director_keys` defaults to the starter board. Returns
    ({directors, consensus, conflicts, verdict, disclaimer}, total_cost)."""
    keys = [k for k in (director_keys or personas.DEFAULT_BOARD) if k in personas.KEYS] \
        or personas.DEFAULT_BOARD
    if mock:
        directors = [{"key": k, "name": personas.get(k)["name"], "first": personas.get(k).get("first"),
                      "take": _MOCK_BOARD.get(k, f"{personas.get(k)['name']} would push on the "
                                                  f"{personas.get(k)['domains'][0]} angle here.")}
                     for k in keys]
        result = {"directors": directors,
                  "consensus": "Charge more, sell before building, and prove you can deliver fast.",
                  "conflicts": "The Closer wants premium pricing; the CFO wants the delivery math "
                               "proven first.",
                  "verdict": "Pursue — but land one paying client at a higher price this week and "
                             "track your hours-to-deliver.",
                  "disclaimer": personas.DISCLAIMER}
        return result, 0.0

    cost = 0.0
    with ThreadPoolExecutor(max_workers=min(4, len(keys))) as ex:  # fan-out: the subagent pattern
        results = list(ex.map(lambda k: _director_take(idea, plan_text, focus, k), keys))
    directors = [r for r, _ in results]
    cost += sum(c for _, c in results)
    matrix, c = _synthesize(idea, focus, directors)
    cost += c
    return {"directors": directors, **matrix, "disclaimer": personas.DISCLAIMER}, round(cost, 4)


def review_section(idea: str, section_title: str, section_draft: str, plan_text: str,
                   director_keys: list[str] | None = None, mock: bool = False) -> tuple[dict, float]:
    """Board reviews ONE finalized plan section — the 'each step gets vetted by your board' behavior."""
    focus = (f"Review this section of the plan — '{section_title}'. Is it right? What would you change?"
             f"\n\nSECTION DRAFT:\n{section_draft}")
    return convene(idea, plan_text, focus, director_keys, mock=mock)


if __name__ == "__main__":  # self-test (mock, no API)
    res, cost = convene("sales-as-a-service for local businesses", "## Offer\nFlat monthly retainer.",
                        "Is the pricing right?", ["closer", "cfo"], mock=True)
    assert cost == 0.0 and len(res["directors"]) == 2
    assert res["verdict"] and res["consensus"] and "composite" in res["disclaimer"].lower()
    assert {d["key"] for d in res["directors"]} == {"closer", "cfo"}
    # bad keys fall back to the default board, never empty
    res2, _ = convene("idea", "", "anything", ["nonsense"], mock=True)
    assert res2["directors"] and len(res2["directors"]) == len(personas.DEFAULT_BOARD)
    rev, _ = review_section("idea", "What you charge", "## Pricing\n$500/mo", "", ["closer"], mock=True)
    assert rev["directors"][0]["key"] == "closer"
    print("board.py self-test OK — convene + review_section (mock)")
