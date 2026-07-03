"""The diverge/converge funnel routes: /api/brainstorm -> /merge -> /commit, plus the /route spine
and lazy /node content. Mock mode (conftest sets FILG_MOCK=1), driven through the real TestClient."""

from conftest import wait_status

RAW = "I'm a baker and think I'm really good. I live in the middle of nowhere, how do I sell what I make?"


def _brainstorm(client, idea=RAW):
    r = client.post("/api/brainstorm", json={"idea": idea})
    assert r.status_code == 200, r.text
    return r.json()


# ── brainstorm: anonymous, no email, seeds the tree ──────────────────────────
def test_brainstorm_is_anonymous_and_seeds_option_nodes():
    from fastapi.testclient import TestClient
    from app import main
    c = TestClient(main.app)
    s = _brainstorm(c)
    assert s["stage"] == "brainstorm"
    # a brainstorm fork node + one option child per direction
    kinds = sorted(n["kind"] for n in s["tree"]["nodes"])
    assert "brainstorm" in kinds and kinds.count("option") >= 1
    an = s["activeNode"]
    assert an["kind"] == "brainstorm" and an["options"]
    assert an["options"][0]["direction"]["title"]     # each option carries a direction card


def test_brainstorm_rejects_too_short(client):
    r = client.post("/api/brainstorm", json={"idea": "hi"})
    assert r.status_code == 400


def test_brainstorm_roasts_gibberish(client):
    r = client.post("/api/brainstorm", json={"idea": "asdfjkl qwerty zxcvbn hjkl"})
    assert r.status_code == 200 and r.json().get("gibberish")


# ── merge: converge chosen options into one refined idea ─────────────────────
def test_merge_produces_a_refined_node_with_the_cull(client):
    s = _brainstorm(client)
    sid = s["id"]
    opt_ids = [o["id"] for o in s["activeNode"]["options"]]
    r = client.post(f"/api/plan/{sid}/merge", json={"options": opt_ids})
    assert r.status_code == 200
    s = wait_status(client, sid)
    assert s["stage"] == "refined"
    an = s["activeNode"]
    assert an["kind"] == "refined" and an["thesis"]
    assert isinstance(an["kept"], list) and isinstance(an["dropped"], list)
    assert an["research"]["prose"]["title"]           # light first-pass skim rode along


def test_merge_needs_a_selection(client):
    s = _brainstorm(client)
    r = client.post(f"/api/plan/{s['id']}/merge", json={"options": []})
    assert r.status_code == 400


# ── commit: the deep run, attached under the refined node ────────────────────
def test_commit_runs_deep_build_and_starts_the_plan(client):
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/merge", json={"options": [o["id"] for o in s["activeNode"]["options"]]})
    s = wait_status(client, sid)
    r = client.post(f"/api/plan/{sid}/commit", json={})
    assert r.status_code == 200
    s = wait_status(client, sid)
    assert s["stage"] == "building" and s["status"] == "building"
    assert s["proposal"] and s["vetting"]             # deep research + vet + first draft landed
    # a section node now hangs under the refined node — the funnel history is preserved in the tree
    kinds = sorted(n["kind"] for n in s["tree"]["nodes"])
    assert "brainstorm" in kinds and "refined" in kinds and "section" in kinds


def test_commit_from_body_thesis(client):
    s = _brainstorm(client)
    sid = s["id"]
    r = client.post(f"/api/plan/{sid}/commit", json={"thesis": "Sell homemade care boxes to people far from home"})
    assert r.status_code == 200
    s = wait_status(client, sid)
    assert s["proposal"] and s["stage"] == "building"


# ── the /route spine ─────────────────────────────────────────────────────────
def test_route_classifies_commit_intent(client):
    s = _brainstorm(client)
    r = client.post(f"/api/plan/{s['id']}/route", json={"prompt": "I'm sold, build the plan", "mode": "build"})
    assert r.status_code == 200
    d = r.json()["decision"]
    assert d["intent"] == "commit" and d["confirm"] is True


def test_route_plan_steer_hard_clash_returns_a_fork(client):
    # build all the way to the plan stage, then send a contradicting steer
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/commit", json={"thesis": "A done-for-you SaaS onboarding service"})
    s = wait_status(client, sid)
    assert s["stage"] == "building"
    r = client.post(f"/api/plan/{sid}/route",
                    json={"prompt": "actually sell physical hardware instead", "mode": "build"})
    body = r.json()
    assert body["decision"]["intent"] == "steer"
    assert body.get("fork") and body["fork"]["clash"] and body["fork"]["skeptic_say"]


def test_route_plan_steer_integrable_no_fork(client):
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/commit", json={"thesis": "A done-for-you SaaS onboarding service"})
    wait_status(client, sid)
    r = client.post(f"/api/plan/{sid}/route", json={"prompt": "make the pricing simpler", "mode": "build"})
    assert "fork" not in r.json()                      # a tweak integrates, no pivot fork


# ── lazy node content for the graph zoom ─────────────────────────────────────
def test_node_content_returns_the_option_card(client):
    s = _brainstorm(client)
    opt = s["activeNode"]["options"][0]["id"]
    r = client.get(f"/api/plan/{s['id']}/node/{opt}")
    assert r.status_code == 200 and r.json()["kind"] == "option"
    assert r.json()["direction"]["title"]


def test_node_content_unknown_node_404(client):
    s = _brainstorm(client)
    r = client.get(f"/api/plan/{s['id']}/node/nope")
    assert r.status_code == 404


# ── the v2 two-panel surface shell + assets serve ────────────────────────────
def test_v2_shell_and_assets_serve(client):
    page = client.get("/v2")
    assert page.status_code == 200 and "window.FILG=" in page.text   # shares the config head
    assert '/static/v2.js' in page.text and '/static/v2.css' in page.text
    assert client.get("/static/v2.js").status_code == 200
    assert client.get("/static/v2.css").status_code == 200
    # the live shell (/) is untouched by the v2 addition
    assert client.get("/").status_code == 200
