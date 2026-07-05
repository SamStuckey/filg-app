"""Board + research v2 integration (2026-07-06): the convene history rides the node payloads, chat
lookups persist on the session, and the stress-test state is exposed on the plan state — the server
side of 'one chat, three displays' for the board room."""

import time

GRAB_BAG = "I fix small business phone systems and want to sell that as a service"


def wait_status(client, sid):
    for _ in range(200):
        s = client.get(f"/api/plan/{sid}").json()
        if s["status"] != "researching":
            return s
        time.sleep(0.01)
    raise AssertionError("plan never left researching")


def _start(client, email):
    sid = client.post("/api/plan/start", json={"idea": GRAB_BAG, "email": email}).json()["id"]
    wait_status(client, sid)
    return sid


def test_convene_lands_on_state_and_node(client):
    sid = _start(client, "bd1@x.com")
    r = client.post(f"/api/plan/{sid}/board", json={"question": "is the pricing sane?"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("directors") and d.get("skeptic")          # the real convene shape
    s = client.get(f"/api/plan/{sid}").json()
    assert s["board"] and s["board"][-1]["section"] == "convene"
    assert "pricing sane" in s["board"][-1]["title"]
    # the node endpoint now carries the step's board history (the node view renders it)
    nid = s["tree"]["active"]
    n = client.get(f"/api/plan/{sid}/node/{nid}").json()
    assert isinstance(n.get("board"), list) and n["board"], "active node should carry its convenes"
    assert n["board"][-1]["section"] == "convene"


def test_lookup_claims_persist_on_session(client):
    sid = _start(client, "bd2@x.com")
    r = client.post(f"/api/plan/{sid}/lookup", json={"message": "how big is the market?"})
    assert r.status_code == 200 and r.json()["claims"]
    s = client.get(f"/api/plan/{sid}").json()
    assert s["lookups"], "lookup claims must survive on the session (reload-stable research stack)"
    n0 = len(s["lookups"])
    # a second identical lookup dedupes instead of doubling
    client.post(f"/api/plan/{sid}/lookup", json={"message": "how big is the market?"})
    assert len(client.get(f"/api/plan/{sid}").json()["lookups"]) == n0


def test_stress_test_state_exposed(client):
    sid = _start(client, "bd3@x.com")
    r = client.post(f"/api/plan/{sid}/stress-test", json={})
    assert r.status_code == 200 and r.json().get("started")
    for _ in range(300):                       # the mock worker runs in a thread — wait it out
        st = client.get(f"/api/plan/{sid}/stress-test").json()
        if st["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert st["status"] == "done" and st.get("result")
    s = client.get(f"/api/plan/{sid}").json()  # the durable state rides the plan payload for v2
    assert s.get("skeptic") and s["skeptic"]["status"] == "done" and s["skeptic"]["result"]


def test_forge_and_seat_director(client):
    sid = _start(client, "bd4@x.com")
    r = client.post(f"/api/plan/{sid}/director/forge",
                    json={"description": "a grumpy dental office manager who hates vendor pitches"})
    assert r.status_code == 200
    persona = r.json()["persona"]
    assert persona.get("key") and persona.get("name") and persona.get("voice")
    r2 = client.post(f"/api/plan/{sid}/director/save", json={"persona": persona})
    assert r2.status_code == 200
    d = r2.json()
    assert any(c["key"] == persona["key"] for c in d["customDirectors"])
    assert persona["key"] in d["directors"]     # seated on the active board


def test_help_pricing_facts_track_the_live_ladder():
    """The help prompt's pricing block is GENERATED from tiers.py + billing.py — it must carry the
    live prices and none of the dead models (the 2026-07-06 QA caught help quoting '$13 one-time,
    no subscription' from a hardcoded prompt)."""
    from app import main, tiers
    block = main._help_system()
    for t in tiers.catalog():                      # every live tier, by label and price
        assert t["label"] in block and f"${t['price']:g}/mo" in block
    assert "$7" in block and "watermark" in block  # the PDF story (credits + free watermarked copy)
    for dead in ("$13", "$35", "no monthly subscription"):
        assert dead not in block, f"dead monetization copy leaked into help: {dead}"
    assert "Keep going" in block and "Pivot" in block   # v2 verbs, not just the classic buttons
    # 2026-07-06 QA round 2: money answers lead with the BYOK-vs-subscription fork, and help
    # explicitly disclaims authority on pricing/billing/legal (the pricing page is binding)
    assert "Lead with the FORK" in block
    assert "NOT qualified" in block and "authoritative" in block
    # help is a real SKILL now (app/skills/help), not a hardcoded prompt in main.py
    import skill_registry
    assert skill_registry.exists("help")


def test_commit_rejected_leaves_the_tree_untouched(client):
    """Validate first, mutate last (2026-07-06): a commit aimed at a node it can't build from must
    400 WITHOUT moving the active pointer — it used to jump active onto the bad node first, leaving
    the plan describing two different nodes and every retry re-failing."""
    r = client.post("/api/brainstorm", json={"idea": GRAB_BAG, "email": "vm@x.com"})
    sid = r.json()["id"]
    s = client.get(f"/api/plan/{sid}").json()
    active_before = s["tree"]["active"]                  # the brainstorm fork
    r2 = client.post(f"/api/plan/{sid}/commit", json={"node": active_before, "thesis": ""})
    assert r2.status_code == 400                         # a fork has no thesis — rejected
    s2 = client.get(f"/api/plan/{sid}").json()
    assert s2["tree"]["active"] == active_before         # ...and NOTHING moved
    assert s2["status"] != "researching"                 # no phantom run started
    # an option node CAN carry a commit — same body shape, now it starts
    opt = next(n["id"] for n in s2["tree"]["nodes"] if n["kind"] == "option")
    r3 = client.post(f"/api/plan/{sid}/commit", json={"node": opt})
    assert r3.status_code == 200


def test_commit_resolves_a_drifted_pointer(client):
    """'Build the plan' means the nearest buildable idea, not 'hope the pointer is right': with a
    refined node in the tree but the active pointer stranded on the brainstorm fork (the corruption
    the old mutate-before-validate bug left behind), commit resolves to the refined node and starts
    the build there — no 400 loop."""
    from app import store
    r = client.post("/api/brainstorm", json={"idea": GRAB_BAG, "email": "drift@x.com"})
    sid = r.json()["id"]
    s = client.get(f"/api/plan/{sid}").json()
    fork = s["tree"]["active"]
    opts = [n["id"] for n in s["tree"]["nodes"] if n["kind"] == "option"][:1]
    client.post(f"/api/plan/{sid}/merge", json={"options": opts})
    s = wait_status(client, sid)
    refined = s["tree"]["active"]
    assert s["activeNode"]["kind"] == "refined"
    # simulate the stranded pointer a corrupted session carries
    raw = store.plan_get(sid)
    raw["tree"]["active"] = fork
    store.plan_save(sid, tree=raw["tree"])
    r2 = client.post(f"/api/plan/{sid}/commit", json={})
    assert r2.status_code == 200, r2.json()
    s2 = wait_status(client, sid)
    assert s2["tree"]["active"] != fork          # the pointer healed onto the build's branch
    # the deep build grew out of the REFINED node (it's on the new active's ancestry)
    parents = {n["id"]: n.get("parent") for n in s2["tree"]["nodes"]}
    cur, chain = s2["tree"]["active"], set()
    while cur:
        chain.add(cur); cur = parents.get(cur)
    assert refined in chain


def test_direct_commit_records_its_picks(client):
    """'I'm sold' straight off the brainstorm (skipping the merge) must RECORD the choice: the built
    node carries selected=[picks] so the graph joins through the checked option instead of drawing
    it passed-over (a pivot-commit read as 'nevermind' while the build honored it — Sam's QA)."""
    r = client.post("/api/brainstorm", json={"idea": GRAB_BAG, "email": "pk@x.com"})
    sid = r.json()["id"]
    s = client.get(f"/api/plan/{sid}").json()
    opt = next(n["id"] for n in s["tree"]["nodes"] if n["kind"] == "option")
    r2 = client.post(f"/api/plan/{sid}/commit", json={"options": [opt]})   # thesis derived server-side
    assert r2.status_code == 200
    s2 = wait_status(client, sid)
    built = next(n for n in s2["tree"]["nodes"] if n["id"] == s2["tree"]["active"])
    assert built.get("selected") == [opt], "the pick must ride the built node as a join"
    # the fork's own story shows the option as picked (the ✓ in the history view)
    fork = next(n["id"] for n in s2["tree"]["nodes"] if n["kind"] == "brainstorm")
    story = client.get(f"/api/plan/{sid}/node/{fork}").json()
    assert any(o.get("picked") for o in story.get("options", []))
