"""
app/rag/ — a self-contained hybrid-RAG module for FILG.

"Answer questions over a set of documents" as its own subpackage with its own SQLite tables
(`rag_*` in the existing FILG_DB) and no rewiring of FILG's plan/research/grade/synth pipeline.
It reuses two things the app already has: `pipeline.call()` (so reranking + answer generation run
through FILG's existing cheap-model tier and per-run cost ledger) and the BYOK key pattern.

The retrieval pipeline, in order:
    ingest → chunk → embed → store               (offline, once per document)
    query → vector + BM25 retrieve → merge → rerank → answer-with-citations   (per question)

Built in stages; this file's only job is to mark the package. Each stage adds one module:
    ingest.py  — load + chunk            (stage 1)
    store.py   — rag_* tables            (stage 1, extended each stage)
    embed.py   — embeddings seam         (stage 2)
    search.py  — cosine + BM25 + RRF + rerank   (stages 2-4)
    answer.py  — cited answer            (stage 5)
    eval.py    — tiny eval + LLM judge   (stage 6)
"""
