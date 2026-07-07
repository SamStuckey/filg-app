#!/usr/bin/env python3
"""
Director Forge — mint a custom Board director from a one-line description.

A side feature of the Board: the operator describes the advisor they wish they
had, and the forge runs a small distill -> draft -> QA tree to produce a FILG
composite director (voice + domains) they can seat on their board. The result is
the same shape as every built-in persona (app/personas.py) and is built on the
same legal base (director_base), so board.convene runs it with no special casing
once it's stored on the session as a custom director.

We never claim to model a real person. If the operator prompts for one, the
forge still produces a fictional composite archetype in FILG's own voice.

Real mode = three cheap calls (distill the lens, draft the persona, QA it for
distinctness + usefulness). Mock mode returns a canned persona so the app's mock
mode and self-tests cost nothing. `on_progress(msg)` streams the tree's steps.
"""

from __future__ import annotations

import json
import re

from app import personas  # noqa: E402
from app import skill_registry as skills  # noqa: E402

_FALLBACK_DOMAINS = ["strategy", "advice"]


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (s or "director")[:32]


def _key_for(name: str, existing: set) -> str:
    base = "custom-" + _slug(name)
    key, i = base, 2
    while key in existing:
        key, i = f"{base}-{i}", i + 1
    return key


def _progress(on_progress, msg: str) -> None:
    if on_progress:
        try:
            on_progress(msg)
        except Exception:  # noqa: BLE001 — progress is best-effort, never fail the forge
            pass


def _mock_persona(description: str, existing: set) -> dict:
    head = (description.strip().split(",")[0] or "Wildcard").strip()[:40].title() or "Wildcard"
    name = "The " + head
    return {
        "key": _key_for(name, existing), "name": name, "first": "Custom",
        "blurb": "your custom-forged advisor",
        "domains": ["strategy", "advice", "custom"],
        "voice": ("You are a custom composite advisor the operator forged for their board. They asked "
                  f"for: {description.strip()[:200]}. Bring exactly that lens to the plan, lead with the "
                  "one thing you'd change, and stay blunt and specific."),
        "custom": True,
        "trace": [
            {"step": "distill", "note": "Pulled the lens + expertise from your description."},
            {"step": "draft", "note": "Drafted the voice and skill set."},
            {"step": "qa", "note": "Checked they're distinct from your other directors."},
        ],
    }


def forge(description: str, existing_keys=None, mock: bool = False, on_progress=None) -> tuple[dict, float]:
    """Forge a custom director from `description`. Returns (persona, cost). The persona is a draft —
    the caller decides whether to seat it (store it on the session). `existing_keys` are the keys
    already taken (built-ins + the session's customs) so the new key is unique."""
    description = (description or "").strip()
    existing = set(existing_keys or []) | set(personas.KEYS)
    if mock:
        _progress(on_progress, "Distilling the archetype")
        _progress(on_progress, "Drafting the director")
        _progress(on_progress, "QA: checking they're distinct + useful")
        return _mock_persona(description, existing), 0.0

    from engine.pipeline import LEDGER, call, extract_json, SONNET
    start = len(LEDGER.rows)
    roster = "; ".join(f"{p['name']} ({', '.join(p['domains'][:3])})"
                       for p in personas.PERSONAS if not p.get("standing"))

    # 1) distill — pull the lens + skill set out of the description
    _progress(on_progress, "Distilling the archetype")
    d = extract_json(call("forge_distill", SONNET, max_tokens=350, system=skills.VOICE, cache=True, prompt=(
        "An operator wants a custom business advisor for their board. From their description, distill the "
        "advisor. Output STRICTLY this JSON, no preamble:\n"
        '{"angle": "the one lens this advisor brings (1 sentence)", '
        '"pushes_on": "what they push the founder to do (1 sentence)", '
        '"domains": ["6 to 10 lowercase skill keywords, single words or hyphenated"]}\n\n'
        f"DESCRIPTION:\n{description}"))) or {}

    # 2) draft — synthesize the persona in the director_base style (fictional composite, never a real person)
    _progress(on_progress, "Drafting the director")
    dr = extract_json(call("forge_draft", SONNET, max_tokens=450, system=skills.system("director_base"),
                           cache=True, prompt=(
        "Draft a composite board director (a fictional archetype, NEVER a real named person, even if the "
        "brief named one) from this. Output STRICTLY this JSON, no preamble:\n"
        '{"name": "The X (a short title, like \'The Closer\')", '
        '"first": "an original human first name (not modeled on anyone)", '
        '"blurb": "operator-facing, 3 to 6 words", '
        '"voice": "2 to 4 sentences, second person (\'You are ...\'), the persona instruction: their '
        'lens, what they care about, and that they lead with the one thing they would change"}\n\n'
        f"BRIEF:\nangle: {d.get('angle', '')}\npushes on: {d.get('pushes_on', '')}\n"
        f"original description: {description}\n\n"
        f"EXISTING BOARD (make this advisor DISTINCT from these):\n{roster}"))) or {}

    # 3) QA — distinct? coherent? useful? tighten the voice
    _progress(on_progress, "QA: checking they're distinct + useful")
    qa = extract_json(call("forge_qa", SONNET, max_tokens=450, system=skills.VOICE, cache=True, prompt=(
        "QA this drafted board director. Is it a COHERENT, DISTINCT, USEFUL composite advisor — not a "
        "duplicate of the existing board, and a fictional archetype rather than a real named person? "
        "Tighten the voice if needed. Output STRICTLY this JSON, no preamble:\n"
        '{"name": "...", "first": "...", "blurb": "...", '
        '"voice": "the final, tightened persona instruction", '
        '"note": "1 line on what they add to the board"}\n\n'
        f"DRAFT:\n{json.dumps(dr)}\n\nEXISTING BOARD:\n{roster}"))) or {}

    final = {**dr, **{k: v for k, v in qa.items() if v}}  # QA overrides the draft where it returned a value
    name = (final.get("name") or "The Custom Advisor").strip()
    domains = [str(x).lower().strip() for x in (d.get("domains") or _FALLBACK_DOMAINS) if str(x).strip()]
    persona = {
        "key": _key_for(name, existing), "name": name,
        "first": (final.get("first") or "Custom").strip(),
        "blurb": (final.get("blurb") or "your custom-forged advisor").strip(),
        "domains": domains[:10] or list(_FALLBACK_DOMAINS),
        "voice": (final.get("voice") or dr.get("voice") or "").strip(),
        "custom": True,
        "trace": [
            {"step": "distill", "note": (d.get("angle") or "Pulled the lens from your description.").strip()},
            {"step": "draft", "note": f"Drafted {name}."},
            {"step": "qa", "note": (qa.get("note") or "Checked they're distinct and useful.").strip()},
        ],
    }
    return persona, round(LEDGER.cost_slice(start), 4)


if __name__ == "__main__":  # self-test (mock, no API)
    p, cost = forge("a ruthless YC partner who has seen a thousand startups fail", mock=True)
    assert cost == 0.0 and p["custom"] and p["key"].startswith("custom-")
    assert p["voice"] and p["name"] and len(p["trace"]) == 3
    # unique key against an existing custom of the same name
    p2, _ = forge("a ruthless YC partner who has seen a thousand startups fail",
                  existing_keys=[p["key"]], mock=True)
    assert p2["key"] != p["key"]
    print("director_forge.py self-test OK (mock) —", p["key"], "/", p["name"])
