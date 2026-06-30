#!/usr/bin/env python3
"""
ingest.py — turn raw documents into retrievable chunks (RAG stage 1).

Pure and deterministic: no API calls, no DB. Text in, list-of-chunks out. Persistence lives in
store.py; embeddings live in embed.py. Keeping this layer pure means the whole chunking strategy is
unit-testable with zero spend.

WHY CHUNK AT ALL
    Retrieval works on a *unit*. A whole document is the wrong unit: it's too big to embed into one
    meaningful vector (every topic in it averages together into mush) and too big to hand the answer
    model (cost + irrelevance). So we cut documents into bite-sized passages and retrieve those.

THE TWO KNOBS, AND THE TRADEOFFS
    target_chars  — how big each chunk is. (We measure in CHARACTERS, not tokens, to avoid a
                    tokenizer dependency. Rule of thumb: ~4 chars ≈ 1 token, so 1000 chars ≈ 250
                    tokens.)
        too BIG   → one chunk covers several ideas, so its embedding is a blurry average and matches
                    weakly; retrieved text is mostly irrelevant filler, diluting the answer + costing
                    tokens.
        too SMALL → a chunk loses the context that makes it make sense (a sentence whose subject was
                    in the previous one); a single answer gets split across chunks so no one chunk is
                    a strong match.
        sweet spot for prose: a few hundred words (~800-1200 chars). Default 1000.

    overlap_chars — how much of the end of one chunk is repeated at the start of the next.
        WHY       → chunk boundaries are arbitrary; a fact can straddle one. Without overlap the half
                    that lands in chunk B has lost its lead-in from chunk A, so neither chunk answers
                    the question well. Overlap makes the straddling fact land intact in at least one
                    chunk.
        COST      → duplicated text → more chunks → more embedding cost, and the same passage can
                    surface twice in results (we dedupe downstream). Keep it ~10-20% of target.
                    Default 150 (15%).

BOUNDARIES
    We cut on natural seams first (paragraph, then word) rather than mid-word, so each chunk reads as
    coherent prose. Paragraphs that are themselves bigger than target get hard-wrapped at word
    boundaries.
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_TARGET_CHARS = 1000
DEFAULT_OVERLAP_CHARS = 150

# Plain-text document types we ingest in v1. PDF/URL ingestion is a deliberate later add (it needs a
# parser dep / network), so the first build stays dependency-light and demonstrable.
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text"}


def normalize(text: str) -> str:
    """Tidy whitespace so chunking is stable: unify newlines, drop trailing spaces, collapse runs of
    blank lines to a single paragraph break. Does NOT touch the words themselves."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)      # strip trailing whitespace per line
    text = re.sub(r"\n{3,}", "\n\n", text)      # 3+ blank lines → one paragraph break
    return text.strip()


def _word_cut(s: str, limit: int) -> int:
    """The largest cut index <= limit that falls on a space (a word boundary). Falls back to a hard
    cut at limit only when there's no usable space in the back half (avoids tiny slivers)."""
    if len(s) <= limit:
        return len(s)
    sp = s.rfind(" ", 0, limit)
    return sp if sp > limit // 2 else limit


def _hardwrap(para: str, target: int) -> list[str]:
    """Break one over-long paragraph into <=target pieces at word boundaries."""
    out, s = [], para
    while len(s) > target:
        cut = _word_cut(s, target)
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    if s:
        out.append(s)
    return out


def _overlap_tail(s: str, overlap: int) -> str:
    """The last ~`overlap` chars of `s`, trimmed forward to the next word boundary so the carried-over
    text starts on a whole word. Empty when overlap is off."""
    if overlap <= 0 or len(s) <= overlap:
        return s if 0 < overlap else ""
    tail = s[-overlap:]
    sp = tail.find(" ")
    return tail[sp + 1:] if sp != -1 else tail


def chunk_text(text: str, target_chars: int = DEFAULT_TARGET_CHARS,
               overlap_chars: int = DEFAULT_OVERLAP_CHARS) -> list[str]:
    """Split `text` into overlapping, paragraph-aware chunks of about `target_chars` each.

    Strategy: split into paragraphs, hard-wrap any paragraph bigger than target, then greedily pack
    paragraphs into chunks until the next one wouldn't fit. When a chunk closes, the next one starts
    with an overlap tail of the one before it.
    """
    text = normalize(text)
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[str] = []
    for p in paras:
        units.extend([p] if len(p) <= target_chars else _hardwrap(p, target_chars))

    chunks: list[str] = []
    cur = ""
    for u in units:
        if cur and len(cur) + 2 + len(u) > target_chars:   # +2 for the "\n\n" join
            chunks.append(cur)
            tail = _overlap_tail(cur, overlap_chars)
            cur = f"{tail}\n\n{u}".strip() if tail else u
        else:
            cur = f"{cur}\n\n{u}".strip() if cur else u
    if cur:
        chunks.append(cur)
    return chunks


def read_text_files(paths) -> list[tuple[str, str]]:
    """Read plain-text/markdown files → list of (title, text). The title defaults to the filename
    stem. Silently skips paths that aren't existing text files (so a mixed list is fine)."""
    docs: list[tuple[str, str]] = []
    for p in paths:
        path = Path(p)
        if path.suffix.lower() in TEXT_SUFFIXES and path.is_file():
            docs.append((path.stem, path.read_text(encoding="utf-8", errors="replace")))
    return docs


if __name__ == "__main__":  # self-test (no API, no DB)
    sample = (
        "FILG turns a plain-text business idea into a sellable offer.\n\n"
        "The source-credibility gate is the moat: it grades vendor stats instead of "
        "laundering them as fact.\n\n"
        + ("word " * 400)        # a deliberately over-long paragraph → must hard-wrap
    )
    cs = chunk_text(sample, target_chars=300, overlap_chars=60)
    assert len(cs) >= 3, cs
    assert all(len(c) <= 300 + 60 + 20 for c in cs), [len(c) for c in cs]   # ~target (+overlap slack)
    # overlap: the tail of one chunk should reappear at the head of the next somewhere
    assert any(cs[i][-20:].split() and cs[i].split()[-1] in cs[i + 1] for i in range(len(cs) - 1))
    assert chunk_text("") == [] and chunk_text("   \n\n  ") == []
    one = chunk_text("short doc", target_chars=1000)
    assert one == ["short doc"]
    print(f"ingest.py self-test OK — {len(cs)} chunks from sample; empty→[]; short→1 chunk")
