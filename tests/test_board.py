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
