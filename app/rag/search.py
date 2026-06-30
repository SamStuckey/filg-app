#!/usr/bin/env python3
"""
search.py — retrieval over the indexed chunks.

Stage 2 (this file, first cut): SEMANTIC search — embed the query, find the nearest chunk vectors by
cosine similarity. Stage 3 adds keyword/BM25 search; stage 4 merges the two (hybrid) and reranks.

COSINE SIMILARITY — how "nearest in meaning space" is actually measured
    Each chunk and the query are vectors (points/arrows from the origin). Cosine similarity is the
    cosine of the ANGLE between two vectors: it ignores how LONG they are and looks only at the
    DIRECTION they point. Two texts about the same thing point the same way → angle ≈ 0 → cosine ≈ 1.
    Unrelated texts point in different directions → cosine ≈ 0. Opposite → -1.
        cos(q, c) = (q · c) / (|q| · |c|)        # dot product, divided by the two lengths
    We use the ANGLE, not raw distance, because vector length tracks incidental things (longer text,
    word frequency) that we don't want to dominate "is this about the same topic". Computing it for
    every chunk and taking the top-k is brute force — at this corpus size (hundreds–low thousands of
    chunks) that's sub-millisecond in numpy, so no vector-index dependency is needed yet.
"""

from __future__ import annotations

import os
import sys

import numpy as np

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/app on path → `from rag import`
if _APP not in sys.path:
    sys.path.insert(0, _APP)
from rag import embed, store   # noqa: E402

DEFAULT_K = 5


def _cosine_topk(qvec: np.ndarray, mat: np.ndarray, k: int) -> list[tuple[int, float]]:
    """Indices + cosine scores of the k rows of `mat` closest in direction to `qvec`. Normalizing both
    sides turns the cosine formula into a plain dot product."""
    q = qvec / (np.linalg.norm(qvec) or 1.0)
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    sims = (mat @ q) / norms
    k = min(k, len(sims))
    top = np.argpartition(-sims, k - 1)[:k]            # cheap top-k, then sort just those
    top = top[np.argsort(-sims[top])]
    return [(int(i), float(sims[i])) for i in top]


def semantic_search(query: str, k: int = DEFAULT_K, *, doc_id: str | None = None,
                    mock: bool = False, key: str | None = None,
                    model: str = embed.DEFAULT_MODEL) -> list[dict]:
    """Top-k chunks most similar in MEANING to `query`. Returns hit dicts:
        {chunk_id, doc_id, ord, text, score}   (score = cosine similarity, higher = closer)
    Embeds the query in the SAME mode/model as the chunks (mock-vs-real and dims must match)."""
    rows = store.embedded_chunks(doc_id)
    if not rows:
        return []
    qvec = np.asarray(embed.embed_query(query, mock=mock, key=key, model=model), dtype=np.float32)
    rows = [r for r in rows if r["vector"] is not None and r["vector"].shape == qvec.shape]
    if not rows:
        return []   # all stored vectors are a different dim → embedded under a different mode/model
    mat = np.vstack([r["vector"] for r in rows])
    return [{"chunk_id": rows[i]["id"], "doc_id": rows[i]["doc_id"], "ord": rows[i]["ord"],
             "text": rows[i]["text"], "score": score} for i, score in _cosine_topk(qvec, mat, k)]


def index_pending(*, doc_id: str | None = None, mock: bool = False, key: str | None = None,
                  model: str = embed.DEFAULT_MODEL) -> int:
    """Embed every chunk that doesn't have a vector yet and persist it. Returns how many were embedded.
    Idempotent: ingest documents, call this, and they become searchable; re-running is a no-op."""
    pending = store.chunks_missing_embeddings(doc_id)
    if not pending:
        return 0
    vectors = embed.embed_texts([c["text"] for c in pending], mock=mock, key=key, model=model)
    store.save_embeddings([(c["id"], v) for c, v in zip(pending, vectors)])
    return len(pending)


if __name__ == "__main__":  # self-test (mock embeddings, no API)
    store.clear()
    did = store.add_document("Animals & finance", [
        "The cat sat quietly on the warm mat by the window.",
        "Dogs are loyal animals that enjoy long walks outdoors.",
        "Quarterly revenue grew twelve percent across the European market.",
    ])
    n = index_pending(mock=True)
    assert n == 3 and not store.chunks_missing_embeddings()
    # NOTE: the MOCK embedding matches shared WORDS, not synonyms — so queries here reuse a keyword
    # from the target chunk. A real embedding model would match "sales"→"revenue" etc.; that synonym
    # power is exactly why production uses real embeddings (proven separately in embed.py).
    hits = semantic_search("where did the cat rest on the mat?", k=2, mock=True)
    assert hits and hits[0]["ord"] == 0, hits           # the cat chunk ranks first
    assert hits[0]["score"] >= hits[-1]["score"]        # sorted by similarity, descending
    fin = semantic_search("how much did revenue grow?", k=1, mock=True)
    assert fin[0]["ord"] == 2, fin                      # the revenue chunk (shares "revenue")
    assert index_pending(mock=True) == 0                # idempotent
    store.clear()
    print(f"rag/search.py self-test OK — cat→chunk0 ({hits[0]['score']:.3f}), revenue→chunk2 semantic top-k")
