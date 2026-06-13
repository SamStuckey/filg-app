#!/usr/bin/env python3
"""
SQLite-backed job store for the FILG app.

Replaces the in-memory `JOBS` dict in the launch skeleton so async runs, their graded results, and
the share links they back (`/r/{id}`) survive a process restart — on Render the in-memory store
dropped every in-flight job and expired every share link on each redeploy. Results are stashed as
JSON in a single-file DB.

The DB path is `FILG_DB` (default `app/filg.db`); usage.py shares the same file. On Render the
filesystem is ephemeral, so point `FILG_DB` at a mounted persistent disk for it to actually persist.
stdlib-only (sqlite3) so it still "runs anywhere".
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("FILG_DB") or os.path.join(_HERE, "filg.db")

_init_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    # New connection per call: safe to use across the request handlers and the background run thread.
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
        con = _connect()
        try:
            with con:
                con.execute("PRAGMA journal_mode=WAL")  # readers (status/share) don't block the writer
                con.execute(
                    "CREATE TABLE IF NOT EXISTS jobs ("
                    "  id TEXT PRIMARY KEY,"
                    "  status TEXT NOT NULL,"      # running | done | error
                    "  idea TEXT,"
                    "  user TEXT,"
                    "  mode TEXT,"                 # teardown | full
                    "  result TEXT,"               # JSON blob from the engine
                    "  error TEXT,"
                    "  created_at TEXT NOT NULL)")
        finally:
            con.close()
        _initialized = True


def create(job_id: str, idea: str, user: str, mode: str) -> None:
    init()
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO jobs (id, status, idea, user, mode, created_at) VALUES (?,?,?,?,?,?)",
                (job_id, "running", idea, user, mode, datetime.now(timezone.utc).isoformat()))
    finally:
        con.close()


def finish(job_id: str, result: dict, mode: str) -> None:
    con = _connect()
    try:
        with con:
            con.execute("UPDATE jobs SET status='done', result=?, mode=? WHERE id=?",
                        (json.dumps(result), mode, job_id))
    finally:
        con.close()


def fail(job_id: str, error: str) -> None:
    con = _connect()
    try:
        with con:
            con.execute("UPDATE jobs SET status='error', error=? WHERE id=?", (error, job_id))
    finally:
        con.close()


def get(job_id: str) -> dict | None:
    """Return the job as a dict (result parsed back from JSON), or None if unknown."""
    init()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    job = dict(row)
    job["result"] = json.loads(job["result"]) if job["result"] else None
    return job


if __name__ == "__main__":  # quick self-test (no API)
    import tempfile
    DB = tempfile.mktemp(suffix=".db")
    create("abc123", "an idea about helping dentists", "a@x.com", "teardown")
    assert get("abc123")["status"] == "running"
    finish("abc123", {"prose": {"title": "T"}, "rows": [], "stats": {}, "cost": 0.4}, "teardown")
    j = get("abc123")
    assert j["status"] == "done" and j["result"]["prose"]["title"] == "T"
    fail("abc123", "boom")
    assert get("abc123")["status"] == "error"
    assert get("nope") is None
    print("store.py self-test OK")
