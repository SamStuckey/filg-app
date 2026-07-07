"""RAG stage 5 — grounded answer generation with citations."""

from app.rag import answer, search, store


def _seed():
    store.clear()
    store.add_document("Handbook", [
        "Employees accrue fifteen days of paid vacation per year.",
        "To reset your password, visit account settings and click reset.",
        "The office is closed on all federal holidays.",
    ], source="pasted")
    search.index_pending(mock=True)


# ── mock path ────────────────────────────────────────────────────────────────
def test_answer_is_grounded_and_carries_numbered_sources():
    _seed()
    res, cost = answer.answer("how many vacation days do employees get?", k=3, mock=True)
    assert res["sources"], res
    assert res["sources"][0]["ord"] == 0                 # the vacation chunk was retrieved first
    assert res["sources"][0]["title"] == "Handbook"      # provenance for the citation
    assert res["sources"][0]["n"] == 1
    assert 1 in res["cited"] and cost == 0.0


def test_empty_index_gives_honest_no_answer_without_calling_the_model():
    store.clear()
    res, cost = answer.answer("anything", k=3, mock=True)
    assert res["answer"] == answer._NO_ANSWER and res["sources"] == [] and cost == 0.0


# ── real generation path via patch_call ──────────────────────────────────────
# Retrieval is mocked (isolating the generation/citation-parsing layer) so these don't hit the real
# embeddings API; patch_call replaces the answer LLM call.
def test_real_answer_parses_citations_from_model_output(patch_call, monkeypatch):
    _seed()
    hits = search.retrieve("vacation days?", k=3, mock=True)
    monkeypatch.setattr(search, "retrieve", lambda *a, **k: hits)
    patch_call("Employees get fifteen days of paid vacation per year [1].")
    res, cost = answer.answer("vacation days?", k=3, mock=False)
    assert res["cited"] == [1]
    assert res["used_chunks"] == len(res["sources"]) >= 1


def test_cited_ignores_out_of_range_markers(patch_call, monkeypatch):
    _seed()
    hits = search.retrieve("vacation days?", k=3, mock=True)
    monkeypatch.setattr(search, "retrieve", lambda *a, **k: hits)
    patch_call("This cites a real source [1] and a bogus one [99].")
    res, _ = answer.answer("vacation days?", k=3, mock=False)
    assert 1 in res["cited"] and 99 not in res["cited"]   # only in-range source numbers count


def test_doc_scoped_answer():
    store.clear()
    a = store.add_document("A", ["the cat sat on the mat"])
    store.add_document("B", ["quarterly revenue grew in europe"])
    search.index_pending(mock=True)
    res, _ = answer.answer("cat", k=5, doc_id=a, mock=True)
    assert res["sources"] and all(s["doc_id"] == a for s in res["sources"])
