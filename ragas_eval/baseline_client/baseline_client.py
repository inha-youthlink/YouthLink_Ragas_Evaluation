# RAG 없이 LLM에 질문만 보내 할루시네이션 기준선 답변을 받는 클라이언트
import time

from openai import AsyncOpenAI

from ragas_eval.rag_client.rag_client import RagResult

# RAG 답변 프롬프트(조각 조립)와 맞추지 않은 기준선 문구. 바꾸면 버전도 올려 run.metadata로 구분한다
PROMPT_VERSION = "baseline-v0"
SYSTEM_PROMPT = "당신은 한국 청년 정책 안내 도우미입니다. 사용자의 질문에 한국어로 정확하게 답하세요."
BASELINE_RETRIEVER = "none"


async def call_baseline(client: AsyncOpenAI, model: str, question: str) -> RagResult:
    """RAG LLMClient.chat과 같은 방식(responses.create, 기본 생성 파라미터)으로 호출해 검색 유무만 다르게 한다."""
    started = time.perf_counter()
    response = await client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
    )
    latency_ms = round((time.perf_counter() - started) * 1000)

    # 토큰 한도 등으로 잘린 응답(status="incomplete")을 정상 답변으로 채점하지 않는다
    if getattr(response, "status", "completed") != "completed":
        raise ValueError("baseline 응답이 완료되지 않음")
    answer = response.output_text
    if not answer or not answer.strip():
        raise ValueError("baseline 응답이 비어 있음")
    usage = response.usage
    tokens = {"prompt_tokens": usage.input_tokens, "completion_tokens": usage.output_tokens} if usage else {}
    return RagResult(
        answer=answer,
        retrieved_contexts=[],
        chunks=[],
        latency_ms=latency_ms,
        tokens=tokens,
        chat_model=model,
        retriever=BASELINE_RETRIEVER,
        top_k=None,
    )
