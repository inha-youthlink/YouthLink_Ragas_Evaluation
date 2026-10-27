# RagResult 타입 검증과 /internal/pipeline 호출 테스트
import asyncio
import json
import re

import httpx
import pytest
from pydantic import ValidationError

from ragas_eval.rag_client.rag_client import RagResult, call_pipeline


def test_rag_settings_are_kept():
    result = RagResult(
        answer="a", retrieved_contexts=[], chunks=[], chat_model="test-chat-model", retriever="vector", top_k=10
    )

    assert (result.chat_model, result.retriever, result.top_k) == ("test-chat-model", "vector", 10)


def test_offline_result_has_no_latency_tokens_or_rag_settings():
    result = RagResult(answer="정답", retrieved_contexts=["근거"], chunks=[{"content": "근거"}])

    assert result.latency_ms is None
    assert result.tokens == {}
    assert result.retrieved_chunks == []
    assert (result.chat_model, result.retriever, result.top_k) == (None, None, None)


def test_zero_top_k_fails():
    with pytest.raises(ValidationError):
        RagResult(answer="a", retrieved_contexts=[], chunks=[], top_k=0)


@pytest.mark.parametrize("latency_ms", [-1, 2_147_483_648])
def test_latency_outside_db_integer_range_fails(latency_ms):
    with pytest.raises(ValidationError):
        RagResult(answer="a", retrieved_contexts=[], chunks=[], latency_ms=latency_ms)


def chunk(score, index=0, **extra):
    return {
        "chunk_id": f"00000000-0000-0000-0000-00000000000{index}",
        "policy_no": "TEST-0001",
        "chunk_type": "body",
        "score": score,
        **extra,
    }


def with_trace(payload, **changes):
    return {**payload, "trace": {**payload["trace"], **changes}}


def call_with(payload, status_code=200, profile=None):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=payload)

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag") as client:
            return await call_pipeline(client, question="가짜 질문", profile=profile)

    return asyncio.run(call()), requests


def test_call_pipeline_posts_question_and_profile_with_debug(rag_payload):
    profile = {"age": 25, "region_code": "11000"}

    _, requests = call_with(rag_payload, profile=profile)

    (request,) = requests
    assert request.method == "POST"
    assert request.url.path == "/internal/pipeline"
    assert request.url.params["debug"] == "true"
    assert json.loads(request.content) == {"question": "가짜 질문", "profile": profile}


def test_call_pipeline_sends_empty_profile_by_default(rag_payload):
    _, requests = call_with(rag_payload)

    assert json.loads(requests[0].content)["profile"] == {}


def test_call_pipeline_maps_answer_and_trace(rag_payload):
    trace = rag_payload["trace"]

    result, _ = call_with(rag_payload)

    assert result.answer == rag_payload["answer"]
    assert result.retrieved_contexts == trace["contexts"]
    assert result.chunks == [{"content": c} for c in trace["contexts"]]
    assert result.retrieved_chunks == trace["retrieved_chunks"]
    assert result.latency_ms == 1200
    assert result.tokens == {"prompt_tokens": 850, "completion_tokens": 40}
    assert (result.chat_model, result.retriever, result.top_k) == ("test-chat-model", "vector", 10)


def test_contexts_keep_rag_order(rag_payload):
    payload = with_trace(rag_payload, contexts=["정책 1 근거", "정책 2 근거", "정책 3 근거"])

    result, _ = call_with(payload)

    assert result.retrieved_contexts == ["정책 1 근거", "정책 2 근거", "정책 3 근거"]


def test_retrieved_chunks_are_kept_as_original_with_optional_content(rag_payload):
    chunks = [chunk(0.9, 1, content="청크 본문"), chunk(0.5, 2)]

    result, _ = call_with(with_trace(rag_payload, retrieved_chunks=chunks))

    assert result.retrieved_chunks == chunks


def test_null_trace_values_are_kept_empty(rag_payload):
    payload = with_trace(
        rag_payload,
        contexts=[],
        retrieved_chunks=[],
        latency_ms={"total": None},
        tokens={"prompt": None, "completion": 40},
        top_k=None,
    )

    result, _ = call_with(payload)

    assert (result.retrieved_contexts, result.chunks, result.retrieved_chunks) == ([], [], [])
    assert result.latency_ms is None
    assert result.tokens == {"completion_tokens": 40}
    assert result.top_k is None


@pytest.mark.parametrize("status_code", [404, 422, 500, 503])
def test_http_error_raises(rag_payload, status_code):
    with pytest.raises(httpx.HTTPStatusError):
        call_with(rag_payload, status_code=status_code)


@pytest.mark.parametrize(
    "key", ["contexts", "retrieved_chunks", "latency_ms", "tokens", "chat_model", "retriever", "top_k"]
)
def test_missing_trace_key_fails(rag_payload, key):
    trace = {k: v for k, v in rag_payload["trace"].items() if k != key}

    with pytest.raises(ValueError, match=re.escape(f"trace.{key}:missing")):
        call_with({**rag_payload, "trace": trace})


@pytest.mark.parametrize(
    ("changes", "loc"),
    [
        pytest.param({"latency_ms": {"retrieve": 300}}, "trace.latency_ms.total:missing", id="no-total"),
        pytest.param({"latency_ms": 1200}, "trace.latency_ms:model_type", id="latency-int"),
        pytest.param({"tokens": {"prompt": 850}}, "trace.tokens.completion:missing", id="no-completion"),
        pytest.param({"contexts": [None]}, "trace.contexts.0:string_type", id="context-null"),
    ],
)
def test_malformed_trace_fails(rag_payload, changes, loc):
    with pytest.raises(ValueError, match=re.escape(loc)):
        call_with(with_trace(rag_payload, **changes))


@pytest.mark.parametrize(
    ("payload_patch", "loc"),
    [
        pytest.param({"answer": None}, "answer:string_type", id="answer-null"),
        pytest.param({"trace": None}, "trace:model_type", id="trace-null"),
    ],
)
def test_invalid_response_fails(rag_payload, payload_patch, loc):
    with pytest.raises(ValueError, match=loc):
        call_with({**rag_payload, **payload_patch})


def test_non_json_body_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>비밀-페이지</html>")

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag") as client:
            return await call_pipeline(client, question="가짜 질문")

    with pytest.raises(ValueError, match="본문이 JSON이 아님") as exc_info:
        asyncio.run(call())

    assert "비밀-페이지" not in str(exc_info.value)
    assert exc_info.value.__context__ is None


def test_chunk_without_policy_no_fails(rag_payload):
    bad_chunk = {k: v for k, v in chunk(0.5).items() if k != "policy_no"}

    with pytest.raises(ValueError, match=r"trace\.retrieved_chunks\.0\.policy_no:missing"):
        call_with(with_trace(rag_payload, retrieved_chunks=[bad_chunk]))


def test_invalid_context_fails_without_leaking_values(rag_payload):
    payload = with_trace(rag_payload, contexts=[{"secret": "비밀-근거"}])

    with pytest.raises(ValueError, match=r"trace\.contexts\.0:string_type") as exc_info:
        call_with(payload)

    assert "비밀-근거" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize(
    ("changes", "loc"),
    [
        pytest.param({"latency_ms": {"total": -1}}, "trace.latency_ms.total:greater_than_equal", id="negative-latency"),
        pytest.param({"top_k": 0}, "trace.top_k:greater_than_equal", id="zero-top-k"),
    ],
)
def test_out_of_range_trace_value_fails(rag_payload, changes, loc):
    with pytest.raises(ValueError, match=re.escape(loc)):
        call_with(with_trace(rag_payload, **changes))


def test_trace_details_keep_candidates_cards_analysis_handler_conditions(rag_payload):
    cards = {"candidate_nos": ["TEST-0001", "TEST-0002"], "selected": [1], "fallback": False}
    analysis = {"sub_queries": [{"intent": "policy_detail"}], "tokens": {"prompt": 700, "completion": 90}}
    conditions = {"lines": ["지역: 서울"], "filtered_policy_count": 120, "split_search": True}
    payload = with_trace(rag_payload, cards=cards, analysis=analysis, handler="default_rag", conditions=conditions)
    payload["policies"] = [{"policy_no": "TEST-0001", "policy_name": "테스트 정책", "score": 0.9}]

    result, _ = call_with(payload)

    assert result.trace_details == {
        "cards": cards,
        "card_policy_nos": ["TEST-0001"],
        "analysis": analysis,
        "handler": "default_rag",
        "conditions": conditions,
        "prompt_version": "generate_v1",
    }


def test_trace_details_keep_clarify_reason(rag_payload):
    payload = with_trace(rag_payload, handler="clarify", clarify_reason="condition")

    result, _ = call_with(payload)

    assert result.trace_details == {"handler": "clarify", "clarify_reason": "condition", "prompt_version": "generate_v1"}


def test_answer_prompt_is_kept_per_sample_and_embedding_model_on_result(rag_payload):
    payload = with_trace(rag_payload, prompt_version="answer_common_v2+answer_cards_v2", embedding_model="test-rag-emb")

    result, _ = call_with(payload)

    assert result.trace_details == {"prompt_version": "answer_common_v2+answer_cards_v2"}
    assert result.embedding_model == "test-rag-emb"


@pytest.mark.parametrize("key", ["prompt_version", "embedding_model"])
def test_non_string_version_fails(rag_payload, key):
    with pytest.raises(ValueError, match=re.escape(f"trace.{key}:string_type")):
        call_with(with_trace(rag_payload, **{key: 1}))


def test_analysis_tokens_are_recorded_apart_from_generation_tokens(rag_payload):
    payload = with_trace(rag_payload, analysis={"tokens": {"prompt": 700, "completion": None}})

    result, _ = call_with(payload)

    assert result.tokens == {"prompt_tokens": 850, "completion_tokens": 40, "analysis_prompt_tokens": 700}


def test_response_without_new_trace_keys_has_no_details(rag_payload):
    payload = {**rag_payload, "policies": [{"policy_no": "TEST-0001"}]}

    result, _ = call_with(payload)

    # cards가 없는 예전 응답의 policies는 카드가 아니므로 남기지 않는다
    assert result.trace_details == {"prompt_version": "generate_v1"}
    assert result.tokens == {"prompt_tokens": 850, "completion_tokens": 40}


@pytest.mark.parametrize(
    ("changes", "loc"),
    [
        pytest.param({"cards": {"candidate_nos": "TEST-0001"}}, "trace.cards.candidate_nos", id="candidates-not-list"),
        pytest.param({"handler": 1}, "trace.handler", id="handler-not-str"),
        pytest.param({"filtered_policy_nos": "TEST-0001"}, "trace.filtered_policy_nos", id="filtered-not-list"),
        pytest.param({"filtered_policy_nos": [1]}, "trace.filtered_policy_nos.0", id="filtered-item-not-str"),
    ],
)
def test_malformed_trace_detail_fails(rag_payload, changes, loc):
    with pytest.raises(ValueError, match=re.escape(loc)):
        call_with(with_trace(rag_payload, **changes))


@pytest.mark.parametrize("filtered", [["TEST-0001", "TEST-0002"], [], None], ids=["list", "empty", "null"])
def test_filtered_policy_nos_is_kept_when_key_is_present(rag_payload, filtered):
    result, _ = call_with(with_trace(rag_payload, filtered_policy_nos=filtered))

    # null(필터 미적용)과 [](적용했으나 0건)도 키가 있으면 남겨, 예전 RAG(키 없음)와 구분한다
    assert result.trace_details == {"filtered_policy_nos": filtered, "prompt_version": "generate_v1"}


def test_filtered_policy_nos_is_omitted_when_key_is_absent(rag_payload):
    result, _ = call_with(rag_payload)

    assert "filtered_policy_nos" not in result.trace_details


def test_card_without_policy_no_fails(rag_payload):
    with pytest.raises(ValueError, match=re.escape("policies.0.policy_no")):
        call_with({**rag_payload, "policies": [{"policy_name": "번호 없음"}]})
