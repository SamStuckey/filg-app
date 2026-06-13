#!/usr/bin/env python3
"""
Usage metering + cost guardrails for the FILG free tier.

The free tier costs ~$0.40–1.00 of compute per run, so it must be metered BEFORE launch or a spike
becomes a surprise bill. This is the smallest real version:
  - per-user free-run cap (default 1)
  - a global daily-spend kill switch (default $20/day) — hard stop for everyone, paid included
JSON-backed and stdlib-only so it runs anywhere. Production: move to Postgres + Stripe entitlement.

Env overrides:
  FILG_FREE_RUNS     free runs per user            (default 1)
  FILG_DAILY_BUDGET  global $/day kill switch       (default 20)
  FILG_USAGE_DB      path to the json store
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date

_HERE = os.path.dirname(__file__)
STORE = os.environ.get("FILG_USAGE_DB", os.path.join(_HERE, "..", "app", "usage.json"))
FREE_RUNS = int(os.environ.get("FILG_FREE_RUNS", "1"))
DAILY_BUDGET = float(os.environ.get("FILG_DAILY_BUDGET", "20"))

_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(STORE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"users": {}, "daily": {}}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, STORE)


def can_run(user_id: str, is_paid: bool = False) -> tuple[bool, str]:
    """Gate a run. Returns (allowed, reason). Checks the global kill switch first, then the
    per-user free cap (paid users skip the free cap but still hit the kill switch)."""
    with _lock:
        d = _load()
        today = date.today().isoformat()
        if d.get("daily", {}).get(today, 0.0) >= DAILY_BUDGET:
            return False, f"daily budget (${DAILY_BUDGET:.0f}) reached — kill switch tripped"
        if is_paid:
            return True, "ok (paid)"
        used = d.get("users", {}).get(user_id, {}).get("runs", 0)
        if used >= FREE_RUNS:
            return False, f"free limit reached ({FREE_RUNS} run) — upgrade to keep going"
        return True, "ok"


def record_run(user_id: str, cost: float) -> None:
    with _lock:
        d = _load()
        today = date.today().isoformat()
        d.setdefault("daily", {})[today] = round(d.get("daily", {}).get(today, 0.0) + cost, 4)
        u = d.setdefault("users", {}).setdefault(user_id, {"runs": 0, "spend": 0.0})
        u["runs"] += 1
        u["spend"] = round(u["spend"] + cost, 4)
        _save(d)


def snapshot() -> dict:
    d = _load()
    today = date.today().isoformat()
    return {"today_spend": d.get("daily", {}).get(today, 0.0),
            "daily_budget": DAILY_BUDGET, "free_runs": FREE_RUNS,
            "users": len(d.get("users", {}))}


if __name__ == "__main__":  # quick self-test (no API)
    import tempfile
    STORE = tempfile.mktemp(suffix=".json")
    assert can_run("a@x.com") == (True, "ok")
    record_run("a@x.com", 0.45)
    ok, why = can_run("a@x.com")
    assert not ok, "free cap should block 2nd run"
    assert can_run("a@x.com", is_paid=True)[0], "paid should pass"
    print("usage.py self-test OK:", snapshot())
