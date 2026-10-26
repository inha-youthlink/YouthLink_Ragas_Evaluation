# 평가 실행 순서(run INSERT → samples INSERT → 응답·채점 UPDATE → 집계)를 조율
import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID

import httpx
import psycopg
from openai import AsyncOpenAI

from ragas_eval.baseline_client import baseline_client
from ragas_eval.config import Settings, get_settings
from ragas_eval.dataset.dataset import (
    GoldenSample,
    RoutingSample,
    load_evaluation_samples,
    load_golden,
    load_routing,
    reviewed_only,
)
from ragas_eval.rag_client import rag_client
from ragas_eval.rag_client.rag_client import RagResult
from ragas_eval.repository import repository
from ragas_eval.retrieval import retrieval
from ragas_eval.routing import routing
from ragas_eval.scorer import scorer
from ragas_eval.scorer.scorer import Mode, ScoreResult

logger = logging.getLogger(__name__)

CONCURRENCY = 5  # PRD "실행 환경" asyncio.Semaphore(5)
DB_CONNECT_TIMEOUT_SECONDS = 10
# RAG 일시 장애(연결 오류·타임아웃·5xx)만 재시도한다. 계약 위반·4xx는 재시도해도 같으므로 바로 실패
RAG_RETRIES = 2
RAG_RETRY_DELAY_SECONDS = 1.0
DATASET_FILE = re.compile(r"(golden_v\d+)\.jsonl")
ROUTING_FILE = re.compile(r"(routing_v\d+)\.jsonl")
ROUTING_MODE = "routing"
# 라우팅 평가는 채점 LLM·임베딩을 쓰지 않아 NOT NULL 컬럼에 'none'을 남긴다 (baseline의 rag_retriever와 같은 방식)
NO_MODEL = "none"

Answerer = Callable[[GoldenSample], Awaitable[RagResult]]
RoutingAnswerer = Callable[[RoutingSample], Awaitable[RagResult]]
Scorer = Callable[[GoldenSample, str, list[str]], Awaitable[ScoreResult]]


class Workers(NamedTuple):
    """모드별 응답 함수와 채점 함수. 테스트에서 가짜로 바꿔 끼운다."""

    answer: Answerer
    score: Scorer


class RunOutcome(NamedTuple):
    run_id: UUID
    status: str  # SUCCEEDED 또는 RUNNING(미완료 샘플이 있어 --resume 대상)
    pending_answer: int
    pending_scores: int


def run(
    golden_path: str, repeat: int = 1, offline: bool = False, baseline: bool = False, resume: str | None = None
) -> list[RunOutcome]:
    if repeat < 1:
        raise ValueError(f"repeat는 1 이상이어야 함: {repeat}")
    resume_id = _parse_run_id(resume) if resume else None
    if resume_id and repeat != 1:
        raise ValueError("--resume은 --repeat와 함께 쓸 수 없음 (재개는 run 1개만)")
    mode = resolve_mode(offline=offline, baseline=baseline)
    version = dataset_version(golden_path)
    settings = get_settings()
    if mode == "baseline" and not settings.baseline_model:
        raise ValueError("baseline 모드에는 BASELINE_MODEL 설정이 필요함")

    with _connect(settings) as conn:
        if resume_id:
            check_resumable(conn, resume_id, mode=mode, dataset_version=version, settings=settings)
            # 재개는 run 시작 때 고정된 샘플을 이어가므로 정책 변경(freshness) 필터를 다시 적용하지 않는다
            samples = reviewed_only(load_golden(golden_path))
            return asyncio.run(_resume(conn, settings, mode, resume_id, samples))
        samples = load_evaluation_samples(golden_path, conn)
        if not samples:
            raise ValueError(f"평가할 샘플이 없음 (reviewed·정책 변경 필터 후 0건): {golden_path}")
        return asyncio.run(_repeat(conn, settings, mode, version, repeat, samples))


def resolve_mode(*, offline: bool, baseline: bool) -> Mode:
    if offline and baseline:
        raise ValueError("--offline과 --baseline은 함께 쓸 수 없음")
    return "offline" if offline else "baseline" if baseline else "rag"


def dataset_version(golden_path: str) -> str:
    matched = DATASET_FILE.fullmatch(Path(golden_path).name)
    if not matched:
        raise ValueError(f"확정 데이터셋(golden_vN.jsonl)만 평가할 수 있음: {Path(golden_path).name}")
    return matched.group(1)


def offline_result(sample: GoldenSample) -> RagResult:
    """PRD "오프라인 모드": 정답을 답변으로, 정책 원문을 검색 결과로 넣는다."""
    return RagResult(
        answer=sample.ground_truth,
        retrieved_contexts=list(sample.reference_contexts),
        chunks=[{"content": c} for c in sample.reference_contexts],
    )


def contexts_of(chunks: Sequence[Mapping[str, Any]]) -> list[str]:
    """저장된 contexts([{"content": ...}])를 채점 입력으로 되돌린다. score가 있으면 내림차순, 없으면 저장 순서."""
    if all("score" in c for c in chunks):
        chunks = sorted(chunks, key=lambda c: c["score"], reverse=True)
    return [c["content"] for c in chunks]


def start_run(
    conn: psycopg.Connection,
    samples: Iterable[GoldenSample],
    *,
    dataset_version: str,
    repeat_no: int,
    mode: Mode,
    settings: Settings,
) -> UUID:
    metadata: dict[str, Any] = {"scoring_prompt": scorer.SCORING_PROMPT_VERSION}
    baseline_fields: dict[str, Any] = {}
    if mode == "baseline":
        # PRD "베이스라인 모드" run 기록: 답변 모델·검색 없음·프롬프트 버전
        metadata["prompt_version"] = baseline_client.PROMPT_VERSION
        baseline_fields = {"rag_chat_model": settings.baseline_model, "rag_retriever": baseline_client.BASELINE_RETRIEVER}
    run_id = repository.insert_run(
        conn,
        dataset_version=dataset_version,
        repeat_no=repeat_no,
        mode=mode,
        judge_model=settings.judge_model,
        embedding_model=settings.embedding_model,
        metadata=metadata,
        **baseline_fields,
    )
    repository.insert_samples(conn, run_id, samples)
    conn.commit()
    return run_id


def check_resumable(
    conn: psycopg.Connection, run_id: UUID, *, mode: Mode, dataset_version: str, settings: Settings
) -> None:
    """run 1개 = 같은 조건 1회 실행. 조건이 바뀌었으면 재개하지 않고 새 run으로 실행하게 한다."""
    info = repository.get_run(conn, run_id)
    conn.commit()
    if info is None:
        raise ValueError(f"재개할 run이 없음: {run_id}")
    if info.status != "RUNNING":
        raise ValueError(f"RUNNING run만 재개할 수 있음 (현재 {info.status}). FAILED run은 새 run으로 다시 실행")
    if info.mode != mode:
        raise ValueError(f"run의 mode가 다름: run={info.mode}, 요청={mode}")
    if info.dataset_version != dataset_version:
        raise ValueError(f"run의 dataset_version이 다름: run={info.dataset_version}, 요청={dataset_version}")
    expected = {
        "judge_model": settings.judge_model,
        "embedding_model": settings.embedding_model,
        "scoring_prompt": scorer.SCORING_PROMPT_VERSION,
    }
    if mode == "baseline":
        expected |= {"rag_chat_model": settings.baseline_model, "prompt_version": baseline_client.PROMPT_VERSION}
    changed = [name for name, value in expected.items() if getattr(info, name) != value]
    if changed:
        raise ValueError(f"run 시작 때와 설정이 다름: {', '.join(changed)}. 새 run으로 실행")


async def execute(
    conn: psycopg.Connection,
    run_id: UUID,
    mode: Mode,
    samples: Mapping[str, GoldenSample],
    workers: Workers,
    concurrency: int = CONCURRENCY,
) -> RunOutcome:
    """남은 샘플(응답 → 채점)을 처리하고 모두 끝났으면 집계한다. 새 run과 재개가 같은 경로를 쓴다."""
    pending = repository.find_pending(conn, run_id)
    missing = [sid for sid in (*pending.need_answer, *pending.need_scores) if sid not in samples]
    if missing:
        raise ValueError(f"golden 파일에 없는 sample_id {len(missing)}건: {', '.join(missing[:5])}")
    # 재개 시 다른 golden 파일로 나머지를 채점해 한 run에 다른 질문·정답이 섞이지 않게 한다
    texts = repository.load_sample_texts(conn, run_id, [*pending.need_answer, *pending.need_scores])
    changed = [sid for sid, text in texts.items() if text != (samples[sid].question, samples[sid].ground_truth)]
    if changed:
        raise ValueError(f"golden 질문·정답이 run에 저장된 값과 다름 {len(changed)}건: {', '.join(changed[:5])}")
    stored = repository.load_answers(conn, run_id, pending.need_scores)
    conn.commit()

    semaphore = asyncio.Semaphore(concurrency)
    jobs = [(sid, None) for sid in pending.need_answer] + [(sid, stored[sid]) for sid in pending.need_scores]
    tasks = [
        asyncio.create_task(_process(conn, run_id, mode, samples[sid], prior, workers, semaphore)) for sid, prior in jobs
    ]
    try:
        await asyncio.gather(*tasks)
        return _finish(conn, run_id)
    except Exception:
        await _fail_run(conn, run_id, tasks)
        raise


async def _fail_run(conn: psycopg.Connection, run_id: UUID, tasks: Sequence[asyncio.Task[None]]) -> None:
    # 남은 작업이 FAILED run에 쓰지 않도록 멈춘 뒤 종료 처리한다
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    conn.rollback()
    repository.finish_run(conn, run_id, "FAILED")
    conn.commit()
    logger.error("예기치 못한 오류로 run을 FAILED로 종료: run_id=%s", run_id)


async def _process(
    conn: psycopg.Connection,
    run_id: UUID,
    mode: Mode,
    sample: GoldenSample,
    stored: tuple[str, list[dict[str, Any]]] | None,
    workers: Workers,
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        if stored is None:
            try:
                result = await workers.answer(sample)
            except Exception as e:
                # 응답 실패는 answer NULL로 남겨 --resume에서 다시 호출한다. 응답 본문·비밀값은 남기지 않는다
                logger.warning("응답 실패, answer NULL 유지: sample_id=%s %s", sample.sample_id, type(e).__name__)
                return
            repository.update_answer(conn, run_id, sample.sample_id, result, _retrieval_metrics(mode, sample, result))
            if mode == "rag":
                _record_rag_settings(conn, run_id, result)
            conn.commit()
            if _is_clarify(result):
                return
            answer, contexts = result.answer, result.retrieved_contexts
        else:
            answer, chunks = stored
            contexts = contexts_of(chunks)
        score = await workers.score(sample, answer, contexts)
        repository.update_scores(conn, run_id, sample.sample_id, score)
        conn.commit()


def _retrieval_metrics(mode: Mode, sample: GoldenSample, result: RagResult) -> dict[str, Any] | None:
    # baseline·offline·되묻기는 검색을 하지 않아 계산하지 않는다. 입력은 update_answer가 metadata에 남기는 값과 같다
    if mode != "rag" or _is_clarify(result):
        return None
    return retrieval.compute_retrieval_metrics(
        sample.policy_no, {"retrieved_chunks": result.retrieved_chunks, **result.trace_details}
    )


def _is_clarify(result: RagResult) -> bool:
    # 되묻기 응답은 RAGAS·검색 적중률에서 빼고 보고서에서 따로 센다 (PRD "되묻기 응답")
    return result.trace_details.get("handler") == repository.CLARIFY_HANDLER


def _record_rag_settings(conn: psycopg.Connection, run_id: UUID, result: RagResult) -> None:
    """첫 응답의 RAG 설정을 run에 남기고, 이후 응답이 다른 설정이면(RAG 재배포) 한 run에 섞이지 않게 멈춘다."""
    repository.update_run_rag_settings(
        conn, run_id, chat_model=result.chat_model, retriever=result.retriever, top_k=result.top_k
    )
    info = repository.get_run(conn, run_id)
    stored = {"chat_model": info.rag_chat_model, "retriever": info.rag_retriever, "top_k": info.rag_top_k}
    changed = [k for k, v in stored.items() if getattr(result, k) is not None and getattr(result, k) != v]
    if changed:
        conn.rollback()
        raise ValueError(f"RAG 설정이 run 도중 바뀜: {', '.join(changed)}. 새 run으로 실행")


def _finish(conn: psycopg.Connection, run_id: UUID) -> RunOutcome:
    pending = repository.find_pending(conn, run_id)
    if pending.need_answer or pending.need_scores:
        conn.commit()
        logger.warning(
            "미완료 샘플이 있어 RUNNING으로 남김 (응답 %d건, 채점 %d건). --resume %s 로 재개",
            len(pending.need_answer),
            len(pending.need_scores),
            run_id,
        )
        return RunOutcome(run_id, "RUNNING", len(pending.need_answer), len(pending.need_scores))
    repository.finish_run(conn, run_id, "SUCCEEDED")
    conn.commit()
    return RunOutcome(run_id, "SUCCEEDED", 0, 0)


async def _repeat(
    conn: psycopg.Connection,
    settings: Settings,
    mode: Mode,
    version: str,
    repeat: int,
    samples: list[GoldenSample],
) -> list[RunOutcome]:
    by_id = {s.sample_id: s for s in samples}
    outcomes: list[RunOutcome] = []
    async with _workers(settings, mode) as workers:
        for repeat_no in range(1, repeat + 1):
            run_id = start_run(
                conn, samples, dataset_version=version, repeat_no=repeat_no, mode=mode, settings=settings
            )
            outcome = await execute(conn, run_id, mode, by_id, workers)
            outcomes.append(outcome)
            if outcome.status != "SUCCEEDED":
                # 응답 실패가 남았으면 같은 원인(RAG 장애 등)으로 다음 반복도 실패할 가능성이 높다
                logger.warning("미완료 run이 있어 남은 반복 %d회를 실행하지 않음", repeat - repeat_no)
                break
    return outcomes


async def _resume(
    conn: psycopg.Connection, settings: Settings, mode: Mode, run_id: UUID, samples: list[GoldenSample]
) -> list[RunOutcome]:
    async with _workers(settings, mode) as workers:
        return [await execute(conn, run_id, mode, {s.sample_id: s for s in samples}, workers)]


@asynccontextmanager
async def _workers(settings: Settings, mode: Mode) -> AsyncIterator[Workers]:
    """모드별 응답 함수와 채점 함수를 만들고, 쓰는 동안 HTTP·OpenAI 클라이언트를 열어 둔다."""
    async with AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        base_url=settings.openai_base_url,
        timeout=settings.openai_timeout,
        max_retries=settings.openai_max_retries,
    ) as openai_client:
        metrics = scorer.build_metrics(openai_client, settings.judge_model, settings.embedding_model)

        async def score(sample: GoldenSample, answer: str, contexts: list[str]) -> ScoreResult:
            return await scorer.score_sample(
                metrics,
                question=sample.question,
                answer=answer,
                retrieved_contexts=contexts,
                reference=sample.ground_truth,
                reference_contexts=sample.reference_contexts,
                mode=mode,
            )

        if mode == "rag":
            async with rag_client.make_client(settings.rag_base_url) as http:

                async def answer_rag(sample: GoldenSample) -> RagResult:
                    return await call_rag_with_retry(http, sample)

                yield Workers(answer_rag, score)
        elif mode == "baseline":

            async def answer_baseline(sample: GoldenSample) -> RagResult:
                return await baseline_client.call_baseline(openai_client, settings.baseline_model, sample.question)

            yield Workers(answer_baseline, score)
        else:

            async def answer_offline(sample: GoldenSample) -> RagResult:
                return offline_result(sample)

            yield Workers(answer_offline, score)


def route(routing_path: str) -> RunOutcome:
    """PRD "라우팅 평가": 대화 없는 단일 질문을 RAG에 보내 질문 유형을 채점한다. 반복·재개 없이 run 1개."""
    version = routing_dataset_version(routing_path)
    samples = reviewed_only(load_routing(routing_path))
    if not samples:
        raise ValueError(f"평가할 샘플이 없음 (reviewed 필터 후 0건): {routing_path}")
    settings = get_settings()
    with _connect(settings) as conn:
        run_id = start_routing_run(conn, samples, dataset_version=version)
        return asyncio.run(_route(conn, settings, run_id, {s.sample_id: s for s in samples}))


def routing_dataset_version(routing_path: str) -> str:
    matched = ROUTING_FILE.fullmatch(Path(routing_path).name)
    if not matched:
        raise ValueError(f"확정 데이터셋(routing_vN.jsonl)만 평가할 수 있음: {Path(routing_path).name}")
    return matched.group(1)


def start_routing_run(conn: psycopg.Connection, samples: Iterable[RoutingSample], *, dataset_version: str) -> UUID:
    run_id = repository.insert_run(
        conn,
        dataset_version=dataset_version,
        repeat_no=1,
        mode=ROUTING_MODE,
        judge_model=NO_MODEL,
        embedding_model=NO_MODEL,
    )
    repository.insert_samples(conn, run_id, samples)
    conn.commit()
    return run_id


async def execute_routing(
    conn: psycopg.Connection,
    run_id: UUID,
    samples: Mapping[str, RoutingSample],
    answer: RoutingAnswerer,
    concurrency: int = CONCURRENCY,
) -> RunOutcome:
    """응답 → 유형 채점을 샘플마다 저장하고, 모두 응답했으면 run.metadata.routing에 집계해 종료한다."""
    semaphore = asyncio.Semaphore(concurrency)
    scored: list[tuple[list[str], dict[str, Any]]] = []

    async def process(sample: RoutingSample) -> None:
        async with semaphore:
            try:
                result = await answer(sample)
            except Exception as e:
                logger.warning("응답 실패, answer NULL 유지: sample_id=%s %s", sample.sample_id, type(e).__name__)
                return
            score = routing.score_routing(sample.expected_intent, result.trace_details.get("analysis"))
            repository.update_answer(conn, run_id, sample.sample_id, result, routing=score)
            _record_rag_settings(conn, run_id, result)
            conn.commit()
            scored.append((sample.expected_intent, score))

    tasks = [asyncio.create_task(process(sample)) for sample in samples.values()]
    try:
        await asyncio.gather(*tasks)
        # 라우팅은 RAGAS 지표를 채우지 않으므로 응답 여부만으로 완료를 판단한다
        unanswered = len(repository.find_pending(conn, run_id).need_answer)
        if unanswered:
            conn.commit()
            logger.warning("미응답 %d건이 있어 RUNNING으로 남김. 새 route로 다시 실행: run_id=%s", unanswered, run_id)
            return RunOutcome(run_id, "RUNNING", unanswered, 0)
        repository.update_run_metadata(conn, run_id, {"routing": routing.summarize_routing(scored)})
        repository.finish_run(conn, run_id, "SUCCEEDED")
        conn.commit()
        return RunOutcome(run_id, "SUCCEEDED", 0, 0)
    except Exception:
        await _fail_run(conn, run_id, tasks)
        raise


async def _route(
    conn: psycopg.Connection, settings: Settings, run_id: UUID, samples: Mapping[str, RoutingSample]
) -> RunOutcome:
    async with _routing_answerer(settings) as answer:
        return await execute_routing(conn, run_id, samples, answer)


@asynccontextmanager
async def _routing_answerer(settings: Settings) -> AsyncIterator[RoutingAnswerer]:
    async with rag_client.make_client(settings.rag_base_url) as http:

        async def answer(sample: RoutingSample) -> RagResult:
            # 대화 없는 단일 질문이라 프로필·앞 턴 없이 보낸다
            return await _call_with_retry(http, sample.question, None)

        yield answer


async def call_rag_with_retry(
    http: httpx.AsyncClient, sample: GoldenSample, delay_seconds: float = RAG_RETRY_DELAY_SECONDS
) -> RagResult:
    return await _call_with_retry(http, sample.question, sample.profile.to_request(), delay_seconds)


async def _call_with_retry(
    http: httpx.AsyncClient,
    question: str,
    profile: Mapping[str, Any] | None,
    delay_seconds: float = RAG_RETRY_DELAY_SECONDS,
) -> RagResult:
    for attempt in range(RAG_RETRIES + 1):
        try:
            return await rag_client.call_pipeline(http, question, profile)
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            is_server_error = not isinstance(e, httpx.HTTPStatusError) or e.response.status_code >= 500
            if not is_server_error or attempt == RAG_RETRIES:
                raise
            await asyncio.sleep(delay_seconds * (attempt + 1))
    raise AssertionError("unreachable")


def _parse_run_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise ValueError(f"--resume에는 run_id(UUID)를 넣어야 함: {value!r}") from None


def _connect(settings: Settings) -> psycopg.Connection:
    # DSN 파싱 오류 메시지에 접속 문자열 일부가 남을 수 있어 에러 종류만 남긴다
    try:
        return psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=DB_CONNECT_TIMEOUT_SECONDS)
    except psycopg.Error as e:
        raise RuntimeError(f"DB 연결 실패: {type(e).__name__}") from None
