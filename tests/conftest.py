"""Shared pytest fixtures + path/env setup for the FILG test suite.

Set the env BEFORE importing any app module: mock mode (no API spend), a throwaway
sqlite DB, and a high free-run cap so the metering guardrail doesn't trip tests.
Paths mirror how the app wires itself (prototype/ + app/ on sys.path, plus repo
root for `import app.main`).
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

sys.path.insert(0, str(ROOT / "prototype"))   # engine
sys.path.insert(0, str(ROOT / "app"))         # bare sibling imports (skill_registry, personas, ...)
sys.path.insert(0, str(ROOT))                 # `import app.main`

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
    import pipeline

    def setter(responses):
        def fake(stage, model, prompt, *, max_tokens=1500, tools=None, system=None, cache=False):
            if callable(responses):
                return responses(stage, prompt)
            if isinstance(responses, dict):
                return responses.get(stage, "")
            return responses
        monkeypatch.setattr(pipeline, "call", fake)
    return setter


def wait_status(client, sid, target="building", tries=80, delay=0.05):
    """Poll a plan session until it leaves 'researching' (the background thread finishes)."""
    s = client.get(f"/api/plan/{sid}").json()
    for _ in range(tries):
        if s["status"] != "researching":
            break
        time.sleep(delay)
        s = client.get(f"/api/plan/{sid}").json()
    return s
