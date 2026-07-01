# Method corpus

The `.md` files here are the **method corpus**: FILG's durable, curated methodology that the planner
grounds against and cites (see `app/rag/grounding.py`). Retrieval scopes to the `method` collection.

## These are SKELETONS — fill them, don't ship them empty

Every `*.md` file currently ships as a **skeleton**: real section structure and `▶` paragraph prompts,
but no method yet. Each carries a `METHOD SKELETON` banner, and **`ingest_method_dir` refuses to ingest
any file that still has that banner** — so an un-filled template can never leak scaffolding into the
corpus. When a doc holds your real method, delete its banner; that's what makes it ingestable.

## The one rule for what goes in a doc

**Durable method only.** Put in the timeless, opinionated "how to think about X" that doesn't rot and
that a generic web search returns as scattered or mediocre. Keep OUT anything time-sensitive — current
market rates, market size, who the competitors are today, which tool is best right now. Those are
fetched **live** and graded by the credibility gate (with staleness/`as_of` labeling). Each skeleton
marks its `LIVE, NOT CORPUS` traps inline.

Rule of thumb per paragraph: still true in two years? a point of view you hold (not a lookup)? no date
needed? → corpus. Otherwise → live.

## Writing guidance

- **One idea per paragraph** — paragraphs become chunks, so each should stand alone when retrieved.
- **Specific and opinionated**, not platitudes ("anchor three tiers so the middle is the target," not
  "pricing matters").
- **Title = citation label.** The filename stem becomes the cited source (`[method: "Pricing a
  productized service"]`), so name files like real sources.
- **Yours or credibly sourced.** Your own FRI method is strongest; distilling from a named book/course is
  fine (put the source in the title). Generic AI filler is the one thing to avoid — it's what turns
  grounding into laundering.

## Ingest

```python
from rag import grounding
grounding.ingest_method_dir(mock=False)   # embeds the filled *.md (needs OPENAI_API_KEY); skips skeletons + this README
```

Re-ingesting ADDS documents, so clear the collection first when you revise (`store.delete_document` /
`store.clear`).

## The seven docs (one per plan section)

| File | Plan section it grounds |
|---|---|
| `choosing-a-niche.md` | The setup |
| `designing-a-productized-offer.md` | What you sell |
| `positioning-and-differentiation.md` | Why you win |
| `pricing-a-productized-service.md` | What you charge |
| `go-to-market-for-solo-operators.md` | How you get customers |
| `delivery-and-systemization.md` | How you deliver |
| `validation-and-first-30-days.md` | Your first 30 days |
