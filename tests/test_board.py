"""Board of Directors — convening, the collaboration matrix, and review_section."""

import board
import personas


def test_convene_mock_structure():
    res, cost = board.convene("sales service", "## Offer\nflat retainer", "is pricing right?",
                              ["closer", "cfo"], mock=True)
    assert cost == 0.0
    assert {d["key"] for d in res["directors"]} == {"closer", "cfo"}
    assert res["consensus"] and res["verdict"] and "composite" in res["disclaimer"].lower()


def test_convene_bad_keys_fall_back_to_default_board():
    res, _ = board.convene("idea", "", "anything", ["nonsense"], mock=True)
    assert len(res["directors"]) == len(personas.DEFAULT_BOARD)


def test_review_section_targets_the_section():
    rev, _ = board.review_section("idea", "What you charge", "## Pricing\n$500/mo", "",
                                  ["closer"], mock=True)
    assert rev["directors"][0]["key"] == "closer" and rev["verdict"]


def test_convene_real_assembles_takes_and_matrix(patch_call):
    # Each director call returns a take; the synth call returns the matrix JSON.
    def reply(stage, prompt):
        if stage == "board_synth":
            return '{"consensus": "agree on X", "conflicts": "none", "verdict": "ship it"}'
        return f"take for {stage}"
    patch_call(reply)
    res, _ = board.convene("idea", "plan", "vet it", ["closer", "cfo"], mock=False)
    assert len(res["directors"]) == 2 and all(d["take"] for d in res["directors"])
    assert res["verdict"] == "ship it" and res["consensus"] == "agree on X"


def test_skeptic_is_a_standing_seat():
    # the skeptic is always present with a committed, structured verdict
    res, _ = board.convene("idea", "", "vet it", ["closer", "cfo"], mock=True)
    sk = res["skeptic"]
    assert sk and sk["key"] == "skeptic"
    assert sk["verdict"] in board._VERDICTS and sk["rationale"]
    assert "confidence" in sk and "suggested_change" in sk


def test_skeptic_not_duplicated_as_a_lane():
    # asking for the skeptic as a director must not list it among the operator-picked lanes
    res, _ = board.convene("idea", "", "vet it", ["closer", "skeptic"], mock=True)
    assert [d["key"] for d in res["directors"]] == ["closer"]
    assert res["skeptic"]["key"] == "skeptic"
    # and it's excluded from the operator-pickable catalog
    assert "skeptic" not in {p["key"] for p in personas.catalog()}


def test_skeptic_can_be_disabled():
    res, _ = board.convene("idea", "", "vet it", ["closer"], mock=True, skeptic=False)
    assert res["skeptic"] is None


def test_skeptic_verdict_is_parsed_and_clamped_in_real_mode(patch_call):
    def reply(stage, prompt):
        if stage == "board_skeptic":
            return ('{"verdict": "non-starter", "rationale": "the margin dies on delivery", '
                    '"suggested_change": "cap scope", "confidence": "high"}')
        if stage == "board_synth":
            return '{"consensus": "c", "conflicts": "none", "verdict": "v"}'
        return f"take for {stage}"
    patch_call(reply)
    res, _ = board.convene("idea", "plan", "vet it", ["closer"], mock=False)
    assert res["skeptic"]["verdict"] == "non-starter"
    assert res["skeptic"]["rationale"] == "the margin dies on delivery"
    assert res["skeptic"]["suggested_change"] == "cap scope"
