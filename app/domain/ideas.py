"""The browsable idea catalog — the front door's zero-AI-cost hook (front_door_strategy.md T4/T5).

Serves ideas mined from popular-media ingestions (v0: The Koerner Office catalog, built by the
bizdev `idea-mining` pipeline) as static data: a random "roll" and an honest trending list. No AI
call, no key, no account. Every economics figure carries a LABEL saying what the number is and
who claims it — invariant #1 applied to the hook itself, which no competitor's idea feed does.

The trending signal is programmatic and labeled (episode counts in the source catalog), never a
faked algorithm. Future `idea-mining` runs over other channels/subreddits append to the JSON and
the surfaces get richer with no code change.
"""

from __future__ import annotations

import json
import os
import random

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "idea_catalog.json")
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        with open(_PATH, encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def catalog_meta() -> dict:
    return dict(_load().get("meta") or {})


def all_ideas() -> list[dict]:
    return list(_load().get("ideas") or [])


def get_idea(idea_id: str) -> dict | None:
    return next((i for i in all_ideas() if i.get("id") == idea_id), None)


def random_idea(exclude: str | None = None) -> dict:
    """One roll of the dice. `exclude` avoids serving the same card twice in a row."""
    pool = [i for i in all_ideas() if i.get("id") != exclude] or all_ideas()
    return random.choice(pool)


def trending(n: int = 5) -> list[dict]:
    """The honest trending list: ranked by how often the source catalog covers the idea
    (episode count), tier as the tiebreak. The signal is labeled at the surface; there is
    no invented trending algorithm here."""
    ranked = sorted(all_ideas(), key=lambda i: (-(i.get("episodes") or 0), i.get("tier") or 9))
    return ranked[:max(1, n)]


def seed_prompt(idea: dict) -> str:
    """The prompt a card's Build-this button drops into the intake box: the idea in one line,
    phrased the way the funnel expects an operator to talk."""
    return (f"{idea.get('one_liner', '')} I want to build this as {idea.get('title', 'a business')}. "
            f"Help me shape it around my own skills.")
