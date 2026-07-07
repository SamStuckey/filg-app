#!/usr/bin/env python3
"""
The decision tree — first-class nodes, kinds, attachments, and tree operations.

Every build session IS a tree of decisions: the operator's input at the root, forks where
alternatives were spread, choices that merged them, and staged artifacts grown from the winner.
This module owns that structure. The host app owns what the nodes MEAN (its node kinds, their
payloads, and their display language); the engine owns how they wire together.

WIRE SHAPE (persisted as one JSON blob; also what the frontend reads — do not break it):

    tree = {"nodes": {id: node, ...}, "active": id, "_run": epoch}
    node = {
        "id":       str,          # short unique id (uuid4 hex[:8])
        "parent":   str | None,   # edge up — None only on a root
        "children": [str, ...],   # edges down, in creation order
        "kind":     str,          # node kind (absent = the registered default kind)
        **payload,                # kind-specific fields, owned by the host app
        **attachments,            # registered per-node histories (see below)
    }

Dict-in, dict-out on purpose: the store persists this blob opaquely and the frontend renders it
directly, so a class wrapper would just be a second shape to keep in sync. The module gives the
shape one owner instead.

KINDS. A node kind names what a node is (a raw input, a fork of alternatives, one alternative,
a converged choice, a staged artifact...). The host registers its kinds with `register_kind`
(display label + description) and sets which kind an unmarked node means via
`set_default_kind` — legacy nodes carry no "kind" field, so the default IS wire compatibility.
`kind(node)` and `label(node)` then work for every consumer (views, share pages, routing).

ATTACHMENTS. An attachment is a named, bounded, per-node history — evidence and commentary that
rides the node it belongs to. Each is registered with a bound (how many items a node keeps) and
an inherit policy (whether a child starts with its parent's items, e.g. an advisory-board trail
follows the branch; a build log stays with the node that ran the build). Future relationships —
decisions, blockers, linked sub-trees — are new attachments or payload fields registered here,
not new tree mechanics.

RUN EPOCHS. A long background run must not clobber a user who moved on. `begin_run` stamps the
tree with a fresh epoch token; when the run finishes it checks `run_is_current` against the
FRESH tree and discards its result (never the spend) if the user pivoted meanwhile.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

# ── ids ───────────────────────────────────────────────────────────────────────
def new_id() -> str:
    return uuid.uuid4().hex[:8]


# ── kind registry ─────────────────────────────────────────────────────────────
@dataclass
class Kind:
    name: str
    label: str = ""            # display label for a node with no title of its own
    desc: str = ""             # one-liner for docs/tooling


_KINDS: dict[str, Kind] = {}
_DEFAULT_KIND = "node"


def register_kind(name: str, *, label: str = "", desc: str = "") -> Kind:
    """Declare a node kind. Idempotent by name — re-registering replaces the metadata."""
    k = Kind(name, label=label, desc=desc)
    _KINDS[name] = k
    return k


def set_default_kind(name: str) -> None:
    """The kind an unmarked node means. Part of the wire contract: legacy nodes carry no
    'kind' field, so the host must name what those are (FILG: the staged 'section' node)."""
    global _DEFAULT_KIND
    _DEFAULT_KIND = name


def kinds() -> dict[str, Kind]:
    return dict(_KINDS)


def kind(node: dict) -> str:
    """A node's kind; absent field → the registered default."""
    return (node or {}).get("kind") or _DEFAULT_KIND


def label(node: dict, fallback: str = "") -> str:
    """Display label for a node: its own title, else its kind's registered label, else
    `fallback` (the host supplies positional labels like 'Part 3' where it wants them)."""
    n = node or {}
    if n.get("title"):
        return n["title"]
    k = _KINDS.get(kind(n))
    if k and k.label:
        return k.label
    return fallback


# ── attachment registry ───────────────────────────────────────────────────────
@dataclass
class Attachment:
    name: str
    max_items: int             # per-node bound — histories never grow without limit
    inherit: bool = False      # a new child starts with its parent's items
    desc: str = ""


_ATTACHMENTS: dict[str, Attachment] = {}


def register_attachment(name: str, *, max_items: int, inherit: bool = False,
                        desc: str = "") -> Attachment:
    """Declare a per-node history (board trail, build log, and future: decisions, blockers...).
    Idempotent by name."""
    a = Attachment(name, max_items=max_items, inherit=inherit, desc=desc)
    _ATTACHMENTS[name] = a
    return a


def attachments() -> dict[str, Attachment]:
    return dict(_ATTACHMENTS)


def clip(name: str, items: list) -> list:
    """Bound an attachment's items to its registered max (keeps the newest)."""
    a = _ATTACHMENTS.get(name)
    return list(items or [])[-a.max_items:] if a else list(items or [])


def append_attachment(node: dict, name: str, item) -> None:
    """Append one item to a node's attachment, enforcing its bound."""
    node[name] = clip(name, list(node.get(name) or []) + [item])


def inherit_attachments(nodes: dict, node: dict) -> None:
    """Seed a freshly created node with its parent's inheritable attachments (e.g. the
    advisory trail follows the branch so earlier reviews keep steering after a fork)."""
    parent = nodes.get(node.get("parent") or "")
    if not parent:
        return
    for a in _ATTACHMENTS.values():
        if a.inherit and parent.get(a.name):
            node[a.name] = clip(a.name, parent[a.name])


# The progress stream uses `§`-prefixed sentinel lines (§LANES§ / §LANEDONE§) to drive live UI
# animation; they are transport, not history, so a persisted log drops them.
_SENTINEL = "§"
LOG = register_attachment("log", max_items=40, inherit=False,
                          desc="build receipts — how this node was built (one op's slice)")


def op_log(progress: list, start: int) -> list:
    """One op's slice of a progress stream, cleaned for persistence on the node it built:
    sentinel lines dropped, bounded to the log attachment's max."""
    return clip("log", [ln for ln in progress[start:] if not str(ln).startswith(_SENTINEL)])


# ── nodes + tree operations ───────────────────────────────────────────────────
def new_node(content: dict, parent: str | None) -> dict:
    """Wrap pure node content (payload from the host's builders) with the tree bookkeeping
    this module owns: id, parent edge, empty children."""
    return {"id": new_id(), "parent": parent, "children": [], **content}


def seed(node: dict) -> dict:
    """A fresh single-node tree with `node` active."""
    return {"nodes": {node["id"]: node}, "active": node["id"]}


def attach(tree: dict, node: dict, *, activate: bool = True,
           inherit: bool = False) -> dict:
    """Wire `node` into `tree`: index it, append it to its parent's children (when the parent
    exists — a missing/pruned parent leaves the node reachable by id, same as before), apply
    inheritable attachments when asked, and optionally move the active pointer onto it."""
    nodes = tree.setdefault("nodes", {})
    if inherit:
        inherit_attachments(nodes, node)
    nodes[node["id"]] = node
    parent = node.get("parent")
    if parent and nodes.get(parent):
        nodes[parent].setdefault("children", []).append(node["id"])
    if activate:
        tree["active"] = node["id"]
    return node


def active_node(tree: dict) -> dict | None:
    t = tree or {}
    return (t.get("nodes") or {}).get(t.get("active"))


def get(tree: dict, node_id: str | None) -> dict | None:
    return ((tree or {}).get("nodes") or {}).get(node_id or "")


def chain(nodes: dict, node_id: str | None) -> list[dict]:
    """Root → node path (the committed decision path). Cycle-safe: a corrupt parent loop
    terminates instead of hanging."""
    out, seen, cur = [], set(), node_id
    while cur is not None and cur in nodes and cur not in seen:
        seen.add(cur)
        out.append(nodes[cur])
        cur = nodes[cur].get("parent")
    out.reverse()
    return out


def ancestors(nodes: dict, node: dict):
    """Walk UP from `node` (excluded), yielding each ancestor. Cycle-safe."""
    seen = set()
    cur = nodes.get(node.get("parent")) if node.get("parent") else None
    while cur is not None and cur["id"] not in seen:
        seen.add(cur["id"])
        yield cur
        cur = nodes.get(cur.get("parent")) if cur.get("parent") else None


def newest_first(nodes: dict):
    """Nodes newest-first (dict insertion order = creation order)."""
    return reversed(list(nodes.values()))


def _letters(i: int) -> str:
    """0 → a, 1 → b, … 26 → aa (spreadsheet-column style, for absurdly wide forks)."""
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(97 + r) + s
    return s


def _visual_parent(n: dict, nodes: dict) -> str | None:
    """The node this one hangs beneath ON THE GRAPH. A JOIN (its `selected` names the branches it
    merged) is drawn under its first merged pick, one level deeper — even though its structural
    parent is the fork. Everything else hangs under its structural parent."""
    for sid in (n.get("selected") or []):
        if sid in nodes:
            return sid
    return n.get("parent")


def numbers(nodes: dict) -> dict[str, str]:
    """A human 'box number' per node: its VISUAL depth from the root (1-based, joins counted
    beneath the pick they merged), plus a sibling letter when its parent forked — so a linear
    funnel reads 1, 2, 3a/3b/3c (the fork's options), 4 (the join), 5… Joins never take a letter
    against the options they chose between; two joins under the same fork letter among themselves.
    The number is how the UI and any modal/log REFER to a box, so one owner computes it (here)
    and every view inherits it. Cycle-safe; an orphaned subtree numbers from where its chain
    breaks (still stable, still unique among siblings)."""
    out = {}
    for nid, n in nodes.items():
        depth, seen, cur = 1, {nid}, n
        while True:
            pid = _visual_parent(cur, nodes)
            if not pid or pid not in nodes or pid in seen:
                break
            seen.add(pid)
            cur = nodes[pid]
            depth += 1
        letter = ""
        parent = nodes.get(n.get("parent") or "")
        if parent:
            joins = bool(n.get("selected"))
            sibs = [c for c in (parent.get("children") or [])
                    if c in nodes and bool(nodes[c].get("selected")) == joins]
            if len(sibs) > 1 and nid in sibs:
                letter = _letters(sibs.index(nid))
        out[nid] = f"{depth}{letter}"
    return out


# ── run epochs ────────────────────────────────────────────────────────────────
def begin_run(tree: dict) -> str:
    """Stamp the tree with this run's epoch token. Any navigation that abandons in-flight work
    (a pivot, another run) stamps a new one, invalidating the old run's result."""
    tok = new_id()
    tree["_run"] = tok
    return tok


def run_is_current(tree: dict, token: str | None) -> bool:
    """Whether a finishing background run may still attach its result. No token = legacy
    caller that never stamped — allowed (matches pre-epoch behavior)."""
    return not token or (tree or {}).get("_run") == token


if __name__ == "__main__":  # self-test: python -m engine.tree
    register_kind("idea", label="The idea")
    register_kind("fork", label="Fork")
    set_default_kind("stage")
    register_attachment("board", max_items=3, inherit=True)

    root = new_node({"kind": "idea", "draft": "raw input", "board": [1, 2, 3, 4]}, None)
    t = seed(root)
    assert active_node(t) is root and root["children"] == []
    child = attach(t, new_node({"title": "A few directions", "kind": "fork"}, root["id"]),
                   inherit=True)
    assert child["board"] == [2, 3, 4]                      # inherited, clipped to the bound
    assert root["children"] == [child["id"]] and t["active"] == child["id"]
    leaf = attach(t, new_node({"step": 2}, child["id"]), activate=False)
    assert t["active"] == child["id"] and kind(leaf) == "stage"
    assert [n["id"] for n in chain(t["nodes"], leaf["id"])] == [root["id"], child["id"], leaf["id"]]
    assert [n["id"] for n in ancestors(t["nodes"], leaf)] == [child["id"], root["id"]]
    assert label(root) == "The idea" and label(child) == "A few directions"
    assert label(leaf, "Part 3") == "Part 3"
    append_attachment(leaf, "board", "x")
    assert leaf["board"] == ["x"]
    assert op_log(["a", "§LANES§[]", "b"], 0) == ["a", "b"]
    tok = begin_run(t)
    assert run_is_current(t, tok) and run_is_current(t, None)
    begin_run(t)
    assert not run_is_current(t, tok)
    # corrupt parent cycle terminates
    a = new_node({}, None); b = new_node({}, a["id"]); a["parent"] = b["id"]
    assert len(chain({a["id"]: a, b["id"]: b}, b["id"])) == 2
    # box numbers: depth on a linear path, sibling letters at a fork, cycle-safe
    leaf2 = attach(t, new_node({"step": 2}, child["id"]), activate=False)
    nums = numbers(t["nodes"])
    assert nums[root["id"]] == "1" and nums[child["id"]] == "2"
    assert nums[leaf["id"]] == "3a" and nums[leaf2["id"]] == "3b"
    # a JOIN (selected names its picks) hangs beneath its first pick: depth 4, no letter against
    # the options it chose between; its own child continues 5
    join = attach(t, new_node({"selected": [leaf["id"]]}, child["id"]), activate=False)
    after = attach(t, new_node({}, join["id"]), activate=False)
    nums = numbers(t["nodes"])
    assert nums[join["id"]] == "4" and nums[after["id"]] == "5"
    assert nums[leaf["id"]] == "3a" and nums[leaf2["id"]] == "3b"   # options unchanged
    assert _letters(0) == "a" and _letters(25) == "z" and _letters(26) == "aa"
    assert numbers({a["id"]: a, b["id"]: b})   # the corrupt cycle still terminates
    print("tree.py self-test OK —", len(_KINDS), "kinds,", len(_ATTACHMENTS), "attachments")
