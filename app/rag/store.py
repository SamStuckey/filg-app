#!/usr/bin/env python3
"""
store.py — SQLite persistence for the RAG module (stage 1: documents + chunks).

Mirrors the app's main store.py idioms exactly: stdlib sqlite3, one fresh connection per call, WAL
mode, an idempotent init() with a PRAGMA-table_info migration block, JSON only where needed, and a
__main__ self-test. It shares the same FILG_DB file but owns its own `rag_*` tables, so it touches
zero existing tables and nothing existing imports it.

Stage 1 stores the retrieval UNIT (the chunk). Later stages extend the SAME tables in the migration
block — an `embedding` BLOB column on rag_chunks (stage 2) and an FTS5 virtual table (stage 3) — so
the schema grows without a rewrite.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
# Same DB file as the rest of the app (constraint: reuse SQLite). Default sits next to app/store.py's.
DB = os.environ.get("FILG_DB") or os.path.join(os.path.dirname(_HERE), "filg.db")

_init_lock = threading.Lock()
_initialized = False
_FTS_OK: bool | None = None        # set in init(): True if SQLite has FTS5, else fall back to a TF scan
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")   # words for the FTS MATCH expr + the fallback scorer


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
        con = _connect()
        try:
            with con:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute(
                    "CREATE TABLE IF NOT EXISTS rag_documents ("
                    "  id TEXT PRIMARY KEY,"
                    "  title TEXT,"
                    "  source TEXT,"                  # filename / url / 'pasted' — provenance for citations
                    "  n_chunks INTEGER NOT NULL DEFAULT 0,"
                    "  created_at TEXT NOT NULL)")
                con.execute(
                    "CREATE TABLE IF NOT EXISTS rag_chunks ("
                    "  id TEXT PRIMARY KEY,"           # '{doc_id}:{ord}' — stable + citation-friendly
                    "  doc_id TEXT NOT NULL,"
                    "  ord INTEGER NOT NULL,"          # 0-based position within its document
                    "  text TEXT NOT NULL,"
                    "  created_at TEXT NOT NULL)")
                con.execute("CREATE INDEX IF NOT EXISTS ix_rag_chunks_doc ON rag_chunks(doc_id)")
                # Migration block for columns/tables added in later stages (SQLite has no
                # ADD COLUMN IF NOT EXISTS) — add any missing column, ignore if present.
                have = {r["name"] for r in con.execute("PRAGMA table_info(rag_chunks)")}
                if "embedding" not in have:                 # stage 2: the chunk's vector
                    con.execute("ALTER TABLE rag_chunks ADD COLUMN embedding BLOB")
                if "dim" not in have:                       # vector length (validates query↔chunk match)
                    con.execute("ALTER TABLE rag_chunks ADD COLUMN dim INTEGER")
                # Stage 3: a full-text index for keyword/BM25 search. FTS5 is a compile-time SQLite
                # option; if this build lacks it we degrade to a term-frequency scan (still functional,
                # just not true BM25). A contentless-ish standalone table keyed by chunk_id, backfilled
                # from any pre-existing rows so it survives the stage-2→3 migration.
                global _FTS_OK
                try:
                    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts "
                                "USING fts5(chunk_id UNINDEXED, text)")
                    con.execute("INSERT INTO rag_chunks_fts(chunk_id, text) SELECT id, text "
                                "FROM rag_chunks WHERE id NOT IN (SELECT chunk_id FROM rag_chunks_fts)")
                    _FTS_OK = True
                except sqlite3.OperationalError:
                    _FTS_OK = False
        finally:
            con.close()
        _initialized = True


def add_document(title: str, chunks: list[str], source: str | None = None) -> str:
    """Persist a document and its already-computed chunks in one transaction. Returns the doc id.
    Chunk ids are deterministic ('{doc_id}:{ord}') so citations and re-ingestion stay stable."""
    init()
    doc_id = uuid.uuid4().hex[:12]
    now = _now()
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO rag_documents (id, title, source, n_chunks, created_at) VALUES (?,?,?,?,?)",
                (doc_id, title, source, len(chunks), now))
            con.executemany(
                "INSERT INTO rag_chunks (id, doc_id, ord, text, created_at) VALUES (?,?,?,?,?)",
                [(f"{doc_id}:{i}", doc_id, i, c, now) for i, c in enumerate(chunks)])
            if _FTS_OK:                              # mirror into the keyword index
                con.executemany("INSERT INTO rag_chunks_fts(chunk_id, text) VALUES (?,?)",
                                [(f"{doc_id}:{i}", c) for i, c in enumerate(chunks)])
    finally:
        con.close()
    return doc_id


def get_document(doc_id: str) -> dict | None:
    init()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM rag_documents WHERE id=?", (doc_id,)).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def get_chunk(chunk_id: str) -> dict | None:
    init()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM rag_chunks WHERE id=?", (chunk_id,)).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def list_chunks(doc_id: str | None = None) -> list[dict]:
    """All chunks (optionally scoped to one document), ordered by document then position."""
    init()
    con = _connect()
    try:
        if doc_id:
            rows = con.execute(
                "SELECT * FROM rag_chunks WHERE doc_id=? ORDER BY ord", (doc_id,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM rag_chunks ORDER BY doc_id, ord").fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def list_documents() -> list[dict]:
    init()
    con = _connect()
    try:
        rows = con.execute("SELECT * FROM rag_documents ORDER BY created_at DESC").fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def counts() -> dict:
    """{documents, chunks} — handy for the demo, tests, and the eval harness."""
    init()
    con = _connect()
    try:
        d = con.execute("SELECT COUNT(*) AS n FROM rag_documents").fetchone()["n"]
        c = con.execute("SELECT COUNT(*) AS n FROM rag_chunks").fetchone()["n"]
    finally:
        con.close()
    return {"documents": d, "chunks": c}


# ── Embeddings (stage 2) ─────────────────────────────────────────────────────
# Vectors are stored as raw float32 bytes in the `embedding` BLOB — compact and trivially decoded back
# to a numpy array with np.frombuffer. numpy is imported lazily so stage-1 callers stay dep-free.
def _to_blob(vector):
    import numpy as np
    return np.asarray(vector, dtype=np.float32).tobytes()


def save_embedding(chunk_id: str, vector) -> None:
    """Attach one vector to its chunk."""
    init()
    blob = _to_blob(vector)
    con = _connect()
    try:
        with con:
            con.execute("UPDATE rag_chunks SET embedding=?, dim=? WHERE id=?",
                        (blob, len(vector), chunk_id))
    finally:
        con.close()


def save_embeddings(items) -> None:
    """Batch-attach vectors: `items` is an iterable of (chunk_id, vector). One transaction."""
    rows = [(_to_blob(v), len(v), cid) for cid, v in items]
    if not rows:
        return
    init()
    con = _connect()
    try:
        with con:
            con.executemany("UPDATE rag_chunks SET embedding=?, dim=? WHERE id=?", rows)
    finally:
        con.close()


def _decode_rows(rows) -> list[dict]:
    import numpy as np
    out = []
    for r in rows:
        d = dict(r)
        d["vector"] = np.frombuffer(d.pop("embedding"), dtype=np.float32) if d.get("embedding") else None
        out.append(d)
    return out


def embedded_chunks(doc_id: str | None = None) -> list[dict]:
    """Chunks that HAVE an embedding, each with its `vector` decoded to a numpy array. The candidate
    set semantic_search scores against. Optionally scoped to one document."""
    init()
    con = _connect()
    try:
        if doc_id:
            rows = con.execute(
                "SELECT * FROM rag_chunks WHERE embedding IS NOT NULL AND doc_id=? ORDER BY doc_id, ord",
                (doc_id,)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM rag_chunks WHERE embedding IS NOT NULL ORDER BY doc_id, ord").fetchall()
    finally:
        con.close()
    return _decode_rows(rows)


def chunks_missing_embeddings(doc_id: str | None = None) -> list[dict]:
    """Chunks with no embedding yet (for incremental indexing). Text included; no vector."""
    init()
    con = _connect()
    try:
        if doc_id:
            rows = con.execute(
                "SELECT id, doc_id, ord, text FROM rag_chunks WHERE embedding IS NULL AND doc_id=? "
                "ORDER BY ord", (doc_id,)).fetchall()
        else:
            rows = con.execute(
                "SELECT id, doc_id, ord, text FROM rag_chunks WHERE embedding IS NULL "
                "ORDER BY doc_id, ord").fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


# ── Keyword / BM25 search (stage 3) ──────────────────────────────────────────
def fts_enabled() -> bool:
    """True iff this SQLite build has FTS5 (so keyword search uses real BM25, not the fallback)."""
    init()
    return bool(_FTS_OK)


def keyword_match(query: str, k: int = 5, doc_id: str | None = None) -> list[dict]:
    """Top-k chunks by KEYWORD relevance. Uses SQLite FTS5's bm25() when available, else a
    term-frequency fallback. Returns hit dicts {chunk_id, doc_id, ord, text, score} with score
    normalized to higher=better (so it lines up with semantic_search's cosine score)."""
    init()
    toks = _TOKEN_RE.findall(query or "")
    if not toks:
        return []
    con = _connect()
    try:
        if _FTS_OK:
            # Quote each term so FTS treats it as a literal (no AND/OR/NEAR/column operators from user
            # text); OR them so any term can match. bm25() returns more-negative = better, so negate it.
            expr = " OR ".join(f'"{t}"' for t in toks)
            params: list = [expr]
            scope = ""
            if doc_id:
                scope = " AND c.doc_id=?"
                params.append(doc_id)
            params.append(k)
            rows = con.execute(
                "SELECT c.id AS chunk_id, c.doc_id, c.ord, c.text, bm25(rag_chunks_fts) AS b "
                "FROM rag_chunks_fts f JOIN rag_chunks c ON c.id=f.chunk_id "
                f"WHERE rag_chunks_fts MATCH ?{scope} ORDER BY b LIMIT ?", params).fetchall()
            return [{"chunk_id": r["chunk_id"], "doc_id": r["doc_id"], "ord": r["ord"],
                     "text": r["text"], "score": -float(r["b"])} for r in rows]
        return _keyword_fallback(con, [t.lower() for t in toks], k, doc_id)
    finally:
        con.close()


def _keyword_fallback(con, toks: list[str], k: int, doc_id: str | None) -> list[dict]:
    """No-FTS5 fallback: score each chunk by how many query terms it contains, with a light length
    normalization (long chunks shouldn't win just by being long). Crude vs BM25 but keeps keyword
    search working anywhere; hybrid merge is rank-based (RRF) so the score scale doesn't matter."""
    sql = "SELECT id, doc_id, ord, text FROM rag_chunks" + (" WHERE doc_id=?" if doc_id else "")
    rows = con.execute(sql, (doc_id,) if doc_id else ()).fetchall()
    scored = []
    wanted = set(toks)
    for r in rows:
        words = _TOKEN_RE.findall(r["text"].lower())
        tf = sum(words.count(t) for t in wanted)
        if tf:
            scored.append((tf / (1.0 + math.log(len(words) + 1)), r))
    scored.sort(key=lambda x: -x[0])
    return [{"chunk_id": r["id"], "doc_id": r["doc_id"], "ord": r["ord"], "text": r["text"],
             "score": float(s)} for s, r in scored[:k]]


def delete_document(doc_id: str) -> None:
    init()
    con = _connect()
    try:
        with con:
            con.execute("DELETE FROM rag_chunks WHERE doc_id=?", (doc_id,))
            con.execute("DELETE FROM rag_documents WHERE id=?", (doc_id,))
            if _FTS_OK:
                con.execute("DELETE FROM rag_chunks_fts WHERE chunk_id LIKE ?", (f"{doc_id}:%",))
    finally:
        con.close()


def clear() -> None:
    """Wipe every rag_* row (not the schema). For the eval harness, demos, and test isolation."""
    init()
    con = _connect()
    try:
        with con:
            con.execute("DELETE FROM rag_chunks")
            con.execute("DELETE FROM rag_documents")
            if _FTS_OK:
                con.execute("DELETE FROM rag_chunks_fts")
    finally:
        con.close()


if __name__ == "__main__":  # self-test (no API)
    import tempfile
    DB = tempfile.mktemp(suffix=".db")
    _initialized = False
    did = add_document("Test doc", ["chunk zero text", "chunk one text", "chunk two text"],
                       source="pasted")
    assert get_document(did)["n_chunks"] == 3
    assert get_chunk(f"{did}:1")["text"] == "chunk one text"
    assert [c["ord"] for c in list_chunks(did)] == [0, 1, 2]
    assert counts() == {"documents": 1, "chunks": 3}
    add_document("Second", ["a", "b"])
    assert counts() == {"documents": 2, "chunks": 5}
    assert len(list_documents()) == 2
    # stage 2: embeddings round-trip through the BLOB column
    assert chunks_missing_embeddings(did) and not embedded_chunks(did)   # none embedded yet
    save_embeddings([(f"{did}:0", [1.0, 0.0, 0.0]), (f"{did}:1", [0.0, 1.0, 0.0])])
    save_embedding(f"{did}:2", [0.0, 0.0, 1.0])
    emb = embedded_chunks(did)
    assert len(emb) == 3 and not chunks_missing_embeddings(did)
    assert emb[1]["dim"] == 3 and list(emb[1]["vector"]) == [0.0, 1.0, 0.0]   # decodes back exactly
    # stage 3: keyword/BM25 over the FTS index
    kid = add_document("Codes", ["restart the device to reset",
                                 "error E-4021 means a failed auth token", "office in Denver"])
    km = keyword_match("E-4021 token", k=2)
    assert km and km[0]["text"].startswith("error E-4021"), km   # exact rare term wins
    assert keyword_match("nonexistentword", k=5) == [] and keyword_match("", k=5) == []
    assert all(km[i]["score"] >= km[i + 1]["score"] for i in range(len(km) - 1))   # higher=better
    delete_document(kid)
    delete_document(did)
    assert counts() == {"documents": 1, "chunks": 2} and get_document(did) is None
    clear()
    assert counts() == {"documents": 0, "chunks": 0}
    print("rag/store.py self-test OK — documents, chunks, embeddings persist; scope, delete, clear")
