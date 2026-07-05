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
