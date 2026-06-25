"""Intake (shape) + vet — including the REAL-mode JSON parsing the prod bug lived in.

The mock paths return canned dicts; the bug was in the real path where the model's
fenced object-with-array got mis-parsed. test_shape_real_*_returns_dict exercises
exactly that and would have caught the regression."""

import intake

GRAB_BAG = "I like basketball, Magic the Gathering, and food, and I'm good at sales"


# ── mock paths ───────────────────────────────────────────────────────────────
def test_shape_mock_focuses_and_keeps_edge():
    shaped, cost = intake.shape(GRAB_BAG, mock=True)
    assert cost == 0.0 and shaped["coherent"] and shaped["thesis"]
    assert "sales" in shaped["founder_edge"].lower()       # leads with the edge, not the hobbies
    assert len(shaped["wedges_considered"]) >= 2           # shows it chose, didn't blend


def test_vet_mock_returns_verdict():
    shaped, _ = intake.shape(GRAB_BAG, mock=True)
    vetting, _ = intake.vet(GRAB_BAG, shaped, None, mock=True)
    assert vetting["verdict"] in ("pursue", "pivot", "kill")
    assert vetting["first_test"] and vetting["scores"]


# ── real-mode parsing (the regression surface) ───────────────────────────────
def test_shape_real_object_with_array_returns_dict(patch_call):
    # THE bug: a fenced object containing an array must parse to a dict, not the inner list.
    patch_call('```json\n{"coherent": true, "thesis": "Sell sales-as-a-service to local shops", '
               '"founder_edge": "good at sales", "wedges_considered": ["sales svc", "coaching"], '
               '"clarifying_question": null}\n```')
    shaped, _ = intake.shape(GRAB_BAG, mock=False)
    assert isinstance(shaped, dict)
    assert shaped["thesis"].startswith("Sell sales") and shaped["wedges_considered"] == ["sales svc", "coaching"]


def test_shape_real_tolerates_non_object_reply(patch_call):
    # If the model misbehaves and returns a bare array, degrade gracefully (no 500).
    patch_call('["a", "b"]')
    shaped, _ = intake.shape(GRAB_BAG, mock=False)
    assert isinstance(shaped, dict) and shaped["thesis"]   # falls back to the raw idea as thesis


def test_vet_real_parses_scores_object(patch_call):
    patch_call('{"verdict": "pivot", "scores": {"demand": 4, "founder_fit": 5}, '
               '"reason": "sharper niche needed", "biggest_risk": "x", '
               '"first_test": "dm 20 owners", "ninety_day_win": "3 clients"}')
    shaped, _ = intake.shape(GRAB_BAG, mock=True)
    vetting, _ = intake.vet(GRAB_BAG, shaped, None, mock=False)
    assert vetting["verdict"] == "pivot" and vetting["scores"]["founder_fit"] == 5


def test_vet_real_bad_verdict_defaults_to_pursue(patch_call):
    patch_call('{"verdict": "definitely maybe", "scores": {}}')
    shaped, _ = intake.shape(GRAB_BAG, mock=True)
    vetting, _ = intake.vet(GRAB_BAG, shaped, None, mock=False)
    assert vetting["verdict"] == "pursue"   # unknown verdict normalized


def test_premortem_mock_returns_status_judged_assumptions():
    shaped, _ = intake.shape(GRAB_BAG, mock=True)
    pm, cost = intake.premortem(GRAB_BAG, shaped, None, mock=True)
    assert cost == 0.0 and pm
    assert all(a["assumption"] and a["status"] in intake._PM_STATUS for a in pm)


def test_premortem_real_clamps_bad_status_and_caps(patch_call):
    patch_call('{"assumptions": [{"assumption": "a1", "status": "nonsense", "why": "w1"}, '
               '{"assumption": "a2", "status": "breaks", "why": "w2"}, '
               '{"assumption": "", "status": "holds", "why": "skip me"}]}')
    shaped, _ = intake.shape(GRAB_BAG, mock=True)
    pm, _ = intake.premortem(GRAB_BAG, shaped, None, mock=False)
    assert [a["status"] for a in pm] == ["shaky", "breaks"]   # bad→shaky; empty assumption dropped
