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


def _research_blocks(session: dict) -> tuple[str, str]:
    rows = ((session.get("research") or {}).get("rows")) or []
    cited = "\n".join(f"- {r['text']} [{r.get('url', '')}]" for r in rows if r.get("mark") == "ok") \
        or "- (none cleared)"
    flagged = "\n".join(f"- {r['text']} [{r.get('url', '')}]" for r in rows if r.get("mark") == "warn") \
        or "- (none flagged)"
    return cited, flagged


def _convo(history: list, limit: int = 12) -> str:
    msgs = (history or [])[-limit:]
    if not msgs:
        return "(this is the first message)"
    label = {"user": "OPERATOR", "assistant": "ADVISOR"}
    return "\n".join(f"{label.get(m.get('role'), 'OPERATOR')}: {m.get('content', '')}" for m in msgs)


def chat_reply(session: dict, message: str, history: list | None = None,
               mock: bool = False) -> tuple[str, float]:
    """Answer `message` about the plan in `session`. `history` is the prior chat thread. Returns
    (reply, cost)."""
    idea = planner._working_idea(session)
    edge = planner._founder(session) or "(not captured)"
    vet = session.get("vetting") or {}
    plan_text = planner.bundle_markdown(idea, session.get("files") or {})

    if mock:
        return (f"On “{message.strip()[:80]}”: grounded in your plan for {idea}, the honest read is to "
                f"lead with your edge ({edge}) and pressure-test the riskiest assumption "
                f"({vet.get('biggest_risk') or 'your main assumption'}) before scaling. "
                f"Next step: {vet.get('first_test') or 'run one cheap test this week'}. (mock)"), 0.0

    from pipeline import LEDGER, call, SONNET  # heavy; real mode only
    start = len(LEDGER.rows)
    cited, flagged = _research_blocks(session)
    prompt = (
        f"THE PLAN (the operator's finished business plan for: {idea}):\n{plan_text}\n\n"
        f"GRADED RESEARCH — CITED:\n{cited}\n\nFLAGGED (vendor) CLAIMS:\n{flagged}\n\n"
        f"KILL-GATE: verdict={vet.get('verdict', 'n/a')} · biggest_risk={vet.get('biggest_risk', 'n/a')} "
        f"· cheapest_first_test={vet.get('first_test', 'n/a')}\n\n"
        f"FOUNDER EDGE: {edge}\n\nTHE BOARD (composite directors you may channel):\n"
        f"{_board_roster(session)}\n\nCONVERSATION SO FAR:\n{_convo(history)}\n\n"
        f"OPERATOR'S NEW MESSAGE: {message.strip()}\n\nReply as the advisor.")
    reply = call("plan_chat", SONNET, max_tokens=700, system=skills.system("business_advisor"),
                 cache=True, prompt=prompt)
    return reply.strip(), round(LEDGER.cost_slice(start), 4)


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
