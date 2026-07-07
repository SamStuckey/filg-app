"""app/domain — the use-case plug. These tests pin the contract a sibling product must meet
when it swaps the domain package: a complete stage spec, full canned-copy coverage, and a
registered tree vocabulary. A hole here (a section without WOD copy, a kind nobody registered)
surfaces as a KeyError deep in a build, so we check it flat-out instead."""

from app.domain import copy as domain_copy
from app.domain import nodes as vocab
from app.domain import sections as spec
from engine import tree as dtree


def test_sections_are_complete_and_uniquely_keyed():
    assert spec.N == len(spec.SECTIONS) >= 1
    keys = [s["key"] for s in spec.SECTIONS]
    files = [s["file"] for s in spec.SECTIONS]
    assert len(set(keys)) == len(keys) and len(set(files)) == len(files)
    for s in spec.SECTIONS:
        assert s["key"] and s["file"].endswith(".md") and s["title"] and "sub" in s


def test_section_lookup():
    assert spec.section("offer")["file"] == "2-what-you-sell.md"
    assert spec.section("nope") is None


def test_canned_copy_covers_every_section():
    keys = {s["key"] for s in spec.SECTIONS}
    assert set(domain_copy.MOCK_DRAFT) == keys     # mock mode drafts every section
    assert set(domain_copy.WOD) == keys            # waste-of-time mode covers every section
    assert all(isinstance(v, str) and v.strip() for v in domain_copy.MOCK_DRAFT.values())
    assert all(isinstance(v, str) and v.strip() for v in domain_copy.WOD.values())


def test_qa_checks_shape():
    assert spec.QA_CHECKS and all(
        isinstance(cid, str) and cid.isupper() and question.strip()
        for cid, question in spec.QA_CHECKS)


def test_tree_vocabulary_registered():
    kinds = dtree.kinds()
    for k in ("idea", "brainstorm", "option", "refined", "fork", "section"):
        assert k in kinds, f"kind {k!r} not registered"
    assert dtree.kind({}) == "section"             # the wire-compat default
    atts = dtree.attachments()
    assert "board" in atts and atts["board"].inherit and atts["board"].max_items == 12
    assert "log" in atts and not atts["log"].inherit
    assert vocab.BOARD is not None


def test_planner_exposes_the_spec_unchanged():
    from app import planner
    assert planner.SECTIONS is spec.SECTIONS and planner.N == spec.N
    assert planner.QA_CHECKS is spec.QA_CHECKS
