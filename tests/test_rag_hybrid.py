"""RAG stage 4 — hybrid merge (RRF) + rerank."""

from app.rag import search, store


def _seed():
    store.clear()
    store.add_document("kb", [
        "Reset the router by holding the button for ten seconds.",
        "Error E-4021 means the router failed to authenticate.",
        "The cafeteria serves lunch from noon until two o'clock.",
    ])
    search.index_pending(mock=True)


# ── RRF merge ────────────────────────────────────────────────────────────────
def test_hybrid_fuses_a_chunk_found_by_both_retrievers_to_the_top():
    _seed()
    hits = search.hybrid_search("router E-4021 problem", k=5, mock=True)
    top = hits[0]
    assert top["ord"] == 1                                  # the chunk both retrievers like
    assert set(top["found_by"]) == {"semantic", "keyword"}
    assert "semantic" in top["ranks"] and "keyword" in top["ranks"]


def test_rrf_scores_are_descending():
    _seed()
    hits = search.hybrid_search("router reset button", k=5, mock=True)
    assert all(hits[i]["score"] >= hits[i + 1]["score"] for i in range(len(hits) - 1))


def test_hybrid_dedupes_chunks_across_lists():
    _seed()
    hits = search.hybrid_search("router E-4021", k=10, mock=True)
    ids = [h["chunk_id"] for h in hits]
    assert len(ids) == len(set(ids))                        # no chunk appears twice after the merge


# ── rerank ───────────────────────────────────────────────────────────────────
def test_rerank_truncates_to_top_n_and_tags_scores():
    _seed()
    cands = search.hybrid_search("router authentication error", k=5, mock=True)
    ranked = search.rerank("router authentication error", cands, top_n=2, mock=True)
    assert len(ranked) == 2
    assert all("rerank_score" in h for h in ranked)
    assert ranked[0]["rerank_score"] >= ranked[1]["rerank_score"]


def test_rerank_empty_is_safe():
    assert search.rerank("q", [], top_n=5, mock=True) == []


# ── real rerank path (LLM-judge JSON), exercised via patch_call ──────────────
def test_real_rerank_applies_judge_scores(patch_call):
    _seed()
    cands = search.hybrid_search("router error", k=3, mock=True)
    n = len(cands)
    # judge gives the LAST candidate the highest score → the order must reverse
    patch_call("[" + ",".join('{"i": %d, "score": %d}' % (i, i) for i in range(n)) + "]")
    ranked = search.rerank("router error", cands, top_n=n, mock=False)
    assert ranked[0]["chunk_id"] == cands[-1]["chunk_id"]
    assert ranked[0]["rerank_score"] >= ranked[-1]["rerank_score"]


def test_real_rerank_falls_back_to_rrf_on_unparseable_reply(patch_call):
    _seed()
    cands = search.hybrid_search("router error", k=3, mock=True)
    patch_call("the model rambled and returned no json")
    ranked = search.rerank("router error", cands, top_n=3, mock=False)
    assert [h["chunk_id"] for h in ranked] == [c["chunk_id"] for c in cands[:3]]   # RRF order preserved


# ── full retrieve entrypoint ─────────────────────────────────────────────────
def test_retrieve_returns_reranked_top_k():
    _seed()
    hits = search.retrieve("how do I fix the E-4021 router error?", k=2, mock=True)
    assert len(hits) <= 2 and hits[0]["ord"] == 1
    assert "rerank_score" in hits[0]
