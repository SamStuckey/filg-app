#!/usr/bin/env python3
"""
The view layer — how a session row is SHAPED for a consumer, with no route logic and no writes.

plan_state is the master session -> frontend serializer (the poll payload); tree_view /
active_node_view are its tree-shaped halves; mirror flattens the active node back onto the
legacy flat columns the frontend still reads; ensure_tree lazily seeds a tree for pre-branching
plans; share_path renders the committed decision path for the public share page. Pure functions
of the session dict — main.py routes call them, nothing here calls back into main.
"""

from __future__ import annotations

from engine import provider
from engine import tree as dtree

from app import advisor, context, ops, planner, store
from app.access import _has_pdf_access

_kind = context.kind   # one owner for "what kind is this node" — the context engine


def share_path(s: dict) -> list[dict]:
    """The committed decision path (root → active) as public-safe steps: kind + label + the operator's
    pivot/steer note. This is the provenance trail — what the plan decided and why, not just the output.
    Reads the STORED tree (nodes = dict keyed by id; kind derived via _kind), not the frontend view."""
    tree = s.get("tree") or {}
    nodes = tree.get("nodes") or {}
    out = []
    for n in dtree.chain(nodes, tree.get("active")):
        out.append({"kind": _kind(n),
                    "label": dtree.label(n, f"Part {int(n.get('step') or 0) + 1}"),
                    "note": (n.get("feedback") or "").strip()})
    return out


def plan_state(s: dict) -> dict:
    """Shape a session row for the frontend."""
    return {
        "id": s["id"], "status": s["status"], "idea": s["idea"], "error": s.get("error"),
        "research": s.get("research"),
        "shaped": s.get("shaped"), "vetting": s.get("vetting"),
        "directors": s.get("directors") or [], "board": s.get("board") or [],
        "customDirectors": [{"key": p["key"], "name": p["name"], "first": p.get("first"),
                             "blurb": p.get("blurb"), "domains": p.get("domains") or []}
                            for p in (s.get("custom_directors") or [])],   # custom-forged board chips
        "files": [{"path": p, "content": c} for p, c in (s.get("files") or {}).items()],
        "sections": [{"file": x["file"], "title": x["title"], "sub": x["sub"]}
                     for x in planner.SECTIONS],
        "step": s.get("step", 0), "total": planner.N, "proposal": s.get("proposal"),
        "done": s["status"] == "done", "shared": bool(s.get("shared")),
        "owned": bool((s.get("user") or "").strip()),   # anonymous taste vs claimed-by-an-account
        "qa": s.get("qa"),   # final QA-pass report {notes, fixed} on the finished plan

        "tree": tree_view(s["tree"]) if s.get("tree") else None,
        "stage": s.get("stage"),                      # funnel position: brainstorm | refined | building | done
        "activeNode": active_node_view(s),           # the active node's funnel payload (option cards / refined idea / fork)
        "chat": s.get("chat") or [], "chatStarters": advisor.STARTERS,
        "decisions": s.get("decisions") or [],   # standing axioms — the Summary tab's editable index
        "lookups": s.get("lookups") or [],   # persisted chat-lookup claims — the research stack survives a reload
        # the stress-test's durable state (status + result only; live progress rides its poll route)
        "skeptic": ({"status": (s.get("skeptic") or {}).get("status"),
                     "result": (s.get("skeptic") or {}).get("result")} if s.get("skeptic") else None),
        "progress": s.get("progress") or [],
        "cost": s.get("cost") or 0, "tokens": s.get("tokens") or 0,   # live session usage meter
        "stack": s.get("stack") or provider.DEFAULT_STACK,            # chosen model stack
        # PDF access for THIS active branch + the account's remaining credits. The button shows
        # Download when this plan is already unlocked OR there are credits to spend; else Unlock ($7=3).
        # A new branch built from an earlier node is a fresh plan_key → locked until claimed.
        "pdfUnlocked": _has_pdf_access((s.get("user") or "").strip(), ops.plan_key(s), verified=True),
        "pdfCredits": store.credits_left((s.get("user") or "").strip()),
    }


# ── Branching decision tree — engine/tree.py owns the wiring; planner supplies pure content ──
_new_node = dtree.new_node   # node bookkeeping (id / parent / children) has one owner: the engine
_kind = context.kind         # one owner for "what kind is this node" — the context engine


def tree_view(tree: dict) -> dict:
    """Trim the stored node tree to what the frontend needs to draw + navigate it. Carries each node's
    `kind` so the graph can render option/refined/section/fork nodes differently. `show` is on from the
    first render (even a single node) so the decision-graph surface is always there."""
    nodes = tree.get("nodes") or {}
    return {"active": tree.get("active"),
            "nodes": [{"id": n["id"], "parent": n.get("parent"), "step": n.get("step", 0),
                       "kind": _kind(n), "title": n.get("title"), "feedback": n.get("feedback"),
                       # a refined node names the options it JOINED — the graph draws it as a merge
                       # of those branches, not a sibling branch off the brainstorm fork
                       **({"selected": n.get("selected")} if n.get("selected") else {}),
                       # the standing decisions in force when this node was built (ids) — the
                       # frontend renders the 🧭 reference note from these
                       **({"decisions": n.get("decisions")} if n.get("decisions") else {})}
                      for n in nodes.values()],
            "show": bool(nodes)}


def active_node_view(s: dict) -> dict | None:
    """The active node's full funnel payload, so the frontend can render the current stage (the option
    cards, the refined idea, a pending fork) without a second fetch. Section nodes carry no extra
    payload (the existing `proposal`/`sections` fields already cover them)."""
    t = s.get("tree") or {}
    a = (t.get("nodes") or {}).get(t.get("active"))
    if not a:
        return None
    k = _kind(a)
    view = {"id": a["id"], "kind": k, "title": a.get("title"),
            "log": a.get("log") or []}   # persisted build receipts, so a reload restores them (§v2 #10)
    if k == "brainstorm":
        view["spread"] = a.get("spread")
        view["feedback"] = a.get("feedback")   # the pivot ask this spread answers (if any)
        view["set_aside"] = a.get("set_aside")  # declined-out-loud part of the ask — shown, not hidden
        view["options"] = [{"id": c, "direction": ((t["nodes"].get(c) or {}).get("direction"))}
                           for c in a.get("children", []) if _kind(t["nodes"].get(c) or {}) == "option"]
    elif k == "option":
        view["direction"] = a.get("direction")
    elif k == "refined":
        for f in ("thesis", "founder_edge", "mold", "kept", "dropped", "research", "selected",
                  "questions", "feedback"):
            view[f] = a.get(f)
    elif k == "fork":
        view["question"] = a.get("question")
        view["options"] = a.get("options")
    return view


def mirror(tree: dict) -> dict:
    """Flat session fields (step/files/proposal/history/board/status) for the active node, so the
    existing _plan_state + frontend renders keep working off the active branch unchanged. A funnel node
    (brainstorm/option/refined/fork) isn't a plan section, so it mirrors to neutral 'building' state with
    no proposal — the funnel payload rides on _active_node_view instead."""
    a = tree["nodes"][tree["active"]]
    if _kind(a) != "section":
        # funnel nodes carry no plan state, but chat convenes stored on them still surface
        return {"step": 0, "files": {}, "history": [], "board": a.get("board") or [],
                "proposal": None, "qa": None, "status": "building"}
    done = a["step"] >= planner.N
    return {"step": a["step"], "files": a["files"], "history": a["history"], "board": a["board"],
            "proposal": (None if done else {"section": a["section"], "title": a["title"],
                                            "draft": a["draft"], "change": a.get("change")}),
            "qa": a.get("qa") if done else None,   # the final QA-pass report, surfaced on the finished branch
            "status": "done" if done else "building",
            # the funnel stage must land on 'done' too, or the frontend keeps offering the next
            # chapter forever ('Part 8 of 7' + Keep going — the off-ramp bug, 2026-07-04)
            **({"stage": "done"} if done else {})}


def ensure_tree(s: dict) -> dict:
    """Return the session's node tree, lazily seeding a single-node tree from legacy flat state for
    plans created before branching existed (so resumed in-progress plans still go Next/Back)."""
    t = s.get("tree")
    if t and t.get("nodes"):
        return t
    p = s.get("proposal") or {}
    step = s.get("step", 0) or 0
    sec = next((x for x in planner.SECTIONS if x["key"] == p.get("section")), None)
    node = dtree.new_node({"step": step, "section": (sec or {}).get("key"),
                      "title": (sec or {}).get("title", "Plan complete"),
                      "sub": (sec or {}).get("sub", ""), "draft": p.get("draft"),
                      "files": s.get("files") or {}, "history": s.get("history") or [],
                      "board": s.get("board") or [], "feedback": None}, None)
    return dtree.seed(node)

