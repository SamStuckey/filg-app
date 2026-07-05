"""Router — the single prompt box that always wins, plus the pivot-fork integration gate.

Mock paths use keyword heuristics (also what dev/frontend runs on); the real paths (patch_call)
exercise the JSON parse + schema normalization the mock never touches."""

import router


# ── mock routing (keyword heuristics) ────────────────────────────────────────
def test_commit_is_flagged_costly():
    d, cost = router.route("ok I'm sold, build the plan", stage="refined", mock=True)
    assert cost == 0.0 and d["intent"] == "commit" and d["confirm"] is True


def test_restart_keep_captures_what_to_keep():
    d, _ = router.route("start over but keep the food truck angle", mock=True)
    assert d["intent"] == "restart_keep" and d["keep"] and "food truck" in d["keep"]


def test_hard_restart_is_confirmed():
    d, _ = router.route("this all sucks, something else", mock=True)
    assert d["intent"] == "restart_hard" and d["confirm"] is True


def test_question_routes_to_ask():
    assert router.route("what does this cost?", mock=True)[0]["intent"] == "ask"


def test_mode_is_a_prior_not_a_cage():
    # ambiguous prompt inside the research tool → a research question
    assert router.route("cheaper competitors", mode="research", mock=True)[0]["target"] == "research"
    # but a clear global instruction breaks out of the mode
    d = router.route("this all sucks, something else", mode="board", mock=True)[0]
    assert d["intent"] == "restart_hard"


def test_plain_build_prompt_is_a_steer():
    d = router.route("make the pricing simpler", mode="build", mock=True)[0]
    assert d["intent"] == "steer" and d["steer"] == "make the pricing simpler"


# ── real-mode parse + normalization ──────────────────────────────────────────
def test_route_real_parses_and_keeps_confirm_only_on_costly(patch_call):
    patch_call('{"intent": "steer", "target": "current", "keep": null, "steer": "lean B2B", '
               '"confirm": true, "say": "Reworking to B2B"}')
    d, _ = router.route("go B2B", mock=False)
    # confirm must be stripped: steer is not a costly route
    assert d["intent"] == "steer" and d["confirm"] is False and d["steer"] == "lean B2B"


def test_route_real_bad_intent_normalized(patch_call):
    patch_call('{"intent": "nonsense", "target": "nowhere"}')
    d, _ = router.route("hmm", mode="build", mock=False)
    assert d["intent"] == "steer" and d["target"] == "current"     # unknowns fall back sanely


def test_route_real_feedback_misread_as_help_becomes_a_steer(patch_call):
    # the live drift: 'not quite, <wild pivot>' classified ask/help — the backstop turns it into a steer
    patch_call('{"intent": "ask", "target": "help"}')
    d, _ = router.route("not quite, forget baking entirely and lean into industrial power tools",
                        mode="build", mock=False)
    assert d["intent"] == "steer" and d["steer"].startswith("not quite")


def test_route_real_short_product_question_still_reaches_help(patch_call):
    patch_call('{"intent": "ask", "target": "help"}')
    d, _ = router.route("what does this cost?", mode="build", mock=False)
    assert d["intent"] == "ask" and d["target"] == "help"


def test_route_real_bad_intent_in_tool_defaults_to_ask(patch_call):
    patch_call('{"intent": "nonsense"}')
    d, _ = router.route("hmm", mode="research", mock=False)
    assert d["intent"] == "ask" and d["target"] == "research"


# ── the pivot-fork integration gate ──────────────────────────────────────────
def test_integration_tweak_integrates_mock():
    ok, _ = router.check_integration("make it cheaper", "sell a subscription box", mock=True)
    assert ok["integrable"] and not ok["clash"]


def test_integration_hard_contradiction_forks_mock():
    c, _ = router.check_integration("actually sell physical hardware instead", "a SaaS service", mock=True)
    assert not c["integrable"] and c["clash"] and c["skeptic_say"]


def test_integration_real_defaults_to_integrable_when_missing(patch_call):
    patch_call('{"clash": "", "skeptic_say": ""}')   # no "integrable" key → default true (bend, don't fork)
    ok, _ = router.check_integration("tweak the tone", "sell coaching", mock=False)
    assert ok["integrable"]


def test_integration_real_clash_carries_reason(patch_call):
    patch_call('{"integrable": false, "clash": "different buyer entirely", "skeptic_say": "not the same business"}')
    c, _ = router.check_integration("sell to enterprises now", "a consumer app", mock=False)
    assert not c["integrable"] and c["clash"] == "different buyer entirely"
