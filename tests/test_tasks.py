"""The execution layer — the Roadmap + Codex (app/domain/tasks.py + the /roadmap API).

Covers the contract: extract (plan → roadmap), task CRUD + validation, check-off, drag-reorder,
blocker state + dependency edges, the codex (artifact pointers) + task linking, the ICS feed, the
account-walled task chat, and the derived read model (next action / progress / blocked flags).
"""


def _demo(client):
    """The mock-only demo seeder makes a finished plan with files + decisions + a tree, then
    redirects to its roadmap. Grab the sid from the redirect."""
    r = client.get("/roadmap-demo", follow_redirects=False)
    assert r.status_code == 302, r.text
    return r.headers["location"].split("/plan/")[1].split("/roadmap")[0]


def _extract(client, sid):
    r = client.post(f"/api/plan/{sid}/roadmap/extract", json={})
    assert r.status_code == 200, r.text
    return r.json()


def test_extract_builds_a_provenance_stamped_roadmap(client):
    sid = _demo(client)
    # before extract: empty, not yet extracted
    assert client.get(f"/api/plan/{sid}/roadmap").json()["extracted"] is False
    j = _extract(client, sid)
    rm = j["roadmap"]
    assert rm["tasks"] and rm["milestones"] and rm["goals"]
    assert j["extracted"] and j["next_action"] and j["progress"]["total"] == len(rm["tasks"])
    # every generated task is stamped with the plan node + the standing decisions in force
    assert all(t["source"] == "generated" and t["node"] for t in rm["tasks"])
    assert all(len(t["decisions"]) == 2 for t in rm["tasks"])   # the demo pins 2 decisions
    # re-extract without force is refused (won't clobber edits); force replaces
    assert client.post(f"/api/plan/{sid}/roadmap/extract", json={}).status_code == 409
    assert client.post(f"/api/plan/{sid}/roadmap/extract", json={"force": True}).status_code == 200


def test_task_crud_checkoff_and_reorder(client):
    sid = _demo(client)
    _extract(client, sid)
    # add
    r = client.post(f"/api/plan/{sid}/tasks", json={"text": "Register the LLC"})
    assert r.status_code == 200
    tid = r.json()["added"]["id"]
    assert r.json()["added"]["source"] == "manual"
    # check off → progress moves
    r = client.patch(f"/api/plan/{sid}/tasks/{tid}", json={"status": "done"})
    assert r.json()["progress"]["done"] >= 1
    # reorder: reverse the ids, first id becomes order 0
    ids = [t["id"] for t in client.get(f"/api/plan/{sid}/roadmap").json()["roadmap"]["tasks"]]
    r = client.post(f"/api/plan/{sid}/tasks/reorder", json={"ids": ids[::-1]})
    got = [t["id"] for t in r.json()["roadmap"]["tasks"]]
    assert got[0] == ids[-1]
    # validation + 404s
    assert client.post(f"/api/plan/{sid}/tasks", json={"text": " x "}).status_code == 400
    assert client.patch(f"/api/plan/{sid}/tasks/nope", json={"status": "done"}).status_code == 404
    # delete
    r = client.request("DELETE", f"/api/plan/{sid}/tasks/{tid}")
    assert tid not in [t["id"] for t in r.json()["roadmap"]["tasks"]]


def test_blocker_and_dependency_flags(client):
    sid = _demo(client)
    j = _extract(client, sid)
    ids = [t["id"] for t in j["roadmap"]["tasks"]]
    a, b = ids[0], ids[1]
    # external blocker note → blocked
    client.patch(f"/api/plan/{sid}/tasks/{b}", json={"blocker_note": {"what": "waiting on EIN"}})
    assert client.get(f"/api/plan/{sid}/roadmap").json()["blocked"][b] is True
    # dependency edge: b blocked_by a (a not done) → blocked; a done → unblocked
    client.patch(f"/api/plan/{sid}/tasks/{b}", json={"blocker_note": None, "blocked_by": [a]})
    assert client.get(f"/api/plan/{sid}/roadmap").json()["blocked"][b] is True
    client.patch(f"/api/plan/{sid}/tasks/{a}", json={"status": "done"})
    assert client.get(f"/api/plan/{sid}/roadmap").json()["blocked"][b] is False
    # deleting a drops the dangling dependency edge on b
    client.request("DELETE", f"/api/plan/{sid}/tasks/{a}")
    bt = next(t for t in client.get(f"/api/plan/{sid}/roadmap").json()["roadmap"]["tasks"] if t["id"] == b)
    assert a not in bt["blocked_by"]


def test_codex_pointer_and_task_link(client):
    sid = _demo(client)
    j = _extract(client, sid)
    tid = j["roadmap"]["tasks"][0]["id"]
    # add an artifact and link it to a task; kind is inferred from the URL
    r = client.post(f"/api/plan/{sid}/codex", json={
        "title": "Sales tracker", "url": "https://docs.google.com/spreadsheets/d/abc",
        "note": "week 1: 14 units", "task": tid})
    assert r.status_code == 200
    a = r.json()["added"]
    assert a["kind"] == "sheet" and tid in a["tasks"]
    # the task now references the artifact
    t = next(t for t in r.json()["roadmap"]["tasks"] if t["id"] == tid)
    assert a["id"] in t["artifacts"]
    # edit + delete (delete also unlinks from the task)
    client.patch(f"/api/plan/{sid}/codex/{a['id']}", json={"note": "week 2: 20 units"})
    assert client.get(f"/api/plan/{sid}/roadmap").json()["codex"][0]["note"] == "week 2: 20 units"
    r = client.request("DELETE", f"/api/plan/{sid}/codex/{a['id']}")
    assert r.json()["codex"] == []
    t = next(t for t in r.json()["roadmap"]["tasks"] if t["id"] == tid)
    assert a["id"] not in t["artifacts"]
    assert client.post(f"/api/plan/{sid}/codex", json={}).status_code == 400


def test_ics_feed(client):
    sid = _demo(client)
    _extract(client, sid)
    r = client.get(f"/api/plan/{sid}/roadmap.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VCALENDAR" in r.text and "BEGIN:VEVENT" in r.text


def test_task_chat_is_grounded(client):
    sid = _demo(client)
    j = _extract(client, sid)
    tid = j["roadmap"]["tasks"][0]["id"]
    r = client.post(f"/api/plan/{sid}/tasks/{tid}/chat", json={"message": "what does this mean?"})
    assert r.status_code == 200 and r.json()["reply"]
    assert client.post(f"/api/plan/{sid}/tasks/nope/chat", json={"message": "hi"}).status_code == 404


def test_replan_proposes_never_mutates(client):
    sid = _demo(client)
    j = _extract(client, sid)
    ids = [t["id"] for t in j["roadmap"]["tasks"]]
    # block one task so replan has a 'resolve' suggestion
    client.patch(f"/api/plan/{sid}/tasks/{ids[1]}", json={"blocker_note": {"what": "waiting on X"}})
    r = client.post(f"/api/plan/{sid}/roadmap/replan", json={})
    assert r.status_code == 200
    rp = r.json()
    assert rp["next"] and rp["framing"]
    assert any(s["kind"] == "resolve" for s in rp["suggestions"])
    # replan didn't change stored state (it only proposes)
    before = client.get(f"/api/plan/{sid}/roadmap").json()["progress"]
    client.post(f"/api/plan/{sid}/roadmap/replan", json={})
    assert client.get(f"/api/plan/{sid}/roadmap").json()["progress"] == before


def test_handoff_prompt(client):
    sid = _demo(client)
    j = _extract(client, sid)
    tid = j["roadmap"]["tasks"][0]["id"]
    r = client.get(f"/api/plan/{sid}/roadmap/handoff", params={"task": tid})
    assert r.status_code == 200 and j["roadmap"]["tasks"][0]["text"][:10] in r.json()["prompt"]
    # honors standing decisions from the demo seed
    assert "No cold-call marketing" in r.json()["prompt"]
    whole = client.get(f"/api/plan/{sid}/roadmap/handoff")
    assert "30-day plan" in whole.json()["prompt"]
    assert client.get(f"/api/plan/{sid}/roadmap/handoff", params={"task": "nope"}).status_code == 404


def test_digest_preview_and_optin(client):
    sid = _demo(client)
    _extract(client, sid)
    r = client.get(f"/api/plan/{sid}/digest/preview")
    assert r.status_code == 200 and r.json()["subject"] and "mail_enabled" in r.json()
    # opt in — stored on the roadmap; sending is inert without a key (reported, not errored)
    r = client.post(f"/api/plan/{sid}/digest", json={"cadence": True, "send_now": True})
    assert r.json()["cadence"] is True
    assert r.json()["send"]["sent"] is False   # no RESEND_API_KEY in test env
    assert client.get(f"/api/plan/{sid}/digest/preview").json()["cadence"] is True


def test_digest_due_logic():
    from app.main import _digest_due
    from datetime import datetime, timedelta, timezone
    sun = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)   # a Sunday, 18:00 UTC
    mon = datetime(2026, 7, 13, 18, 0, tzinfo=timezone.utc)   # a Monday
    assert _digest_due(sun, None) is True                     # first-time, in the Sunday window
    assert _digest_due(mon, None) is False                    # not the window, no catch-up yet
    recent = (sun - timedelta(days=2)).isoformat()
    assert _digest_due(sun, recent) is False                  # <6 days since last → never
    stale = (mon - timedelta(days=9)).isoformat()
    assert _digest_due(mon, stale) is True                    # >8 days → catch-up even off-window


def test_plans_with_cadence_query(client):
    from app import store
    sid = _demo(client)
    _extract(client, sid)
    assert sid not in store.plans_with_cadence()              # not opted in yet
    client.post(f"/api/plan/{sid}/digest", json={"cadence": True})
    # the demo session is ownerless (anon) → still excluded (no one to mail)
    assert sid not in store.plans_with_cadence()
    store.plan_claim(sid, "founder@example.com")              # give it an owner
    assert sid in store.plans_with_cadence()


def test_roadmap_404_on_unknown_session(client):
    assert client.get("/api/plan/nope/roadmap").status_code == 404
