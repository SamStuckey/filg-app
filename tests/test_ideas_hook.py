"""The front-door idea hook (front_door_strategy.md T4/T5/T9): the browsable catalog, the roll,
the honest trending signal, and the funnel counters. Free side of the wall — no AI, no auth."""

from app import store
from app.domain import ideas


def test_catalog_contract():
    """Every idea is complete, and every economics figure carries a label from the declared
    vocabulary — invariant #1 (label, never launder) applied to the hook surface itself."""
    meta = ideas.catalog_meta()
    labels = set(meta["labels"])
    assert labels, "the catalog must declare its label vocabulary"
    all_ideas = ideas.all_ideas()
    assert len(all_ideas) >= 15
    for i in all_ideas:
        for field in ("id", "title", "one_liner", "startup_cost", "tier",
                      "confidence", "episodes", "economics", "method"):
            assert i.get(field) not in (None, "", []), f"{i.get('id')} missing {field}"
        for e in i["economics"]:
            assert e.get("claim") and e.get("label"), f"{i['id']} has an unlabeled claim"
            assert e["label"] in labels, f"{i['id']} uses undeclared label {e['label']!r}"


def test_trending_is_the_labeled_signal():
    """Trending ranks by the programmatic signal (episode counts), never an invented score."""
    top = ideas.trending(5)
    assert len(top) == 5
    counts = [i["episodes"] for i in top]
    assert counts == sorted(counts, reverse=True)


def test_roll_and_exclude():
    seen = {ideas.random_idea()["id"] for _ in range(30)}
    assert len(seen) > 3, "the roll should spread across the catalog"
    for _ in range(10):
        assert ideas.random_idea(exclude="no-code-micro-saas")["id"] != "no-code-micro-saas"


def test_ideas_endpoints_and_funnel_counters(client):
    r = client.get("/api/ideas/trending")
    assert r.status_code == 200
    body = r.json()
    assert len(body["ideas"]) == 5
    assert all(i.get("seed") for i in body["ideas"]), "cards need their Build-this seed prompt"
    assert body["meta"]["source"]

    r = client.get("/api/ideas/roll")
    assert r.status_code == 200
    first = r.json()
    assert first["idea"]["id"] and first["seed"]
    r = client.get(f"/api/ideas/roll?exclude={first['idea']['id']}")
    assert r.json()["idea"]["id"] != first["idea"]["id"]

    counts = {c["event"]: c["n"] for c in store.funnel_counts(days=1)}
    assert counts.get("browse_trending", 0) >= 1
    assert counts.get("roll", 0) >= 2


def test_funnel_track_never_raises(monkeypatch):
    """Instrumentation is fire-and-forget: a broken DB must not take a route down."""
    monkeypatch.setattr(store, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    store.funnel_track("roll")   # must swallow
