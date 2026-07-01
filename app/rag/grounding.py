#!/usr/bin/env python3
"""
grounding.py — cited METHOD-corpus grounding for the planner (RAG integration).

The planner's synth already runs on the `synth_section` SKILL (curated instruction) + the gate-graded
RESEARCH. This adds a THIRD input, when a curated methodology corpus has been ingested: the passages
from that corpus most relevant to the section being written, injected as a cited block the synth can
lean on.

SELF-DISABLING BY DESIGN: with no method corpus ingested, `method_grounding()` returns an empty block
and the synth prompt is byte-for-byte unchanged. So the integration is safe in production the moment
it ships — it does nothing until someone loads a method corpus into the `method` collection.

WHY RAG HERE (not just a bigger skill):
  - A real methodology library is far too large to paste into every call — retrieval fetches only the
    relevant slice per section.
  - The FILG-specific reason: retrieved method passages come WITH A SOURCE, so the synth can cite them
    — which matches the credibility-gate ethos (grounded + attributable, not asserted). A skill shapes
    HOW the model reasons; this injects SPECIFIC, cited method it can draw on.

The corpus CONTENT is the actual IP and is authored/curated separately (see `corpus/method/`, which
currently holds a labeled PLACEHOLDER seed only — swap it for real method). This module is the plumbing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/app on path → `from rag import`
if _APP not in sys.path:
    sys.path.insert(0, _APP)
from rag import embed, ingest, search, store   # noqa: E402

METHOD_COLLECTION = "method"
DEFAULT_METHOD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus", "method")
DEFAULT_K = 3


def has_method_corpus(collection: str = METHOD_COLLECTION) -> bool:
    """True iff any chunks are loaded in the method collection — the guard that makes grounding a
    no-op until a corpus exists."""
    return store.counts(collection)["chunks"] > 0


def method_grounding(query: str, k: int = DEFAULT_K, *, collection: str = METHOD_COLLECTION,
                     mock: bool = False, key: str | None = None,
                     model: str = embed.DEFAULT_MODEL) -> tuple[str, list[dict], float]:
    """Retrieve the top method passages for `query` and format them as a cited block for a synth
    prompt. Returns (block, sources, cost). Empty corpus or no hits → ("", [], 0.0), so the caller can
    concatenate the block unconditionally and the prompt is unchanged when there's nothing to add."""
    if not has_method_corpus(collection):
        return "", [], 0.0
    start = None
    if not mock:
        from pipeline import LEDGER   # meter the rerank spend; heavy import, real mode only
        start = len(LEDGER.rows)
    hits = search.retrieve(query, k, collection=collection, mock=mock, key=key, model=model)
    if not hits:
        return "", [], 0.0

    titles: dict[str, str] = {}
    sources = []
    for i, h in enumerate(hits):
        did = h["doc_id"]
        if did not in titles:
            titles[did] = (store.get_document(did) or {}).get("title") or did
        sources.append({"n": i + 1, "chunk_id": h["chunk_id"], "title": titles[did], "text": h["text"]})

    lines = "\n".join(f'- {s["text"]}  [method: "{s["title"]}"]' for s in sources)
    block = ("\n\nMETHOD PLAYBOOK (FILG's curated methodology — draw on it where it fits THIS business "
             'and cite as [method: "<source title>"]; ignore any point that does not apply):\n' + lines)
    if mock:
        return block, sources, 0.0
    from pipeline import LEDGER
    return block, sources, round(LEDGER.cost_slice(start), 4)


def ingest_method_dir(path: str = DEFAULT_METHOD_DIR, *, collection: str = METHOD_COLLECTION,
                      mock: bool = False, key: str | None = None, model: str = embed.DEFAULT_MODEL,
                      target_chars: int = ingest.DEFAULT_TARGET_CHARS,
                      overlap_chars: int = ingest.DEFAULT_OVERLAP_CHARS) -> dict:
    """Load every .md/.txt file in `path` into the method collection, chunk, and embed. Returns
    {documents, chunks, embedded}. Idempotent-ish: it ADDS documents, so clear the collection first if
    re-ingesting the same files (see store.clear / delete_document)."""
    files = sorted(str(p) for p in Path(path).glob("*")
                   if p.is_file() and not p.name.lower().startswith("readme"))
    docs = ingest.read_text_files(files)
    for title, text in docs:
        store.add_document(title, ingest.chunk_text(text, target_chars, overlap_chars),
                           source=title, collection=collection)
    embedded = search.index_pending(collection=collection, mock=mock, key=key, model=model)
    c = store.counts(collection)
    return {"documents": c["documents"], "chunks": c["chunks"], "embedded": embedded}


if __name__ == "__main__":  # self-test (mock embeddings, no API)
    store.clear()
    assert not has_method_corpus() and method_grounding("pricing", mock=True) == ("", [], 0.0)
    store.add_document("Pricing a Productized Service", [
        "Price on the outcome, not the hour: a fixed monthly fee beats hourly for productized work.",
        "Anchor with three tiers; most buyers pick the middle, so design the middle to be your target.",
    ], source="seed", collection=METHOD_COLLECTION)
    search.index_pending(collection=METHOD_COLLECTION, mock=True)
    block, sources, cost = method_grounding("how should I price my monthly service tiers?", mock=True)
    assert block and "METHOD PLAYBOOK" in block and 'method: "Pricing a Productized Service"' in block
    assert sources and sources[0]["title"] == "Pricing a Productized Service" and cost == 0.0
    # scoping: a doc in another collection must NOT leak into method grounding
    store.add_document("Unrelated", ["the office cafeteria serves lunch at noon"],
                       collection="default")
    search.index_pending(collection="default", mock=True)
    b2, s2, _ = method_grounding("where is lunch served?", mock=True)
    assert all("cafeteria" not in s["text"] for s in s2), s2   # default-collection doc stays out
    store.clear()
    print(f"grounding.py self-test OK — cited method block ({len(sources)} sources), "
          f"collection-scoped, self-disabling on empty corpus")
