"""The gating wave (§v2 #14): the account wall, plan claiming, and orphan cleanup.

The contract: the free taste — initial prompt → direction spread → direction select → the merged
first idea (with its first-pass research) — runs ANONYMOUS, on the house. The next click (commit =
the deep build) and everything past it requires an account; a signed-in user claims their anonymous
taste plan; an unclaimed plan is purged after ~48h.
"""

from datetime import datetime, timedelta, timezone

from conftest import wait_status

from app import auth as app_auth
from app import access, main, ops, store

IDEA = "I like basketball, Magic the Gathering, and food, and I'm good at sales"


def _auth_on(monkeypatch, user=None):
    """Flip the auth regime on and set who the request verifies as (None = signed out)."""
    monkeypatch.setattr(app_auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(app_auth, "user_from_request",
                        lambda req: ({"id": user, "email": user} if user else None))


def _taste(client):
    """Run the anonymous free taste up to the wall: brainstorm → pick a direction → merge."""
    d = client.post("/api/brainstorm", json={"idea": IDEA}).json()
    sid = d["id"]
    opts = d["activeNode"]["options"]
    assert opts, "the spread should offer directions"
    r = client.post(f"/api/plan/{sid}/merge", json={"options": [opts[0]["id"]]})
    assert r.status_code == 200
    s = wait_status(client, sid)
    assert s["stage"] == "refined"
    return sid, s


def test_free_taste_runs_anonymous_even_with_auth_on(client, monkeypatch):
    _auth_on(monkeypatch, user=None)                 # signed OUT
    sid, s = _taste(client)                          # brainstorm + merge never hit the wall
    assert s["activeNode"]["thesis"]
    assert (store.plan_get(sid).get("user") or "") == ""   # ownerless — the anonymous taste


def test_commit_is_the_wall_for_anonymous(client, monkeypatch):
    _auth_on(monkeypatch, user=None)
    sid, _ = _taste(client)
    r = client.post(f"/api/plan/{sid}/commit", json={})
    assert r.status_code == 401 and r.json().get("needAccount") is True
    # the other engine verbs are walled too
    r = client.post(f"/api/plan/{sid}/chat", json={"message": "so what next?"})
    assert r.status_code == 401 and r.json().get("needAccount") is True
    r = client.post(f"/api/plan/{sid}/lookup", json={"message": "market size?"})
    assert r.status_code == 401 and r.json().get("needAccount") is True
    # …while reads and pure navigation stay open (the taste is still browsable)
    assert client.get(f"/api/plan/{sid}").status_code == 200


def test_signin_claims_the_anonymous_plan_and_unwalls(client, monkeypatch):
    _auth_on(monkeypatch, user=None)
    sid, _ = _taste(client)
    # they sign up at the wall → the explicit claim attaches the plan to the new account
    _auth_on(monkeypatch, user="newbie@x.com")
    r = client.post(f"/api/plan/{sid}/claim", json={})
    assert r.status_code == 200
    assert store.plan_get(sid)["user"] == "newbie@x.com"
    # keys are off in tests (no BYOK regime) → no key wall either; the commit now proceeds
    r = client.post(f"/api/plan/{sid}/commit", json={})
    assert r.status_code == 200
    wait_status(client, sid)


def test_claim_never_reassigns_an_owned_plan(client, monkeypatch):
    _auth_on(monkeypatch, user=None)
    sid, _ = _taste(client)
    store.plan_claim(sid, "first@x.com")
    _auth_on(monkeypatch, user="second@x.com")
    assert client.post(f"/api/plan/{sid}/claim", json={}).status_code == 404   # not yours, not leaked
    assert store.plan_get(sid)["user"] == "first@x.com"
    # idempotent for the actual owner
    _auth_on(monkeypatch, user="first@x.com")
    assert client.post(f"/api/plan/{sid}/claim", json={}).status_code == 200


def test_wall_autoclaims_for_a_signed_in_user(client, monkeypatch):
    """A signed-in user who tastes anonymously-created state doesn't need the explicit claim — the
    wall claims it in passing on their first gated verb."""
    _auth_on(monkeypatch, user=None)
    sid, _ = _taste(client)
    _auth_on(monkeypatch, user="walker@x.com")
    r = client.post(f"/api/plan/{sid}/commit", json={})
    assert r.status_code == 200
    assert store.plan_get(sid)["user"] == "walker@x.com"
    wait_status(client, sid)


def test_orphan_purge_scraps_only_old_accountless_plans():
    store.init()
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    con = store._connect()
    with con:
        for pid, user, created in (("orph-old", "", old), ("orph-new", "", None),
                                   ("owned-old", "own@x.com", old)):
            con.execute("INSERT OR REPLACE INTO plan_sessions (id, user, idea, status, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (pid, user, "x", "building",
                         created or datetime.now(timezone.utc).isoformat()))
    con.close()
    n = store.purge_orphan_plans(48)
    assert n >= 1
    assert store.plan_get("orph-old") is None          # old + ownerless → gone
    assert store.plan_get("orph-new") is not None      # fresh taste → kept
    assert store.plan_get("owned-old") is not None     # owned → never touched


def test_gibberish_pivot_gets_the_roast(client):
    d = client.post("/api/brainstorm", json={"idea": IDEA}).json()
    sid = d["id"]
    r = client.post(f"/api/plan/{sid}/rebrainstorm",
                    json={"feedback": "asldkfjasldkfjaslkdfjlkasjdflkjasdf"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("gibberish") is True and body.get("title") and body.get("body")
    # the tree is untouched — a roast is not a spread
    assert store.plan_get(sid)["tree"]["active"] == d["tree"]["active"]


def test_legacy_start_walled_when_auth_on(client, monkeypatch):
    """/api/plan/start (the retired v1 taste) jumps straight to the deep research run — with auth on
    it must demand an account, then a key/subscription, never an open anonymous spend."""
    _auth_on(monkeypatch, user=None)
    r = client.post("/api/plan/start", json={"idea": IDEA, "email": "rando@x.com"})
    assert r.status_code == 401 and r.json().get("needAccount") is True
    # signed in but keyless + tierless in the paid regime → the key/subscribe fork
    from app import access, keys as keys_mod
    monkeypatch.setattr(keys_mod, "enabled", lambda: True)
    monkeypatch.setattr(access, "_is_byok", lambda u: False)
    _auth_on(monkeypatch, user="starter@x.com")
    r = client.post("/api/plan/start", json={"idea": IDEA})
    assert r.status_code == 402 and r.json().get("needKey") is True


def test_merge_reads_the_kill_switch(client, monkeypatch):
    """The taste's one web-touching step (merge) runs in a background thread — the daily kill switch
    must be read at the route, before the spawn (metered-but-uncapped = an invariant-#3 breach)."""
    from engine import usage
    d = client.post("/api/brainstorm", json={"idea": IDEA}).json()
    sid, opts = d["id"], d["activeNode"]["options"]
    monkeypatch.setattr(ops, "MOCK", False)                       # the gate skips mock runs
    monkeypatch.setattr(access, "_provider_for",
                        lambda u: type("P", (), {"bills_filg": True})())
    monkeypatch.setattr(usage, "kill_switch_tripped", lambda: True)
    r = client.post(f"/api/plan/{sid}/merge", json={"options": [opts[0]["id"]]})
    assert r.status_code == 402 and r.json().get("needKey") is True
    assert store.plan_get(sid)["status"] != "researching"          # nothing spawned


def test_legacy_run_walled_when_auth_on(client, monkeypatch):
    _auth_on(monkeypatch, user=None)
    r = client.post("/api/run", json={"idea": IDEA, "email": "rando@x.com"})
    assert r.status_code == 401 and r.json().get("needAccount") is True
