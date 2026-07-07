#!/usr/bin/env python3
"""
Decisions — the operator's standing axioms ("no cold-call marketing", "this stays a non-profit").

A decision is a constraint the operator declared settled. It is NOT a steer (one-shot feedback on
one draft): once pinned, it rides EVERY model-facing surface — section drafts, pivots/spreads,
merges, board convenes, the advisor — via the context engine's `decisions_block` view, and every
node built while it was in force records its id (the `decisions` attachment). That record is what
makes edit/remove honest: `impact()` names the steps a decision shaped, so the operator can revisit
and pivot them instead of silently building on a constraint that no longer exists.

Weights (the color code): non_negotiable > firm > nice_to_have. The prompt block orders hardest
first and labels each, so a model can't launder a hard constraint into a suggestion.

`detect()` classifies a summary-tab chat message as a candidate decision ("i don't want X" is a
preference, "what should I charge?" is not). Mock mode is deterministic regex; real mode is one
cheap Haiku call, schema-validated with the regex as the parse-failure fallback.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

WEIGHTS = ("non_negotiable", "firm", "nice_to_have")
LABELS = {"non_negotiable": "NON-NEGOTIABLE", "firm": "FIRM", "nice_to_have": "NICE-TO-HAVE"}
_ORDER = {w: i for i, w in enumerate(WEIGHTS)}
MAX_DECISIONS = 30      # per plan — a wall of axioms stops being axioms
MAX_TEXT = 240
MAX_WHY = 300


def normalize_weight(w) -> str:
    """Tolerate display forms ('non-negotiable', 'Nice to have') → the stable keys."""
    k = re.sub(r"[\s-]+", "_", str(w or "").strip().lower())
    return k if k in WEIGHTS else "firm"


def clean(raw: dict) -> dict | None:
    """Validate/normalize an incoming decision payload. Returns None when there's no usable text."""
    text = " ".join(str((raw or {}).get("text") or "").split()).strip()
    if len(text) < 4:
        return None
    why = " ".join(str((raw or {}).get("why") or "").split()).strip()
    return {"text": text[:MAX_TEXT], "why": why[:MAX_WHY],
            "weight": normalize_weight((raw or {}).get("weight"))}


def new(text: str, why: str = "", weight: str = "firm") -> dict | None:
    d = clean({"text": text, "why": why, "weight": weight})
    if d is None:
        return None
    d["id"] = "dc" + uuid.uuid4().hex[:10]
    d["created_at"] = datetime.now(timezone.utc).isoformat()
    return d


def ordered(decision_list: list | None) -> list[dict]:
    """Hardest first (stable within a weight) — the render order everywhere."""
    return sorted([d for d in (decision_list or []) if isinstance(d, dict) and d.get("text")],
                  key=lambda d: _ORDER.get(d.get("weight"), 1))


def impact(session: dict, decision_id: str) -> list[dict]:
    """The nodes built while this decision was in force — the revisit modal's list. Reads the
    `decisions` stamp each build op leaves on the nodes it creates. Committed-path standing rides
    along so the frontend can rank what matters."""
    from engine import tree as dtree  # noqa: PLC0415 — keep this module import-light
    t = (session or {}).get("tree") or {}
    nodes = t.get("nodes") or {}
    on_path = {n["id"] for n in dtree.chain(nodes, t.get("active"))}
    out = []
    for n in nodes.values():
        if decision_id in (n.get("decisions") or []):
            out.append({"id": n["id"], "kind": dtree.kind(n),
                        "label": dtree.label(n, f"Part {int(n.get('step') or 0) + 1}"),
                        "onPath": n["id"] in on_path})
    return out


# ── Detection — is this summary-tab message a standing decision? ──────────────
# The declarative markers: a first-person commitment/refusal or an absolute. Questions never are.
_DECLARE = re.compile(
    r"\b(i'?d (rather|prefer|like)|i'?m (not|committed|set)|"
    r"i (want|won'?t|will( not)?|need|prefer|refuse|insist|only|never|"
    r"can'?t stand|hate|am (not )?(going|willing)|would (never|rather)|must)|"
    r"(don'?t|do not|won'?t|will not|refuse to) (want|do|use|touch|sell|run|hire|cold[- ]?call)|"
    r"(don'?t|do not) want|"
    r"we (won'?t|never|only|refuse|want|need)|no cold|never|non[- ]?negotiable|has to (be|stay)|"
    r"must (be|stay|not|never)|absolutely (no|not))\b", re.I)
_HARD = re.compile(r"\b(never|won'?t|will not|refuse|non[- ]?negotiable|absolutely (no|not)|hard no|"
                   r"no way|don'?t want|do not want|can'?t stand|hate|must not)\b", re.I)
_SOFT = re.compile(r"\b(prefer|ideally|would be nice|nice to have|'?d like|lean(ing)? toward|"
                   r"if possible|would rather|'?d rather)\b", re.I)


def _guess_weight(text: str) -> str:
    if _HARD.search(text):
        return "non_negotiable"
    if _SOFT.search(text):
        return "nice_to_have"
    return "firm"


def _heuristic(prompt: str) -> dict | None:
    p = (prompt or "").strip()
    if len(p) < 8 or "?" in p:
        return None
    if not _DECLARE.search(p):
        return None
    return {"text": p[:MAX_TEXT], "why": "", "weight": _guess_weight(p)}


def detect(prompt: str, mock: bool = False) -> tuple[dict | None, float]:
    """Classify a summary-tab message as a candidate standing decision. Returns
    ({text, why, weight} or None, cost). The offer is a QUESTION to the operator — nothing is
    pinned until they confirm — so a false positive costs one chat bubble, not a laundered axiom."""
    if mock:
        return _heuristic(prompt), 0.0
    quick = _heuristic(prompt)
    if quick is None and ("?" in prompt or len(prompt.strip()) < 8):
        return None, 0.0   # a question is never a decision — skip the model call
    from engine.pipeline import LEDGER, call, extract_json, HAIKU  # noqa: PLC0415 — real mode only
    start = len(LEDGER.rows)
    out = call("decision_detect", HAIKU, max_tokens=200, cache=True, prompt=(
        "A solo operator building a business plan typed a message into the plan-summary tab. Decide "
        "whether it DECLARES a standing decision — an axiom, non-negotiable, or settled preference "
        "that should constrain every future step (e.g. 'no cold-call marketing', 'this stays a "
        "non-profit', 'I'd prefer local clients'). A question, a one-off edit request, or an "
        "instruction about the current draft is NOT a decision. Output STRICTLY this JSON, no "
        "preamble:\n"
        '{"decision": true|false, "text": "the decision restated in <=15 words, their words", '
        '"why": "any context they gave, one short clause (\'\' if none)", '
        '"weight": "non_negotiable | firm | nice_to_have"}\n\n'
        f"THE MESSAGE:\n{prompt.strip()}"))
    cost = round(LEDGER.cost_slice(start), 4)
    data = extract_json(out)
    if not isinstance(data, dict):
        return quick, cost   # unparseable verdict → the deterministic read stands
    if not data.get("decision"):
        return None, cost
    offer = clean({"text": data.get("text") or prompt, "why": data.get("why"),
                   "weight": data.get("weight")})
    return offer, cost


if __name__ == "__main__":  # self-test (mock, no API)
    d = new("No cold-call marketing", why="I hate phones", weight="non-negotiable")
    assert d and d["id"].startswith("dc") and d["weight"] == "non_negotiable"
    assert clean({"text": "  "}) is None and clean({"text": "x"}) is None
    assert normalize_weight("Nice to have") == "nice_to_have" and normalize_weight("junk") == "firm"
    rows = ordered([{"text": "a", "weight": "nice_to_have"}, {"text": "b", "weight": "non_negotiable"},
                    {"text": "c", "weight": "firm"}])
    assert [r["text"] for r in rows] == ["b", "c", "a"]
    # detection: declaratives offer, questions/imperatives don't
    off, c = detect("i don't want to do cold call marketing", mock=True)
    assert c == 0.0 and off and off["weight"] == "non_negotiable"
    # an intensifier between the subject and the refusal must not dodge detection (Sam, 2026-07-07)
    assert detect("I really really do not want to do cold calling", mock=True)[0]["weight"] \
        == "non_negotiable"
    assert detect("I want to run a non-profit", mock=True)[0]["weight"] == "firm"
    assert detect("ideally I'd prefer local clients", mock=True)[0]["weight"] == "nice_to_have"
    assert detect("what should I charge?", mock=True)[0] is None
    assert detect("make the pricing section shorter", mock=True)[0] is None
    # impact walks the node stamps
    sess = {"tree": {"active": "n2", "nodes": {
        "n1": {"id": "n1", "kind": "refined", "parent": None, "children": ["n2"],
               "decisions": ["dcx"]},
        "n2": {"id": "n2", "parent": "n1", "children": [], "title": "The setup",
               "decisions": ["dcx", "dcy"]},
        "n3": {"id": "n3", "parent": "n1", "children": [], "title": "Sibling"}}}}
    hit = impact(sess, "dcx")
    assert {h["id"] for h in hit} == {"n1", "n2"} and all(h["onPath"] for h in hit)
    assert impact(sess, "dcz") == []
    print("decisions.py self-test OK — weights, detection, impact stamped")
