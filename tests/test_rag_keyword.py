"""RAG stage 3 — keyword/BM25 search via SQLite FTS5 (with fallback)."""

from rag import search, store


def _seed():
    store.clear()
    store.add_document("kb", [
        "To reset the unit, restart the device and clear the cache.",
        "Error E-4021 indicates a failed authentication token.",
        "Our billing office is located in downtown Denver, Colorado.",
    ])


def test_keyword_finds_exact_rare_term():
    _seed()
    hits = search.keyword_search("E-4021", k=3)
    assert hits and hits[0]["ord"] == 1               # the error-code chunk, which semantic would smear


def test_scores_are_descending_higher_is_better():
    _seed()
    hits = search.keyword_search("device cache reset", k=3)
    assert hits[0]["ord"] == 0
    assert all(hits[i]["score"] >= hits[i + 1]["score"] for i in range(len(hits) - 1))


def test_no_match_and_empty_query_return_empty():
    _seed()
    assert search.keyword_search("zzzqqq-nomatch", k=5) == []
    assert search.keyword_search("", k=5) == []
    assert search.keyword_search("   ?!  ", k=5) == []   # punctuation only → no tokens


def test_special_chars_in_query_do_not_crash_fts():
    _seed()
    # raw FTS operators / quotes in user text must be treated as literals, not syntax
    assert isinstance(search.keyword_search('Denver AND "office" OR (token)', k=3), list)


def test_doc_scoped_keyword_search():
    store.clear()
    a = store.add_document("A", ["error E-4021 failed token"])
    store.add_document("B", ["error E-4021 different document"])
    hits = search.keyword_search("E-4021", k=5, doc_id=a)
    assert hits and all(h["doc_id"] == a for h in hits)


def test_fts_index_stays_in_sync_on_delete():
    store.clear()
    a = store.add_document("A", ["unique-marker-xyz appears here"])
    assert search.keyword_search("unique-marker-xyz", k=5)
    store.delete_document(a)
    assert search.keyword_search("unique-marker-xyz", k=5) == []   # index cleaned up with the doc
