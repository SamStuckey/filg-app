"""RAG stage 2 — embeddings (mock), the BLOB vector store, and semantic top-k search."""

from rag import embed, ingest, search, store


# ── embeddings ───────────────────────────────────────────────────────────────
def test_mock_embeddings_are_deterministic_and_normalized():
    v1 = embed.embed_query("the quick brown fox", mock=True)
    v2 = embed.embed_query("the quick brown fox", mock=True)
    assert v1 == v2                                   # deterministic → reproducible tests
    assert abs(sum(x * x for x in v1) ** 0.5 - 1.0) < 1e-6   # L2-normalized


def test_shared_words_score_higher_than_unrelated():
    dot = lambda a, b: sum(i * j for i, j in zip(a, b))
    cat_a = embed.embed_query("the cat sat on the mat", mock=True)
    cat_b = embed.embed_query("a cat on a mat", mock=True)
    fin = embed.embed_query("annual revenue and profit margins", mock=True)
    assert dot(cat_a, cat_b) > dot(cat_a, fin)


def test_empty_input_returns_empty():
    assert embed.embed_texts([]) == []


# ── vector store ─────────────────────────────────────────────────────────────
def test_embeddings_round_trip_through_blob():
    store.clear()
    did = store.add_document("d", ["alpha", "beta"])
    assert store.chunks_missing_embeddings(did) and not store.embedded_chunks(did)
    search.index_pending(mock=True)
    emb = store.embedded_chunks(did)
    assert len(emb) == 2 and not store.chunks_missing_embeddings(did)
    assert emb[0]["dim"] == embed.MOCK_DIM and emb[0]["vector"] is not None


# ── semantic search ──────────────────────────────────────────────────────────
def _seed():
    store.clear()
    store.add_document("mixed", [
        "The cat sat quietly on the warm mat by the window.",
        "Dogs are loyal animals that enjoy long walks outdoors.",
        "Quarterly revenue grew twelve percent across the European market.",
    ])
    search.index_pending(mock=True)


def test_semantic_search_finds_the_relevant_chunk():
    # Mock embeddings match shared WORDS (not synonyms), so test queries reuse a target keyword.
    _seed()
    hits = search.semantic_search("where did the cat rest on the mat?", k=2, mock=True)
    assert hits[0]["ord"] == 0
    assert hits[0]["score"] >= hits[-1]["score"]      # descending by similarity
    assert search.semantic_search("how much did revenue grow?", k=1, mock=True)[0]["ord"] == 2


def test_k_caps_results_and_empty_index_is_safe():
    _seed()
    assert len(search.semantic_search("anything", k=2, mock=True)) == 2
    store.clear()
    assert search.semantic_search("anything", k=5, mock=True) == []


def test_index_pending_is_idempotent():
    _seed()
    assert search.index_pending(mock=True) == 0       # everything already embedded


def test_doc_scoped_search():
    store.clear()
    a = store.add_document("A", ["the cat sat on the mat"])
    store.add_document("B", ["quarterly revenue grew in europe"])
    search.index_pending(mock=True)
    hits = search.semantic_search("cat", k=5, doc_id=a, mock=True)
    assert hits and all(h["doc_id"] == a for h in hits)
