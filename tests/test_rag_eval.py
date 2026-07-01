"""RAG stage 6 — the eval harness (retrieval metrics + LLM-judge answer score)."""

from rag import eval as rag_eval


def test_mock_eval_produces_expected_metrics():
    report = rag_eval.evaluate(mock=True, k=5)
    s = report["summary"]
    assert s["n_cases"] == len(rag_eval.DEFAULT_CASES)
    assert set(s) == {"n_cases", "k", "hit_rate", "mrr", "avg_judge", "cost"}
    assert s["hit_rate"] == 1.0            # every question's gold chunk is retrieved (small, clean set)
    assert s["mrr"] > 0.0 and s["cost"] == 0.0
    assert 0.0 <= s["avg_judge"] <= 5.0 and s["avg_judge"] > 0.0


def test_rows_carry_per_question_detail():
    report = rag_eval.evaluate(mock=True, k=5)
    assert len(report["rows"]) == len(rag_eval.DEFAULT_CASES)
    for r in report["rows"]:
        assert r["hit"] is True and r["rank"] >= 1
        assert r["rr"] == 1.0 / r["rank"]
        assert 1 in r["cited"]             # the mock answer cites its top source


def test_first_relevant_rank_helper():
    sources = [{"n": 1, "text": "irrelevant filler"}, {"n": 2, "text": "contains the GOLD phrase here"}]
    assert rag_eval._first_relevant_rank(sources, "gold phrase") == 2
    assert rag_eval._first_relevant_rank(sources, "absent") is None


def test_mock_judge_scores_by_overlap():
    case = rag_eval.EvalCase("q", "gold", "fifteen paid vacation days per year")
    perfect, cost = rag_eval.judge_answer(case, "fifteen paid vacation days per year", mock=True)
    poor, _ = rag_eval.judge_answer(case, "completely unrelated sentence about weather", mock=True)
    assert perfect == 5.0 and cost == 0.0
    assert poor < perfect


def test_real_judge_parses_score(patch_call):
    case = rag_eval.EvalCase("vacation?", "fifteen days", "Fifteen paid vacation days per year.")
    patch_call('{"score": 4}')
    score, _ = rag_eval.judge_answer(case, "You get 15 vacation days.", mock=False)
    assert score == 4.0


def test_real_judge_clamps_out_of_range(patch_call):
    case = rag_eval.EvalCase("q", "g", "ref")
    patch_call('{"score": 99}')
    assert rag_eval.judge_answer(case, "answer", mock=False)[0] == 5.0   # clamped to the 0-5 rubric
