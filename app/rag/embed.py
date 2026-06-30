#!/usr/bin/env python3
"""
embed.py — turn text into embedding vectors (RAG stage 2).

WHY THIS IS A SEPARATE PROVIDER
    Claude has NO embeddings API. Embedding (text → vector) is a different job from generation
    (prompt → text), so it needs its own model from a provider that offers one. We default to OpenAI's
    `text-embedding-3-small`: 1536 dims, ~$0.02 / 1M tokens (cheap), and it reuses the `openai` client
    the app ALREADY depends on for OpenRouter — so no new dependency. Voyage AI (Anthropic's
    recommended embeddings provider) is a clean future swap behind this same seam.

WHAT AN EMBEDDING IS (recap)
    A list of numbers that pins a piece of text to a location in "meaning space", arranged so that
    text with similar meaning lands at nearby points. Stage 2's whole trick: embed every chunk once at
    ingest, embed the question at query time, then "find relevant chunks" = "find the nearest chunk
    vectors" (see search.py for the cosine-distance part).

THE MOCK
    `mock=True` returns a DETERMINISTIC, pure-Python vector (no API, no spend, reproducible) so the
    entire retrieval pipeline is testable offline. It's a signed hashing bag-of-words embedding: each
    word bumps a fixed coordinate, so two texts that SHARE words get overlapping non-zero dims and thus
    higher cosine similarity. That's a crude stand-in for real semantics — enough to prove the
    plumbing and to write a meaningful "the query finds the right chunk" test, NOT enough to match true
    synonyms (real embeddings do that; that's the whole reason to use them in production).

BYOK
    `key` is the embeddings API key. Real path defaults to env `OPENAI_API_KEY`. Wiring a per-user key
    through keys.py (the same encrypted BYOK store the chat keys use) is a later integration step; this
    seam already accepts the key so that step is just plumbing.
"""

from __future__ import annotations

import hashlib
import math
import os
import re

DEFAULT_MODEL = "text-embedding-3-small"
MOCK_DIM = 256                                  # real model is 1536; the mock is smaller + pure-Python
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_EMBED_BATCH = 96                               # texts per API request (OpenAI accepts large batches)


def embed_texts(texts, *, mock: bool = False, key: str | None = None,
                model: str = DEFAULT_MODEL) -> list[list[float]]:
    """Embed a list of texts → a list of vectors (aligned to the input order). Empty in → empty out."""
    texts = list(texts)
    if not texts:
        return []
    if mock:
        return [_mock_vector(t) for t in texts]
    from openai import OpenAI   # heavy import, real mode only (mirrors the app's lazy-import pattern)
    client = OpenAI(api_key=key or os.environ.get("OPENAI_API_KEY"))
    out: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        batch = texts[i:i + _EMBED_BATCH]
        resp = client.embeddings.create(model=model, input=batch)
        # The API echoes order, but sort by index to be safe before extracting.
        out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
    return out


def embed_query(text: str, **kw) -> list[float]:
    """Embed a single query string → one vector. Same model/mode as the chunks it'll be compared to."""
    return embed_texts([text], **kw)[0]


def _mock_vector(text: str, dim: int = MOCK_DIM) -> list[float]:
    """Deterministic signed hashing embedding, L2-normalized. Pure Python (no numpy) so embed.py stays
    importable and testable with zero deps."""
    vec = [0.0] * dim
    for tok in _TOKEN_RE.findall(text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0 if (h >> 8) & 1 else -1.0   # sign spreads tokens, reduces collisions
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


if __name__ == "__main__":  # self-test (mock only, no API)
    a, b, c = _mock_vector("the cat sat on the mat"), _mock_vector("a cat sat on a mat"), \
        _mock_vector("quarterly revenue grew in europe")
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert len(a) == MOCK_DIM and abs(dot(a, a) - 1.0) < 1e-6      # normalized
    assert dot(a, b) > dot(a, c)                                   # shared words → higher similarity
    assert embed_texts([]) == [] and len(embed_texts(["x", "y"], mock=True)) == 2
    assert embed_query("hello world", mock=True) == _mock_vector("hello world")
    print(f"embed.py self-test OK — mock dim={MOCK_DIM}; shared-word sim {dot(a, b):.3f} "
          f"> unrelated {dot(a, c):.3f}")
