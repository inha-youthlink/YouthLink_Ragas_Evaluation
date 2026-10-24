# ragas_evaluation_run, ragas_evaluation_samples INSERT·UPDATE·집계·재개 조회
# 이 모듈의 함수는 커밋하지 않는다. 트랜잭션 경계는 호출하는 runner가 정한다
from collections.abc import Iterable, Mapping
from typing import Any, Literal, NamedTuple
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from ragas_eval.dataset.dataset import GoldenSample
from ragas_eval.rag_client.rag_client import RagResult
from ragas_eval.scorer.scorer import METRIC_NAMES, Mode, ScoreResult

FinalStatus = Literal["SUCCEEDED", "FAILED"]
FINAL_STATUSES = ("SUCCEEDED", "FAILED")

INSERT_RUN = """
    INSERT INTO ragas_evaluation_run (
        dataset_version, repeat_no, mode, rag_chat_model, rag_retriever, rag_top_k,
        judge_model, embedding_model, metadata
    )
    VALUES (
        %(dataset_version)s, %(repeat_no)s, %(mode)s, %(rag_chat_model)s, %(rag_retriever)s, %(rag_top_k)s,
        %(judge_model)s, %(embedding_model)s, %(metadata)s
    )
    RETURNING run_id
"""

# 첫 RAG 응답의 설정만 남긴다. 이미 기록된 값은 덮어쓰지 않는다
UPDATE_RUN_RAG_SETTINGS = """
    UPDATE ragas_evaluation_run
    SET rag_chat_model = COALESCE(rag_chat_model, %(chat_model)s),
        rag_retriever = COALESCE(rag_retriever, %(retriever)s),
        rag_top_k = COALESCE(rag_top_k, %(top_k)s)
    WHERE run_id = %(run_id)s AND status = 'RUNNING'
"""

# 종료된 run에 샘플이 추가되면 저장된 sample_count·avg_*가 어긋나므로 INSERT 전에 run을 잠그고 상태를 확인한다
LOCK_RUNNING_RUN = """
    SELECT 1 FROM ragas_evaluation_run
    WHERE run_id = %(run_id)s AND status = 'RUNNING'
    FOR UPDATE
"""

INSERT_SAMPLE = """
    INSERT INTO ragas_evaluation_samples (run_id, sample_id, policy_no, question, ground_truth)
    VALUES (%(run_id)s, %(sample_id)s, %(policy_no)s, %(question)s, %(ground_truth)s)
"""

# 샘플 UPDATE는 RUNNING run에만 적용해 종료된 이전 run 행을 보호한다
UPDATE_ANSWER = """
    UPDATE ragas_evaluation_samples s
    SET answer = %(answer)s,
        contexts = %(contexts)s,
        latency_ms = %(latency_ms)s,
        metadata = s.metadata || %(patch)s
    FROM ragas_evaluation_run r
    WHERE r.run_id = s.run_id AND r.status = 'RUNNING'
      AND s.run_id = %(run_id)s AND s.sample_id = %(sample_id)s
"""

UPDATE_SCORES = sql.SQL(
    """
    UPDATE ragas_evaluation_samples s
    SET {assignments},
        metadata = (s.metadata - 'errors') || %(patch)s
    FROM ragas_evaluation_run r
    WHERE r.run_id = s.run_id AND r.status = 'RUNNING'
      AND s.run_id = %(run_id)s AND s.sample_id = %(sample_id)s
"""
).format(
    assignments=sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(name), sql.Placeholder(name)) for name in METRIC_NAMES
    )
)

# AVG는 NULL(채점 실패·모드 제외 지표)을 빼고 평균낸다
FINISH_RUN = sql.SQL(
    """
    UPDATE ragas_evaluation_run r
    SET sample_count = agg.sample_count,
        {assignments},
        finished_at = NOW(),
        status = %(status)s
    FROM (
        SELECT COUNT(*) AS sample_count, {averages}
        FROM ragas_evaluation_samples
        WHERE run_id = %(run_id)s
    ) agg
    WHERE r.run_id = %(run_id)s AND r.status = 'RUNNING'
"""
).format(
    assignments=sql.SQL(", ").join(
        sql.SQL("{} = agg.{}").format(sql.Identifier(f"avg_{name}"), sql.Identifier(f"avg_{name}"))
        for name in METRIC_NAMES
    ),
    averages=sql.SQL(", ").join(
        sql.SQL("AVG({}) AS {}").format(sql.Identifier(name), sql.Identifier(f"avg_{name}")) for name in METRIC_NAMES
    ),
)

# 재개 기준(PRD "평가 실행 순서"): answer IS NULL → 응답부터, 지표 전부 NULL → 채점부터
FIND_PENDING = sql.SQL(
    """
    SELECT sample_id, answer IS NULL AS need_answer
    FROM ragas_evaluation_samples
    WHERE run_id = %(run_id)s AND (answer IS NULL OR ({all_null}))
    ORDER BY id
"""
).format(all_null=sql.SQL(" AND ").join(sql.SQL("{} IS NULL").format(sql.Identifier(n)) for n in METRIC_NAMES))


SELECT_RUN = """
    SELECT status, mode, dataset_version, judge_model, embedding_model, rag_chat_model,
           metadata->>'prompt_version' AS prompt_version, rag_retriever, rag_top_k,
           metadata->>'scoring_prompt' AS scoring_prompt
    FROM ragas_evaluation_run
    WHERE run_id = %(run_id)s
"""

# 재개 시 채점만 남은 샘플은 저장된 응답을 다시 쓴다 (RAG·baseline 재호출 비용 방지)
SELECT_ANSWERS = """
    SELECT sample_id, answer, contexts
    FROM ragas_evaluation_samples
    WHERE run_id = %(run_id)s AND sample_id = ANY(%(sample_ids)s) AND answer IS NOT NULL
"""


SELECT_SAMPLE_TEXTS = """
    SELECT sample_id, question, ground_truth
    FROM ragas_evaluation_samples
    WHERE run_id = %(run_id)s AND sample_id = ANY(%(sample_ids)s)
"""


class RunNotWritableError(RuntimeError):
    """run이 없거나 RUNNING이 아니거나(종료된 이전 run) 대상 샘플이 없어 UPDATE되지 않음."""


class Pending(NamedTuple):
    need_answer: list[str]
    need_scores: list[str]


class RunInfo(NamedTuple):
    """재개 전 확인할 run 조건 (상태, 모드, 데이터셋, 채점·답변 모델, baseline 프롬프트 버전)."""

    status: str
    mode: str
    dataset_version: str
    judge_model: str
    embedding_model: str
    rag_chat_model: str | None
    prompt_version: str | None
    rag_retriever: str | None
    rag_top_k: int | None
    scoring_prompt: str | None


def insert_run(
    conn: psycopg.Connection,
    *,
    dataset_version: str,
    repeat_no: int,
    mode: Mode,
    judge_model: str,
    embedding_model: str,
    rag_chat_model: str | None = None,
    rag_retriever: str | None = None,
    rag_top_k: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> UUID:
    params = {
        "dataset_version": dataset_version,
        "repeat_no": repeat_no,
        "mode": mode,
        "rag_chat_model": rag_chat_model,
        "rag_retriever": rag_retriever,
        "rag_top_k": rag_top_k,
        "judge_model": judge_model,
        "embedding_model": embedding_model,
        "metadata": Jsonb(dict(metadata or {})),
    }
    with conn.cursor() as cur:
        cur.execute(INSERT_RUN, params)
        (run_id,) = cur.fetchone()
    return run_id


def update_run_rag_settings(
    conn: psycopg.Connection, run_id: UUID, *, chat_model: str | None, retriever: str | None, top_k: int | None
) -> None:
    params = {"run_id": run_id, "chat_model": chat_model, "retriever": retriever, "top_k": top_k}
    _execute_one(conn, UPDATE_RUN_RAG_SETTINGS, params, run_id)


def insert_samples(conn: psycopg.Connection, run_id: UUID, samples: Iterable[GoldenSample]) -> int:
    rows = [
        {
            "run_id": run_id,
            "sample_id": s.sample_id,
            "policy_no": s.policy_no,
            "question": s.question,
            "ground_truth": s.ground_truth,
        }
        for s in samples
    ]
    with conn.cursor() as cur:
        cur.execute(LOCK_RUNNING_RUN, {"run_id": run_id})
        if cur.fetchone() is None:
            raise RunNotWritableError(f"RUNNING run이 아니어서 샘플을 추가할 수 없음: run_id={run_id}")
        cur.executemany(INSERT_SAMPLE, rows)
    return len(rows)


def update_answer(
    conn: psycopg.Connection,
    run_id: UUID,
    sample_id: str,
    result: RagResult,
    retrieval: Mapping[str, Any] | None = None,
) -> None:
    patch: dict[str, Any] = {}
    if result.tokens:
        patch["tokens"] = result.tokens
    if result.retrieved_chunks:
        patch["retrieved_chunks"] = result.retrieved_chunks
    patch |= result.trace_details
    # 응답과 같은 UPDATE에 넣어, 재개 시 채점만 남은 샘플도 검색 적중률이 이미 있게 한다
    if retrieval is not None:
        patch["retrieval"] = dict(retrieval)
    params = {
        "run_id": run_id,
        "sample_id": sample_id,
        "answer": result.answer,
        "contexts": Jsonb(result.chunks),
        "latency_ms": result.latency_ms,
        "patch": Jsonb(patch),
    }
    _execute_one(conn, UPDATE_ANSWER, params, run_id, sample_id)


def update_scores(conn: psycopg.Connection, run_id: UUID, sample_id: str, score: ScoreResult) -> None:
    # 다시 채점할 때 이전 errors가 남지 않도록 키를 지운 뒤, 실패가 있을 때만 새로 쓴다
    params = {
        "run_id": run_id,
        "sample_id": sample_id,
        "patch": Jsonb({"errors": score.errors} if score.errors else {}),
        **{name: getattr(score, name) for name in METRIC_NAMES},
    }
    _execute_one(conn, UPDATE_SCORES, params, run_id, sample_id)


def finish_run(conn: psycopg.Connection, run_id: UUID, status: FinalStatus) -> None:
    if status not in FINAL_STATUSES:
        raise ValueError(f"종료 status는 {FINAL_STATUSES} 중 하나여야 함: {status!r}")
    _execute_one(conn, FINISH_RUN, {"run_id": run_id, "status": status}, run_id)


def find_pending(conn: psycopg.Connection, run_id: UUID) -> Pending:
    with conn.cursor() as cur:
        cur.execute(FIND_PENDING, {"run_id": run_id})
        rows = cur.fetchall()
    return Pending(
        need_answer=[sample_id for sample_id, need_answer in rows if need_answer],
        need_scores=[sample_id for sample_id, need_answer in rows if not need_answer],
    )


def get_run(conn: psycopg.Connection, run_id: UUID) -> RunInfo | None:
    with conn.cursor() as cur:
        cur.execute(SELECT_RUN, {"run_id": run_id})
        row = cur.fetchone()
    return RunInfo(*row) if row else None


def load_answers(
    conn: psycopg.Connection, run_id: UUID, sample_ids: Iterable[str]
) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    with conn.cursor() as cur:
        cur.execute(SELECT_ANSWERS, {"run_id": run_id, "sample_ids": list(sample_ids)})
        return {sample_id: (answer, contexts) for sample_id, answer, contexts in cur.fetchall()}


def load_sample_texts(conn: psycopg.Connection, run_id: UUID, sample_ids: Iterable[str]) -> dict[str, tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(SELECT_SAMPLE_TEXTS, {"run_id": run_id, "sample_ids": list(sample_ids)})
        return {sample_id: (question, ground_truth) for sample_id, question, ground_truth in cur.fetchall()}


def _execute_one(
    conn: psycopg.Connection,
    query: str | sql.Composable,
    params: Mapping[str, Any],
    run_id: UUID,
    sample_id: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(query, params)
        if cur.rowcount != 1:
            target = f"run_id={run_id}" + (f" sample_id={sample_id}" if sample_id else "")
            raise RunNotWritableError(f"RUNNING run의 대상 행이 없어 UPDATE되지 않음: {target}")
