"""Standing decisions — the operator's pinned axioms (app/decisions.py + the Summary tab).

Covers the whole contract: CRUD + validation, the summary-tab "pin it?" detection on /route,
the context engine's one prompt renderer, injection into every model surface (section drafts,
merges, spreads, board convenes, the advisor), the per-node stamp + receipts (the reference
trail), and the impact list PATCH/DELETE return for the revisit-and-pivot modal.
"""

from tests.conftest import frontend, wait_status


def _brainstorm(client, idea="I bake sourdough and want to sell it somehow around town"):
    r = client.post("/api/brainstorm", json={"idea": idea})
    assert r.status_code == 200, r.text
    return r.json()


def _add(client, sid, text, weight="firm", why=""):
    r = client.post(f"/api/plan/{sid}/decisions", json={"text": text, "weight": weight, "why": why})
    assert r.status_code == 200, r.text
    return r.json()["added"]


# ── CRUD + validation ────────────────────────────────────────────────────────

def test_decisions_crud_and_plan_state(client):
    s = _brainstorm(client)
    sid = s["id"]
    assert s["decisions"] == []                       # plan_state carries the (empty) index

    d = _add(client, sid, "No cold-call marketing", weight="non-negotiable", why="hates phones")
    assert d["id"].startswith("dc") and d["weight"] == "non_negotiable"   # display form normalizes
    s = client.get(f"/api/plan/{sid}").json()
    assert [x["text"] for x in s["decisions"]] == ["No cold-call marketing"]

    # edit: text + weight move; a material change returns the (empty, nothing built) impact list
    r = client.patch(f"/api/plan/{sid}/decisions/{d['id']}",
                     json={"text": "No outbound calls at all", "weight": "firm"})
    assert r.status_code == 200
    body = r.json()
    assert body["decisions"][0]["text"] == "No outbound calls at all"
    assert body["changed"]["before"]["text"] == "No cold-call marketing"
    assert body["impact"] == []                       # nothing built yet → nothing to revisit

    # a no-op save must NOT open the revisit modal
    r = client.patch(f"/api/plan/{sid}/decisions/{d['id']}",
                     json={"text": "No outbound calls at all", "weight": "firm"})
    assert r.json()["impact"] == [] and r.status_code == 200

    # remove
    r = client.request("DELETE", f"/api/plan/{sid}/decisions/{d['id']}")
    assert r.status_code == 200
    assert r.json()["removed"]["text"] == "No outbound calls at all"
    assert client.get(f"/api/plan/{sid}").json()["decisions"] == []

    # validation: junk text 400s, unknown ids 404
    assert client.post(f"/api/plan/{sid}/decisions", json={"text": "  x "}).status_code == 400
    assert client.patch(f"/api/plan/{sid}/decisions/nope", json={"text": "yy"}).status_code == 404
    assert client.request("DELETE", f"/api/plan/{sid}/decisions/nope").status_code == 404


def test_decisions_cap(client):
    from app import decisions as dm
    sid = _brainstorm(client)["id"]
    for i in range(dm.MAX_DECISIONS):
        _add(client, sid, f"axiom number {i}")
    r = client.post(f"/api/plan/{sid}/decisions", json={"text": "one too many"})
    assert r.status_code == 409


# ── detection on /route — every chat mode offers; work-talk never does ───────

def test_route_offers_the_pin_in_any_mode(client):
    sid = _brainstorm(client)["id"]
    # declarative preference typed in the Summary tab → an OFFER, no routed action
    r = client.post(f"/api/plan/{sid}/route",
                    json={"prompt": "i don't want to do cold call marketing", "summary": True})
    assert r.status_code == 200
    d = r.json()
    assert d["offer"]["weight"] == "non_negotiable" and "decision" not in d
    # a question in the summary tab routes normally — no offer
    r = client.post(f"/api/plan/{sid}/route",
                    json={"prompt": "what should I charge for this?", "summary": True})
    assert "offer" not in r.json() and r.json()["decision"]["intent"] == "ask"
    # a declarative BUSINESS statement offers from every mode (Sam, 2026-07-07) — build...
    r = client.post(f"/api/plan/{sid}/route",
                    json={"prompt": "i don't want to do cold call marketing"})
    assert r.json()["offer"]["weight"] == "non_negotiable"
    # ...and the tool modes (research / board / help)
    for mode in ("research", "board", "help"):
        r = client.post(f"/api/plan/{sid}/route",
                        json={"prompt": "I want to run this as a non-profit", "mode": mode})
        assert r.json().get("offer"), f"no offer in {mode} mode"
    # work-talk with a declarative marker is a STEER, never an offer — build feedback isn't nagged
    r = client.post(f"/api/plan/{sid}/route", json={"prompt": "i want this section shorter"})
    assert "offer" not in r.json() and r.json()["decision"]["intent"] == "steer"
    r = client.post(f"/api/plan/{sid}/route", json={"prompt": "i want to build the plan now"})
    assert "offer" not in r.json()   # a build verb routes, it doesn't pin


# ── stamping + receipts + impact through the live funnel ─────────────────────

def test_nodes_stamp_decisions_and_impact_names_them(client):
    s = _brainstorm(client)
    sid = s["id"]
    d = _add(client, sid, "No cold-call marketing", weight="non_negotiable")

    # merge (background) → the refined node records the decision in force + a 🧭 receipt
    opts = [o["id"] for o in s["activeNode"]["options"]]
    client.post(f"/api/plan/{sid}/merge", json={"options": opts[:1]})
    s = wait_status(client, sid)
    refined = next(n for n in s["tree"]["nodes"] if n["kind"] == "refined")
    assert refined["decisions"] == [d["id"]]
    assert any("🧭 honoring 1 standing decision" in ln for ln in s["activeNode"]["log"])

    # commit (deep build) → the section root is stamped too
    client.post(f"/api/plan/{sid}/commit", json={})
    s = wait_status(client, sid)
    section = next(n for n in s["tree"]["nodes"] if n["kind"] == "section")
    assert section["decisions"] == [d["id"]]

    # /next → the drafted child is stamped
    client.post(f"/api/plan/{sid}/next", json={})
    s = client.get(f"/api/plan/{sid}").json()
    stamped = [n for n in s["tree"]["nodes"] if n.get("decisions") == [d["id"]]]
    assert len(stamped) >= 3

    # PATCH with a material change names the shaped steps (the revisit modal's list)
    r = client.patch(f"/api/plan/{sid}/decisions/{d['id']}",
                     json={"text": "Cold calls are fine actually", "weight": "nice_to_have"})
    impact = r.json()["impact"]
    assert {i["id"] for i in impact} >= {refined["id"], section["id"]}
    # each row names the box the way the graph does (num) and can summarize it (snippet)
    assert all(set(i) >= {"id", "kind", "label", "num", "snippet", "onPath"} for i in impact)
    ref_row = next(i for i in impact if i["id"] == refined["id"])
    assert ref_row["num"] and ref_row["snippet"].startswith("the refined idea")
    # the graph view carries the same box numbers (1 at the root; letters only at forks)
    s = client.get(f"/api/plan/{sid}").json()
    by_id = {n["id"]: n for n in s["tree"]["nodes"]}
    assert all(n.get("num") for n in s["tree"]["nodes"])
    assert ref_row["num"] == by_id[refined["id"]]["num"]

    # DELETE returns the same trail; the stamps stay on the nodes (history, not live pointers)
    r = client.request("DELETE", f"/api/plan/{sid}/decisions/{d['id']}")
    assert {i["id"] for i in r.json()["impact"]} >= {refined["id"], section["id"]}
    s = client.get(f"/api/plan/{sid}").json()
    assert any(n.get("decisions") for n in s["tree"]["nodes"])   # the record survives removal


def test_pivot_spread_is_stamped_and_constrained(client):
    s = _brainstorm(client)
    sid = s["id"]
    d = _add(client, sid, "keep it local, no shipping", weight="firm")
    opt = s["activeNode"]["options"][0]["id"]
    r = client.post(f"/api/plan/{sid}/rebrainstorm",
                    json={"feedback": "make it a wholesale play for cafes", "node": opt})
    assert r.status_code == 200
    s2 = r.json()
    fork = next(n for n in s2["tree"]["nodes"]
                if n["kind"] == "brainstorm" and n.get("feedback"))
    assert fork["decisions"] == [d["id"]]


# ── the one renderer + injection into every model surface (real prompt paths) ─

def test_decisions_block_reaches_the_section_prompt(patch_call):
    from app import planner
    seen = {}
    def spy(stage, prompt):
        seen[stage] = prompt
        return "A clean draft with no tells."
    patch_call(spy)
    from app import context
    block = context.decisions_block({"decisions": [
        {"id": "d1", "text": "No cold-call marketing", "weight": "non_negotiable", "why": ""}]})
    planner.propose("guitar coaching", "brief", {"rows": []}, [], decisions=block)
    assert "NON-NEGOTIABLE] No cold-call marketing" in seen["plan_brief"]
    # without decisions the prompt stays clean (no empty scaffolding)
    seen.clear()
    planner.propose("guitar coaching", "brief", {"rows": []}, [])
    assert "STANDING DECISIONS" not in seen["plan_brief"]


def test_decisions_block_reaches_board_and_advisor_and_merge(patch_call):
    import json as _json
    from app import advisor, board, brainstorm, context
    sess = {"idea": "x", "decisions": [
        {"id": "d1", "text": "this stays a non-profit", "weight": "non_negotiable", "why": ""}]}
    block = context.decisions_block(sess)
    seen = {}
    def spy(stage, prompt):
        seen[stage] = prompt
        if stage == "board_skeptic":
            return _json.dumps({"verdict": "concern", "rationale": "r", "suggested_change": "",
                                "confidence": "low"})
        if stage == "board_synth":
            return _json.dumps({"consensus": "c", "conflicts": "none", "verdict": "v"})
        if stage == "merge":
            return _json.dumps({"thesis": "t", "founder_edge": "e", "mold": "", "kept": [],
                                "dropped": [], "questions": []})
        return "a take"
    patch_call(spy)
    board.convene("idea", "", "is this right?", ["closer"], decisions=block)
    assert "non-profit" in seen["board_closer"] and "non-profit" in seen["board_skeptic"]
    advisor.chat_reply(sess, "how do I grow?", history=[])
    assert "non-profit" in seen["plan_chat"]
    brainstorm.merge("idea", [{"title": "a", "one_liner": "b"}], research=False, decisions=block)
    assert "non-profit" in seen["merge"]


def test_mock_advisor_acknowledges_decisions(client):
    sid = _brainstorm(client)["id"]
    _add(client, sid, "no paid ads")
    r = client.post(f"/api/plan/{sid}/chat", json={"message": "what should I do first?"})
    assert r.status_code == 200
    assert "standing decision" in r.json()["reply"]


# ── frontend wiring guards ───────────────────────────────────────────────────

def test_frontend_carries_the_decisions_surface(client):
    src = frontend(client)
    for needle in ("decListHtml", "decModal", "decRevisitModal", "decOfferChat", "decNoteHtml",
                   "body.summary=true", ".decrow", "non_negotiable", "decPivotSelected",
                   "gnum", "decimpsub"):
        assert needle in src, f"frontend lost {needle!r}"
