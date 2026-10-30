# RAG 서버 /internal/pipeline 호출 클라이언트
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PG_INTEGER_MAX = 2_147_483_647
PIPELINE_PATH = "/internal/pipeline"
# 검색 + LLM 생성까지 기다리므로 httpx 기본값(5초)보다 길게 둔다
RAG_TIMEOUT_SECONDS = 120.0
CONNECT_TIMEOUT_SECONDS = 5.0
# RAG trace.tokens 키 → samples.metadata.tokens 키
TOKEN_KEYS = {"prompt": "prompt_tokens", "completion": "completion_tokens"}
# 질문 분석 토큰(trace.analysis.tokens)은 생성 토큰과 따로 기록된다
ANALYSIS_TOKEN_KEYS = {"prompt": "analysis_prompt_tokens", "completion": "analysis_completion_tokens"}
# 검색 적중률·라우팅 평가용으로 samples.metadata에 원본 그대로 남기는 trace 키 (없으면 저장하지 않음)
# prompt_version은 답변 종류마다 조립한 조각이 달라 run이 아니라 샘플마다 남긴다
TRACE_DETAIL_KEYS = ("cards", "analysis", "handler", "clarify_reason", "conditions", "prompt_version")


class RagResult(BaseModel):
    """RAG 응답 중 평가에 쓰는 값.

    chunks는 samples.contexts에 저장하는 채점 근거([{"content": ...}])이고,
    retrieved_chunks는 trace.retrieved_chunks 원본으로 samples.metadata에 남긴다.
    trace_details는 후보(cards)·카드 정책 번호(card_policy_nos)·질문 분석·핸들러·필터 조건으로 samples.metadata에 남긴다.
    """

    model_config = ConfigDict(frozen=True)

    answer: str
    retrieved_contexts: list[str]
    chunks: list[dict[str, Any]]
    retrieved_chunks: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0, le=PG_INTEGER_MAX)
    tokens: dict[str, int] = Field(default_factory=dict)
    chat_model: str | None = None
    retriever: str | None = None
    top_k: int | None = Field(default=None, ge=1)
    # RAG 검색 임베딩 모델. run의 embedding_model(채점용)과 다르다
    embedding_model: str | None = None
    trace_details: dict[str, Any] = Field(default_factory=dict)


class _RetrievedChunk(BaseModel):
    chunk_id: str
    policy_no: str
    score: float


class _Latency(BaseModel):
    total: int | None = Field(ge=0, le=PG_INTEGER_MAX)


class _Tokens(BaseModel):
    prompt: int | None
    completion: int | None


class _Cards(BaseModel):
    candidate_nos: list[str]


class _Analysis(BaseModel):
    tokens: _Tokens | None = None


class _Policy(BaseModel):
    policy_no: str


class _Trace(BaseModel):
    """PRD "RAG 연동 계약"의 trace 필수 키. 키는 반드시 있어야 하고 값은 null을 허용한다."""

    contexts: list[str]
    retrieved_chunks: list[_RetrievedChunk]
    latency_ms: _Latency
    tokens: _Tokens
    chat_model: str | None
    retriever: str | None
    top_k: int | None = Field(ge=1)
    # 아래는 검색 적중률·라우팅 평가용 선택 키. 있으면 형식만 확인한다
    cards: _Cards | None = None
    analysis: _Analysis | None = None
    handler: str | None = None
    conditions: dict[str, Any] | None = None
    prompt_version: str | None = None
    embedding_model: str | None = None
    # 1차 필터 통과 정책 번호: null=필터 미적용, []=적용했으나 0건. 예전 RAG는 키 자체가 없다
    filtered_policy_nos: list[str] | None = None


class _PipelineResponse(BaseModel):
    answer: str
    policies: list[_Policy] = Field(default_factory=list)
    trace: _Trace


def make_client(base_url: str, internal_api_key: str | None = None) -> httpx.AsyncClient:
    # RAG 운영 모드는 /internal 호출에 X-Internal-Key를 검사한다
    headers = {"X-Internal-Key": internal_api_key} if internal_api_key else None
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(RAG_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
    )


async def call_pipeline(
    client: httpx.AsyncClient, question: str, profile: Mapping[str, Any] | None = None
) -> RagResult:
    # 근거 텍스트(contexts) 같은 무거운 trace는 debug 요청에만 실리므로 평가에서는 항상 켠다
    response = await client.post(
        PIPELINE_PATH, params={"debug": "true"}, json={"question": question, "profile": dict(profile or {})}
    )
    response.raise_for_status()
    # 응답 본문이 로그·traceback에 남지 않도록 위치·종류만 남기고, except 밖에서 던져 __context__도 끊는다
    try:
        return _to_result(response.json())
    except ValidationError as e:
        details = ", ".join(
            f"{'.'.join(map(str, err['loc'])) or '(응답 전체)'}:{err['type']}" for err in e.errors(include_input=False)
        )
    except ValueError:
        details = "본문이 JSON이 아님"
    raise ValueError(f"RAG 응답 계약 위반: {details}")


def _to_result(payload: Any) -> RagResult:
    parsed = _PipelineResponse.model_validate(payload)
    trace = parsed.trace
    tokens = {key: v for src, key in TOKEN_KEYS.items() if (v := getattr(trace.tokens, src)) is not None}
    if trace.analysis and trace.analysis.tokens:
        tokens |= {
            key: v for src, key in ANALYSIS_TOKEN_KEYS.items() if (v := getattr(trace.analysis.tokens, src)) is not None
        }
    details = {key: payload["trace"][key] for key in TRACE_DETAIL_KEYS if payload["trace"].get(key) is not None}
    # null도 "필터 미적용"이라는 정보이므로, 키가 있으면 그대로 남긴다 (없으면 filter_hit 계산 불가)
    if "filtered_policy_nos" in payload["trace"]:
        details["filtered_policy_nos"] = payload["trace"]["filtered_policy_nos"]
    # 예전 RAG는 policies가 카드가 아니라 후보 전체였으므로, cards가 있는 응답에서만 카드로 본다
    if "cards" in details:
        details["card_policy_nos"] = [p.policy_no for p in parsed.policies]
    # contexts는 RAG가 LLM에 넘긴 근거 순서(검색 순위) 그대로 쓴다
    return RagResult(
        answer=parsed.answer,
        retrieved_contexts=trace.contexts,
        chunks=[{"content": c} for c in trace.contexts],
        retrieved_chunks=payload["trace"]["retrieved_chunks"],
        latency_ms=trace.latency_ms.total,
        tokens=tokens,
        chat_model=trace.chat_model,
        retriever=trace.retriever,
        top_k=trace.top_k,
        embedding_model=trace.embedding_model,
        trace_details=details,
    )
