"""RAG stage 1 — chunking strategy + document/chunk persistence."""

from rag import ingest, store


# ── chunking ─────────────────────────────────────────────────────────────────
def test_short_text_is_one_chunk():
    assert ingest.chunk_text("a short note", target_chars=1000) == ["a short note"]


def test_empty_or_whitespace_yields_no_chunks():
    assert ingest.chunk_text("") == []
    assert ingest.chunk_text("   \n\n\t  ") == []


def test_chunks_respect_target_size_with_overlap_slack():
    text = "\n\n".join(f"Paragraph {i}. " + ("filler words here " * 6) for i in range(40))
    chunks = ingest.chunk_text(text, target_chars=400, overlap_chars=80)
    assert len(chunks) > 1
    # each chunk is about target; overlap + a trailing unit can push slightly past, never wildly
    assert all(len(c) <= 400 + 80 + 40 for c in chunks), [len(c) for c in chunks]


def test_oversized_paragraph_is_hard_wrapped():
    giant = "word " * 500   # one ~2500-char paragraph, no blank lines
    chunks = ingest.chunk_text(giant, target_chars=300, overlap_chars=0)
    assert len(chunks) >= 8
    assert all(len(c) <= 300 + 20 for c in chunks)


def test_overlap_carries_context_across_the_boundary():
    text = "\n\n".join(f"Sentence number {i} about widgets and gizmos." for i in range(30))
    with_ov = ingest.chunk_text(text, target_chars=200, overlap_chars=60)
    no_ov = ingest.chunk_text(text, target_chars=200, overlap_chars=0)
    # overlap repeats text → at least as many chunks, and consecutive chunks share a word run
    assert len(with_ov) >= len(no_ov)
    shared = any(set(with_ov[i].split()) & set(with_ov[i + 1].split()) for i in range(len(with_ov) - 1))
    assert shared


def test_normalize_collapses_blank_lines_and_trailing_space():
    assert ingest.normalize("a   \n\n\n\nb") == "a\n\nb"


# ── persistence ──────────────────────────────────────────────────────────────
def test_ingest_round_trip_persists_chunks():
    store.clear()
    chunks = ingest.chunk_text("First idea here.\n\nSecond idea, different topic.", target_chars=30)
    doc_id = store.add_document("My doc", chunks, source="pasted")
    got = store.get_document(doc_id)
    assert got["title"] == "My doc" and got["n_chunks"] == len(chunks)
    persisted = store.list_chunks(doc_id)
    assert [c["text"] for c in persisted] == chunks
    assert all(c["id"] == f"{doc_id}:{c['ord']}" for c in persisted)   # deterministic, citation-ready


def test_counts_and_delete_scope_correctly():
    store.clear()
    a = store.add_document("A", ["x", "y"])
    store.add_document("B", ["z"])
    assert store.counts() == {"documents": 2, "chunks": 3}
    store.delete_document(a)
    assert store.counts() == {"documents": 1, "chunks": 1}
    assert store.get_document(a) is None
