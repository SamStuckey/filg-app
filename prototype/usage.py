#!/usr/bin/env python3
"""
Usage metering + cost guardrails for the FILG free tier.

The free tier costs ~$0.40–1.00 of compute per run, so it must be metered BEFORE launch or a spike
becomes a surprise bill. This is the smallest real version:
  - per-user free-run cap (default 1)
  - a global daily-spend kill switch (default $20/day) — hard stop for everyone, paid included

SQLite-backed (stdlib `sqlite3`, runs anywhere) so caps survive process restarts — the earlier
JSON store on Render's ephemeral disk reset on every redeploy, which let a user re-roll free runs.
Shares one DB file with the app's job store (`FILG_DB`, default `app/filg.db`); put it on a
persistent disk in production. Production proper: Postgres + Stripe entitlement.

Env overrides:
  FILG_FREE_RUNS     free plans (runs) per user    (default 3; set 0 to DISABLE the per-user cap —
                     dev only; the daily kill switch still applies)
  FILG_DAILY_BUDGET  global $/day kill switch       (default 20)
  FILG_DB            path to the shared sqlite db   (FILG_USAGE_DB still honored as a fallback)
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
DB = (os.environ.get("FILG_DB") or os.environ.get("FILG_USAGE_DB")
      or os.path.join(_HERE, "..", "app", "filg.db"))
FREE_RUNS = int(os.environ.get("FILG_FREE_RUNS", "3"))
DAILY_BUDGET = float(os.environ.get("FILG_DAILY_BUDGET", "20"))

_lock = threading.Lock()        # serialize the read-modify-write so the cap stays exact under load
_initialized = False


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _init() -> None:
    global _initialized
    if _initialized:
        return
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    con = _connect()
    try:
        with con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE IF NOT EXISTS usage_users ("
                        "  user_id TEXT PRIMARY KEY,"
                        "  runs INTEGER NOT NULL DEFAULT 0,"
                        "  spend REAL NOT NULL DEFAULT 0)")
            con.execute("CREATE TABLE IF NOT EXISTS usage_daily ("
                        "  day TEXT PRIMARY KEY,"
                        "  spend REAL NOT NULL DEFAULT 0)")
            # Per-subscriber monthly usage: the fair-use meter for paid tiers that run on FILG's key.
            # `period` is the billing-window key (the subscription's current_period_end, or a calendar
            # month as a fallback). A renewal advances the period → a fresh row → the cap resets.
            con.execute("CREATE TABLE IF NOT EXISTS usage_monthly ("
                        "  email TEXT NOT NULL,"
                        "  period TEXT NOT NULL,"
                        "  spend REAL NOT NULL DEFAULT 0,"
                        "  tokens INTEGER NOT NULL DEFAULT 0,"
                        "  PRIMARY KEY (email, period))")
    finally:
        con.close()
    _initialized = True


def can_run(user_id: str, is_paid: bool = False) -> tuple[bool, str]:
    """Gate a run. Returns (allowed, reason). Checks the global kill switch first, then the
    per-user free cap (paid users skip the free cap but still hit the kill switch)."""
    _init()
    with _lock:
        con = _connect()
        try:
            today = date.today().isoformat()
            drow = con.execute("SELECT spend FROM usage_daily WHERE day=?", (today,)).fetchone()
            if (drow["spend"] if drow else 0.0) >= DAILY_BUDGET:
                return False, f"daily budget (${DAILY_BUDGET:.0f}) reached — kill switch tripped"
            if is_paid or FREE_RUNS <= 0:   # FREE_RUNS<=0 → per-user cap disabled (dev); kill switch still applies
                return True, "ok (paid)" if is_paid else "ok (uncapped)"
            urow = con.execute("SELECT runs FROM usage_users WHERE user_id=?", (user_id,)).fetchone()
            if (urow["runs"] if urow else 0) >= FREE_RUNS:
                plural = "s" if FREE_RUNS != 1 else ""
                return False, f"free limit reached ({FREE_RUNS} plan{plural}) — upgrade to keep going"
            return True, "ok"
        finally:
            con.close()


def record_run(user_id: str, cost: float) -> None:
    _init()
    cost = round(cost, 4)
    with _lock:
        con = _connect()
        try:
            with con:
                today = date.today().isoformat()
                con.execute(
                    "INSERT INTO usage_daily (day, spend) VALUES (?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET spend = round(spend + ?, 4)",
                    (today, cost, cost))
                con.execute(
                    "INSERT INTO usage_users (user_id, runs, spend) VALUES (?, 1, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET runs = runs + 1, spend = round(spend + ?, 4)",
                    (user_id, cost, cost))
        finally:
            con.close()


def free_used(user_id: str) -> bool:
    """True iff this user has already taken their one free 'welcome' run (BYOK model: 1 free, then
    bring your own key). Based on the per-user run counter that record_run bumps."""
    if not user_id:
        return False
    _init()
    con = _connect()
    try:
        row = con.execute("SELECT runs FROM usage_users WHERE user_id=?", (user_id,)).fetchone()
    finally:
        con.close()
    return bool(row and row["runs"] >= 1)


def kill_switch_tripped() -> bool:
    """True iff today's global spend has hit the daily budget — the load-bearing cost guard that
    still applies to FILG-key (free welcome) runs even in the BYOK model."""
    _init()
    con = _connect()
    try:
        today = date.today().isoformat()
        row = con.execute("SELECT spend FROM usage_daily WHERE day=?", (today,)).fetchone()
    finally:
        con.close()
    return (row["spend"] if row else 0.0) >= DAILY_BUDGET


def record_spend(cost: float) -> None:
    """Bump only the global daily total (the kill switch) — for spend that isn't a new user run,
    e.g. per-section drafts and add-on calls. Does NOT touch the per-user free-run counter."""
    if not cost:
        return
    _init()
    cost = round(cost, 4)
    with _lock:
        con = _connect()
        try:
            with con:
                today = date.today().isoformat()
                con.execute(
                    "INSERT INTO usage_daily (day, spend) VALUES (?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET spend = round(spend + ?, 4)",
                    (today, cost, cost))
        finally:
            con.close()


# ── Per-subscriber monthly fair-use meter (paid tiers on FILG's key) ──────────
def monthly_usage(email: str, period: str) -> dict:
    """This account's spend ($) + tokens accumulated in the given billing period (zeros if none)."""
    if not email or not period:
        return {"spend": 0.0, "tokens": 0}
    _init()
    con = _connect()
    try:
        row = con.execute("SELECT spend, tokens FROM usage_monthly WHERE email=? AND period=?",
                          (email.strip().lower(), period)).fetchone()
    finally:
        con.close()
    return {"spend": row["spend"] if row else 0.0, "tokens": row["tokens"] if row else 0}


def record_monthly(email: str, period: str, cost: float, tokens: int = 0) -> None:
    """Fold one run's cost + tokens into a subscriber's monthly fair-use total (also bumps the global
    daily kill switch, since the spend is real money on FILG's key)."""
    if not email or not period:
        return
    _init()
    cost = round(cost or 0.0, 4)
    tokens = int(tokens or 0)
    email = email.strip().lower()
    with _lock:
        con = _connect()
        try:
            with con:
                today = date.today().isoformat()
                con.execute(
                    "INSERT INTO usage_daily (day, spend) VALUES (?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET spend = round(spend + ?, 4)",
                    (today, cost, cost))
                con.execute(
                    "INSERT INTO usage_monthly (email, period, spend, tokens) VALUES (?,?,?,?) "
                    "ON CONFLICT(email, period) DO UPDATE SET "
                    "  spend = round(spend + ?, 4), tokens = tokens + ?",
                    (email, period, cost, tokens, cost, tokens))
        finally:
            con.close()


def snapshot() -> dict:
    _init()
    con = _connect()
    try:
        today = date.today().isoformat()
        drow = con.execute("SELECT spend FROM usage_daily WHERE day=?", (today,)).fetchone()
        users = con.execute("SELECT COUNT(*) AS c FROM usage_users").fetchone()["c"]
    finally:
        con.close()
    return {"today_spend": drow["spend"] if drow else 0.0,
            "daily_budget": DAILY_BUDGET, "free_runs": FREE_RUNS, "users": users}


if __name__ == "__main__":  # quick self-test (no API)
    import tempfile
    DB = tempfile.mktemp(suffix=".db")
    FREE_RUNS = 1   # pin the per-user free cap for a deterministic test (module default is env-driven)
    assert can_run("a@x.com") == (True, "ok")
    record_run("a@x.com", 0.45)
    ok, why = can_run("a@x.com")
    assert not ok, "free cap should block 2nd run"
    assert can_run("a@x.com", is_paid=True)[0], "paid should pass"
    # monthly fair-use meter: accumulates per (account, period); a new period resets to zero
    assert monthly_usage("sub@x.com", "p1") == {"spend": 0.0, "tokens": 0}
    record_monthly("sub@x.com", "p1", 1.20, 45000)
    record_monthly("sub@x.com", "p1", 0.80, 15000)
    mu = monthly_usage("sub@x.com", "p1")
    assert round(mu["spend"], 2) == 2.00 and mu["tokens"] == 60000
    assert monthly_usage("sub@x.com", "p2")["spend"] == 0.0, "next period starts fresh"
    print("usage.py self-test OK:", snapshot())
