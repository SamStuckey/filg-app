"""Store — plan session persistence incl. the new intake/vet/board columns + migration."""

from app import store


def test_plan_lifecycle_round_trips_new_fields():
    store.plan_create("t_pl1", "u@x.com", "an idea", directors=["closer", "cfo"])
    s = store.plan_get("t_pl1")
    assert s["status"] == "researching" and s["directors"] == ["closer", "cfo"]
    store.plan_save("t_pl1", status="building", step=1,
                    files={"1-the-setup.md": "# Brief"},
                    shaped={"thesis": "focused"}, vetting={"verdict": "pursue"},
                    board=[{"section": "1-the-setup.md", "verdict": "ship it"}])
    s = store.plan_get("t_pl1")
    assert s["step"] == 1 and s["files"]["1-the-setup.md"] == "# Brief"
    assert s["shaped"]["thesis"] == "focused" and s["vetting"]["verdict"] == "pursue"
    assert s["board"][0]["section"] == "1-the-setup.md"


def test_new_columns_exist_after_migration():
    store.init()
    import sqlite3
    con = sqlite3.connect(store.DB)
    cols = {r[1] for r in con.execute("PRAGMA table_info(plan_sessions)")}
    con.close()
    assert {"shaped", "vetting", "directors", "board"} <= cols


def test_plan_list_scopes_to_user():
    store.plan_create("t_pl2", "owner@x.com", "idea two")
    mine = store.plan_list("owner@x.com")
    assert any(p["id"] == "t_pl2" for p in mine)
    assert store.plan_list("nobody@x.com") == []


def test_unknown_session_is_none():
    assert store.plan_get("does-not-exist") is None
