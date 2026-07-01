#!/usr/bin/env python3
"""
answer.py — grounded answer generation with citations (RAG stage 5).

This is the "generation" half of retrieval-augmented generation. Stages 1-4 find the few best chunks
for a question; this stage hands ONLY those chunks to the LLM and asks it to answer from them and cite
which chunk each fact came from.

WHY GROUND + CITE (the point of the whole exercise)
    An ungrounded LLM answers from its training memory — confident, sometimes wrong, uncheckable. By
    constraining the model to a small set of retrieved passages and requiring a [n] citation on each
    claim, the answer becomes (a) about YOUR documents, not the model's memory, and (b) auditable: a
    reader can click [2] and read the exact source. Retrieval decides what the model is allowed to know;
    the citation rule makes its answer traceable back to that.

    We also tell it to say "the documents don't cover this" rather than fill the gap from memory. A RAG
    system that invents an answer when retrieval comes up empty is worse than one that admits the gap —
    the whole value proposition is trust in the sources.

Returns (result, cost) to match the app's engine-module convention (advisor.py / board.py). `result`:
    {answer, sources:[{n, chunk_id, doc_id, title, ord, text}], cited:[n...], used_chunks}
"""

from __future__ import annotations

import os
import re
import sys

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/app on path → `from rag import`
if _APP not in sys.path:
    sys.path.insert(0, _APP)
from rag import embed, search, store   # noqa: E402

DEFAULT_K = 5
_CITE_RE = re.compile(r"\[(\d+)\]")
_NO_ANSWER = "The documents don't contain anything relevant to that question."


def _sources(hits: list[dict]) -> list[dict]:
    """Number the retrieved chunks 1..N and attach their document title (for human-readable citations).
    Titles are fetched once per distinct document, not per chunk."""
    titles: dict[str, str] = {}
    out = []
    for i, h in enumerate(hits):
        did = h["doc_id"]
        if did not in titles:
            doc = store.get_document(did)
            titles[did] = (doc or {}).get("title") or did
        out.append({"n": i + 1, "chunk_id": h["chunk_id"], "doc_id": did,
                    "title": titles[did], "ord": h["ord"], "text": h["text"]})
    return out


def _cited(text: str, n_sources: int) -> list[int]:
    """The distinct, in-range source numbers the answer actually referenced (e.g. [1][3] → [1, 3])."""
    nums = {int(m) for m in _CITE_RE.findall(text)}
    return sorted(n for n in nums if 1 <= n <= n_sources)


def answer(query: str, k: int = DEFAULT_K, *, doc_id: str | None = None, collection: str | None = None,
           mock: bool = False, key: str | None = None,
           model: str = embed.DEFAULT_MODEL) -> tuple[dict, float]:
    """Answer `query` over the ingested documents, grounded in the top-k retrieved chunks, with
    [n] citations pointing at the numbered sources. Returns (result, cost)."""
    hits = search.retrieve(query, k, doc_id=doc_id, collection=collection, mock=mock, key=key, model=model)
    sources = _sources(hits)
    if not sources:
        return {"answer": _NO_ANSWER, "sources": [], "cited": [], "used_chunks": 0}, 0.0

    if mock:
        top = sources[0]["text"]
        ans = (f"Based on the documents: {top[:160]}"
               f"{'…' if len(top) > 160 else ''} [1] (mock answer)")
        return {"answer": ans, "sources": sources, "cited": _cited(ans, len(sources)),
                "used_chunks": len(sources)}, 0.0

    import skill_registry as skills   # noqa: PLC0415 — FILG's no-AI-tells VOICE rule
    from pipeline import LEDGER, call, SONNET   # heavy; real mode only
    start = len(LEDGER.rows)
    block = "\n\n".join(f'[{s["n"]}] (from "{s["title"]}") {s["text"]}' for s in sources)
    ans = call("rag_answer", SONNET, max_tokens=700, system=skills.VOICE, prompt=(
        "Answer the QUESTION using ONLY the numbered SOURCES below. Rules:\n"
        "- Use only facts stated in the sources; do not add outside knowledge.\n"
        "- Cite every claim with its source number in square brackets, e.g. [1] or [2][3].\n"
        "- If the sources do not contain the answer, say so plainly instead of guessing.\n"
        "- Be concise and specific.\n\n"
        f"QUESTION: {query}\n\nSOURCES:\n{block}\n\nAnswer:")).strip()
    return {"answer": ans, "sources": sources, "cited": _cited(ans, len(sources)),
            "used_chunks": len(sources)}, round(LEDGER.cost_slice(start), 4)


if __name__ == "__main__":  # self-test (mock, no API)
    store.clear()
    store.add_document("Handbook", [
        "Employees accrue fifteen days of paid vacation per year.",
        "To reset your password, visit the account settings page and click 'reset'.",
        "The office is closed on all federal holidays.",
    ], source="pasted")
    search.index_pending(mock=True)
    res, cost = answer("how many vacation days do employees get?", k=3, mock=True)
    assert res["sources"] and res["sources"][0]["ord"] == 0, res      # the vacation chunk retrieved
    assert res["sources"][0]["title"] == "Handbook"                   # citation carries provenance
    assert 1 in res["cited"] and cost == 0.0                          # answer references [1]
    store.clear()                                                     # empty index → honest no-answer
    empty, c0 = answer("anything at all", k=3, mock=True)
    assert empty["answer"] == _NO_ANSWER and empty["sources"] == [] and c0 == 0.0
    print(f"rag/answer.py self-test OK — grounded answer cites {res['cited']} from "
          f"{res['used_chunks']} sources; empty-index → honest no-answer")
