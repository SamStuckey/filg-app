"""Adversarial assumption-checking on the live research path (skeptic.stress_test) + its route."""

import skeptic
from conftest import wait_status


# ── mock path ────────────────────────────────────────────────────────────────
def test_mock_stress_test_shape_and_no_spend():
    res, cost = skeptic.stress_test("an AI tool for property managers",
                                    {"thesis": "AI tenant response"}, mock=True)
    assert cost == 0.0
    assert res["assessments"] and all(a["verdict"] in skeptic._VERDICTS for a in res["assessments"])
    assert set(res["summary"]) == {"survives", "weakened", "broken"}
    assert sum(res["summary"].values()) == len(res["assessments"])


def test_mock_emits_runner_sentinels():
    seen = []
    skeptic.stress_test("x", {"thesis": "y"}, mock=True, on_progress=seen.append)
    assert any(m.startswith("§LANES§") for m in seen)
    assert any(m.startswith("§LANEDONE§") for m in seen)


# ── real path, LLM driven deterministically via patch_call (branches on the prompt) ──
def _fake(stage, prompt):
    p = prompt.lower()
    if "pressure-testing an operator's plan" in p:                     # intake.premortem
        return ('{"assumptions": [{"assumption": "Buyers will pay a monthly fee", '
                '"status": "shaky", "why": "unproven"}]}')
    if "red-team researcher" in p:                                     # skeptic._refute
        return ('[{"text": "A comparable tool churned fast in this segment", '
                '"source_url": "https://sec.gov/filing", "quantitative": false, "as_of": 2023}]')
    if "source-credibility auditor" in p:                              # gate judge_batch
        return '[{"i": 1, "verdict": "TRUST"}]'
    if "delivering a verdict on each assumption" in p:                 # skeptic._verdicts
        return ('[{"i": 1, "verdict": "weakened", "confidence": 0.6, '
                '"why": "credible churn evidence from a primary source"}]')
    return ""


def test_real_stress_test_produces_evidenced_verdict(patch_call):
    patch_call(_fake)
    res, cost = skeptic.stress_test(
        "AI tenant-response tool for small property managers",
        {"thesis": "done-for-you AI tenant response", "founder_edge": "automation"}, mock=False)
    assert len(res["assessments"]) == 1
    a = res["assessments"][0]
    assert a["assumption"] == "Buyers will pay a monthly fee"
    assert a["verdict"] == "weakened" and 0.0 <= a["confidence"] <= 1.0
    assert a["evidence"] and a["evidence"][0]["url"] == "https://sec.gov/filing"
    assert a["evidence"][0]["judge"] == "TRUST"          # disconfirming evidence graded by the gate
    assert res["summary"]["weakened"] == 1


def test_real_no_assumptions_is_safe(patch_call):
    # premortem returns none → stress_test returns an empty, well-formed result without fanning out
    patch_call(lambda stage, prompt: '{"assumptions": []}'
               if "pressure-testing" in prompt.lower() else "")
    res, cost = skeptic.stress_test("vague", {"thesis": "vague"}, mock=False)
    assert res["assessments"] == [] and res["summary"] == {"survives": 0, "weakened": 0, "broken": 0}


def test_unknown_verdict_defaults_to_weakened(patch_call):
    def fake(stage, prompt):
        p = prompt.lower()
        if "pressure-testing an operator's plan" in p:
            return '{"assumptions": [{"assumption": "A", "status": "shaky", "why": "x"}]}'
        if "red-team researcher" in p:
            return "[]"                                   # no disconfirming evidence found
        if "delivering a verdict on each assumption" in p:
            return '[{"i": 1, "verdict": "nonsense", "confidence": 2.0, "why": "?"}]'
        return ""
    patch_call(fake)
    res, _ = skeptic.stress_test("idea", {"thesis": "t"}, mock=False)
    a = res["assessments"][0]
    assert a["verdict"] == "weakened" and a["confidence"] == 1.0   # bad verdict + confidence clamped
    assert a["evidence"] == []


# ── the /stress-test route (mock mode, through the FastAPI app) ───────────────
_IDEA = "I like basketball, Magic the Gathering, and food, and I'm good at sales"


def test_stress_test_route_returns_assessments(client):
    sid = client.post("/api/plan/start", json={"idea": _IDEA, "email": "st@x.com"}).json()["id"]
    s = wait_status(client, sid)
    assert s["shaped"]["thesis"]
    r = client.post(f"/api/plan/{sid}/stress-test")
    assert r.status_code == 200
    body = r.json()
    assert body["assessments"] and body["summary"]
    assert all(a["verdict"] in skeptic._VERDICTS for a in body["assessments"])
    assert "cost" in body and "tokens" in body


def test_stress_test_route_unknown_session_is_404(client):
    assert client.post("/api/plan/does-not-exist/stress-test").status_code == 404


def test_stress_test_route_requires_shaped(client):
    from app import store
    store.plan_create("st_raw", "st@x.com", "an unshaped idea")     # researching, no shaped yet
    assert client.post("/api/plan/st_raw/stress-test").status_code == 409
