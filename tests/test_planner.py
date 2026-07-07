"""Plan builder — prepare(), the tree verbs (forward/rebranch), and board feed-forward."""

from app import planner


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


def test_prepare_reuse_skips_reshape_and_carries_claims(monkeypatch):
    # T2: given a prior payload (the refined node's merge skim, same thesis), prepare must NOT re-shape
    # the idea and must carry the fetched claims into research (which re-grades them, no web fan-out).
    from app import intake

    def boom_shape(*a, **k):
        raise AssertionError("intake.shape ran on the reuse path")

    monkeypatch.setattr(intake, "shape", boom_shape)
    seen = {}

    def spy_research(idea, mock=False, on_progress=None, prior_claims=None):
        seen["idea"], seen["prior_claims"] = idea, prior_claims
        return {"prose": {"title": "t"}, "rows": [], "stats": {}, "lanes": [], "claims": [], "cost": 0.0}

    monkeypatch.setattr(planner, "research", spy_research)
    prior = {"thesis": "a sharp reused thesis", "founder_edge": "sales",
             "claims": [{"text": "x", "source_url": "https://census.gov", "quantitative": True, "lane": "L0"}]}
    prep = planner.prepare("ignored raw idea", mock=True, prior=prior)
    assert seen["idea"] == "a sharp reused thesis"          # research runs on the merged thesis, unchanged
    assert seen["prior_claims"] == prior["claims"]          # the skim's claims are carried forward
    assert prep["shaped"]["thesis"] == "a sharp reused thesis"
    assert prep["shaped"]["founder_edge"] == "sales"        # built from the refined node, not a re-shape


def test_working_idea_prefers_thesis():
    assert planner._working_idea({"idea": "raw", "shaped": {"thesis": "focused"}}) == "focused"
    assert planner._working_idea({"idea": "raw"}) == "raw"  # back-compat


def _fresh_root():
    prep = planner.prepare("guitar coaching idea", mock=True)
    return prep, planner.root_node(prep["proposal"])


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


def test_rebranch_redrafts_in_place():
    # "not quite" = re-draft the same step as a sibling; nothing finalized, the note steers it
    prep, root = _fresh_root()
    sib, _ = planner.rebranch("guitar coaching", prep["research"], root, "make it punchier", mock=True)
    assert sib["step"] == 0 and sib["files"] == {} and "revised" in sib["draft"]


def test_forward_finalizes_and_advances():
    prep, root = _fresh_root()
    child, _ = planner.forward("guitar coaching", prep["research"], root, None, mock=True)
    assert child["step"] == 1 and len(child["files"]) == 1


def test_board_reviews_each_section_and_feeds_forward():
    prep, root = _fresh_root()
    child, _ = planner.forward("guitar coaching", prep["research"], root, None, mock=True,
                               directors=["closer", "cfo"])
    assert len(child["board"]) == 1 and len(child["board"][0]["directors"]) == 2
    assert child["board"][0]["verdict"]                     # synthesized takeaway
    assert "board-guided" in child["draft"]                 # takeaway steered the next draft
    assert planner._board_notes(child["board"]).startswith("- on")


def test_no_board_means_no_reviews():
    prep, root = _fresh_root()
    child, _ = planner.forward("guitar coaching", prep["research"], root, None, mock=True)
    assert child["board"] == []


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
