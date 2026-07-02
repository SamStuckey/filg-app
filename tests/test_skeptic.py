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


# ── the /stress-test route: async start + poll, streaming the runner sentinels ───
_IDEA = "I like basketball, Magic the Gathering, and food, and I'm good at sales"


def _wait_skeptic(client, sid, tries=120, delay=0.03):
    import time
    st = client.get(f"/api/plan/{sid}/stress-test").json()
    for _ in range(tries):
        if st.get("status") in ("done", "error"):
            break
        time.sleep(delay)
        st = client.get(f"/api/plan/{sid}/stress-test").json()
    return st


def test_stress_test_route_starts_streams_and_returns_result(client):
    sid = client.post("/api/plan/start", json={"idea": _IDEA, "email": "st@x.com"}).json()["id"]
    assert wait_status(client, sid)["shaped"]["thesis"]
    started = client.post(f"/api/plan/{sid}/stress-test")
    assert started.status_code == 200 and started.json()["started"] is True
    st = _wait_skeptic(client, sid)
    assert st["status"] == "done"
    assert st["result"]["assessments"] and st["result"]["summary"]
    assert all(a["verdict"] in skeptic._VERDICTS for a in st["result"]["assessments"])
    # the runner sentinels streamed into progress → the panel can paint the attack tree live
    assert any(str(l).startswith("§LANES§") for l in st["progress"])
    assert any(str(l).startswith("§LANEDONE§") for l in st["progress"])


def test_stress_test_state_idle_before_start(client):
    sid = client.post("/api/plan/start", json={"idea": _IDEA, "email": "st2@x.com"}).json()["id"]
    wait_status(client, sid)
    assert client.get(f"/api/plan/{sid}/stress-test").json()["status"] == "idle"


def test_stress_test_route_unknown_session_is_404(client):
    assert client.post("/api/plan/does-not-exist/stress-test").status_code == 404
    assert client.get("/api/plan/does-not-exist/stress-test").status_code == 404


def test_stress_test_route_requires_shaped(client):
    from app import store
    store.plan_create("st_raw", "st@x.com", "an unshaped idea")     # researching, no shaped yet
    assert client.post("/api/plan/st_raw/stress-test").status_code == 409
