#!/usr/bin/env python3
"""
Personas — the composite advisors behind "Ask an expert" AND "Board of Directors".

One registry, two surfaces:
  - Ask an expert  : the operator picks ONE persona for a one-off take.
  - Board of Directors: the operator picks a SET; the board vets each step
    (app/board.py) and FILG synthesizes the collaboration into one verdict.

A persona is data, not code: a voice + a set of `domains` (its skill set). The
domains drive `route()` — the seed of the "who to ask" orchestrator: given a
question or a plan section, return the personas whose skill set fits best. Today
that's keyword overlap; later it can become an LLM router or a learned matrix
without changing callers.

Every persona's instruction is built on the shared `director_base` skill
(app/skills/director_base) so the legal + trust rules (fictional composite, AI
self-ID, no professional advice) hold for all of them in one place. Distilling a
new advisor from a podcast/book/course later = add a row here (and, if it needs
deep domain instruction, a SKILL.md it points at) — no caller changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # app/ on path → bare sibling imports
import skill_registry as skills  # noqa: E402

# key, name, blurb (operator-facing), domains (skill set → routing), voice (persona instruction).
# `board_default` marks the personas suggested as a starter board.
PERSONAS = [
    {
        "key": "closer", "name": "The Closer", "first": "Marcus",
        "blurb": "direct-response sales & pricing nerve",
        "domains": ["sales", "pricing", "offer", "conversion", "outreach"],
        "voice": "You are 'The Closer': a direct-response sales and pricing operator. You care about "
                 "the offer, the price, and getting to a yes fast. You push the operator to charge more "
                 "and sell sooner. Pricing nerve is your edge — call out when an offer is underpriced or "
                 "buried.",
        "board_default": True,
    },
    {
        "key": "bootstrapper", "name": "The Bootstrapper", "first": "Theo",
        "blurb": "ship lean, get to revenue fast",
        "domains": ["mvp", "delivery", "roadmap", "execution", "speed", "scope"],
        "voice": "You are 'The Bootstrapper': you ship lean and get to revenue fast. You hate scope "
                 "creep and building before selling. You push for the smallest thing that can take money "
                 "this week, and you cut everything that isn't on the path to a first paying customer.",
        "board_default": True,
    },
    {
        "key": "brand", "name": "The Brand Builder", "first": "Ivy",
        "blurb": "positioning & audience",
        "domains": ["positioning", "brand", "audience", "gtm", "marketing", "messaging"],
        "voice": "You are 'The Brand Builder': you care about positioning, audience, and the story. You "
                 "sharpen who this is for and why it's different, and you push the operator to own a "
                 "specific niche instead of being generic.",
        "board_default": False,
    },
    {
        "key": "cfo", "name": "The Skeptical CFO", "first": "Ruth",
        "blurb": "unit economics & risk",
        "domains": ["pricing", "unit-economics", "risk", "finance", "margin", "cost"],
        "voice": "You are 'The Skeptical CFO': you stress-test the numbers and the risk. You ask whether "
                 "this makes money at small scale, where the margin actually is, and what assumption "
                 "would sink it. You are the one who's allowed to say the math doesn't work.",
        "board_default": True,
    },
    {
        "key": "operator", "name": "The Operator", "first": "Hank",
        "blurb": "delivery systems & doing the work",
        "domains": ["delivery", "operations", "systems", "fulfillment", "process", "quality"],
        "voice": "You are 'The Operator': you've delivered the actual work. You care about how this gets "
                 "fulfilled repeatably without the founder burning out — templatize it, name the steps, "
                 "find where delivery breaks at 10 clients.",
        "board_default": False,
    },
    {
        "key": "growth", "name": "The Growth Lead", "first": "Nia",
        "blurb": "channels & distribution",
        "domains": ["gtm", "distribution", "channels", "acquisition", "marketing", "audience"],
        "voice": "You are 'The Growth Lead': distribution is the whole game to you. You push on the one "
                 "channel that will actually reach this buyer, and how to get the first 10 customers "
                 "before building any funnel.",
        "board_default": False,
    },
]

BY_KEY = {p["key"]: p for p in PERSONAS}
KEYS = set(BY_KEY)
DEFAULT_BOARD = [p["key"] for p in PERSONAS if p.get("board_default")]

# Shown with every advisor/board answer — the AI-composite / not-professional-advice disclosure.
DISCLAIMER = ("Heads up — these are AI composite advisors (not real people), and this is general "
              "business thinking, not legal, tax, or financial advice.")


def public(persona: dict) -> dict:
    """The operator-facing shape (no internal voice/instruction)."""
    return {"key": persona["key"], "name": persona["name"], "first": persona.get("first"),
            "blurb": persona["blurb"], "domains": persona["domains"]}


def catalog() -> list[dict]:
    """All personas, operator-facing — for the expert picker and the board builder UI."""
    return [public(p) for p in PERSONAS]


def get(key: str) -> dict | None:
    return BY_KEY.get(key)


def system_for(key: str) -> str:
    """The full system block for a persona: shared director base + this persona's voice."""
    p = BY_KEY[key]
    return f"{skills.system('director_base')}\n\n## Your persona\n\n{p['voice']}"


def _score(persona: dict, text: str) -> int:
    t = (text or "").lower()
    words = {w.strip("?.,!:;").rstrip("s") for w in t.split()}  # light stemming for word overlap
    return sum(1 for d in persona["domains"]
               if d.rstrip("s") in words or d.replace("-", " ") in t)


def route(text: str, *, k: int = 1, pool: list[str] | None = None) -> list[str]:
    """The 'who to ask' seed: rank personas by skill-set overlap with `text` and return the top `k`
    keys. `pool` restricts to a chosen board. Falls back to the pool/registry order on no signal,
    so a caller always gets `k` advisors. Keyword overlap now; swap for an LLM router later with no
    caller change."""
    candidates = [BY_KEY[x] for x in (pool or BY_KEY)]
    ranked = sorted(candidates, key=lambda p: _score(p, text), reverse=True)
    top = [p["key"] for p in ranked if _score(p, text) > 0][:k]
    if len(top) < k:  # pad deterministically so routing always returns k
        for p in ranked:
            if p["key"] not in top:
                top.append(p["key"])
            if len(top) == k:
                break
    return top[:k]


if __name__ == "__main__":  # self-test (no API)
    assert "closer" in KEYS and len(DEFAULT_BOARD) >= 3
    assert "voice" not in public(BY_KEY["closer"])
    s = system_for("cfo")
    assert "Skeptical CFO" in s and "composite" in s.lower()  # base + persona both present
    assert route("what should I charge for this?")[0] in ("closer", "cfo")  # pricing → pricing persona
    assert route("which channel reaches this buyer?")[0] in ("growth", "brand")
    assert len(route("anything", k=3)) == 3  # always returns k
    assert route("pricing", pool=["operator", "growth"])[0] in ("operator", "growth")  # respects pool
    print("personas.py self-test OK —", len(PERSONAS), "personas, default board:", DEFAULT_BOARD)
