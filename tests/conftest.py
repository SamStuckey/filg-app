"""Shared pytest fixtures + path/env setup for the FILG test suite.

Set the env BEFORE importing any app module: mock mode (no API spend), a throwaway
sqlite DB, and a high free-run cap so the metering guardrail doesn't trip tests.
The repo root goes on sys.path so `app` and `engine` import as packages.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

os.environ["FILG_MOCK"] = "1"
os.environ["FILG_DB"] = os.path.join(tempfile.mkdtemp(prefix="filg-test-"), "test.db")
os.environ["FILG_FREE_RUNS"] = "1000"     # don't let the free-tier cap make tests flaky
os.environ["FILG_DAILY_BUDGET"] = "1000"

sys.path.insert(0, str(ROOT))   # `app` + `engine` import as packages from the repo root

import pytest  # noqa: E402


@pytest.fixture
def client():
    """A FastAPI TestClient against the real app (mock mode)."""
    from fastapi.testclient import TestClient
    from app import main
    return TestClient(main.app)


@pytest.fixture
def patch_call(monkeypatch):
    """Replace pipeline.call so real-mode (mock=False) code paths run their JSON parsing/assembly
    without an API key. Pass a str (same reply for every stage), a dict (stage -> reply), or a
    callable(stage, prompt) -> reply. This is how we exercise extract_json / intake / vet / board
    real paths — the layer the mock self-tests never touched."""
    from engine import pipeline

    def setter(responses):
        def fake(stage, model, prompt, *, max_tokens=1500, tools=None, system=None, cache=False):
            if callable(responses):
                return responses(stage, prompt)
            if isinstance(responses, dict):
                return responses.get(stage, "")
            return responses
        monkeypatch.setattr(pipeline, "call", fake)
    return setter


def frontend(client):
    """The full SPA source across the display layer — the templated shell (`/`, the former v2
    surface promoted to root when v1 retired, 2026-07-06) plus the external stylesheet and script
    (`/static/*`). UI-wiring guards assert on this; it also proves the /static mount serves."""
    return (client.get("/").text
            + "\n" + client.get("/static/styles.css").text
            + "\n" + client.get("/static/app.js").text)


def wait_status(client, sid, target="building", tries=80, delay=0.05):
    """Poll a plan session until it leaves 'researching' (the background thread finishes)."""
    s = client.get(f"/api/plan/{sid}").json()
    for _ in range(tries):
        if s["status"] != "researching":
            break
        time.sleep(delay)
        s = client.get(f"/api/plan/{sid}").json()
    return s


def start_plan(client, idea, email="", directors=None, stack=None):
    """A session at the first BUILD step, driven through the live funnel (brainstorm -> pick the
    first direction -> merge -> commit) — the replacement for the retired /api/plan/start entry.
    Returns the session id; the deep build has already landed (status left 'researching')."""
    body = {"idea": idea}
    if email:
        body["email"] = email
    if directors:
        body["directors"] = directors
    if stack:
        body["stack"] = stack
    r = client.post("/api/brainstorm", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert not d.get("gibberish"), f"unexpected roast for {idea!r}"
    sid = d["id"]
    opts = [o["id"] for o in (d.get("activeNode") or {}).get("options") or []]
    r = client.post(f"/api/plan/{sid}/merge", json={"options": opts[:1]})
    assert r.status_code == 200, r.text
    wait_status(client, sid)
    r = client.post(f"/api/plan/{sid}/commit", json={})
    assert r.status_code == 200, r.text
    s = wait_status(client, sid)
    assert s["status"] == "building", s.get("error") or s["status"]
    return sid


def finish_plan(client, sid):
    """Roll a building session forward to DONE (the full 7 sections + the QA pass)."""
    for _ in range(12):
        s = client.get(f"/api/plan/{sid}").json()
        if s["status"] == "done":
            return s
        r = client.post(f"/api/plan/{sid}/next", json={})
        assert r.status_code == 200, r.text
        wait_status(client, sid)
    raise AssertionError("plan never finished")
