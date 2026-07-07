"""Contract tests for the CONTEXT ENGINE (app/context.py) — the single owner of what the model sees.

The rule these tests enforce: every user-visible state fact must appear in the view of every surface
that has to answer about it. Each of the four real context drops from 2026-07-04 is pinned here as an
assertion; a new drop should fail HERE, not in the operator's chat. When you add state to the tree,
add it to the engine's renderers AND add its fact to _SESSION + an assertion below.
"""

from app import context


def _session():
    """A synthetic session exercising every state fact the views must carry:
    idea → steered brainstorm fork → 2 options (one picked, one passed) → refined join → a section."""
    return {
        "idea": "cookies with ex cons on tiktok",
        "proposal": {"title": "What you sell"},
        "tree": {"active": "sec1", "nodes": {
            "i1": {"id": "i1", "kind": "idea", "parent": None, "children": ["b1"],
                   "draft": "cookies with ex cons on tiktok"},
            "b1": {"id": "b1", "kind": "brainstorm", "parent": "i1", "children": ["o1", "o2"],
                   "feedback": "keep the story angle, drop the branded cookie delivery"},
            "o1": {"id": "o1", "kind": "option", "parent": "b1",
                   "direction": {"title": "Redemption Bakery Series",
                                 "one_liner": "A TikTok creator channel."}},
            "o2": {"id": "o2", "kind": "option", "parent": "b1",
                   "direction": {"title": "Corporate gift boxes",
                                 "one_liner": "B2B gifting with story cards."}},
            "r1": {"id": "r1", "kind": "refined", "parent": "o1", "children": ["sec1"],
                   "selected": ["o1"], "thesis": "A creator channel that sells cookies"},
            "sec1": {"id": "sec1", "parent": "r1", "title": "The setup",
                     "draft": "the setup body text"},
        }},
    }


# ── drop #1: the router claimed on-screen options didn't exist ───────────────
def test_screen_enumerates_options_at_a_fork():
    s = _session()
    s["tree"]["active"] = "b1"
    ctx = context.screen(s)
    assert "1) 'Redemption Bakery Series'" in ctx
    assert "2) 'Corporate gift boxes'" in ctx


def test_screen_frames_a_browsed_node():
    ctx = context.screen(_session(), "o2")
    assert "EARLIER node" in ctx and "Corporate gift boxes" in ctx


# ── drop #2: a fork's steering note vanished from a later pivot's path ────────
def test_path_carries_fork_steering():
    lines = context.path(_session(), "sec1")
    assert any("drop the branded cookie delivery" in x for x in lines)


def test_path_is_ancestors_only():
    # pivoting from o2: its sibling o1, the refined join, and the section are NOT context
    lines = "\n".join(context.path(_session(), "o2"))
    assert "Corporate gift boxes" in lines
    assert "Redemption Bakery Series" not in lines and "creator channel that sells" not in lines


# ── drop #3: the advisor couldn't say which option was picked ────────────────
def test_journey_marks_picked_and_passed():
    j = context.journey(_session())
    assert "✓ PICKED] 'Redemption Bakery Series'" in j
    assert "passed over] 'Corporate gift boxes'" in j
    assert "drop the branded cookie delivery" in j   # fork steers ride along


# ── drop #5 (class): the advisor must know WHERE the operator is ─────────────
def test_situation_frames_a_read_abandoned_node():
    sit = context.situation(_session(), "o2")
    assert "READING" in sit and "PASSED OVER" in sit and "Corporate gift boxes" in sit


def test_situation_flags_a_midflight_build():
    assert "MID-FLIGHT" in context.situation(_session(), None, "Writing the next part")
    s = dict(_session(), status="researching")
    assert "MID-FLIGHT" in context.situation(s)


def test_situation_always_names_the_active_step():
    # 'which section am I in?' must never get 'I can't see your screen' — position is unconditional
    sit = context.situation(_session())
    assert "They are ON: the 'The setup' section" in sit
    assert "READING" not in sit and "MID-FLIGHT" not in sit   # idle frontier → coaching stays allowed


# ── the moat's labels survive every view ─────────────────────────────────────
def test_evidence_keeps_cited_and_flagged_split():
    cited, flagged = context.evidence({"research": {"rows": [
        {"mark": "ok", "text": "independent stat", "url": "https://gov.example"},
        {"mark": "warn", "text": "vendor stat", "url": "https://vendor.example"}]}})
    assert "independent stat" in cited and "independent stat" not in flagged
    assert "vendor stat" in flagged and "vendor stat" not in cited


# ── every node kind renders through ONE snippet (add new kinds/fields here) ──
def test_every_kind_has_a_snippet():
    s = _session()["tree"]["nodes"]
    assert context.snippet(s["i1"]).startswith("the original idea")
    assert "steered by" in context.snippet(s["b1"])
    assert context.snippet(s["b1"] | {"feedback": ""}) == "the fork where directions were spread"
    assert "Redemption Bakery Series" in context.snippet(s["o1"])
    assert context.snippet(s["r1"]).startswith("the refined idea")
    assert "The setup" in context.snippet(s["sec1"])


def test_views_are_bounded():
    # a pathological session can't blow the router's budget
    s = _session()
    s["tree"]["nodes"]["b1"]["feedback"] = "x" * 5000
    s["idea"] = "y" * 5000
    assert len(context.screen(s)) <= context.SCREEN_CAP
    assert len(context.journey(s)) <= context.JOURNEY_CAP
