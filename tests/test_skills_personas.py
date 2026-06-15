"""Skill registry + persona registry."""

import personas
import skill_registry as skills


def test_registry_loads_expected_skills():
    found = skills.names()
    for s in ("intake", "vet", "synth_section", "director_base"):
        assert s in found and skills.exists(s)
    assert not skills.exists("does-not-exist")


def test_skill_system_bodies_are_real():
    assert "thesis" in skills.system("intake").lower()
    assert "pursue" in skills.system("vet").lower()
    assert skills.meta("intake").get("model") == "sonnet"  # frontmatter parsed


def test_persona_catalog_hides_internal_voice():
    cat = personas.catalog()
    assert cat and all("voice" not in p for p in cat)
    assert all({"key", "name", "blurb", "domains"} <= set(p) for p in cat)


def test_default_board_is_subset_of_personas():
    assert personas.DEFAULT_BOARD and set(personas.DEFAULT_BOARD) <= personas.KEYS


def test_system_for_merges_base_and_voice():
    s = personas.system_for("cfo")
    assert "Skeptical CFO" in s and "composite" in s.lower()  # persona voice + director_base rules


def test_route_picks_by_domain_and_always_returns_k():
    assert personas.route("what should I charge?")[0] in ("closer", "cfo")
    assert personas.route("which channel reaches this buyer?")[0] in ("growth", "brand")
    assert len(personas.route("anything at all", k=3)) == 3
    assert personas.route("pricing", pool=["operator", "growth"])[0] in ("operator", "growth")
