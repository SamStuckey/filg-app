"""RAG integration — method-corpus grounding + collection scoping + the planner hook."""

from app import planner
from app.rag import grounding, search, store


# ── collection scoping in the store/search layer ─────────────────────────────
def test_collection_scopes_retrieval():
    store.clear()
    store.add_document("Method", ["price on the outcome not the hour"], collection="method")
    store.add_document("Other", ["the cafeteria serves lunch at noon"], collection="default")
    search.index_pending(mock=True)
    hits = search.retrieve("pricing", k=5, collection="method", mock=True)
    assert hits and all(h["doc_id"] != "" for h in hits)
    assert all("cafeteria" not in h["text"] for h in hits)          # default-collection doc stays out


# ── grounding helper ─────────────────────────────────────────────────────────
def test_grounding_is_noop_on_empty_corpus():
    store.clear()
    assert grounding.has_method_corpus() is False
    assert grounding.method_grounding("anything", mock=True) == ("", [], 0.0)


def test_grounding_returns_cited_method_block():
    store.clear()
    store.add_document("Pricing a Productized Service", [
        "Price on the outcome, not the hour: a fixed monthly fee beats hourly for productized work.",
        "Anchor with three tiers; buyers pick the middle, so design the middle to be your target.",
    ], collection=grounding.METHOD_COLLECTION)
    search.index_pending(collection=grounding.METHOD_COLLECTION, mock=True)
    block, sources, cost = grounding.method_grounding("how should I price my tiers?", mock=True)
    assert "METHOD PLAYBOOK" in block and 'method: "Pricing a Productized Service"' in block
    assert sources and sources[0]["title"] == "Pricing a Productized Service" and cost == 0.0


def test_ingest_method_dir_loads_real_and_skips_readme_and_skeletons(tmp_path):
    store.clear()
    (tmp_path / "README.md").write_text("# readme\nnot method")
    (tmp_path / "real.md").write_text(
        "# Pricing\n\nPrice on the outcome, not the hour.\n\nAnchor three tiers around the middle.")
    (tmp_path / "draft.md").write_text(
        "<!-- METHOD SKELETON -->\n# Draft\n\n▶ write the durable pricing method here")
    stats = grounding.ingest_method_dir(path=str(tmp_path), mock=True)
    assert stats["documents"] == 1 and stats["chunks"] >= 1 and stats["embedded"] == stats["chunks"]
    assert [d["title"] for d in store.list_documents()] == ["real"]   # README + un-filled skeleton skipped


def test_shipped_method_corpus_is_all_skeletons(tmp_path):
    # The committed corpus should ship as skeletons only — nothing ingests until an author fills one.
    store.clear()
    stats = grounding.ingest_method_dir(mock=True)          # the real app/rag/corpus/method dir
    assert stats["chunks"] == 0 and not grounding.has_method_corpus()


# ── the planner hook (real path, LLM mocked via patch_call) ──────────────────
def _capture(patch_call):
    seen = {}
    patch_call(lambda stage, prompt: seen.setdefault("prompt", prompt) or "draft text")
    return seen


def test_planner_injects_method_block_when_corpus_present(patch_call, monkeypatch):
    # monkeypatch grounding so the planner's real path doesn't hit real embeddings; we assert the hook
    # concatenates whatever grounding returns into the synth prompt.
    monkeypatch.setattr(grounding, "method_grounding",
                        lambda *a, **k: ("\n\nMETHOD PLAYBOOK (x):\n- price on the outcome "
                                         '[method: "Pricing"]', [{"n": 1}], 0.0))
    seen = _capture(patch_call)
    planner.propose("a bookkeeping service for dentists", "pricing", {"rows": []}, [], mock=False)
    assert "METHOD PLAYBOOK" in seen["prompt"] and 'method: "Pricing"' in seen["prompt"]


def test_planner_prompt_unchanged_when_no_method_corpus(patch_call):
    store.clear()                                           # empty corpus → grounding self-disables
    seen = _capture(patch_call)
    planner.propose("a bookkeeping service for dentists", "pricing", {"rows": []}, [], mock=False)
    assert "METHOD PLAYBOOK" not in seen["prompt"]          # real hook, no corpus, no embeddings call
