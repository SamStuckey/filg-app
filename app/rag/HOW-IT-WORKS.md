# Hybrid RAG module — how it works

A self-contained "answer questions over a set of documents" feature for FILG. You load documents; at
query time it retrieves the most relevant passages two different ways, merges and reranks them, and has
an LLM answer using only those passages, with citations. It lives in `app/rag/`, owns its own `rag_*`
tables in the existing SQLite DB, and does not touch FILG's plan/research/grade/synth pipeline.

## The problem it solves

An LLM only knows its training data plus whatever you paste into the prompt. To answer over *your*
documents you can't fine-tune (Claude can't be fine-tuned) and you can't paste everything (too big, too
expensive). RAG is the third way: **retrieve** the few passages relevant to the question, then let the
model **generate** an answer from them. The model becomes a reasoning engine over text you hand it, not
a memory you hope it has.

## The pipeline

```
INGEST (once)     text → chunk → embed → store
QUERY (per ask)   question → [semantic + keyword] → RRF merge → rerank → answer with citations
```

| Stage | File | What it does |
|---|---|---|
| 1 Ingest/chunk | `ingest.py` | Cut documents into overlapping ~1000-char chunks on paragraph/word seams. |
| 2 Embed + vector search | `embed.py`, `store.py`, `search.py` | Turn each chunk into a vector; find nearest by cosine similarity. |
| 3 Keyword search | `store.py`, `search.py` | BM25 over an FTS5 index — exact-term matching. |
| 4 Merge + rerank | `search.py` | Reciprocal Rank Fusion of the two lists, then an LLM-judge reorder. |
| 5 Answer | `answer.py` | LLM answers from the top chunks only, cites each claim `[n]`. |
| 6 Eval | `eval.py` | Score retrieval (hit-rate, MRR) + answers (LLM judge) so quality is a number. |

## Key concepts, in one line each

- **Chunk** — the retrieval unit. Documents are too big to embed or hand to the model whole.
- **Embedding / vector** — a list of numbers pinning a chunk to a location in "meaning space"; similar
  meaning → nearby points, so search becomes a distance calculation.
- **Cosine similarity** — compares the *angle* between the query and a chunk vector (direction, not
  length), so incidental things like text length don't decide relevance.
- **BM25** — classic keyword ranking: term frequency (with diminishing returns) × rarity of the term ×
  length normalization. SQLite's `bm25()` gives it for free.
- **RRF (Reciprocal Rank Fusion)** — merges two ranked lists by rank position (`1/(60+rank)`) instead
  of raw score, because cosine and BM25 scores aren't on the same scale.
- **Rerank** — an LLM scores each candidate against the question and reorders, fixing precision at the
  top (only the top few chunks reach the answer model).
- **Grounding + citations** — the answer uses only retrieved chunks and cites each claim, so it's about
  your documents and is auditable; it says "the documents don't cover this" rather than guess.

## Why hybrid beats either search alone

Semantic search understands meaning (matches "cancel" to "terminate") but is fuzzy on exact tokens —
error codes, product names, rare jargon get smeared into the averaged vector. Keyword/BM25 nails the
exact token but is blind to synonyms. Running both and fusing them gives better **recall** (you find the
right chunk whichever way it matches); the rerank then gives better **precision** (the genuinely best
chunk lands at position 1). Recall from the merge, precision from the rerank.

## The tradeoffs we chose, and why

- **Vectors as BLOBs + numpy cosine, not sqlite-vec.** At this corpus size (hundreds–low-thousands of
  chunks) a brute-force cosine is sub-millisecond, and a float32 BLOB column has zero dependence on
  Render's SQLite build flags. sqlite-vec would work (loadable extensions are supported) but adds
  runtime risk for no speed we need yet. Clean future swap when the corpus grows.
- **FTS5 for keyword, with a fallback.** FTS5 + `bm25()` are built into SQLite (verified on the
  runtime), so keyword search needs no dependency. If some build lacks FTS5, keyword search degrades to
  a term-frequency scan instead of breaking.
- **OpenAI `text-embedding-3-small` for embeddings.** Claude has no embeddings API, so this one step
  reaches outside Anthropic. It reuses the `openai` client the app already has (for OpenRouter), so no
  new dependency; it's cheap (~$0.02/1M tokens). Voyage AI is a future swap behind the `embed.py` seam.
- **LLM-judge rerank, not Cohere.** Reuses the exact lever FILG already trusts (its source-credibility
  gate is an LLM judge), routed through `pipeline.call` so it rides the user's chosen model stack /
  cost tier — no new vendor, key, or dependency. Cohere Rerank is a future swap behind `rerank()`.
- **Chunk size ~1000 chars, ~15% overlap.** Big enough that a chunk keeps its context, small enough
  that its embedding isn't a blurry average of many topics; overlap keeps a fact that straddles a
  boundary intact in at least one chunk. Measured in characters to avoid a tokenizer dependency.

## Running it

```
python3 app/rag/eval.py            # deterministic mock scorecard, no API, no spend
python3 app/rag/eval.py --real     # real embeddings + LLM judge (needs OPENAI_API_KEY + ANTHROPIC_API_KEY)
python3 -m pytest tests/test_rag_*.py -q
```

Every module also has a `__main__` self-test. **Mock mode** runs the whole pipeline with deterministic,
pure-Python stand-ins (a hashing embedder, a lexical-overlap reranker/judge) so it's free and
reproducible — the mock matches *shared words*, not synonyms, so mock eval numbers read artificially
high; real embeddings are what deliver the synonym-matching the design is for.

## Integration: grounding the planner (`grounding.py`)

The first real use is grounding the plan builder in a cited **method corpus**. The planner's synth
(`planner.propose`) already runs on the `synth_section` skill + gate-graded research; grounding adds a
third input — the method passages most relevant to the section being written, injected as a cited
`METHOD PLAYBOOK` block the synth can draw on and attribute.

- **Collections.** The store now tags every doc/chunk with a `collection` (default `"default"`), and
  retrieval scopes to one. The method corpus lives in the `method` collection, isolated from any future
  corpus (user docs, plan/research). One set of tables, many corpora.
- **Self-disabling.** `grounding.method_grounding()` returns an empty block when the `method` collection
  is empty, and `planner.propose` concatenates it unconditionally. So with no corpus ingested, the synth
  prompt is byte-for-byte unchanged — the integration is inert in production until someone loads method.
  The hook is also wrapped so grounding can never break plan building.
- **The corpus is the IP, and it's separate.** `app/rag/corpus/method/` holds a **labeled placeholder
  seed** only. Grounding the planner in shallow, generic notes just launders genericness as citations —
  the exact failure the credibility gate exists to prevent — so replace the seed with genuinely
  differentiated, credible method before treating grounded output as real IP.

Load a corpus and measure the lift:

```python
from rag import grounding
grounding.ingest_method_dir(mock=False)     # embeds app/rag/corpus/method/*.md (needs OPENAI_API_KEY)
```

To measure whether grounding actually helps: draft a section twice on a real key — once with the method
collection populated, once empty — and judge which draft is better and whether it cites `[method: …]`.
(Mock mode can't show the quality delta: `propose` returns canned drafts in mock, so the lift only
appears on a real run.)

## Status and next step

Grounding is wired into the planner (additive, self-disabling) and fully tested. Still **not wired to a
route or the BYOK key store.** Remaining plumbing: FastAPI endpoints in `main.py` (ingest / ask) guarded
by the existing `_key_wall` + `_run_slot`, and resolving the embeddings key through `keys.py` the same
way chat keys are (the `embed.py`/`answer.py`/`search.py`/`grounding.py` seams already accept a `key`
argument). The embeddings key is a distinct provider from the chat key, so it's a new BYOK slot, not a
reuse of the OpenRouter/Anthropic key.
