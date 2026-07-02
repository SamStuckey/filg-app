#!/usr/bin/env python3
"""
VOICE copy-lint — the single canonical AI-tells blocklist + a deterministic linter.

This is the ONE machine-readable source for the voice rules. Two consumers read it:
  1. the PROMPT — `skill_registry.VOICE` is generated from `VOICE_RULE` here, so every
     author call already asks the model to avoid these tells; and
  2. the GATE — `lint(text)` deterministically catches what the prompt missed, and the
     author seam (`spine.run_author`) reprompts until clean.

It is a GENERATE→VALIDATE→REPROMPT loop, NOT post-processing: we never strip or edit the
model's text after the fact (Sam's standing "no post-processing for VOICE" decision). The
linter only ever GATES; the writer rewrites.

The deployed app cannot read the profile repo's universal CLAUDE.md at runtime, so the
canonical blocklist is vendored here. `voice_guide.md` in filg-docs is the human-readable
mirror, not a second machine source. Keep this list in sync with the universal blocklist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── the canonical blocklist (single source of truth) ─────────────────────────
DASHES = "—–"   # em-dash —, en-dash –

# Canonical universal-CLAUDE.md AI-tell words, unioned with FILG's existing extras so this
# refactor never loosens what the product already banned.
BANNED_WORDS = [
    # universal blocklist
    "delve", "tapestry", "testament", "landscape", "realm", "comprehensive",
    "leverage", "synergy", "robust", "seamless", "elevate", "unlock",
    # FILG extras (previously in skill_registry.VOICE)
    "ever-evolving", "pivotal", "crucial", "vibrant", "underscore", "navigate",
]

# Scaffolding / phrase patterns. Each is a (label, compiled-regex) — case-insensitive.
BANNED_PHRASES = [
    ("\"honest(ly)\"", re.compile(r"\bhonest(ly)?\b", re.I)),
    ("\"to be honest\"", re.compile(r"\bto be honest\b", re.I)),
    ("\"in today's ...\"", re.compile(r"\bin today'?s\b", re.I)),
    ("\"but here's the thing\"", re.compile(r"\bbut here'?s the thing\b", re.I)),
    ("\"it's not just X, it's Y\"", re.compile(r"\bnot just\b[^.\n]{0,60}?,?\s*it'?s\b", re.I)),
    ("\"Moreover/Furthermore/In conclusion\" scaffolding",
     re.compile(r"(^|\n)\s*(moreover|furthermore|in conclusion)\b", re.I)),
]


@dataclass
class Finding:
    kind: str       # what rule fired (the token or phrase label)
    context: str    # the offending text + a little surrounding context (so a model can locate it)


def _context(text: str, start: int, end: int, pad: int = 24) -> str:
    a = max(0, start - pad)
    b = min(len(text), end + pad)
    snippet = text[a:b].replace("\n", " ").strip()
    return f"…{snippet}…" if (a > 0 or b < len(text)) else snippet


def lint(text: str) -> list[Finding]:
    """Return every voice violation in `text` (empty list = clean). Findings carry located
    context, not just a count, so the reprompt can tell the writer exactly what to remove."""
    if not text:
        return []
    out: list[Finding] = []
    for i, ch in enumerate(text):
        if ch in DASHES:
            name = "em-dash" if ch == "—" else "en-dash"
            out.append(Finding(name, _context(text, i, i + 1)))
    for w in BANNED_WORDS:
        for m in re.finditer(r"\b" + re.escape(w) + r"\b", text, re.I):
            out.append(Finding(f"\"{w}\"", _context(text, m.start(), m.end())))
    for label, rx in BANNED_PHRASES:
        for m in rx.finditer(text):
            out.append(Finding(label, _context(text, m.start(), m.end())))
    return out


def is_clean(text: str) -> bool:
    return not lint(text)


def feedback(findings: list[Finding]) -> str:
    """Actionable reprompt body: each located offender + the fix instruction. Deduped, capped so
    the feedback stays lean. Never a bare count (a model can't fix what it can't locate)."""
    seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    for f in findings:
        key = (f.kind, f.context)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- remove {f.kind} near: {f.context}")
        if len(lines) >= 20:
            break
    return ("Your draft used banned AI-tell language. Rewrite to remove EACH of these, keeping the "
            "meaning and length, and re-emit the WHOLE text:\n" + "\n".join(lines))


# ── the generated prompt rule (single-sourced from the lists above) ──────────
VOICE_RULE = (
    "## Voice (applies to everything you write)\n\n"
    "Write like a human operator, not an AI.\n"
    "- Do NOT use the em-dash or en-dash characters (— or –). Use commas, periods, or parentheses.\n"
    "- Never use the words \"honest\" or \"honestly\" (or \"to be honest\"). Say it straight instead.\n"
    "- Avoid these AI-tell words: " + ", ".join(BANNED_WORDS) + ".\n"
    "- Avoid scaffolding phrases: \"in today's ...\", \"but here's the thing\", "
    "\"it's not just X, it's Y\", and \"Moreover/Furthermore/In conclusion\".\n"
    "- Be plain, specific, and direct. No throat-clearing, no preamble.\n"
    "- Structure with markdown headings and lists. Do not output rows of dashes as separators."
)


if __name__ == "__main__":  # self-test (no API)
    clean = "We help contractors answer every call and book more jobs. Plain and specific."
    dirty = ("This robust, seamless platform will leverage synergy to elevate your business — "
             "honestly, it's not just software, it's a testament to today's needs.")
    assert is_clean(clean), lint(clean)
    kinds = {f.kind for f in lint(dirty)}
    assert "em-dash" in kinds and "\"robust\"" in kinds and "\"leverage\"" in kinds, kinds
    assert "honest" in VOICE_RULE and "em-dash" in VOICE_RULE
    assert feedback(lint(dirty)).startswith("Your draft used banned")
    print("voice_lint.py self-test OK —", len(BANNED_WORDS), "words,", len(lint(dirty)), "findings on the dirty sample")
