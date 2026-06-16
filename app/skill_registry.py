#!/usr/bin/env python3
"""
App-side skill registry — the product's IP, externalized from code.

A FILG "skill" is the same shape as a Claude Code SKILL.md: a markdown file of
curated, durable instruction (the reasoning scaffold that makes us better than a
raw prompt). The difference is *where it runs* — Claude Code skills load into the
harness; the deployed app talks to the raw Anthropic SDK, which has no concept of
skills. So the app needs its own copy. This module is that loader.

Layout:  app/skills/<name>/SKILL.md   (optional `---` YAML-ish frontmatter + body)

The body becomes the **system block** of an API call — stable across every run,
so it caches (set `cache=True` on pipeline.call). Runtime data (the idea, the
research, the plan-so-far) is interpolated by the calling code into the *user*
message, never baked into the skill. That separation is what makes the skill
re-usable and the system prefix cacheable.

Forward-compat: dropping a new SKILL.md (e.g. one distilled from a podcast, a
book, or a free business course) into app/skills/<name>/ makes it loadable with
no code change — `system("<name>")`. Personas (app/personas.py) and the board
orchestrator (app/board.py) both resolve their instruction text through here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# Standing voice rule — appended to EVERY skill's system block so all generated, user-facing text
# avoids the usual AI tells. One place to edit; applies everywhere (synthesis, advisor, board, …).
VOICE = (
    "## Voice (applies to everything you write)\n\n"
    "Write like a human operator, not an AI.\n"
    "- Do NOT use the em-dash or en-dash characters (— or –). Use commas, periods, or parentheses.\n"
    "- Never use the word \"honestly\" or the phrase \"to be honest\". Say the thing directly.\n"
    "- Avoid these AI-tell words: delve, tapestry, comprehensive, leverage, synergy, robust, "
    "seamless, elevate, unlock, realm, testament, ever-evolving, pivotal, crucial, vibrant, "
    "underscore (as a verb), navigate (when used figuratively).\n"
    "- Be plain, specific, and direct. No throat-clearing, no preamble.\n"
    "- Structure with markdown headings and lists. Do not output rows of dashes as separators."
)


def _parse(text: str) -> tuple[dict, str]:
    """Split optional leading `---` frontmatter (simple key: value lines) from the body."""
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            _, fm, body = parts
            for line in fm.strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
    return meta, body.strip()


@lru_cache(maxsize=None)
def _load(name: str) -> tuple[dict, str]:
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.exists():
        raise KeyError(f"no skill {name!r} at {path}")
    return _parse(path.read_text(encoding="utf-8"))


def system(name: str) -> str:
    """The skill's body + the standing VOICE rule — use as the (cacheable) system block of an API
    call. VOICE is stable, so the prefix still caches."""
    return f"{_load(name)[1]}\n\n{VOICE}"


def meta(name: str) -> dict:
    """The skill's frontmatter (e.g. {'model': 'sonnet', 'domains': '...'})."""
    return dict(_load(name)[0])


def exists(name: str) -> bool:
    return (SKILLS_DIR / name / "SKILL.md").exists()


def names() -> list[str]:
    if not SKILLS_DIR.exists():
        return []
    return sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))


if __name__ == "__main__":  # self-test (no API)
    found = names()
    assert "intake" in found and "vet" in found, found
    assert system("intake") and "thesis" in system("intake").lower()
    assert system("vet") and ("pursue" in system("vet").lower())
    assert "honestly" in system("intake") and "em-dash" in system("intake")  # VOICE appended to all
    assert exists("director_base") and not exists("nope")
    print("skill_registry.py self-test OK —", len(found), "skills:", ", ".join(found))
