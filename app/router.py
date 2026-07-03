#!/usr/bin/env python3
"""
Router — the single prompt box that always wins.

Every free-text prompt in the unified build surface goes through `route()`, which reads
the prompt against WHERE the user is (stage) and the active tool MODE (build/help/
research/board), and returns one action. The mode is a disambiguation prior, never a
cage: a clearly global instruction ("back up") breaks out of any tool.

`check_integration()` is the pivot-fork gate: when a plan-stage steer might contradict
the committed idea, it decides whether the steer is integrable or a hard clash. Default
is "bend as far as possible" — only a genuine contradiction of the locked thesis or a
built chapter forks (hard-contradiction-only). A clash surfaces the discard/pivot fork.

Cheap by design: the router runs on Haiku (it fires on every prompt); the integration
check runs on Sonnet (rarer, higher stakes). `mock=True` returns canned decisions.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))                       # app/  -> skills
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "prototype"))  # prototype/ -> engine
import skill_registry as skills  # noqa: E402

STAGES = ("brainstorm", "merge", "refined", "plan")
MODES = ("build", "help", "research", "board")
INTENTS = ("steer", "commit", "diverge", "restart_keep", "restart_hard", "ask")
TARGETS = ("current", "commit", "brainstorm", "research", "board", "help", "plan")


def _mock_route(prompt: str, stage: str, mode: str) -> dict:
    """Deterministic routing for mock/dev/tests — keyword heuristics that mirror the skill's intents
    so the frontend and the API tests exercise real branches without spend."""
    p = (prompt or "").lower()
    # global break-outs first — they win over the active mode
    if any(k in p for k in ("build it", "i'm sold", "im sold", "build the plan", "just build")):
        return {"intent": "commit", "target": "commit", "keep": None, "steer": prompt,
                "confirm": True, "say": "Committing to the full research + build."}
    if "start over" in p and "keep" in p:
        keep = prompt.split("keep", 1)[1].strip(" .") or None
        return {"intent": "restart_keep", "target": "brainstorm", "keep": keep, "steer": None,
                "confirm": False, "say": "Starting over, holding on to that."}
    if any(k in p for k in ("this all sucks", "something else", "throw it out", "hate it all")):
        return {"intent": "restart_hard", "target": "brainstorm", "keep": None, "steer": None,
                "confirm": True, "say": "Clearing it and starting fresh."}
    if any(k in p for k in ("other option", "other direction", "different direction", "back to option")):
        return {"intent": "diverge", "target": "brainstorm", "keep": None, "steer": None,
                "confirm": False, "say": "Pulling up other directions."}
    if p.strip().endswith("?") or p.startswith(("what", "why", "how", "who", "when", "does", "can ")):
        tgt = mode if mode in ("research", "board", "help") else "plan"
        return {"intent": "ask", "target": tgt, "keep": None, "steer": None,
                "confirm": False, "say": "Answering that."}
    # mode prior: an ambiguous prompt inside a tool is a question for that tool
    if mode in ("research", "board", "help"):
        return {"intent": "ask", "target": mode, "keep": None, "steer": None,
                "confirm": False, "say": "Answering from " + mode + "."}
    return {"intent": "steer", "target": "current", "keep": None, "steer": prompt,
            "confirm": False, "say": "Working that into this part."}


def _clean(decision: dict, prompt: str, mode: str) -> dict:
    """Normalize a raw decision dict into the strict schema (tolerate model drift)."""
    d = decision if isinstance(decision, dict) else {}
    intent = str(d.get("intent", "")).lower().strip()
    if intent not in INTENTS:
        intent = "ask" if mode in ("research", "board", "help") else "steer"
    target = str(d.get("target", "")).lower().strip()
    if target not in TARGETS:
        target = {"steer": "current", "commit": "commit", "diverge": "brainstorm",
                  "restart_keep": "brainstorm", "restart_hard": "brainstorm",
                  "ask": (mode if mode in ("research", "board", "help") else "plan")}[intent]
    return {
        "intent": intent,
        "target": target,
        "keep": (str(d.get("keep")).strip() or None) if d.get("keep") else None,
        "steer": (str(d.get("steer")).strip() or None) if d.get("steer") else (
            prompt if intent in ("steer", "commit") else None),
        # only the two costly/destructive routes may demand a confirm
        "confirm": bool(d.get("confirm")) and intent in ("commit", "restart_hard"),
        "say": (str(d.get("say")).strip() or "On it."),
    }


def route(prompt: str, stage: str = "plan", mode: str = "build",
          context: str | None = None, mock: bool = False) -> tuple[dict, float]:
    """Classify a free-text prompt into one action. `stage` is where they are in the funnel;
    `mode` is the active tool tint (build/help/research/board, a prior); `context` is a compact
    summary of what they're looking at. Returns (decision, cost)."""
    stage = stage if stage in STAGES else "plan"
    mode = mode if mode in MODES else "build"
    if mock:
        return _mock_route(prompt, stage, mode), 0.0
    from pipeline import LEDGER, call, extract_json, HAIKU  # heavy; real mode only
    start = len(LEDGER.rows)
    ctx = f"\n\nWHAT THEY'RE LOOKING AT:\n{context}" if context else ""
    out = call("router", HAIKU, max_tokens=300, system=skills.system("router"), cache=True, prompt=(
        f"STAGE: {stage}\nACTIVE MODE: {mode}{ctx}\n\nTHE USER TYPED:\n{prompt}\n\nRoute it now."))
    return _clean(extract_json(out), prompt, mode), round(LEDGER.cost_slice(start), 4)


# ── The pivot-fork gate ──────────────────────────────────────────────────────
# When a plan-stage steer might contradict the committed idea, decide whether we can bend the plan
# to honor it (integrable) or whether it's a hard clash that forks the flow. Hard-contradiction-only:
# default to integrable; only fork when the steer genuinely breaks the locked thesis or a built chapter.

_MOCK_INTEGRATION = {
    "integrable": True,
    "clash": "",
    "skeptic_say": "",
}


def check_integration(steer: str, thesis: str, plan_md: str = "",
                      mock: bool = False) -> tuple[dict, float]:
    """Decide whether a steer can be worked into the plan or hard-clashes with it. Returns
    ({integrable: bool, clash: str, skeptic_say: str}, cost). Default-safe toward integrable —
    only a genuine contradiction of the committed thesis or an already-built chapter forks."""
    if mock:
        # mock heuristic: an explicit contradiction phrase forks; everything else integrates
        p = (steer or "").lower()
        if any(k in p for k in ("actually sell physical", "completely different business",
                                "opposite of", "scrap the whole")):
            return {"integrable": False,
                    "clash": "This changes the core of what you're selling, not just this part.",
                    "skeptic_say": "That's a different business, not a tweak to this one."}, 0.0
        return dict(_MOCK_INTEGRATION), 0.0
    from pipeline import LEDGER, call, extract_json, SONNET
    start = len(LEDGER.rows)
    out = call("integration_check", SONNET, max_tokens=300, system=skills.VOICE, prompt=(
        "A solo operator is building a business plan and just gave a piece of feedback. Decide whether "
        "you can HONOR it by revising the plan, or whether it fundamentally CONTRADICTS the committed "
        "idea (a different buyer, a different core offer, or a different business entirely). Default to "
        "integrable: only say it clashes if honoring it would break the locked thesis or an already-"
        "written section, not merely because it's a big change. Output STRICTLY this JSON, no preamble:\n"
        '{"integrable": true|false, "clash": "one sentence on the contradiction (empty if integrable)", '
        '"skeptic_say": "the blunt one-line objection in a skeptic\'s voice (empty if integrable)"}\n\n'
        f"COMMITTED THESIS:\n{thesis}\n\nTHE PLAN SO FAR:\n{plan_md or '(just starting)'}\n\n"
        f"THEIR FEEDBACK:\n{steer}"))
    d = extract_json(out)
    d = d if isinstance(d, dict) else {}
    integrable = bool(d.get("integrable", True))   # tolerate missing → default integrable
    return {"integrable": integrable,
            "clash": ("" if integrable else (str(d.get("clash") or "").strip()
                      or "This clashes with the committed idea.")),
            "skeptic_say": ("" if integrable else (str(d.get("skeptic_say") or "").strip()))}, \
        round(LEDGER.cost_slice(start), 4)


if __name__ == "__main__":  # self-test (mock, no API)
    # routing by keyword, mode-aware
    d, c = route("just build the plan already", stage="refined", mock=True)
    assert c == 0.0 and d["intent"] == "commit" and d["confirm"]
    assert route("start over but keep the food truck angle", mock=True)[0]["intent"] == "restart_keep"
    assert route("start over but keep the food truck angle", mock=True)[0]["keep"]
    assert route("this all sucks, something else", mock=True)[0]["intent"] == "restart_hard"
    assert route("show me other directions", mock=True)[0]["intent"] == "diverge"
    assert route("what does this cost?", mode="build", mock=True)[0]["intent"] == "ask"
    # mode as a prior: an ambiguous prompt inside research is a research question
    assert route("cheaper competitors", mode="research", mock=True)[0]["target"] == "research"
    # ...but a global break-out wins over the mode
    assert route("this all sucks, something else", mode="board", mock=True)[0]["intent"] == "restart_hard"
    # plain build steer
    assert route("make the pricing simpler", mock=True)[0]["intent"] == "steer"
    # integration gate: hard contradiction forks, a tweak integrates
    ok, _ = check_integration("make it cheaper", "sell a subscription box", mock=True)
    assert ok["integrable"]
    clash, _ = check_integration("actually sell physical hardware instead", "a SaaS service", mock=True)
    assert not clash["integrable"] and clash["clash"] and clash["skeptic_say"]
    # skill loads with VOICE
    assert "always wins" in skills.system("router").lower() and "em-dash" in skills.system("router")
    print("router.py self-test OK — intents routed, mode prior honored, pivot gate works")
