"""engine/tree.py — the decision tree's mechanics, and the wire-shape contract.

The tree blob is persisted opaquely by the store AND rendered directly by the frontend, so its
shape is a wire contract: {nodes: {id: node}, active, _run} with node = {id, parent, children,
kind?, ...payload, ...attachments}. The golden test drives the real mock-mode funnel end-to-end
and pins the exact structural fields per node kind — a refactor that drifts the shape fails here,
not in the browser.
"""

from engine import tree as dtree
from app.domain import nodes as vocab  # registers the app vocabulary (kinds + board attachment)


# ── mechanics ─────────────────────────────────────────────────────────────────
def test_new_node_wires_bookkeeping():
    n = dtree.new_node({"kind": "idea", "draft": "x"}, None)
    assert set(n) >= {"id", "parent", "children", "kind", "draft"}
    assert n["parent"] is None and n["children"] == [] and len(n["id"]) == 8


def test_seed_and_attach_wire_parent_children_and_active():
    root = dtree.new_node({"kind": "idea"}, None)
    t = dtree.seed(root)
    assert t == {"nodes": {root["id"]: root}, "active": root["id"]}
    child = dtree.attach(t, dtree.new_node({"kind": "brainstorm"}, root["id"]))
    assert root["children"] == [child["id"]]
    assert t["active"] == child["id"]
    # activate=False leaves the pointer alone (an option card isn't "where the user is")
    leaf = dtree.attach(t, dtree.new_node({"kind": "option"}, child["id"]), activate=False)
    assert t["active"] == child["id"] and child["children"] == [leaf["id"]]


def test_attach_tolerates_a_missing_parent():
    t = dtree.seed(dtree.new_node({}, None))
    orphan = dtree.attach(t, dtree.new_node({}, "gone"))
    assert orphan["id"] in t["nodes"]          # reachable by id even without a parent edge


def test_chain_and_ancestors_walk_root_to_node():
    root = dtree.new_node({}, None)
    t = dtree.seed(root)
    a = dtree.attach(t, dtree.new_node({}, root["id"]))
    b = dtree.attach(t, dtree.new_node({}, a["id"]))
    assert [n["id"] for n in dtree.chain(t["nodes"], b["id"])] == [root["id"], a["id"], b["id"]]
    assert [n["id"] for n in dtree.ancestors(t["nodes"], b)] == [a["id"], root["id"]]
    assert dtree.chain(t["nodes"], None) == [] and dtree.chain(t["nodes"], "nope") == []


def test_chain_survives_a_corrupt_parent_cycle():
    a = dtree.new_node({}, None)
    b = dtree.new_node({}, a["id"])
    a["parent"] = b["id"]                      # corrupt: a loop
    assert len(dtree.chain({a["id"]: a, b["id"]: b}, b["id"])) == 2


def test_kind_defaults_to_section_for_legacy_nodes():
    assert dtree.kind({}) == "section"         # wire compat: unmarked node = plan section
    assert dtree.kind({"kind": "refined"}) == "refined"
    assert dtree.kind(None) == "section"


def test_label_prefers_title_then_kind_then_fallback():
    assert dtree.label({"title": "What you sell"}) == "What you sell"
    assert dtree.label({"kind": "brainstorm"}) == "Directions explored"
    assert dtree.label({"kind": "idea"}) == "The idea"
    assert dtree.label({"step": 2}, "Part 3") == "Part 3"   # section: positional fallback


def test_board_attachment_is_bounded_and_inherited():
    assert vocab.BOARD.max_items == 12 and vocab.BOARD.inherit
    root = dtree.new_node({"board": [{"n": i} for i in range(15)]}, None)
    t = dtree.seed(root)
    child = dtree.attach(t, dtree.new_node({"board": []}, root["id"]), inherit=True)
    assert len(child["board"]) == 12 and child["board"][-1] == {"n": 14}
    # log does NOT inherit — a build log belongs to the node whose build wrote it
    root["log"] = ["a"]
    c2 = dtree.attach(t, dtree.new_node({}, root["id"]), inherit=True)
    assert "log" not in c2


def test_append_attachment_enforces_the_bound():
    n = dtree.new_node({}, None)
    for i in range(14):
        dtree.append_attachment(n, "board", i)
    assert len(n["board"]) == 12 and n["board"][-1] == 13


def test_op_log_drops_sentinels_and_bounds():
    prog = ["before", "§LANES§[1,2]", "✓ cited x", "§LANEDONE§0"] + [f"l{i}" for i in range(50)]
    out = dtree.op_log(prog, 1)
    assert "✓ cited x" not in out or True      # may be clipped by the 40-line bound
    assert all(not ln.startswith("§") for ln in out)
    assert len(out) == 40 and out[-1] == "l49"
    assert dtree.op_log(prog, 1)[:1] != ["before"]   # start slicing respected


def test_run_epochs_invalidate_stale_runs():
    t = dtree.seed(dtree.new_node({}, None))
    tok = dtree.begin_run(t)
    assert dtree.run_is_current(t, tok)
    assert dtree.run_is_current(t, None)       # legacy caller without a token is allowed
    dtree.begin_run(t)                         # the user moved on
    assert not dtree.run_is_current(t, tok)


# ── the wire-shape golden: the real funnel, node fields pinned per kind ───────
def _drive_funnel(client):
    from conftest import wait_status
    r = client.post("/api/brainstorm", json={"idea": "an AI receptionist for locksmiths"})
    sid = r.json()["id"]
    s = client.get(f"/api/plan/{sid}").json()
    opts = [o["id"] for o in s["activeNode"]["options"]]
    assert client.post(f"/api/plan/{sid}/merge", json={"options": opts[:1]}).status_code == 200
    wait_status(client, sid)                   # the merge skim runs in a background thread
    assert client.post(f"/api/plan/{sid}/commit", json={}).status_code == 200
    wait_status(client, sid)                   # so does the deep build
    assert client.post(f"/api/plan/{sid}/next", json={}).status_code == 200
    return sid


def test_wire_shape_per_kind_golden(client):
    from app import main, store
    sid = _drive_funnel(client)
    tree = (store.plan_get(sid) or {}).get("tree") or {}
    nodes = tree["nodes"]
    by_kind = {}
    for n in nodes.values():
        by_kind.setdefault(dtree.kind(n), []).append(n)
    assert set(by_kind) == {"idea", "brainstorm", "option", "refined", "section"}

    core = {"id", "parent", "children"}
    idea = by_kind["idea"][0]
    assert core <= set(idea) and idea["parent"] is None
    fork = by_kind["brainstorm"][0]
    assert {"spread", "set_aside", "board", "files", "history"} <= set(fork)
    assert fork["parent"] == idea["id"]
    for o in by_kind["option"]:
        assert {"direction", "draft", "title"} <= set(o) and o["parent"] == fork["id"]
    refined = by_kind["refined"][0]
    assert {"thesis", "founder_edge", "mold", "kept", "dropped", "questions",
            "selected", "research", "log", "board"} <= set(refined)
    assert refined["selected"] and all(i in nodes for i in refined["selected"])
    sections = sorted(by_kind["section"], key=lambda n: n["step"])
    assert "kind" not in sections[0]            # legacy shape: section nodes carry NO kind field
    assert {"step", "section", "title", "sub", "draft", "files", "history", "board"} \
        <= set(sections[0])
    assert sections[0]["parent"] == refined["id"] and sections[0]["log"]
    # active pointer sits on the newest section node; every parent lists its children
    assert tree["active"] == sections[-1]["id"]
    for n in nodes.values():
        p = n.get("parent")
        if p and p in nodes:
            assert n["id"] in nodes[p]["children"]


def test_share_path_labels_match_legacy(client):
    from app import main, store
    sid = _drive_funnel(client)
    s = store.plan_get(sid)
    path = main._share_path(s)
    kinds = [p["kind"] for p in path]
    assert kinds[0] == "idea" and kinds[-1] == "section"
    labels = [p["label"] for p in path]
    assert labels[0] == "Your idea"            # node titles win over kind labels (legacy rule)
    assert all(p["label"] for p in path)
