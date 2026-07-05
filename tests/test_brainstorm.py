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
