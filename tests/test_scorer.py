# ScoreResult 타입 검증과 모드별 지표 7개 채점 테스트 (지표는 AsyncMock, 실제 LLM 호출 없음)
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError
from ragas.metrics.collections.factual_correctness.util import (
    ClaimDecompositionOutput,
    NLIStatementOutput,
    StatementFaithfulnessAnswer,
)

from ragas_eval.scorer import scorer
from ragas_eval.scorer.scorer import METRIC_NAMES, MODES, ScoreResult, build_metrics, score_sample

QUESTION = "가짜 질문"
ANSWER = "가짜 답변"
CONTEXTS = ["근거 1", "근거 2"]
REFERENCE = "가짜 정답"
REFERENCE_CONTEXTS = ["정책 원문 1"]
RETRIEVAL_METRICS = ("context_precision", "context_recall", "faithfulness")


def test_score_result_keeps_seven_metrics():
    result = ScoreResult(
        context_precision=0.9,
        context_recall=1.0,
        faithfulness=0.0,
        answer_relevancy=0.75,
        factual_correctness=0.6,
        reference_faithfulness=0.4,
        answer_coverage=0.8,
    )

    assert result.model_dump(exclude={"errors"}) == {
        "context_precision": 0.9,
        "context_recall": 1.0,
        "faithfulness": 0.0,
        "answer_relevancy": 0.75,
        "factual_correctness": 0.6,
        "reference_faithfulness": 0.4,
        "answer_coverage": 0.8,
    }


def test_score_result_fields_match_metric_names():
    assert set(ScoreResult.model_fields) - {"errors"} == set(METRIC_NAMES)


def test_modes_match_db_check_values():
    assert MODES == ("rag", "baseline", "offline")


def test_failed_metric_is_none_with_error():
    result = ScoreResult(faithfulness=None, errors={"faithfulness": "TimeoutError"})

    assert result.faithfulness is None
    assert result.context_precision is None
    assert result.errors == {"faithfulness": "TimeoutError"}


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf")])
def test_out_of_range_or_nan_score_fails(value):
    with pytest.raises(ValidationError):
        ScoreResult(faithfulness=value)


def fake_metrics(**values):
    """지표명 → ascore 반환값(숫자) 또는 예외. 지정하지 않은 지표는 0.5."""
    metrics = {}
    for name in METRIC_NAMES:
        value = values.get(name, 0.5)
        metric = SimpleNamespace(ascore=AsyncMock())
        if isinstance(value, BaseException):
            metric.ascore.side_effect = value
        else:
            metric.ascore.return_value = SimpleNamespace(value=value)
        metrics[name] = metric
    return metrics


def score(metrics, mode="rag", retrieved_contexts=CONTEXTS):
    return asyncio.run(
        score_sample(
            metrics,
            question=QUESTION,
            answer=ANSWER,
            retrieved_contexts=retrieved_contexts,
            reference=REFERENCE,
            reference_contexts=REFERENCE_CONTEXTS,
            mode=mode,
        )
    )


def test_each_metric_receives_its_inputs():
    metrics = fake_metrics()

    score(metrics)

    metrics["context_precision"].ascore.assert_awaited_once_with(
        user_input=QUESTION, retrieved_contexts=CONTEXTS, reference=REFERENCE
    )
    metrics["context_recall"].ascore.assert_awaited_once_with(
        user_input=QUESTION, retrieved_contexts=CONTEXTS, reference=REFERENCE
    )
    metrics["faithfulness"].ascore.assert_awaited_once_with(
        user_input=QUESTION, response=ANSWER, retrieved_contexts=CONTEXTS
    )
    metrics["answer_relevancy"].ascore.assert_awaited_once_with(user_input=QUESTION, response=ANSWER)
    metrics["factual_correctness"].ascore.assert_awaited_once_with(response=ANSWER, reference=REFERENCE)
    metrics["reference_faithfulness"].ascore.assert_awaited_once_with(
        user_input=QUESTION, response=ANSWER, retrieved_contexts=REFERENCE_CONTEXTS
    )
    metrics["answer_coverage"].ascore.assert_awaited_once_with(response=ANSWER, reference=REFERENCE)


def test_metric_values_map_to_score_result():
    metrics = fake_metrics(
        context_precision=0.9,
        context_recall=1,
        faithfulness=0.0,
        answer_relevancy=0.75,
        factual_correctness=0.6,
        reference_faithfulness=0.4,
        answer_coverage=0.8,
    )

    result = score(metrics)

    assert result == ScoreResult(
        context_precision=0.9,
        context_recall=1.0,
        faithfulness=0.0,
        answer_relevancy=0.75,
        factual_correctness=0.6,
        reference_faithfulness=0.4,
        answer_coverage=0.8,
    )


def test_baseline_skips_retrieval_metrics_without_errors():
    metrics = fake_metrics()

    result = score(metrics, mode="baseline", retrieved_contexts=[])

    for name in RETRIEVAL_METRICS:
        metrics[name].ascore.assert_not_awaited()
        assert getattr(result, name) is None
    assert (
        result.answer_relevancy,
        result.factual_correctness,
        result.reference_faithfulness,
        result.answer_coverage,
    ) == (0.5, 0.5, 0.5, 0.5)
    assert result.errors == {}


def test_baseline_does_not_need_retrieval_metric_objects():
    metrics = {name: m for name, m in fake_metrics().items() if name not in RETRIEVAL_METRICS}

    result = score(metrics, mode="baseline", retrieved_contexts=[])

    assert (
        result.answer_relevancy,
        result.factual_correctness,
        result.reference_faithfulness,
        result.answer_coverage,
    ) == (0.5, 0.5, 0.5, 0.5)
    assert result.errors == {}


def test_offline_scores_all_seven_metrics():
    metrics = fake_metrics()

    result = score(metrics, mode="offline")

    assert result.model_dump(exclude={"errors"}) == dict.fromkeys(METRIC_NAMES, 0.5)


def test_unknown_mode_fails():
    with pytest.raises(ValueError, match="mode"):
        score(fake_metrics(), mode="unknown")


@pytest.mark.parametrize(
    ("value", "error"),
    [
        pytest.param(TimeoutError("비밀-메시지"), "TimeoutError", id="exception"),
        pytest.param(float("nan"), "NaN", id="nan"),
        pytest.param(None, "InvalidValue", id="none"),
        pytest.param(1.5, "InvalidValue", id="out-of-range"),
        pytest.param(float("inf"), "InvalidValue", id="inf"),
        pytest.param("0.5", "InvalidValue", id="string"),
        pytest.param(True, "InvalidValue", id="bool"),
    ],
)
def test_failed_metric_becomes_none_and_others_are_kept(value, error):
    metrics = fake_metrics(faithfulness=value)

    result = score(metrics)

    assert result.faithfulness is None
    assert result.errors == {"faithfulness": error}
    assert (result.context_precision, result.context_recall, result.answer_relevancy) == (0.5, 0.5, 0.5)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(1.0000000000000002, 1.0, id="float-error-above-1"),
        pytest.param(-1e-12, 0.0, id="float-error-below-0"),
    ],
)
def test_tiny_float_error_is_clamped(value, expected):
    metrics = fake_metrics(answer_relevancy=value)

    result = score(metrics)

    assert result.answer_relevancy == expected
    assert result.errors == {}


def test_result_without_value_is_invalid():
    metrics = fake_metrics()
    metrics["faithfulness"].ascore.return_value = object()

    result = score(metrics)

    assert result.faithfulness is None
    assert result.errors == {"faithfulness": "InvalidValue"}


def test_missing_metric_object_fails():
    metrics = fake_metrics()
    del metrics["answer_relevancy"]

    with pytest.raises(ValueError, match="answer_relevancy"):
        score(metrics)


def test_all_metrics_failing_keeps_every_error():
    metrics = fake_metrics(**{name: RuntimeError() for name in METRIC_NAMES})

    result = score(metrics)

    assert result.model_dump(exclude={"errors"}) == dict.fromkeys(METRIC_NAMES)
    assert result.errors == dict.fromkeys(METRIC_NAMES, "RuntimeError")


def test_failure_is_logged_without_exception_message(caplog):
    metrics = fake_metrics(context_recall=ValueError("비밀-메시지"))

    with caplog.at_level(logging.WARNING, logger=scorer.__name__):
        score(metrics)

    assert "context_recall" in caplog.text
    assert "ValueError" in caplog.text
    assert "비밀-메시지" not in caplog.text


def test_cancellation_is_not_swallowed():
    metrics = fake_metrics(faithfulness=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        score(metrics)


def test_build_metrics_creates_seven_metrics_without_network(monkeypatch):
    # ragas의 track은 @silent로 예외를 삼키므로 raise 대신 호출 기록으로 확인한다
    posts = []
    monkeypatch.setattr("ragas._analytics.requests.post", lambda *args, **kwargs: posts.append(args))
    client = AsyncOpenAI(api_key="test-key", base_url="http://127.0.0.1:9")

    metrics = build_metrics(client, judge_model="test-judge-model", embedding_model="test-embedding-model")
    asyncio.run(client.close())

    assert posts == []
    assert tuple(metrics) == METRIC_NAMES
    assert [type(m).__name__ for m in metrics.values()] == [
        "ContextPrecision",
        "ContextRecall",
        "Faithfulness",
        "AnswerRelevancy",
        "FactualCorrectness",
        "Faithfulness",
        "AnswerCoverage",
    ]
    assert metrics["reference_faithfulness"] is not metrics["faithfulness"]
    assert metrics["reference_faithfulness"].name == "reference_faithfulness"


@pytest.mark.parametrize(
    "model", ["gpt-5.6-terra", "gpt-5.4-mini", "gpt-5.5", "gpt-5", "gpt-6-luna", "bedrock-gpt-5.6-luna"]
)
def test_make_llm_sends_reasoning_params_for_gpt5_and_newer(model):
    llm = scorer.make_llm(model, AsyncOpenAI(api_key="sk-test"))

    sent = llm._map_provider_params()

    assert "max_tokens" not in sent and "top_p" not in sent
    assert (sent["max_completion_tokens"], sent["temperature"]) == (scorer.REASONING_MAX_COMPLETION_TOKENS, 1.0)


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4.1-mini", "bedrock-gpt-4o", "bedrock-sonnet"])
def test_make_llm_keeps_legacy_model_params(model):
    llm = scorer.make_llm(model, AsyncOpenAI(api_key="sk-test"))

    sent = llm._map_provider_params()

    assert "max_completion_tokens" not in sent and sent["max_tokens"] == 1024


def test_answer_relevancy_uses_fixed_korean_prompt():
    metrics = build_metrics(AsyncOpenAI(api_key="sk-test"), "test-judge", "test-emb")

    prompt = metrics["answer_relevancy"].prompt
    text = prompt.to_string(prompt.input_model(response="만 19세부터 34세까지 신청할 수 있습니다."))

    assert isinstance(prompt, scorer.KoreanAnswerRelevancePrompt)
    assert "한국어" in prompt.instruction and "noncommittal" in prompt.instruction
    assert all("Einstein" not in str(example) for example in prompt.examples)
    assert "만 19세부터 34세까지" in text
    assert scorer.SCORING_PROMPT_VERSION == "answer_relevancy-ko-v1+answer_coverage-v1"


def coverage_llm(claims, verdicts):
    """분해 결과(정답 주장)와 판정(0/1)을 차례로 돌려주는 채점 LLM"""
    statements = [StatementFaithfulnessAnswer(statement=f"주장 {i}", reason="근거", verdict=v) for i, v in enumerate(verdicts)]
    return SimpleNamespace(
        agenerate=AsyncMock(
            side_effect=[ClaimDecompositionOutput(claims=claims), NLIStatementOutput(statements=statements)]
        )
    )


def coverage(llm, answer=ANSWER, reference=REFERENCE):
    return asyncio.run(scorer.AnswerCoverage(llm).ascore(response=answer, reference=reference)).value


def test_answer_coverage_decomposes_only_reference_and_checks_it_against_answer():
    llm = coverage_llm(["주장 A", "주장 B"], [1, 1])

    coverage(llm, answer="정답 내용에 원문 사실을 덧붙인 답변", reference="정답 문장")

    (decompose_prompt, decompose_model), (verify_prompt, verify_model) = (c.args for c in llm.agenerate.await_args_list)
    assert (decompose_model, verify_model) == (ClaimDecompositionOutput, NLIStatementOutput)
    assert "정답 문장" in decompose_prompt and "덧붙인 답변" not in decompose_prompt
    assert "덧붙인 답변" in verify_prompt and "주장 A" in verify_prompt


def test_answer_coverage_uses_high_atomicity_and_coverage_examples():
    llm = coverage_llm(["주장 A"], [1])

    coverage(llm)

    decompose_prompt = llm.agenerate.await_args_list[0].args[0]
    # ragas 예시 중 high/high에만 있는 문장
    assert "Charles Babbage was French." in decompose_prompt


def test_answer_coverage_is_full_when_all_reference_claims_are_in_answer():
    assert coverage(coverage_llm(["주장 A", "주장 B", "주장 C"], [1, 1, 1])) == 1.0


def test_answer_coverage_is_half_when_half_of_reference_is_missing():
    assert coverage(coverage_llm(["주장 A", "주장 B", "주장 C", "주장 D"], [1, 0, 1, 0])) == 0.5


@pytest.mark.parametrize(
    ("claims", "verdicts", "error"),
    [
        pytest.param([], [], "NoClaimsError", id="no-claims"),
        pytest.param(["주장 A", "주장 B"], [1], "VerdictCountMismatchError", id="verdict-count-mismatch"),
    ],
)
def test_answer_coverage_failure_is_none_not_zero(claims, verdicts, error):
    metrics = fake_metrics()
    metrics["answer_coverage"] = scorer.AnswerCoverage(coverage_llm(claims, verdicts))

    result = score(metrics)

    assert result.answer_coverage is None
    assert result.errors == {"answer_coverage": error}
