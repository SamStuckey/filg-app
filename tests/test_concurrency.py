"""Per-run cost ledger + a flat per-user concurrency cap.

The ledger must be isolated per run so concurrent operations don't mis-bill each other, and a user
may run up to a flat CONCURRENCY_CAP ops at once (429 past that). There are no monetization tiers —
the cap is a single constant, not drawn from a plan."""

import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from engine import pipeline
from app import main


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
def test_run_slot_caps_at_flat_limit():
    user = "conc@x.com"                     # flat cap = main.CONCURRENCY_CAP (3)
    cap = main.CONCURRENCY_CAP
    assert main._concurrency_cap(user) == cap
    held = [main._run_slot(user) for _ in range(cap)]
    for cm in held:
        cm.__enter__()
    with pytest.raises(main.BusyError):
        with main._run_slot(user):
            pass
    for cm in reversed(held):   # exit LIFO — context managers (the bound provider/ledger) must unwind in reverse
        cm.__exit__(None, None, None)
    with main._run_slot(user):             # slots freed → works again
        pass


def test_route_returns_429_when_busy(client, monkeypatch):
    monkeypatch.setattr(main, "_concurrency_cap", lambda u: 0)   # force "always busy"
    sid = client.post("/api/plan/start",
                      json={"idea": "a real idea about mobile dog grooming vans",
                            "email": "z@x.com"}).json()["id"]
    r = client.post("/api/plan/" + sid + "/next", json={})
    assert r.status_code == 429 and r.json().get("busy") is True
