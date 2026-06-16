"""Per-run cost ledger + per-user concurrency cap (drawn from the account's plan).

The ledger must be isolated per run so concurrent operations don't mis-bill each other, and a user
may run up to their plan's max_concurrent ops at once (429 past that)."""

import types
from concurrent.futures import ThreadPoolExecutor

import pytest

import pipeline
import plans
from app import main, store


def _usage(tin=10, tout=5):
    return types.SimpleNamespace(input_tokens=tin, output_tokens=tout, server_tool_use=None)


# ── per-run ledger isolation ──────────────────────────────────────────────────
def test_run_ledger_isolates_rows():
    with pipeline.run_ledger():
        pipeline.LEDGER.add("a", pipeline.HAIKU, _usage())
        assert len(pipeline.LEDGER.rows) == 1
    with pipeline.run_ledger():
        assert len(pipeline.LEDGER.rows) == 0   # a fresh run starts clean


def test_bound_carries_ledger_into_worker_threads():
    with pipeline.run_ledger() as led:
        def work(_):
            pipeline.LEDGER.add("x", pipeline.HAIKU, _usage())
            return True
        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(pipeline.bound(work), [1, 2]))
        assert len(led.rows) == 2   # both fan-out workers wrote to THIS run's ledger


# ── per-user concurrency cap ──────────────────────────────────────────────────
def test_run_slot_caps_at_plan_limit():
    user = "conc@x.com"                     # no explicit plan → default plan (byok) → cap 3
    held = [main._run_slot(user) for _ in range(3)]
    for cm in held:
        cm.__enter__()
    with pytest.raises(main.BusyError):
        with main._run_slot(user):
            pass
    for cm in held:
        cm.__exit__(None, None, None)
    with main._run_slot(user):             # slots freed → works again
        pass


def test_plan_override_changes_cap():
    user = "capped@x.com"
    store.set_account_plan(user, "free")   # free → cap 1
    assert main._concurrency_cap(user) == 1
    cm = main._run_slot(user)
    cm.__enter__()
    try:
        with pytest.raises(main.BusyError):
            with main._run_slot(user):
                pass
    finally:
        cm.__exit__(None, None, None)
        store.set_account_plan(user, None)
    assert main._concurrency_cap(user) == plans.max_concurrent(plans.DEFAULT_PLAN)  # back to default


def test_route_returns_429_when_busy(client, monkeypatch):
    monkeypatch.setattr(main, "_concurrency_cap", lambda u: 0)   # force "always busy"
    sid = client.post("/api/plan/start",
                      json={"idea": "a real idea about mobile dog grooming vans",
                            "email": "z@x.com"}).json()["id"]
    r = client.post("/api/plan/" + sid + "/next", json={})
    assert r.status_code == 429 and r.json().get("busy") is True
