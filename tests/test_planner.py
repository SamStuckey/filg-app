"""Plan builder — prepare(), the decision-tree advance, and board feed-forward."""

import planner


def test_prepare_shapes_researches_and_vets():
    prep = planner.prepare("I like basketball, MTG, food, and I'm good at sales", mock=True)
    assert prep["shaped"]["thesis"] and prep["research"]["rows"]
    assert prep["vetting"]["verdict"] in ("pursue", "pivot", "kill")
    assert prep["proposal"]["section"] == "brief" and prep["cost"] == 0.0


def test_research_lanes_get_distinct_persona_owners():
    prep = planner.prepare("guitar coaching for adults", mock=True)
    owned = prep["research"]["owned_lanes"]
    assert owned and all(o["lane"] and o["owner"] and o["owner_name"] for o in owned)
    # the fan-out surfaces as different people, not one anonymous searcher
    assert len({o["owner"] for o in owned}) == len(owned)
    # the standing skeptic is never a lane owner
    assert "skeptic" not in {o["owner"] for o in owned}


def test_prepare_attaches_assumption_premortem_to_vetting():
    prep = planner.prepare("guitar coaching for adults", mock=True)
    pm = prep["vetting"].get("premortem")
    assert pm and all(a["assumption"] and a["status"] in ("holds", "shaky", "breaks") for a in pm)


def test_working_idea_prefers_thesis():
    assert planner._working_idea({"idea": "raw", "shaped": {"thesis": "focused"}}) == "focused"
    assert planner._working_idea({"idea": "raw"}) == "raw"  # back-compat


def _fresh_session():
    prep = planner.prepare("guitar coaching idea", mock=True)
    return {"idea": "guitar coaching", "shaped": prep["shaped"], "research": prep["research"],
            "files": {}, "history": [], "step": 0, "cost": 0.0, "board": [],
            "proposal": prep["proposal"], "status": "building"}


def test_why_you_win_section_and_delivery_model_guide():
    keys = [s["key"] for s in planner.SECTIONS]
    assert "why" in keys and planner.N == 7                      # positioning section now exists
    offer = next(s for s in planner.SECTIONS if s["key"] == "offer")
    assert offer.get("guide") and "reselling" in offer["guide"]  # offer forces the delivery model


def test_propose_injects_founder_edge_and_section_guide(patch_call):
    cap = {}
    patch_call(lambda stage, prompt: cap.setdefault("p", prompt) or "draft")
    r = planner.research("x", mock=True)
    planner.propose("idea", "why", r, [], founder="ten years shipping automations", mock=False)
    assert "UNFAIR ADVANTAGE" in cap["p"] and "ten years shipping" in cap["p"]
    assert "WHAT THIS SECTION MUST DO" in cap["p"]               # the per-section guide is injected


def test_not_quite_stays_on_node():
    s = _fresh_session()
    upd = planner.advance(s, "not_quite", "make it punchier", mock=True)
    assert "step" not in upd and "revised" in upd["proposal"]["draft"]


def test_yes_and_finalizes_and_advances():
    s = _fresh_session()
    upd = planner.advance(s, "yes_and", None, mock=True)
    assert upd["step"] == 1 and len(upd["files"]) == 1


def test_board_reviews_each_section_and_feeds_forward():
    s = _fresh_session()
    upd = planner.advance(s, "yes_and", None, mock=True, directors=["closer", "cfo"])
    assert len(upd["board"]) == 1 and len(upd["board"][0]["directors"]) == 2
    assert upd["board"][0]["verdict"]                       # synthesized takeaway
    assert "board-guided" in upd["proposal"]["draft"]       # takeaway steered the next draft
    assert planner._board_notes(upd["board"]).startswith("- on")


def test_no_board_means_no_reviews():
    s = _fresh_session()
    upd = planner.advance(s, "yes_and", None, mock=True)   # no directors
    assert "board" not in upd


def test_qa_plan_voted_checklist_flags_and_feeds_editor(patch_call):
    cap = {}

    def reply(stage, prompt):
        if stage == "plan_qa_check":          # the voted boolean checklist (×3 → same fail each round)
            return ('[{"i":1,"verdict":"fail","why":"buyer drifts: dentists vs plumbers"},'
                    '{"i":2,"verdict":"pass","why":""},{"i":3,"verdict":"pass","why":""}]')
        if stage == "plan_qa":                # the editor pass must receive the failed check as feedback
            cap["editor_prompt"] = prompt
            return '{"notes": ["tightened a line"], "fixes": {}}'
        return ""

    patch_call(reply)
    files = {"1-the-setup.md": "# Setup\nSell to dentists at $500.",
             "2-what-you-sell.md": "# Offer\nSell to plumbers at $900."}
    _revised, report, _cost = planner.qa_plan("idea", files, mock=False)
    assert report["checks_failed"] == ["CONSISTENT"]
    assert any("CONSISTENT" in n for n in report["notes"])
    assert "CONSISTENT" in cap["editor_prompt"] and "buyer drifts" in cap["editor_prompt"]


def test_qa_plan_all_checks_pass(patch_call):
    def reply(stage, prompt):
        if stage == "plan_qa_check":
            return ('[{"i":1,"verdict":"pass","why":""},{"i":2,"verdict":"pass","why":""},'
                    '{"i":3,"verdict":"pass","why":""}]')
        return '{"notes": [], "fixes": {}}'

    patch_call(reply)
    _r, report, _c = planner.qa_plan("idea", {"1-the-setup.md": "# Setup\nbody long enough to be kept here"}, mock=False)
    assert report["checks_failed"] == []
    assert any("passed" in n for n in report["notes"])
