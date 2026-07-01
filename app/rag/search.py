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
import re
import sys

import numpy as np

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/app on path → `from rag import`
if _APP not in sys.path:
    sys.path.insert(0, _APP)
from rag import embed, store   # noqa: E402

DEFAULT_K = 5
CANDIDATE_K = 20        # how many each retriever contributes to the merge pool before rerank
RRF_K = 60              # Reciprocal Rank Fusion constant (the standard default; see hybrid_search)
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


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
                    collection: str | None = None, mock: bool = False, key: str | None = None,
                    model: str = embed.DEFAULT_MODEL) -> list[dict]:
    """Top-k chunks most similar in MEANING to `query`. Returns hit dicts:
        {chunk_id, doc_id, ord, text, score}   (score = cosine similarity, higher = closer)
    Embeds the query in the SAME mode/model as the chunks (mock-vs-real and dims must match)."""
    rows = store.embedded_chunks(doc_id, collection)
    if not rows:
        return []
    qvec = np.asarray(embed.embed_query(query, mock=mock, key=key, model=model), dtype=np.float32)
    rows = [r for r in rows if r["vector"] is not None and r["vector"].shape == qvec.shape]
    if not rows:
        return []   # all stored vectors are a different dim → embedded under a different mode/model
    mat = np.vstack([r["vector"] for r in rows])
    return [{"chunk_id": rows[i]["id"], "doc_id": rows[i]["doc_id"], "ord": rows[i]["ord"],
             "text": rows[i]["text"], "score": score} for i, score in _cosine_topk(qvec, mat, k)]


def keyword_search(query: str, k: int = DEFAULT_K, *, doc_id: str | None = None,
                   collection: str | None = None) -> list[dict]:
    """Top-k chunks by KEYWORD relevance (BM25 via SQLite FTS5; a term-frequency fallback where FTS5
    isn't compiled in). Same hit shape as semantic_search; score is higher=better.

    WHY KEEP THIS ALONGSIDE SEMANTIC SEARCH
        Semantic search is fuzzy on exact tokens — a product name, an error code like 'E-4021', a rare
        bit of jargon — because those get averaged into the chunk's meaning vector. BM25 matches the
        literal term and rewards it precisely (rare words count more, see store.keyword_match). It's
        blind to synonyms, which is exactly semantic search's strength — so stage 4 runs BOTH and
        merges them. That complementarity is the whole point of 'hybrid'.

    BM25, in one breath: score a chunk by how many query terms it contains (term frequency, with
    diminishing returns), weighting RARE terms more than common ones (inverse document frequency), and
    discounting long chunks so they don't win just by being long. SQLite's bm25() does all of this.
    """
    return store.keyword_match(query, k, doc_id=doc_id, collection=collection)


# ── Hybrid merge (stage 4) ───────────────────────────────────────────────────
def hybrid_search(query: str, k: int = CANDIDATE_K, *, doc_id: str | None = None,
                  collection: str | None = None, mock: bool = False, key: str | None = None,
                  model: str = embed.DEFAULT_MODEL) -> list[dict]:
    """Run BOTH retrievers and merge their ranked lists into one with Reciprocal Rank Fusion (RRF).

    WHY NOT JUST ADD THE SCORES
        Semantic search returns cosine scores (~0..1); BM25 returns its own unbounded scale. They're
        not comparable, so summing them lets whichever scale happens to be larger dominate. RRF sidesteps
        this by throwing the raw scores away and using only RANK POSITION:
            rrf(chunk) = Σ  1 / (RRF_K + rank_in_that_list)      # rank is 1-based
        A chunk near the top of EITHER list gets a solid contribution; a chunk near the top of BOTH
        rises above everything. RRF_K (60, the standard) softens the gap between ranks 1 and 2 so a
        single list can't completely dictate the order. Parameter-light and robust — the reason it's
        the default hybrid-merge method.

    Returns hit dicts: {chunk_id, doc_id, ord, text, score (=rrf), found_by:[...], ranks:{...}}.
    """
    sem = semantic_search(query, k, doc_id=doc_id, collection=collection, mock=mock, key=key, model=model)
    kw = keyword_search(query, k, doc_id=doc_id, collection=collection)
    pool: dict[str, dict] = {}
    for source, hits in (("semantic", sem), ("keyword", kw)):
        for rank0, h in enumerate(hits):
            e = pool.get(h["chunk_id"])
            if e is None:
                e = pool[h["chunk_id"]] = {
                    "chunk_id": h["chunk_id"], "doc_id": h["doc_id"], "ord": h["ord"],
                    "text": h["text"], "score": 0.0, "found_by": [], "ranks": {}}
            e["score"] += 1.0 / (RRF_K + rank0 + 1)
            e["found_by"].append(source)
            e["ranks"][source] = rank0 + 1
    merged = sorted(pool.values(), key=lambda e: -e["score"])
    return merged[:k]


# ── Rerank (stage 4) ─────────────────────────────────────────────────────────
def rerank(query: str, hits: list[dict], top_n: int = DEFAULT_K, *,
           mock: bool = False, key: str | None = None, model=None) -> list[dict]:
    """Reorder candidate `hits` by how well each actually answers `query`, keep the top_n.

    WHAT RERANKING ADDS
        The hybrid merge gives a good candidate SET, but its order is rank arithmetic, not
        comprehension — it doesn't read the chunk against the question. A reranker does: it judges each
        candidate's relevance directly, which fixes precision at the very top (where it matters, because
        only the top few reach the answer model). We use an LLM-judge reranker — the same lever FILG
        already trusts for its source-credibility gate (an LLM scoring each item) — routed through
        pipeline.call so it rides the user's chosen model stack / cost tier. (Cohere Rerank is a clean
        future swap behind this same function; LLM-judge means no new vendor, key, or dependency.)

    mock=True scores by lexical overlap (deterministic, no API) — a crude stand-in good enough to prove
    the reorder happens and to keep tests free.
    """
    if not hits:
        return []
    if mock:
        scored = sorted(hits, key=lambda h: -_overlap(query, h["text"]))
        for h in scored:
            h["rerank_score"] = float(_overlap(query, h["text"]))
        return scored[:top_n]

    from pipeline import LEDGER, call, HAIKU, extract_json   # heavy; real mode only
    _ = LEDGER  # cost is read by the caller via LEDGER.cost_slice around the retrieve/answer block
    listing = "\n\n".join(f"[{i}] {h['text']}" for i, h in enumerate(hits))
    out = call("rag_rerank", HAIKU, max_tokens=30 + 8 * len(hits), prompt=(
        "You are a retrieval reranker. Score how well EACH passage below helps answer the QUESTION, "
        "from 0 (irrelevant) to 10 (directly answers it). Judge relevance only, not writing quality.\n\n"
        f"QUESTION: {query}\n\nPASSAGES:\n{listing}\n\n"
        'Reply ONLY with a JSON array, one item per passage: [{"i": 0, "score": 8}, ...].'))
    data = extract_json(out)
    scores: dict[int, float] = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "i" in item:
                try:
                    scores[int(item["i"])] = float(item.get("score", 0))
                except (TypeError, ValueError):
                    continue
    if not scores:                                  # unparseable judge reply → keep the RRF order
        return hits[:top_n]
    order = sorted(range(len(hits)), key=lambda i: -scores.get(i, -1.0))
    out_hits = []
    for i in order[:top_n]:
        h = dict(hits[i])
        h["rerank_score"] = scores.get(i, 0.0)
        out_hits.append(h)
    return out_hits


def _overlap(query: str, text: str) -> int:
    """Count of distinct query words that appear in `text` (the mock reranker's relevance proxy)."""
    q = set(_TOKEN_RE.findall(query.lower()))
    return sum(1 for w in set(_TOKEN_RE.findall(text.lower())) if w in q)


def retrieve(query: str, k: int = DEFAULT_K, *, candidate_k: int = CANDIDATE_K,
             doc_id: str | None = None, collection: str | None = None, mock: bool = False,
             key: str | None = None, model: str = embed.DEFAULT_MODEL) -> list[dict]:
    """The full retrieval entrypoint: hybrid-merge a candidate pool, then rerank down to the top k.
    This is what stage 5 (answer generation) calls."""
    cands = hybrid_search(query, candidate_k, doc_id=doc_id, collection=collection,
                          mock=mock, key=key, model=model)
    return rerank(query, cands, top_n=k, mock=mock, key=key, model=model)


def index_pending(*, doc_id: str | None = None, collection: str | None = None, mock: bool = False,
                  key: str | None = None, model: str = embed.DEFAULT_MODEL) -> int:
    """Embed every chunk that doesn't have a vector yet and persist it. Returns how many were embedded.
    Idempotent: ingest documents, call this, and they become searchable; re-running is a no-op."""
    pending = store.chunks_missing_embeddings(doc_id, collection)
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

    # keyword/BM25: nails an exact rare token that semantic search would smear
    store.clear()
    store.add_document("codes", [
        "To reset, restart the device and clear the cache.",
        "Error E-4021 indicates a failed authentication token.",
        "Our billing office is located in downtown Denver.",
    ])
    kw = keyword_search("E-4021", k=2)
    assert kw and kw[0]["ord"] == 1, kw                  # the error-code chunk

    # hybrid + rerank: a chunk found by BOTH retrievers fuses to the top
    store.clear()
    store.add_document("kb", [
        "Reset the router by holding the button for ten seconds.",      # semantic-ish for "restart"
        "Error E-4021 means the router failed to authenticate.",        # matches "router" AND "E-4021"
        "The cafeteria serves lunch from noon until two.",              # unrelated
    ])
    index_pending(mock=True)
    hy = hybrid_search("router E-4021 problem", k=5, mock=True)
    assert hy[0]["ord"] == 1 and "semantic" in hy[0]["found_by"] and "keyword" in hy[0]["found_by"], hy
    top = retrieve("router E-4021 problem", k=2, mock=True)
    assert top and top[0]["ord"] == 1 and "rerank_score" in top[0]
    store.clear()
    print(f"rag/search.py self-test OK — semantic + keyword/BM25 + hybrid RRF "
          f"(E-4021 chunk found_by={hy[0]['found_by']}) + rerank; fts={store.fts_enabled()}")
