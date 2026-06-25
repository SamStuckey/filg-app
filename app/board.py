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


_VERDICTS = ("agree", "concern", "dissent", "non-starter")


def _skeptic_take(idea: str, plan_text: str, focus: str) -> tuple[dict, float]:
    """The standing adversary's structured verdict. Ported from biz-skeptic + business-manager G3/G4:
    a counterfactual-CoT prompt (premortem / inversion / strongest objection) that forces a committed
    verdict, NOT an agreeable take. Its rationale is preserved verbatim downstream (G7)."""
    from pipeline import LEDGER, call, extract_json, SONNET
    start = len(LEDGER.rows)
    p = personas.get(personas.SKEPTIC_KEY)
    out = call("board_skeptic", SONNET, max_tokens=400, system=personas.system_for(personas.SKEPTIC_KEY),
               cache=True, prompt=(
        f"IDEA:\n{idea}\n\nPLAN SO FAR:\n{plan_text or '(early — little built yet)'}\n\n"
        f"WHAT TO PRESSURE-TEST:\n{focus}\n\n"
        "Run your three checks silently, then COMMIT to a verdict. Imagine you strongly disagree with "
        "this plan — what is your single strongest objection? Output STRICTLY this JSON, no preamble:\n"
        '{"verdict": "agree | concern | dissent | non-starter", '
        '"rationale": "2-3 blunt sentences in your own voice — the premortem cause and your strongest '
        'objection", '
        '"suggested_change": "the one change that most reduces the risk (\'\' if you agree)", '
        '"confidence": "low | medium | high"}\n\n'
        "verdict meaning: agree = no real objection; concern = a fixable weakness; dissent = you think "
        "the plan is wrong but still buildable; non-starter = a structural flaw that should stop the "
        "build. Default to concern when unsure — never rubber-stamp."))
    data = extract_json(out)
    data = data if isinstance(data, dict) else {}
    verdict = (data.get("verdict") or "concern").strip().lower()
    if verdict not in _VERDICTS:
        verdict = "concern"
    return {"key": personas.SKEPTIC_KEY, "name": p["name"], "first": p.get("first"),
            "verdict": verdict,
            "rationale": (data.get("rationale") or "").strip(),
            "suggested_change": (data.get("suggested_change") or "").strip(),
            "confidence": (data.get("confidence") or "medium").strip().lower()}, \
        round(LEDGER.cost_slice(start), 4)


def _synthesize(idea: str, focus: str, takes: list[dict], skeptic: dict | None = None) -> tuple[dict, float]:
    """Reconcile the directors' takes into a collaboration matrix (agreement / conflict / net call).
    The skeptic's objection is fed in so the net verdict ACCOUNTS for it — but the skeptic's own
    rationale is preserved verbatim by the caller (G7), never paraphrased away here."""
    from pipeline import LEDGER, call, extract_json, SONNET
    start = len(LEDGER.rows)
    board_block = "\n\n".join(f"### {t['name']}\n{t['take']}" for t in takes)
    skeptic_block = ""
    if skeptic and skeptic.get("rationale"):
        skeptic_block = (f"\n\nTHE SKEPTIC'S OBJECTION (verdict: {skeptic['verdict']}):\n"
                         f"{skeptic['rationale']}\n"
                         "Weigh this objection in your net verdict — do not ignore it or paper over it.")
    out = call("board_synth", SONNET, max_tokens=600, system=skills.VOICE, prompt=(
        "You are the chair synthesizing a board of composite advisors. Below are each director's take "
        "on the same question, plus the standing skeptic's objection. Produce strictly this JSON, no "
        "preamble:\n"
        '{"consensus": "what they agree on (1-2 sentences)", '
        '"conflicts": "where they disagree and the tradeoff, including the skeptic if relevant (1-2 '
        'sentences, or \'none\' )", '
        '"verdict": "the net recommendation to the operator (1-2 sentences, decisive)"}\n\n'
        f"QUESTION:\n{focus}\n\nIDEA:\n{idea}\n\nDIRECTORS' TAKES:\n{board_block}{skeptic_block}"))
    data = extract_json(out)
    data = data if isinstance(data, dict) else {}
    return ({"consensus": (data.get("consensus") or "").strip(),
             "conflicts": (data.get("conflicts") or "none").strip(),
             "verdict": (data.get("verdict") or "").strip()},
            round(LEDGER.cost_slice(start), 4))


_MOCK_BOARD = {
    "closer": "Price it higher and lead with the outcome, not the hours. You're leaving money on the "
              "table with a flat low fee.",
    "bootstrapper": "Sell it before you build any of it. Get one paying client this week, then make "
                    "the delivery repeatable.",
    "cfo": "The margin only works if you can deliver in under ~5 hours a client. Model that before you "
           "promise a flat price.",
}

_MOCK_SKEPTIC = {
    "key": "skeptic", "name": "The Skeptic", "first": "Vince",
    "verdict": "concern",
    "rationale": "I've watched this exact play die: the founder sells a flat price, then a client eats "
                 "12 hours and the margin's gone. The plan assumes delivery stays cheap, and that's the "
                 "first thing that breaks.",
    "suggested_change": "Cap scope per client and price the outcome, not a flat retainer, before you "
                        "sign anyone.",
    "confidence": "high",
}


def convene(idea: str, plan_text: str, focus: str, director_keys: list[str] | None = None,
            mock: bool = False, skeptic: bool = True) -> tuple[dict, float]:
    """Run the board on `focus`. `director_keys` defaults to the starter board. The Skeptic is a
    STANDING seat — always present unless `skeptic=False` — so every board has an adversary in the
    room. Returns ({directors, skeptic, consensus, conflicts, verdict, disclaimer}, total_cost)."""
    keys = [k for k in (director_keys or personas.DEFAULT_BOARD)
            if k in personas.KEYS and k != personas.SKEPTIC_KEY] or personas.DEFAULT_BOARD
    if mock:
        directors = [{"key": k, "name": personas.get(k)["name"], "first": personas.get(k).get("first"),
                      "take": _MOCK_BOARD.get(k, f"{personas.get(k)['name']} would push on the "
                                                  f"{personas.get(k)['domains'][0]} angle here.")}
                     for k in keys]
        result = {"directors": directors,
                  "skeptic": dict(_MOCK_SKEPTIC) if skeptic else None,
                  "consensus": "Charge more, sell before building, and prove you can deliver fast.",
                  "conflicts": "The Closer wants premium pricing; the CFO and the Skeptic want the "
                               "delivery math proven first.",
                  "verdict": "Pursue, but land one paying client at a higher price this week and "
                             "track your hours-to-deliver.",
                  "disclaimer": personas.DISCLAIMER}
        return result, 0.0

    cost = 0.0
    # fan-out: the subagent pattern. The skeptic runs alongside the directors in the same wave.
    with ThreadPoolExecutor(max_workers=min(5, len(keys) + 1)) as ex:
        dfut = [ex.submit(_director_take, idea, plan_text, focus, k) for k in keys]
        sfut = ex.submit(_skeptic_take, idea, plan_text, focus) if skeptic else None
        results = [f.result() for f in dfut]
        sk, sc = sfut.result() if sfut else (None, 0.0)
    directors = [r for r, _ in results]
    cost += sum(c for _, c in results) + sc
    matrix, c = _synthesize(idea, focus, directors, sk)
    cost += c
    return {"directors": directors, "skeptic": sk, **matrix,
            "disclaimer": personas.DISCLAIMER}, round(cost, 4)


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
    # the skeptic is a STANDING seat — always present, with a structured verdict
    assert res["skeptic"] and res["skeptic"]["key"] == "skeptic"
    assert res["skeptic"]["verdict"] in _VERDICTS and res["skeptic"]["rationale"]
    # asking for the skeptic as a "director" must NOT duplicate it among the lanes
    res_dup, _ = convene("idea", "", "anything", ["closer", "skeptic"], mock=True)
    assert [d["key"] for d in res_dup["directors"]] == ["closer"] and res_dup["skeptic"]["key"] == "skeptic"
    # skeptic can be turned off
    res_off, _ = convene("idea", "", "anything", ["closer"], mock=True, skeptic=False)
    assert res_off["skeptic"] is None
    # bad keys fall back to the default board, never empty
    res2, _ = convene("idea", "", "anything", ["nonsense"], mock=True)
    assert res2["directors"] and len(res2["directors"]) == len(personas.DEFAULT_BOARD)
    rev, _ = review_section("idea", "What you charge", "## Pricing\n$500/mo", "", ["closer"], mock=True)
    assert rev["directors"][0]["key"] == "closer" and rev["skeptic"]["key"] == "skeptic"
    print("board.py self-test OK — convene + review_section + standing skeptic (mock)")
