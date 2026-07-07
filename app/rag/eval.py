#!/usr/bin/env python3
"""
eval.py — a tiny retrieval/answer evaluation harness (RAG stage 6).

WHY EVAL EXISTS
    RAG has knobs — chunk size, overlap, k, hybrid vs semantic-only, rerank on/off, which embedding
    model. Without a score, tuning them is vibes. A small, fixed eval set turns every change into a
    NUMBER you can watch move, and lets you say "reranking took hit-rate from 0.75 to 1.0" instead of
    "it felt better".

TWO LAYERS OF METRIC (because two different things can fail independently)
    1. RETRIEVAL metrics — did we even fetch the right chunk? Objective and free (no LLM):
         hit@k : is a relevant chunk anywhere in the top-k?  (1/0 per question, averaged = hit-rate)
         MRR   : mean reciprocal rank. reciprocal rank = 1/(position of the first relevant chunk), so
                 rank 1 → 1.0, rank 2 → 0.5, rank 3 → 0.33. Rewards putting the right chunk NEAR THE
                 TOP, not just somewhere in the pile. hit@k is binary; MRR grades the ordering.
       If retrieval misses, no amount of LLM cleverness can save the answer — it can't answer from a
       chunk it never saw. So this is the first thing to check.
    2. ANSWER metric — given the retrieved chunks, was the generated answer actually correct? "Correct"
       is fuzzy, so we use an LLM-JUDGE: show it the question, a short reference answer, and the
       generated answer, and have it score 0-5. Caveat: the judge is itself an LLM, so it's a proxy,
       not ground truth — keep the rubric tight, prefer a cheaper judge than the generator, and read
       scores RELATIVELY (compare configs) rather than as absolute truth.

Run:  python3 app/rag/eval.py            # mock (deterministic, no spend) — prints a scorecard
      python3 app/rag/eval.py --real     # real embeddings + LLM judge (needs OPENAI_API_KEY +
                                         # ANTHROPIC_API_KEY; binds FILG's Anthropic provider)
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/app on path → `from rag import`
from . import answer, embed, ingest, search, store   # noqa: E402

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass
class EvalCase:
    question: str
    gold: str        # a phrase the CORRECT source chunk contains → objective retrieval ground truth
    expected: str    # a short reference answer → the answer judge compares against this


# A self-contained corpus (one fact per paragraph) + questions with known answers. Small on purpose:
# an eval set you can read in full is one you can trust and debug.
DEFAULT_CORPUS = [
    ("Company Handbook",
     "Employees accrue fifteen days of paid vacation per year.\n\n"
     "To reset your password, open account settings and click the reset link.\n\n"
     "The office is closed on all federal holidays.\n\n"
     "Expense reports must be submitted within thirty days of a purchase.\n\n"
     "The support team can be reached at help@example.com around the clock."),
]

DEFAULT_CASES = [
    EvalCase("How many vacation days do employees get?", "fifteen days",
             "Fifteen paid vacation days per year."),
    EvalCase("How do I reset my password?", "reset your password",
             "Open account settings and click the reset link."),
    EvalCase("When is the office closed?", "federal holidays",
             "On all federal holidays."),
    EvalCase("How long do I have to submit an expense report?", "thirty days",
             "Within thirty days of the purchase."),
]


def _first_relevant_rank(sources: list[dict], gold: str) -> int | None:
    """1-based rank of the first retrieved source whose text contains the gold phrase, else None."""
    g = gold.lower()
    for s in sources:
        if g in s["text"].lower():
            return s["n"]
    return None


def _overlap_ratio(reference: str, candidate: str) -> float:
    ref = set(_TOKEN_RE.findall(reference.lower()))
    cand = set(_TOKEN_RE.findall(candidate.lower()))
    return (len(ref & cand) / len(ref)) if ref else 0.0


def judge_answer(case: EvalCase, got: str, *, mock: bool = False, key: str | None = None,
                 model: str = embed.DEFAULT_MODEL) -> tuple[float, float]:
    """Score how well `got` matches the reference answer, 0-5. Returns (score, cost). Mock scores by
    word overlap (deterministic); real mode asks a cheap LLM judge."""
    if mock:
        return float(round(5 * _overlap_ratio(case.expected, got))), 0.0
    from engine.pipeline import LEDGER, call, HAIKU, extract_json   # heavy; real mode only
    start = len(LEDGER.rows)
    out = call("rag_eval_judge", HAIKU, max_tokens=40, prompt=(
        "Score how well the CANDIDATE answer matches the REFERENCE answer to the QUESTION, from 0 "
        "(wrong/irrelevant) to 5 (fully correct). Judge correctness only.\n\n"
        f"QUESTION: {case.question}\nREFERENCE: {case.expected}\nCANDIDATE: {got}\n\n"
        'Reply ONLY with JSON: {"score": <0-5>}.'))
    data = extract_json(out) or {}
    try:
        score = float(data.get("score", 0)) if isinstance(data, dict) else 0.0
    except (TypeError, ValueError):
        score = 0.0
    return max(0.0, min(5.0, score)), round(LEDGER.cost_slice(start), 4)


def evaluate(cases: list[EvalCase] = DEFAULT_CASES, corpus=DEFAULT_CORPUS, *, k: int = 5,
             target_chars: int = 90, overlap_chars: int = 0, mock: bool = True,
             key: str | None = None, model: str = embed.DEFAULT_MODEL) -> dict:
    """Ingest `corpus`, run every case through retrieve→answer→judge, and aggregate the metrics.
    Returns {summary, rows}. WIPES the rag store first (it owns the eval corpus for the run)."""
    store.clear()
    for title, text in corpus:
        store.add_document(title, ingest.chunk_text(text, target_chars, overlap_chars), source="eval")
    search.index_pending(mock=mock, key=key, model=model)

    rows, total_cost = [], 0.0
    for c in cases:
        res, a_cost = answer.answer(c.question, k, mock=mock, key=key, model=model)
        rank = _first_relevant_rank(res["sources"], c.gold)   # sources ARE the retrieved chunks
        jscore, j_cost = judge_answer(c, res["answer"], mock=mock, key=key, model=model)
        total_cost += a_cost + j_cost
        rows.append({"question": c.question, "hit": rank is not None, "rank": rank,
                     "rr": (1.0 / rank) if rank else 0.0, "judge": jscore,
                     "cited": res["cited"], "answer": res["answer"]})

    n = len(rows) or 1
    summary = {
        "n_cases": len(rows), "k": k,
        "hit_rate": sum(r["hit"] for r in rows) / n,       # fraction with a relevant chunk in top-k
        "mrr": sum(r["rr"] for r in rows) / n,             # mean reciprocal rank (ordering quality)
        "avg_judge": sum(r["judge"] for r in rows) / n,    # mean answer correctness (0-5)
        "cost": round(total_cost, 4),
    }
    return {"summary": summary, "rows": rows}


def _print_report(report: dict) -> None:
    s = report["summary"]
    print("\n=== RAG eval scorecard ===")
    print(f"cases={s['n_cases']}  k={s['k']}  fts={store.fts_enabled()}")
    print(f"  hit-rate@{s['k']} : {s['hit_rate']:.0%}   (a relevant chunk was retrieved)")
    print(f"  MRR         : {s['mrr']:.3f}   (1.0 = right chunk always ranked first)")
    print(f"  avg judge   : {s['avg_judge']:.2f} / 5   (answer correctness)")
    print(f"  cost        : ${s['cost']:.4f}")
    print("\n  per question:")
    for r in report["rows"]:
        mark = "hit " if r["hit"] else "MISS"
        rank = f"@{r['rank']}" if r["rank"] else "  -"
        print(f"    [{mark} {rank}] judge={r['judge']:.0f}/5  cite={r['cited']}  {r['question']}")


def main() -> int:
    real = "--real" in sys.argv
    if real:
        from engine import pipeline
        from engine import provider
        with provider.use(provider.anthropic_provider()), pipeline.run_ledger():
            report = evaluate(mock=False)
    else:
        report = evaluate(mock=True)
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
