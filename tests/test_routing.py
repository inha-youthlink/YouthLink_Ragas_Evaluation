# 라우팅 평가 채점(질문 유형 일치)과 run 집계(정확도·분석 실패율·유형별·혼동 행렬) 테스트
import pytest

from ragas_eval.routing.routing import score_routing, summarize_routing

L, D, E = "policy_list", "policy_detail", "eligibility"


def analysis(*intents, fallback=None, prompt="analyze_test"):
    return {"prompt_version": prompt, "sub_queries": [{"intent": i} for i in intents], "fallback_reason": fallback}


def test_matching_single_intent_is_correct():
    result = score_routing([L], analysis(L))

    assert result == {
        "expected_intent": [L],
        "predicted_intent": [L],
        "intent_correct": 1,
        "fallback_reason": None,
        "prompt_version": "analyze_test",
    }


def test_different_intent_is_wrong():
    assert score_routing([L], analysis(D))["intent_correct"] == 0


def test_sub_query_intents_match_regardless_of_order():
    assert score_routing([D, L], analysis(L, D))["intent_correct"] == 1


def test_sub_query_count_must_match():
    assert score_routing([D, D], analysis(D))["intent_correct"] == 0


def test_analysis_failure_is_wrong_and_keeps_reason():
    result = score_routing([L], analysis(fallback="llm_error"))

    assert (result["predicted_intent"], result["intent_correct"], result["fallback_reason"]) == ([], 0, "llm_error")


def test_response_without_analysis_cannot_be_scored():
    result = score_routing([L], None)

    assert (result["predicted_intent"], result["intent_correct"], result["prompt_version"]) == (None, None, None)
    assert result["expected_intent"] == [L]


def test_summary_reports_accuracy_and_fallback_rate_over_scored_samples():
    rows = [
        ([L], score_routing([L], analysis(L))),
        ([D], score_routing([D], analysis(E))),
        ([E], score_routing([E], analysis(fallback="schema_error"))),
        ([L], score_routing([L], None)),
    ]

    summary = summarize_routing(rows)

    assert summary["sample_count"] == 3
    assert summary["accuracy"] == pytest.approx(1 / 3)
    assert summary["fallback_rate"] == pytest.approx(1 / 3)


def test_summary_counts_by_intent_and_confusion_only_for_single_sub_query():
    rows = [
        ([L], score_routing([L], analysis(L))),
        ([L], score_routing([L], analysis(D))),
        ([D], score_routing([D], analysis(D))),
        ([D, L], score_routing([D, L], analysis(D, L))),
        ([E], score_routing([E], analysis(fallback="llm_error"))),
    ]

    summary = summarize_routing(rows)

    assert summary["by_intent"] == {L: {"total": 2, "correct": 1}, D: {"total": 1, "correct": 1}}
    assert summary["confusion"] == {L: {L: 1, D: 1}, D: {D: 1}}


def test_summary_lists_analysis_prompt_versions():
    rows = [
        ([L], score_routing([L], analysis(L, prompt="analyze_v3"))),
        ([L], score_routing([L], analysis(L, prompt="analyze_v4"))),
        ([L], score_routing([L], analysis(L, prompt="analyze_v3"))),
    ]

    assert summarize_routing(rows)["analysis_prompt_versions"] == ["analyze_v3", "analyze_v4"]


def test_summary_without_scored_samples_has_null_rates():
    summary = summarize_routing([([L], score_routing([L], None))])

    assert (summary["sample_count"], summary["accuracy"], summary["fallback_rate"]) == (0, None, None)
