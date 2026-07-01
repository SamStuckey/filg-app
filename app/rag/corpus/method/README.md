# Method corpus — PLACEHOLDER SEED

The `.md` files in this folder are the **method corpus**: FILG's curated methodology that the planner
grounds against and cites (see `app/rag/grounding.py`). Ingest them into the `method` collection with:

```python
from rag import grounding
grounding.ingest_method_dir(mock=False)   # real embeddings; needs OPENAI_API_KEY
```

**These seed files are labeled placeholders, not vetted IP.** They exist only so the grounding plumbing
is demonstrable end to end. A RAG system is only as good as its corpus — grounding the planner in
shallow, generic notes just launders genericness as citations, which is the exact failure the
credibility gate exists to prevent. Replace this content with genuinely differentiated, credible method
(hand-written, or distilled from named credible sources) before treating grounded output as real IP.

`ingest_method_dir` skips any file whose name starts with `readme`, so this note is NOT ingested as
method content — only the real `*.md` method files are.
