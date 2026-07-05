"""Brainstorm — diverge (1-3 vetted-shape directions) + merge (reconcile + adversarial cull).

Mock paths return canned dicts; the real paths (patch_call) exercise the JSON parsing/assembly
the mock self-tests never touch — the same regression surface intake/vet had."""

import brainstorm

RAW = "I'm a baker and think I'm really good. I live in the middle of nowhere, how do I sell what I make?"


# ── mock paths ───────────────────────────────────────────────────────────────
def test_diverge_mock_returns_1_to_3_shaped_directions():
    d, cost = brainstorm.diverge(RAW, mock=True)
    assert cost == 0.0 and d["spread"] in ("tight", "loose")
    assert 1 <= len(d["directions"]) <= 3
    for x in d["directions"]:
        assert x["title"] and x["mold"]          # every direction names its business-model mold


def test_merge_mock_reconciles_and_shows_the_cut():
    d, _ = brainstorm.diverge(RAW, mock=True)
    m, cost = brainstorm.merge(RAW, d["directions"], mock=True)
    assert cost == 0.0 and m["thesis"] and m["founder_edge"]
    assert isinstance(m["kept"], list) and isinstance(m["dropped"], list)
    # the cull is transparent: a dropped thread carries a reason (so the user can veto it)
    assert m["dropped"] and m["dropped"][0]["thread"] and m["dropped"][0]["why"]
    assert m["research"]["prose"]["title"]        # a light first-pass skim rides along


def test_merge_mock_research_false_skips_the_skim():
    d, _ = brainstorm.diverge(RAW, mock=True)
    m, _ = brainstorm.merge(RAW, d["directions"], mock=True, research=False)
    assert "research" not in m                    # fast re-merge, no spend on research


def test_merge_streams_the_dropped_threads():
    d, _ = brainstorm.diverge(RAW, mock=True)
    lines = []
    brainstorm.merge(RAW, d["directions"], mock=True, on_progress=lines.append)
    assert any(line.startswith("✂ dropped") for line in lines)


# ── real-mode parsing (the regression surface) ───────────────────────────────
def test_diverge_real_parses_and_clamps_to_three(patch_call):
    patch_call('```json\n{"spread": "loose", "directions": ['
               '{"title": "A", "one_liner": "a", "mold": "Productized service", "leans_on": "x"},'
               '{"title": "B", "one_liner": "b", "mold": "Subscription box", "leans_on": "y"},'
               '{"title": "C", "one_liner": "c", "mold": "Coaching", "leans_on": "z"},'
               '{"title": "D", "one_liner": "d", "mold": "Marketplace", "leans_on": "w"}]}\n```')
    d, _ = brainstorm.diverge(RAW, mock=False)
    assert d["spread"] == "loose" and len(d["directions"]) == 3   # capped at 3
    assert d["directions"][0]["mold"] == "Productized service"


def test_diverge_real_empty_falls_back_to_passthrough(patch_call):
    patch_call('{"spread": "tight", "directions": []}')
    d, _ = brainstorm.diverge(RAW, mock=False)
    assert d["directions"] and d["directions"][0]["one_liner"]    # never an empty spread


def test_diverge_real_bad_spread_normalized(patch_call):
    patch_call('{"spread": "whatever", "directions": [{"title": "A", "mold": "Coaching"}]}')
    d, _ = brainstorm.diverge(RAW, mock=False)
    assert d["spread"] == "loose"


def test_merge_real_reconcile_parses_kept_and_dropped(patch_call):
    patch_call('{"thesis": "Sell a homemade subscription box to people far from home", '
               '"founder_edge": "you bake well", "mold": "Subscription box", '
               '"kept": ["the box"], "dropped": [{"thread": "office drop", "why": "clashes with the road model"}]}')
    # research=False so the reconcile-only path is exercised without the engine/web
    m, _ = brainstorm.merge(RAW, [{"title": "box", "one_liner": "x"}], mock=False, research=False)
    assert m["thesis"].startswith("Sell a homemade") and m["kept"] == ["the box"]
    assert m["dropped"][0]["thread"] == "office drop" and m["dropped"][0]["why"]


def test_merge_real_tolerates_non_object_reply(patch_call):
    patch_call('["nope"]')
    m, _ = brainstorm.merge(RAW, [{"title": "box"}], mock=False, research=False)
    assert m["thesis"] and isinstance(m["kept"], list)           # degrades, no 500


# ── the pivot-responsiveness seam check (2026-07-06, from Sam's QA: a real pivot was captured as
# feedback but the spread came back generic — schema validation can't see an ignored pivot) ──
PIVOT_INPUT = ('THE OPERATOR IS PIVOTING. Their pivot instruction OUTWEIGHS everything below:\n'
               'add an events angle where customers bake with the team\n\n'
               'COMMITTED PATH (root -> the pivot point):\n- the idea\n- what you sell')
_DIRS = ('{"spread": "loose", "directions": [{"title": "Cookie subscriptions", '
         '"one_liner": "Monthly cookie box.", "mold": "Subscription box"}, '
         '{"title": "Retail cookies", "one_liner": "Sell wholesale.", "mold": "Reselling"}]}')
_DIRS_PIVOTED = ('{"spread": "tight", "directions": [{"title": "Bake-with-the-team events", '
                 '"one_liner": "Customers bake alongside the team at pop-up events.", '
                 '"mold": "Seasonal / pop-up"}]}')


def test_pivot_ignored_twice_fails_loud(patch_call):
    calls = []
    def fake(stage, prompt):
        calls.append(stage)
        if stage == "diverge":
            return _DIRS                                   # generic both times
        return '{"honors_pivot": false, "why": "same directions as before"}'
    patch_call(fake)
    import pytest
    with pytest.raises(RuntimeError, match="ignoring what you asked"):
        brainstorm.diverge(PIVOT_INPUT, mock=False)
    assert calls.count("diverge") == 2 and calls.count("judge") == 2   # reprompted once, then loud


def test_pivot_retry_recovers(patch_call):
    calls = []
    def fake(stage, prompt):
        calls.append(stage)
        if stage == "diverge":
            return _DIRS if calls.count("diverge") == 1 else _DIRS_PIVOTED
        # first spread ignored the pivot; the reprompted one honors it
        return ('{"honors_pivot": false, "why": "generic"}' if calls.count("judge") == 1
                else '{"honors_pivot": true, "why": "events angle present"}')
    patch_call(fake)
    d, _ = brainstorm.diverge(PIVOT_INPUT, mock=False)
    assert d["directions"][0]["title"] == "Bake-with-the-team events"
    # the reprompt quoted the operator's pivot back as a hard requirement
    assert calls == ["diverge", "judge", "diverge", "judge"]


def test_pivot_judge_flake_fails_open(patch_call):
    def fake(stage, prompt):
        return _DIRS if stage == "diverge" else "sorry, I cannot produce JSON right now"
    patch_call(fake)
    d, _ = brainstorm.diverge(PIVOT_INPUT, mock=False)   # unverifiable ≠ ignored — no brick
    assert len(d["directions"]) == 2


def test_plain_input_never_calls_the_judge(patch_call):
    calls = []
    def fake(stage, prompt):
        calls.append(stage)
        return _DIRS
    patch_call(fake)
    brainstorm.diverge(RAW, mock=False)
    assert "judge" not in calls
