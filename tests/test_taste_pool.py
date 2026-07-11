"""The taste-pool split (front_door_strategy.md T2): the anonymous free taste gates on ITS OWN
daily bucket, so subscriber runs and dev QA on FILG's key can no longer wall every lander for
the rest of the day. The global daily budget stays the hard backstop over everything."""

from engine import usage

from app import access, ops, store

IDEA = "a mobile detailing service for busy parents in Denver"


def test_taste_bucket_is_separate_from_global(monkeypatch, tmp_path):
    """Subscriber/global spend fills the global bucket only; the taste gate stays open until
    TASTE spend hits FILG_TASTE_BUDGET. Runs on its own DB — the suite's shared one already
    carries spend from other tests."""
    monkeypatch.setattr(usage, "DB", str(tmp_path / "usage.db"))
    monkeypatch.setattr(usage, "_initialized", False)
    monkeypatch.setattr(usage, "TASTE_BUDGET", 1.0)
    monkeypatch.setattr(usage, "DAILY_BUDGET", 100.0)
    # a heavy internal/subscriber day: $50 of non-taste spend on FILG's key
    usage.record_monthly("sub@x.com", "p1", 25.0)
    usage.record_spend(25.0)                       # e.g. dev QA section drafts
    assert not usage.taste_pool_tapped(), "non-taste spend must not wall landers"
    # real taste spend crosses its own ceiling
    usage.record_spend(0.6, taste=True)
    assert not usage.taste_pool_tapped()
    usage.record_run("anon-taster@x.com", 0.5)     # a taste run feeds the taste bucket too
    assert usage.taste_pool_tapped(), "taste spend past FILG_TASTE_BUDGET must trip the taste gate"


def test_global_budget_stays_the_hard_backstop(monkeypatch):
    monkeypatch.setattr(usage, "TASTE_BUDGET", 1000.0)   # taste bucket wide open
    monkeypatch.setattr(usage, "kill_switch_tripped", lambda: True)
    assert usage.taste_pool_tapped(), "a tripped global kill switch must still gate the taste"


def test_snapshot_reports_both_buckets(monkeypatch):
    snap = usage.snapshot()
    assert "taste_spend" in snap and "taste_budget" in snap
    assert snap["taste_spend"] <= snap["today_spend"]


def test_tapped_pool_402_carries_the_pool_flag(client, monkeypatch):
    """The frontend needs to tell a tapped pool apart from other needKey errors, so an anonymous
    lander gets honest pool copy instead of an account demand (the anti-pattern)."""
    d = client.post("/api/brainstorm", json={"idea": IDEA}).json()
    sid, opts = d["id"], d["activeNode"]["options"]
    monkeypatch.setattr(ops, "MOCK", False)
    monkeypatch.setattr(access, "_provider_for",
                        lambda u: type("P", (), {"bills_filg": True})())
    monkeypatch.setattr(usage, "taste_pool_tapped", lambda: True)
    r = client.post(f"/api/plan/{sid}/merge", json={"options": [opts[0]["id"]]})
    assert r.status_code == 402
    body = r.json()
    assert body.get("needKey") is True and body.get("pool") is True
    assert store.plan_get(sid)["status"] != "researching"
