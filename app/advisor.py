#!/usr/bin/env python3
"""
advisor.py — "chat with your business plan".

The standing advisory layer on top of a finished plan: load the plan, the gate-graded research, the
operator's decisions and edge, and their chosen board into a grounded context, then answer the
operator's questions in the `business_advisor` skill's voice. This is the ongoing-value surface (the
subscription wedge) — the build is acquisition, the advisor is retention.

`mock=True` returns a canned, plan-aware reply with no API spend (dev/tests). Real mode runs one
Sonnet call: the durable instruction is the cached `business_advisor` system block; the plan + research
+ conversation are interpolated into the user message (so the system prefix caches across turns).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))                       # app/  → skills, personas
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "prototype"))  # prototype/ → engine
import context  # noqa: E402 — the context engine (graded-evidence blocks)
import personas  # noqa: E402
import planner  # noqa: E402 — bundle_markdown + working idea/edge helpers
import skill_registry as skills  # noqa: E402

DISCLAIMER = personas.DISCLAIMER  # AI-composite / not-professional-advice line (shown once in the UI)

# Suggested openers — the questions a static plan can't fully pre-answer (the gap that motivated this).
STARTERS = [
    "Am I building this myself or reselling something else?",
    "Why would a customer buy from me and not a competitor?",
    "Is my pricing right, or am I leaving money on the table?",
    "What's the one thing most likely to sink this?",
]


def _board_roster(session: dict) -> str:
    keys = [k for k in (session.get("directors") or personas.DEFAULT_BOARD) if k in personas.KEYS]
    if not keys:
        return "(no board picked — reason across the standard lenses: sales, cost/risk, positioning.)"
    return "\n".join(f"- {personas.get(k)['name']}: {personas.get(k)['blurb']}" for k in keys)


_research_blocks = context.evidence   # graded evidence comes from the context engine, labels intact


def _convo(history: list, limit: int = 12) -> str:
    msgs = (history or [])[-limit:]
    if not msgs:
        return "(this is the first message)"
    label = {"user": "OPERATOR", "assistant": "ADVISOR"}
    return "\n".join(f"{label.get(m.get('role'), 'OPERATOR')}: {m.get('content', '')}" for m in msgs)


def chat_reply(session: dict, message: str, history: list | None = None,
               mock: bool = False, journey: str = "", situation: str = "") -> tuple[str, float]:
    """Answer `message` about the plan in `session`. `history` is the prior chat thread; `journey`
    is the decision-tree digest (the path walked + options offered/picked at every fork); `situation`
    is where the operator is RIGHT NOW (a mid-flight build, an earlier node being read) — it decides
    whether the reply may coach forward at all. Returns (reply, cost)."""
    idea = planner._working_idea(session)
    edge = planner._founder(session) or "(not captured)"
    vet = session.get("vetting") or {}
    plan_text = planner.bundle_markdown(idea, session.get("files") or {})

    if mock:
        picked = journey.count("✓ PICKED")
        calm = "READING" in situation or "MID-FLIGHT" in situation   # rule 5: no forward coaching here
        aware = (" (I can see you're reading an earlier node — answering in place.)" if "READING" in situation
                 else " (A build is running — answering without adding work.)" if "MID-FLIGHT" in situation
                 else "")
        base = (f"On “{message.strip()[:80]}”: grounded in your plan for {idea}"
                + (f" (journey: {picked} picked direction{'s' if picked != 1 else ''} in view)" if journey else "")
                + (f" (you are on: {situation.splitlines()[0][12:80]})" if situation.startswith("They are ON") else "")
                + f", the straight read is to lead with your edge ({edge}) and pressure-test the "
                f"riskiest assumption ({vet.get('biggest_risk') or 'your main assumption'}) before scaling.")
        nxt = ("" if calm else
               f" Next step: {vet.get('first_test') or 'run one cheap test this week'}.")
        return base + nxt + aware + " (mock)", 0.0

    from pipeline import LEDGER, call, SONNET  # heavy; real mode only
    start = len(LEDGER.rows)
    cited, flagged = _research_blocks(session)
    journey_block = (f"THE JOURNEY SO FAR (the decision tree they walked — every fork lists the "
                     f"directions offered and which they PICKED):\n{journey}\n\n") if journey else ""
    situation_block = (f"WHERE THEY ARE RIGHT NOW (this decides whether you may coach forward at "
                       f"all — see rule 5):\n{situation}\n\n") if situation else ""
    prompt = (
        f"THE PLAN (the operator's finished business plan for: {idea}):\n{plan_text}\n\n"
        f"{journey_block}{situation_block}"
        f"GRADED RESEARCH — CITED:\n{cited}\n\nFLAGGED (vendor) CLAIMS:\n{flagged}\n\n"
        f"KILL-GATE: verdict={vet.get('verdict', 'n/a')} · biggest_risk={vet.get('biggest_risk', 'n/a')} "
        f"· cheapest_first_test={vet.get('first_test', 'n/a')}\n\n"
        f"FOUNDER EDGE: {edge}\n\nTHE BOARD (composite directors you may channel):\n"
        f"{_board_roster(session)}\n\nCONVERSATION SO FAR:\n{_convo(history)}\n\n"
        f"OPERATOR'S NEW MESSAGE: {message.strip()}\n\nReply as the advisor.")
    reply = call("plan_chat", SONNET, max_tokens=700, system=skills.system("business_advisor"),
                 cache=True, prompt=prompt)
    return reply.strip(), round(LEDGER.cost_slice(start), 4)


def research_answer(session: dict, question: str, mode: str = "quick", mock: bool = False,
                    on_progress=None) -> tuple[dict, float]:
    """Query the RESEARCH (not the whole plan). Two modes:
      - quick: answer strictly from the research already gathered; it's allowed to say "the research
        doesn't cover this" rather than guess.
      - deep: spawn a fresh, bounded research pass ON the question (gate-graded), then answer from it.
    Returns ({mode, answer, rows?}, cost)."""
    q = (question or "").strip()
    if mode == "deep":
        if mock:
            rows = [{"mark": "ok", "text": f"Fresh finding relevant to: {q[:60]}",
                     "url": "https://example.com", "note": "mock deeper research"}]
            return {"mode": "deep", "answer": f"Deeper research on “{q[:80]}”: here's what fresh, graded "
                    f"sources say… (mock).", "rows": rows}, 0.0
        import teardown  # noqa: PLC0415 — heavy engine import, real mode only
        from pipeline import LEDGER, call, SONNET
        start = len(LEDGER.rows)
        rows, _stats, _lanes = teardown.build_evidence(q, headlines=3, on_progress=on_progress)
        block = "\n".join(
            f"- {r['text']} [{r.get('url', '')}] ({'cited' if r['mark'] == 'ok' else 'vendor/unverified'})"
            for r in rows) or "(the fresh pass turned up nothing usable)"
        ans = call("research_deep", SONNET, max_tokens=550, system=skills.VOICE, prompt=(
            "Answer the operator's question using ONLY this freshly gathered, gate-graded research. Lead "
            "with the answer, cite the cited sources, and flag plainly where you're leaning on a "
            "vendor/unverified claim. If it still doesn't answer the question, say so.\n\n"
            f"QUESTION:\n{q}\n\nFRESH GRADED RESEARCH:\n{block}"))
        return {"mode": "deep", "answer": ans.strip(), "rows": rows}, round(LEDGER.cost_slice(start), 4)

    # quick — ground strictly in what's already been gathered
    if mock:
        return {"mode": "quick", "answer": f"Quick check against your research for “{q[:80]}”: here's "
                "what the gathered sources actually support, and I'll say so if they don't cover it. "
                "(mock)"}, 0.0
    from pipeline import LEDGER, call, SONNET
    start = len(LEDGER.rows)
    cited, flagged = _research_blocks(session)
    ans = call("research_quick", SONNET, max_tokens=450, system=skills.VOICE, prompt=(
        "Answer the operator's question using ONLY the research already gathered for their plan below. "
        "Do NOT use outside knowledge or guess. If the research does not cover it, say clearly that the "
        "current research doesn't answer this and that they can 'go deeper' to research it fresh. Cite "
        "the cited claims you used; note when you're leaning on a flagged (vendor/unverified) one.\n\n"
        f"QUESTION:\n{q}\n\nCITED RESEARCH:\n{cited}\n\nFLAGGED (vendor/unverified):\n{flagged}"))
    return {"mode": "quick", "answer": ans.strip()}, round(LEDGER.cost_slice(start), 4)


if __name__ == "__main__":  # self-test (mock, no API)
    sess = {"idea": "guitar coaching", "shaped": {"thesis": "guitar coaching for adults",
            "founder_edge": "10 years teaching"}, "files": {"1-the-setup.md": "# Setup\nFor adults."},
            "vetting": {"verdict": "pursue", "biggest_risk": "thin pipeline",
                        "first_test": "post in 3 communities"}, "directors": ["closer", "cfo"]}
    r, c = chat_reply(sess, "why would they buy from me?", history=[], mock=True)
    assert "guitar coaching for adults" in r and c == 0.0
    assert _board_roster(sess).startswith("- The Closer")
    assert len(STARTERS) >= 3
    print("advisor.py self-test OK —", len(STARTERS), "starters; board roster + grounding wired")
