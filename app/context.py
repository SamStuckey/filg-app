#!/usr/bin/env python3
"""
context.py — the CONTEXT ENGINE: the single owner of "what the model gets to see".

Every model-facing surface (the router, diverge's pivot input, the advisor, the board) used to
hand-roll its own context string from the session. That's how context drops happen: the tree is the
source of truth, but each consumer carried a private, partial view of it, and every new piece of
state (picks, fork steers, stage) had to be remembered in N places. Four real drops on 2026-07-04
were all this one failure.

The engine has two layers:

  1. ONE RENDERER PER NODE KIND — `snippet(n)` is the canonical one-line description of a node.
     When a node kind grows a field, add it HERE and every view inherits it.
  2. NAMED VIEWS composed from the renderers — consumers request a view, never build strings:
       - path(s, node_id)  — THE PIVOT CONTRACT: root → node only; siblings + descendants dropped;
                             forks carry the instruction that created them. Feeds diverge.
       - screen(s, node_id)— what the user is LOOKING AT right now: the frame node, the active
                             surface, numbered pickable options. Feeds the router.
       - journey(s)        — the whole walked story: the path plus, at every fork, each direction
                             offered marked ✓ PICKED / passed over. Feeds the advisor.
       - evidence(s)       — the gate-graded research, split cited vs flagged. Feeds the advisor
                             and the board.

Pure functions over the session dict — no model calls, no I/O. Tree mechanics (kind resolution,
path walks) come from engine/tree.py; the app's kind vocabulary is registered in app/domain/nodes.
Contract tests in tests/test_context.py assert every user-visible fact (a pick, a pivot steer, an
on-screen option) appears in the view of every surface that must answer about it; a new drop
should fail there, not in the operator's chat.
"""

from __future__ import annotations

from engine import tree as dtree
from app.domain import nodes as _vocab  # noqa: F401 — registers the app's kinds + attachments

# Per-view character budgets. Truncation is centralized so nothing important silently falls off the
# end of an ad-hoc slice; views trim WITHIN structure (per line) rather than chopping the whole tail.
SNIPPET_TEXT = 200     # a node line's content portion
OPTION_TITLE = 70
OPTION_LINE = 140
SCREEN_CAP = 900       # the router runs on every prompt — keep its context lean
JOURNEY_CAP = 4000     # the advisor answers ABOUT the journey — it gets the full story


# A node's kind — the engine resolves it against the registry app/domain/nodes.py declares
# (legacy plan-section nodes carry no `kind` field; the registered default makes them 'section').
kind = dtree.kind


def snippet(n: dict) -> str:
    """THE canonical one-line description of a node — every view renders nodes through this.
    A fork carries the instruction that created it: dropping that is how 'drop the DTC piece'
    evaporated from a later pivot's context."""
    k = kind(n)
    if k == "refined":
        return "the refined idea: " + (n.get("thesis") or "")[:SNIPPET_TEXT]
    if k == "option":
        d = n.get("direction") or {}
        return ("the direction '" + (d.get("title") or "")[:OPTION_TITLE] + "': "
                + (d.get("one_liner") or "")[:OPTION_LINE])
    if k == "idea":
        return "the original idea: " + (n.get("draft") or "")[:SNIPPET_TEXT]
    if k == "brainstorm":
        fb = (n.get("feedback") or "").strip()
        return ("the fork where directions were spread"
                + (f" — steered by: '{fb[:SNIPPET_TEXT]}'" if fb else ""))
    body = (n.get("files") or {}).get(next(iter(n.get("files") or {}), ""), "") or n.get("draft") or ""
    return "the '" + (n.get("title") or "part") + "' section of the plan: " + str(body)[:SNIPPET_TEXT]


def _nodes(s: dict) -> tuple[dict, str | None]:
    t = s.get("tree") or {}
    return (t.get("nodes") or {}), t.get("active")


# Root → at_id, in order; empty when the node is unknown. The engine's cycle-safe walk.
_chain = dtree.chain


def _fork_options(nodes: dict, fork: dict) -> list[dict]:
    return [c for c in (nodes.get(i) for i in (fork.get("children") or []))
            if c and kind(c) == "option"]


def _picked_ids(nodes: dict, on_path: set) -> set:
    """An option counts as picked when it sits on the committed path OR is named in any join's
    `selected` (a refined node records the options it merged)."""
    chosen = set(on_path)
    for x in nodes.values():
        chosen.update(x.get("selected") or [])
    return chosen


# ── The views ────────────────────────────────────────────────────────────────

def path(s: dict, node_id: str | None) -> list[str]:
    """THE PIVOT CONTRACT's context: the pivot node and its ANCESTORS only (root → node, in order) —
    siblings and descendants are dropped. Pivoting from an option means the question was re-answered
    with ONLY that option selected, so the path ending at it IS the affirmative context."""
    nodes, _ = _nodes(s)
    return [snippet(n) for n in _chain(nodes, node_id)]


def screen(s: dict, node_id: str | None = None) -> str:
    """What the user is looking at RIGHT NOW, for the router. `node_id` (a node they have OPEN when
    it isn't the active one) takes over the frame — a question is about THAT node, a pivot grows
    from it. At a brainstorm fork the on-screen options are enumerated 1-based so 'the first two'
    and 'the consulting one' resolve to real picks."""
    parts = []
    nodes, active = _nodes(s)
    opened = nodes.get(node_id) if node_id else None
    if opened and node_id != active:
        parts.append("The user is looking at an EARLIER node of their build: " + snippet(opened))
        parts.append("A steer/pivot should grow from that node; a question is about it")
    a = nodes.get(active) or {}
    if kind(a) == "refined" and a.get("thesis"):
        parts.append("Refined idea: " + a["thesis"])
    elif (s.get("shaped") or {}).get("thesis"):
        parts.append("Idea: " + s["shaped"]["thesis"])
    else:
        parts.append("Idea: " + (s.get("idea") or "")[:160])
    if kind(a) == "brainstorm":   # the options ON SCREEN — without this the router can't see them
        titles = [((o.get("direction") or {}).get("title") or "")[:60]
                  for o in _fork_options(nodes, a)]
        if titles:
            parts.append("ON SCREEN: " + str(len(titles)) + " numbered directions to pick from: "
                         + " ".join(f"{i + 1}) '{x}'" for i, x in enumerate(titles)))
    p = s.get("proposal") or {}
    if p.get("title"):
        parts.append("Currently on the '" + p["title"] + "' part of the plan")
    return " · ".join(parts)[:SCREEN_CAP]


def journey(s: dict) -> str:
    """The decision-tree journey written out for the advisor: the committed path root→active one
    line per node, and at each fork every direction offered with ✓ on the ones the operator picked.
    Without this the advisor only sees the v1 surface (idea/vetting/files) and knows nothing about
    options, picks, or pivots — it literally can't answer 'which option did I pick?'."""
    nodes, active = _nodes(s)
    chain = _chain(nodes, active)
    if not chain:
        return ""
    on_path = {n["id"] for n in chain}
    chosen = _picked_ids(nodes, on_path)
    lines = []
    for n in chain:
        lines.append("- " + snippet(n))
        if kind(n) == "brainstorm":
            for c in _fork_options(nodes, n):
                d = c.get("direction") or {}
                mark = "✓ PICKED" if c["id"] in chosen else "passed over"
                lines.append(f"    · [{mark}] '{(d.get('title') or '')[:OPTION_TITLE]}': "
                             f"{(d.get('one_liner') or '')[:OPTION_LINE]}")
    return "\n".join(lines)[:JOURNEY_CAP]


def situation(s: dict, node_id: str | None = None, working: str | None = None) -> str:
    """Where the operator IS right now, for the advisor — ALWAYS states the active step (so 'which
    section am I in?' has a source of truth), plus what's mid-flight and which node they have open
    with its standing (committed path / picked / passed over). Without this the advisor coaches
    forward from nowhere — or claims it can't see the screen."""
    nodes, active = _nodes(s)
    parts = []
    a = nodes.get(active)
    if a:   # unconditional: the operator's position is never a mystery to the advisor
        parts.append("They are ON: " + snippet(a)
                     + (f" (funnel stage: {s.get('stage')})" if s.get("stage") else ""))
    if working:
        parts.append(f"A build step is MID-FLIGHT right now ({working[:80]}). The machine is already "
                     "working — do not prescribe new work or next steps.")
    elif s.get("status") == "researching":
        parts.append("Deep research is MID-FLIGHT right now. The machine is already working — do not "
                     "prescribe new work or next steps.")
    opened = nodes.get(node_id) if (node_id and node_id != active) else None
    if opened:
        on_path = {n["id"] for n in _chain(nodes, active)}
        chosen = _picked_ids(nodes, on_path)
        standing = ("on the committed path" if opened["id"] in on_path
                    else "PICKED and carried forward" if opened["id"] in chosen
                    else "PASSED OVER / abandoned — they are reading history, not asking to change course")
        parts.append("The operator is READING an earlier node (" + standing + "): " + snippet(opened))
    return "\n".join(parts)


def decisions_block(s: dict) -> str:
    """The operator's STANDING DECISIONS (axioms / non-negotiables / settled preferences) as one
    prompt block — hardest first, each labeled with its weight so a model can't quietly demote a
    hard constraint into a suggestion. Every model-facing surface (section drafts, spreads, merges,
    the board, the advisor) injects THIS block; empty when none are pinned. One renderer, one
    wording — a consumer must never hand-roll its own decisions string."""
    from app import decisions as _dec  # noqa: PLC0415 — avoid a module-load cycle
    rows = _dec.ordered((s or {}).get("decisions"))
    if not rows:
        return ""
    lines = "\n".join(
        f"- [{_dec.LABELS.get(d.get('weight'), 'FIRM')}] {d['text']}"
        + (f" (context: {d['why']})" if d.get("why") else "") for d in rows)
    return ("THE OPERATOR'S STANDING DECISIONS (declared settled — these constrain everything: "
            "honor every NON-NEGOTIABLE absolutely, treat FIRM ones as strong defaults you bend "
            "only with an explicit reason, lean toward NICE-TO-HAVEs when it's free to. When one "
            "of these shapes your output, say which):\n" + lines)


def evidence(s: dict) -> tuple[str, str]:
    """The gate-graded research as two prompt blocks: (cited, flagged). The labels ARE the product —
    a consumer must never re-merge these into one unlabeled list."""
    rows = ((s.get("research") or {}).get("rows")) or []
    cited = "\n".join(f"- {r['text']} [{r.get('url', '')}]" for r in rows if r.get("mark") == "ok") \
        or "- (none cleared)"
    flagged = "\n".join(f"- {r['text']} [{r.get('url', '')}]" for r in rows if r.get("mark") == "warn") \
        or "- (none flagged)"
    return cited, flagged


if __name__ == "__main__":  # self-test: the four 2026-07-04 drops, each pinned as an assertion
    S = {"idea": "cookies with ex cons on tiktok",
         "tree": {"active": "sec1", "nodes": {
             "i1": {"id": "i1", "kind": "idea", "parent": None, "draft": "cookies with ex cons",
                    "children": ["b1"]},
             "b1": {"id": "b1", "kind": "brainstorm", "parent": "i1", "children": ["o1", "o2"],
                    "feedback": "keep the story angle, drop the branded cookie delivery"},
             "o1": {"id": "o1", "kind": "option", "parent": "b1",
                    "direction": {"title": "Redemption Bakery Series", "one_liner": "A TikTok series."}},
             "o2": {"id": "o2", "kind": "option", "parent": "b1",
                    "direction": {"title": "Corporate gift boxes", "one_liner": "B2B gifting."}},
             "r1": {"id": "r1", "kind": "refined", "parent": "o1", "selected": ["o1"],
                    "thesis": "A creator channel that sells cookies", "children": ["sec1"]},
             "sec1": {"id": "sec1", "parent": "r1", "title": "The setup", "draft": "the setup body"}}}}
    # drop 1: the router couldn't see on-screen options → screen() enumerates them at a fork
    S2 = dict(S, tree=dict(S["tree"], active="b1"))
    assert "1) 'Redemption Bakery Series'" in screen(S2) and "2) 'Corporate gift boxes'" in screen(S2)
    # drop 2: a fork's steer vanished from a later pivot's path → snippet carries it
    assert any("drop the branded cookie delivery" in x for x in path(S, "sec1"))
    # drop 3: the advisor couldn't name the pick → journey marks ✓ PICKED / passed over
    j = journey(S)
    assert "✓ PICKED] 'Redemption Bakery Series'" in j and "passed over] 'Corporate gift boxes'" in j
    # drop 4 (class): a browsed node frames the router's read
    assert "EARLIER node" in screen(S, "o2") and "Corporate gift boxes" in screen(S, "o2")
    # drop 5 (class): the advisor must know WHERE the operator is — an abandoned node being read is
    # history, not an invitation to coach forward; a mid-flight build means no new prescriptions
    sit = situation(S, "o2")
    assert "READING" in sit and "PASSED OVER" in sit and "Corporate gift boxes" in sit
    assert "MID-FLIGHT" in situation(S, None, "Writing the next part")
    assert "They are ON: the 'The setup' section" in situation(S)   # position is NEVER a mystery
    # evidence keeps the labels split
    c, f = evidence({"research": {"rows": [{"mark": "ok", "text": "a", "url": "u"},
                                           {"mark": "warn", "text": "b", "url": "v"}]}})
    assert "a" in c and "b" in f
    # standing decisions render hardest-first with their weight labels; empty stays empty
    db = decisions_block({"decisions": [
        {"id": "d1", "text": "Prefer local clients", "weight": "nice_to_have", "why": ""},
        {"id": "d2", "text": "No cold-call marketing", "weight": "non_negotiable", "why": "hates phones"}]})
    assert db.index("[NON-NEGOTIABLE] No cold-call marketing") < db.index("[NICE-TO-HAVE]")
    assert "hates phones" in db and decisions_block({}) == ""
    print("context.py self-test OK — the four known drops are pinned")
