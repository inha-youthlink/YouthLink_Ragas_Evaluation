# 평가 테이블 INSERT·UPDATE·집계·재개 조회 테스트 (임시 PostgreSQL, 테스트마다 롤백)
from decimal import Decimal
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from ragas_eval.dataset.dataset import GoldenSample
from ragas_eval.rag_client.rag_client import RagResult
from ragas_eval.repository.repository import (
    RunNotWritableError,
    find_pending,
    finish_run,
    get_run,
    insert_run,
    load_answers,
    insert_samples,
    update_answer,
    update_run_rag_settings,
    update_scores,
)
from ragas_eval.scorer.scorer import ScoreResult

RUN_FIELDS = {"dataset_version": "golden_v1", "repeat_no": 1, "judge_model": "test-judge", "embedding_model": "test-emb"}


def fetch_one(conn, sql, params):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def run_row(conn, run_id):
    return fetch_one(conn, "SELECT * FROM ragas_evaluation_run WHERE run_id = %s", [run_id])


def sample_row(conn, run_id, sample_id):
    return fetch_one(
        conn, "SELECT * FROM ragas_evaluation_samples WHERE run_id = %s AND sample_id = %s", [run_id, sample_id]
    )


def make_samples(golden_record, count):
    return [
        GoldenSample.model_validate({**golden_record, "sample_id": f"single-{i:06d}"}) for i in range(1, count + 1)
    ]


def rag_result(rag_payload):
    trace = rag_payload["trace"]
    return RagResult(
        answer=rag_payload["answer"],
        retrieved_contexts=trace["contexts"],
        chunks=[{"content": c} for c in trace["contexts"]],
        latency_ms=trace["latency_ms"]["total"],
        tokens={"prompt_tokens": 850, "completion_tokens": 40},
        chat_model=trace["chat_model"],
        retriever=trace["retriever"],
        top_k=trace["top_k"],
    )


@pytest.fixture
def run_id(eval_conn):
    return insert_run(eval_conn, mode="rag", **RUN_FIELDS)


@pytest.fixture
def filled_run(eval_conn, run_id, golden_record):
    insert_samples(eval_conn, run_id, make_samples(golden_record, 2))
    return run_id


@pytest.mark.db
def test_insert_run_starts_running_with_given_conditions(eval_conn):
    run_id = insert_run(
        eval_conn,
        mode="baseline",
        rag_chat_model="test-chat",
        rag_retriever="none",
        metadata={"prompt_version": "baseline-v0"},
        **RUN_FIELDS,
    )

    row = run_row(eval_conn, run_id)
    assert isinstance(run_id, UUID)
    assert (row["status"], row["mode"], row["dataset_version"], row["repeat_no"]) == (
        "RUNNING",
        "baseline",
        "golden_v1",
        1,
    )
    assert (row["rag_chat_model"], row["rag_retriever"], row["rag_top_k"]) == ("test-chat", "none", None)
    assert (row["judge_model"], row["embedding_model"]) == ("test-judge", "test-emb")
    assert row["metadata"] == {"prompt_version": "baseline-v0"}
    assert row["finished_at"] is None


@pytest.mark.db
def test_insert_run_rejects_unknown_mode(eval_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_run(eval_conn, mode="unknown", **RUN_FIELDS)


@pytest.mark.db
def test_rag_settings_are_recorded_once(eval_conn, run_id):
    update_run_rag_settings(eval_conn, run_id, chat_model="test-chat", retriever="vector", top_k=10)
    update_run_rag_settings(eval_conn, run_id, chat_model="other-chat", retriever="hybrid", top_k=3)

    row = run_row(eval_conn, run_id)
    assert (row["rag_chat_model"], row["rag_retriever"], row["rag_top_k"]) == ("test-chat", "vector", 10)


@pytest.mark.db
def test_insert_samples_stores_question_fields(eval_conn, run_id, golden_record):
    count = insert_samples(eval_conn, run_id, make_samples(golden_record, 2))

    row = sample_row(eval_conn, run_id, "single-000002")
    assert count == 2
    assert (row["policy_no"], row["question"], row["ground_truth"]) == (
        "TEST-0001",
        golden_record["question"],
        golden_record["ground_truth"],
    )
    assert (row["answer"], row["contexts"], row["metadata"]) == (None, [], {})


@pytest.mark.db
def test_duplicate_sample_in_run_fails(eval_conn, run_id, golden_record):
    samples = make_samples(golden_record, 1)

    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_samples(eval_conn, run_id, samples + samples)


@pytest.mark.db
def test_answer_coverage_columns_start_null(eval_conn, filled_run):
    assert run_row(eval_conn, filled_run)["avg_answer_coverage"] is None
    assert sample_row(eval_conn, filled_run, "single-000001")["answer_coverage"] is None


@pytest.mark.db
def test_answer_coverage_rejects_out_of_range(eval_conn, filled_run):
    with pytest.raises(psycopg.errors.CheckViolation):
        eval_conn.execute(
            "UPDATE ragas_evaluation_samples SET answer_coverage = 1.5 WHERE run_id = %s", [filled_run]
        )


@pytest.mark.db
def test_update_answer_stores_rag_result(eval_conn, filled_run, rag_payload):
    update_answer(eval_conn, filled_run, "single-000001", rag_result(rag_payload))

    row = sample_row(eval_conn, filled_run, "single-000001")
    assert row["answer"] == rag_payload["answer"]
    assert row["contexts"] == [{"content": c} for c in rag_payload["trace"]["contexts"]]
    assert row["latency_ms"] == 1200
    assert row["metadata"] == {"tokens": {"prompt_tokens": 850, "completion_tokens": 40}}


@pytest.mark.db
def test_update_answer_keeps_trace_details_in_metadata(eval_conn, filled_run, rag_payload):
    details = {"cards": {"candidate_nos": ["TEST-0001"]}, "card_policy_nos": ["TEST-0001"], "handler": "default_rag"}
    result = rag_result(rag_payload).model_copy(update={"trace_details": details})

    update_answer(eval_conn, filled_run, "single-000001", result)

    metadata = sample_row(eval_conn, filled_run, "single-000001")["metadata"]
    assert {key: metadata[key] for key in details} == details
    assert metadata["tokens"] == {"prompt_tokens": 850, "completion_tokens": 40}


@pytest.mark.db
def test_update_answer_keeps_retrieved_chunks_in_metadata(eval_conn, filled_run, rag_payload):
    retrieved = rag_payload["trace"]["retrieved_chunks"]
    result = rag_result(rag_payload).model_copy(update={"retrieved_chunks": retrieved})

    update_answer(eval_conn, filled_run, "single-000001", result)

    row = sample_row(eval_conn, filled_run, "single-000001")
    assert row["metadata"]["retrieved_chunks"] == retrieved
    assert row["metadata"]["tokens"] == {"prompt_tokens": 850, "completion_tokens": 40}


@pytest.mark.db
def test_update_scores_stores_metrics_and_errors_with_tokens(eval_conn, filled_run, rag_payload):
    update_answer(eval_conn, filled_run, "single-000001", rag_result(rag_payload))
    score = ScoreResult(
        context_precision=0.9,
        context_recall=1.0,
        faithfulness=None,
        answer_relevancy=0.75,
        factual_correctness=0.6666,
        reference_faithfulness=0.0,
        answer_coverage=0.5,
        errors={"faithfulness": "TimeoutError"},
    )

    update_scores(eval_conn, filled_run, "single-000001", score)

    row = sample_row(eval_conn, filled_run, "single-000001")
    assert row["context_precision"] == Decimal("0.900")
    assert row["faithfulness"] is None
    assert row["factual_correctness"] == Decimal("0.667")
    assert row["reference_faithfulness"] == Decimal("0.000")
    assert row["answer_coverage"] == Decimal("0.500")
    assert row["metadata"] == {
        "tokens": {"prompt_tokens": 850, "completion_tokens": 40},
        "errors": {"faithfulness": "TimeoutError"},
    }


@pytest.mark.db
def test_offline_shaped_result_is_stored_without_tokens(eval_conn, filled_run):
    offline = RagResult(answer="정답", retrieved_contexts=["근거"], chunks=[{"content": "근거"}])

    update_answer(eval_conn, filled_run, "single-000001", offline)

    row = sample_row(eval_conn, filled_run, "single-000001")
    assert (row["answer"], row["contexts"], row["latency_ms"], row["metadata"]) == (
        "정답",
        [{"content": "근거"}],
        None,
        {},
    )


@pytest.mark.db
def test_rescoring_clears_previous_errors_and_keeps_tokens(eval_conn, filled_run, rag_payload):
    update_answer(eval_conn, filled_run, "single-000001", rag_result(rag_payload))
    update_scores(eval_conn, filled_run, "single-000001", ScoreResult(errors={"faithfulness": "TimeoutError"}))

    update_scores(eval_conn, filled_run, "single-000001", ScoreResult(faithfulness=0.8))

    row = sample_row(eval_conn, filled_run, "single-000001")
    assert row["faithfulness"] == Decimal("0.800")
    assert row["metadata"] == {"tokens": {"prompt_tokens": 850, "completion_tokens": 40}}


@pytest.mark.db
def test_finish_run_averages_ignoring_null(eval_conn, filled_run):
    update_scores(
        eval_conn, filled_run, "single-000001", ScoreResult(context_precision=0.4, answer_relevancy=1.0, answer_coverage=0.5)
    )
    update_scores(eval_conn, filled_run, "single-000002", ScoreResult(context_precision=0.8))

    finish_run(eval_conn, filled_run, "SUCCEEDED")

    row = run_row(eval_conn, filled_run)
    assert row["status"] == "SUCCEEDED"
    assert row["sample_count"] == 2
    assert row["avg_context_precision"] == Decimal("0.600")
    assert row["avg_answer_relevancy"] == Decimal("1.000")
    assert row["avg_answer_coverage"] == Decimal("0.500")
    assert row["avg_faithfulness"] is None
    assert row["finished_at"] is not None


@pytest.mark.db
def test_finished_run_is_not_modified(eval_conn, filled_run, rag_payload):
    finish_run(eval_conn, filled_run, "FAILED")

    with pytest.raises(RunNotWritableError):
        update_answer(eval_conn, filled_run, "single-000001", rag_result(rag_payload))
    with pytest.raises(RunNotWritableError):
        update_scores(eval_conn, filled_run, "single-000001", ScoreResult(context_precision=0.5))
    with pytest.raises(RunNotWritableError):
        finish_run(eval_conn, filled_run, "SUCCEEDED")
    with pytest.raises(RunNotWritableError):
        update_run_rag_settings(eval_conn, filled_run, chat_model="test-chat", retriever="vector", top_k=10)
    assert run_row(eval_conn, filled_run)["status"] == "FAILED"
    assert sample_row(eval_conn, filled_run, "single-000001")["answer"] is None


@pytest.mark.db
def test_samples_cannot_be_added_to_finished_run(eval_conn, filled_run, golden_record):
    finish_run(eval_conn, filled_run, "SUCCEEDED")
    extra = GoldenSample.model_validate({**golden_record, "sample_id": "single-000099"})

    with pytest.raises(RunNotWritableError):
        insert_samples(eval_conn, filled_run, [extra])
    assert sample_row(eval_conn, filled_run, "single-000099") is None


@pytest.mark.db
def test_unknown_sample_is_not_writable(eval_conn, filled_run, rag_payload):
    with pytest.raises(RunNotWritableError):
        update_answer(eval_conn, filled_run, "single-999999", rag_result(rag_payload))


@pytest.mark.db
def test_updates_do_not_touch_other_runs(eval_conn, filled_run, golden_record, rag_payload):
    other_run = insert_run(eval_conn, mode="rag", **{**RUN_FIELDS, "repeat_no": 2})
    insert_samples(eval_conn, other_run, make_samples(golden_record, 1))

    update_answer(eval_conn, filled_run, "single-000001", rag_result(rag_payload))
    finish_run(eval_conn, filled_run, "SUCCEEDED")

    assert sample_row(eval_conn, other_run, "single-000001")["answer"] is None
    assert run_row(eval_conn, other_run)["status"] == "RUNNING"


@pytest.mark.parametrize("status", ["RUNNING", "DONE", ""])
def test_finish_run_rejects_non_final_status(status):
    with pytest.raises(ValueError, match="status"):
        finish_run(None, UUID(int=1), status)


@pytest.mark.db
def test_find_pending_splits_answer_and_score_stages(eval_conn, run_id, golden_record, rag_payload):
    insert_samples(eval_conn, run_id, make_samples(golden_record, 3))
    update_answer(eval_conn, run_id, "single-000002", rag_result(rag_payload))
    update_answer(eval_conn, run_id, "single-000003", rag_result(rag_payload))
    update_scores(eval_conn, run_id, "single-000003", ScoreResult(answer_relevancy=0.5))

    pending = find_pending(eval_conn, run_id)

    assert pending.need_answer == ["single-000001"]
    assert pending.need_scores == ["single-000002"]


@pytest.mark.db
def test_all_failed_scores_are_pending_again(eval_conn, filled_run, rag_payload):
    update_answer(eval_conn, filled_run, "single-000001", rag_result(rag_payload))
    update_scores(eval_conn, filled_run, "single-000001", ScoreResult(errors={"faithfulness": "TimeoutError"}))

    pending = find_pending(eval_conn, filled_run)

    assert pending.need_scores == ["single-000001"]


@pytest.mark.db
def test_sample_with_only_answer_coverage_is_not_pending(eval_conn, filled_run, rag_payload):
    update_answer(eval_conn, filled_run, "single-000001", rag_result(rag_payload))
    update_scores(eval_conn, filled_run, "single-000001", ScoreResult(answer_coverage=0.5))

    pending = find_pending(eval_conn, filled_run)

    assert "single-000001" not in pending.need_scores


@pytest.mark.db
def test_get_run_returns_resume_conditions(eval_conn, run_id):
    finished = insert_run(eval_conn, mode="offline", **{**RUN_FIELDS, "repeat_no": 2})
    finish_run(eval_conn, finished, "FAILED")

    assert get_run(eval_conn, run_id) == (
        "RUNNING", "rag", "golden_v1", "test-judge", "test-emb", None, None, None, None, None
    )
    assert get_run(eval_conn, finished)[:3] == ("FAILED", "offline", "golden_v1")
    assert get_run(eval_conn, UUID(int=0)) is None


@pytest.mark.db
def test_load_answers_returns_stored_answer_and_chunks(eval_conn, filled_run, rag_payload):
    update_answer(eval_conn, filled_run, "single-000001", rag_result(rag_payload))

    answers = load_answers(eval_conn, filled_run, ["single-000001", "single-000002"])

    contexts = [{"content": c} for c in rag_payload["trace"]["contexts"]]
    assert answers == {"single-000001": (rag_payload["answer"], contexts)}


@pytest.mark.db
def test_score_check_constraint_rejects_out_of_range(eval_conn, filled_run):
    with pytest.raises(psycopg.errors.CheckViolation):
        eval_conn.execute(
            "UPDATE ragas_evaluation_samples SET factual_correctness = 1.5 WHERE run_id = %s", [filled_run]
        )
