# 평가 실행 순서·모드·재개·실패 처리 테스트 (응답·채점은 가짜 함수, DB는 임시 PostgreSQL)
import asyncio
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row
from pydantic import SecretStr

from ragas_eval.dataset.dataset import GoldenSample
from ragas_eval.rag_client.rag_client import RagResult
from ragas_eval.repository.repository import finish_run, get_run, insert_run, insert_samples, update_answer
from ragas_eval.runner import runner
from ragas_eval.runner.runner import (
    RunOutcome,
    Workers,
    check_resumable,
    contexts_of,
    dataset_version,
    execute,
    offline_result,
    resolve_mode,
    start_run,
)
from ragas_eval.scorer.scorer import ScoreResult

GOLDEN_PATH = "datasets/golden_v1.jsonl"


def make_samples(golden_record, count):
    return {
        f"single-{i:06d}": GoldenSample.model_validate({**golden_record, "sample_id": f"single-{i:06d}"})
        for i in range(1, count + 1)
    }


def fake_settings(database_url="postgresql://unused/test_db", baseline_model="test-baseline"):
    return SimpleNamespace(
        database_url=SecretStr(database_url),
        openai_api_key=SecretStr("sk-test"),
        rag_base_url="http://rag.test",
        judge_model="test-judge",
        embedding_model="test-emb",
        baseline_model=baseline_model,
    )


def rag_answer(sample, **overrides):
    fields = {
        "answer": f"{sample.sample_id} 답변",
        "retrieved_contexts": ["근거 A", "근거 B"],
        "chunks": [{"content": "근거 B", "score": 0.2}, {"content": "근거 A", "score": 0.9}],
        "latency_ms": 100,
        "tokens": {"prompt_tokens": 10, "completion_tokens": 5},
        "chat_model": "test-chat",
        "retriever": "vector",
        "top_k": 5,
    }
    return RagResult(**{**fields, **overrides})


class Recorder:
    """호출된 sample_id와 채점 입력을 기록하는 가짜 응답·채점 함수."""

    def __init__(self, fail_answer=(), score_error=None):
        self.answered = []
        self.scored = []
        self.fail_answer = set(fail_answer)
        self.score_error = score_error

    def workers(self):
        return Workers(self.answer, self.score)

    async def answer(self, sample):
        self.answered.append(sample.sample_id)
        if sample.sample_id in self.fail_answer:
            raise TimeoutError("가짜 타임아웃")
        return rag_answer(sample)

    async def score(self, sample, answer, contexts):
        self.scored.append((sample.sample_id, answer, contexts))
        if self.score_error:
            raise self.score_error
        return ScoreResult(answer_relevancy=0.5, factual_correctness=1.0)


def fetch(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def run_row(conn, run_id):
    return fetch(conn, "SELECT * FROM ragas_evaluation_run WHERE run_id = %s", [run_id])[0]


@pytest.fixture
def conn(eval_schema_url):
    with psycopg.connect(eval_schema_url) as connection:
        yield connection


def new_run(conn, samples, mode="rag"):
    run_id = insert_run(
        conn, dataset_version="golden_v1", repeat_no=1, mode=mode, judge_model="test-judge", embedding_model="test-emb"
    )
    insert_samples(conn, run_id, samples.values())
    conn.commit()
    return run_id


# ---- 순수 함수 ----


@pytest.mark.parametrize(
    ("path", "expected"), [(GOLDEN_PATH, "golden_v1"), ("/app/datasets/golden_v12.jsonl", "golden_v12")]
)
def test_dataset_version_is_golden_file_stem(path, expected):
    assert dataset_version(path) == expected


@pytest.mark.parametrize("path", ["datasets/golden_candidates.jsonl", "datasets/golden_v1.json", "golden.jsonl"])
def test_dataset_version_rejects_unconfirmed_files(path):
    with pytest.raises(ValueError, match="golden_vN"):
        dataset_version(path)


@pytest.mark.parametrize(
    ("offline", "baseline", "expected"), [(False, False, "rag"), (True, False, "offline"), (False, True, "baseline")]
)
def test_resolve_mode(offline, baseline, expected):
    assert resolve_mode(offline=offline, baseline=baseline) == expected


def test_resolve_mode_rejects_offline_with_baseline():
    with pytest.raises(ValueError, match="함께"):
        resolve_mode(offline=True, baseline=True)


def test_offline_result_uses_reference_as_answer(golden_record):
    sample = GoldenSample.model_validate(golden_record)

    result = offline_result(sample)

    assert result.answer == sample.ground_truth
    assert result.retrieved_contexts == sample.reference_contexts
    assert result.chunks == [{"content": c} for c in sample.reference_contexts]
    assert (result.latency_ms, result.tokens, result.chat_model) == (None, {}, None)


def test_contexts_of_orders_by_score_and_keeps_order_without_score():
    assert contexts_of([{"content": "B", "score": 0.2}, {"content": "A", "score": 0.9}]) == ["A", "B"]
    assert contexts_of([{"content": "첫째"}, {"content": "둘째"}]) == ["첫째", "둘째"]


def test_baseline_without_model_fails_before_db(monkeypatch):
    monkeypatch.setattr(runner, "get_settings", lambda: fake_settings(baseline_model=None))
    monkeypatch.setattr(runner, "_connect", lambda settings: pytest.fail("DB에 접속함"))

    with pytest.raises(ValueError, match="BASELINE_MODEL"):
        runner.run(golden_path=GOLDEN_PATH, repeat=1, baseline=True)


def test_resume_cannot_repeat(monkeypatch):
    monkeypatch.setattr(runner, "get_settings", lambda: pytest.fail("설정을 읽음"))

    with pytest.raises(ValueError, match="--repeat"):
        runner.run(golden_path=GOLDEN_PATH, repeat=2, resume=str(UUID(int=1)))


def test_resume_requires_uuid(monkeypatch):
    monkeypatch.setattr(runner, "get_settings", lambda: pytest.fail("설정을 읽음"))

    with pytest.raises(ValueError, match="run_id"):
        runner.run(golden_path=GOLDEN_PATH, repeat=1, resume="not-a-uuid")


@pytest.mark.parametrize("repeat", [0, -1])
def test_repeat_must_be_positive(repeat):
    with pytest.raises(ValueError, match="repeat"):
        runner.run(golden_path=GOLDEN_PATH, repeat=repeat)


# ---- execute (임시 DB) ----


@pytest.mark.db
def test_execute_answers_scores_and_finishes(conn, golden_record):
    samples = make_samples(golden_record, 2)
    run_id = new_run(conn, samples)
    fake = Recorder()

    outcome = asyncio.run(execute(conn, run_id, "rag", samples, fake.workers()))

    assert outcome == RunOutcome(run_id, "SUCCEEDED", 0, 0)
    assert sorted(fake.answered) == ["single-000001", "single-000002"]
    rows = fetch(conn, "SELECT * FROM ragas_evaluation_samples WHERE run_id = %s ORDER BY sample_id", [run_id])
    assert [r["answer"] for r in rows] == ["single-000001 답변", "single-000002 답변"]
    assert rows[0]["answer_relevancy"] == Decimal("0.500")
    run = run_row(conn, run_id)
    assert (run["status"], run["sample_count"], run["avg_factual_correctness"]) == ("SUCCEEDED", 2, Decimal("1.000"))


@pytest.mark.db
def test_execute_records_rag_settings_only_in_rag_mode(conn, golden_record):
    samples = make_samples(golden_record, 1)
    rag_run = new_run(conn, samples, mode="rag")
    offline_run = new_run(conn, samples, mode="offline")

    asyncio.run(execute(conn, rag_run, "rag", samples, Recorder().workers()))
    asyncio.run(execute(conn, offline_run, "offline", samples, Recorder().workers()))

    settings_of = lambda run_id: tuple(run_row(conn, run_id)[k] for k in ("rag_chat_model", "rag_retriever", "rag_top_k"))
    assert settings_of(rag_run) == ("test-chat", "vector", 5)
    assert settings_of(offline_run) == (None, None, None)


def answer_with_trace(sample):
    other = "TEST-9999"
    return rag_answer(
        sample,
        retrieved_chunks=[
            {"chunk_id": "c1", "policy_no": other, "score": 0.9},
            {"chunk_id": "c2", "policy_no": sample.policy_no, "score": 0.8},
        ],
        trace_details={"cards": {"candidate_nos": [other, sample.policy_no]}, "card_policy_nos": [other]},
    )


@pytest.mark.db
def test_execute_stores_retrieval_metrics_in_rag_mode(conn, golden_record):
    samples = make_samples(golden_record, 1)
    run_id = new_run(conn, samples, mode="rag")
    recorder = Recorder()

    async def answer(sample):
        return answer_with_trace(sample)

    asyncio.run(execute(conn, run_id, "rag", samples, Workers(answer, recorder.score)))

    metadata = fetch(conn, "SELECT metadata FROM ragas_evaluation_samples WHERE run_id = %s", [run_id])[0]["metadata"]
    retrieval = metadata["retrieval"]
    assert (retrieval["card_hit"], retrieval["candidate_hit"], retrieval["retrieval_hit"]) == (0, 1, 1)
    assert (retrieval["rank"], retrieval["hit_at_1"], retrieval["hit_at_3"]) == (2, 0, 1)
    assert retrieval["filter_hit"] is None


@pytest.mark.db
@pytest.mark.parametrize("mode", ["baseline", "offline"])
def test_execute_skips_retrieval_metrics_without_search(conn, golden_record, mode):
    samples = make_samples(golden_record, 1)
    run_id = new_run(conn, samples, mode=mode)
    recorder = Recorder()

    async def answer(sample):
        return answer_with_trace(sample)

    asyncio.run(execute(conn, run_id, mode, samples, Workers(answer, recorder.score)))

    metadata = fetch(conn, "SELECT metadata FROM ragas_evaluation_samples WHERE run_id = %s", [run_id])[0]["metadata"]
    assert "retrieval" not in metadata


@pytest.mark.db
def test_answer_failure_leaves_run_running(conn, golden_record, caplog):
    samples = make_samples(golden_record, 3)
    run_id = new_run(conn, samples)
    fake = Recorder(fail_answer={"single-000002"})

    with caplog.at_level(logging.WARNING, logger=runner.__name__):
        outcome = asyncio.run(execute(conn, run_id, "rag", samples, fake.workers()))

    assert outcome == RunOutcome(run_id, "RUNNING", 1, 0)
    assert get_run(conn, run_id).status == "RUNNING"
    assert "single-000002" in caplog.text and "TimeoutError" in caplog.text
    assert sorted(s for s, _, _ in fake.scored) == ["single-000001", "single-000003"]


@pytest.mark.db
def test_resume_calls_only_pending_samples(conn, golden_record):
    samples = make_samples(golden_record, 2)
    run_id = new_run(conn, samples)
    asyncio.run(execute(conn, run_id, "rag", samples, Recorder(fail_answer={"single-000002"}).workers()))
    retry = Recorder()

    outcome = asyncio.run(execute(conn, run_id, "rag", samples, retry.workers()))

    assert outcome.status == "SUCCEEDED"
    assert retry.answered == ["single-000002"]
    assert [s for s, _, _ in retry.scored] == ["single-000002"]


@pytest.mark.db
def test_resume_scores_with_stored_answer_without_calling_again(conn, golden_record):
    samples = make_samples(golden_record, 1)
    run_id = new_run(conn, samples)
    update_answer(conn, run_id, "single-000001", rag_answer(samples["single-000001"], answer="저장된 답변"))
    conn.commit()
    fake = Recorder()

    outcome = asyncio.run(execute(conn, run_id, "rag", samples, fake.workers()))

    assert outcome.status == "SUCCEEDED"
    assert fake.answered == []
    assert fake.scored == [("single-000001", "저장된 답변", ["근거 A", "근거 B"])]


@pytest.mark.db
def test_unexpected_error_marks_run_failed(conn, golden_record):
    samples = make_samples(golden_record, 1)
    run_id = new_run(conn, samples)

    with pytest.raises(RuntimeError, match="채점 코드 오류"):
        asyncio.run(execute(conn, run_id, "rag", samples, Recorder(score_error=RuntimeError("채점 코드 오류")).workers()))

    assert get_run(conn, run_id).status == "FAILED"


# ---- start_run, check_resumable (임시 DB) ----


@pytest.mark.db
def test_start_run_records_baseline_conditions(conn, golden_record):
    samples = make_samples(golden_record, 2)

    run_id = start_run(
        conn, samples.values(), dataset_version="golden_v1", repeat_no=2, mode="baseline", settings=fake_settings()
    )

    run = run_row(conn, run_id)
    assert (run["status"], run["mode"], run["repeat_no"]) == ("RUNNING", "baseline", 2)
    assert (run["rag_chat_model"], run["rag_retriever"], run["rag_top_k"]) == ("test-baseline", "none", None)
    assert run["metadata"] == {"prompt_version": "baseline-v0", "scoring_prompt": "answer_relevancy-ko-v1+answer_coverage-v1"}
    assert (run["judge_model"], run["embedding_model"]) == ("test-judge", "test-emb")
    count = fetch(conn, "SELECT COUNT(*) AS n FROM ragas_evaluation_samples WHERE run_id = %s", [run_id])[0]["n"]
    assert count == 2


@pytest.mark.db
def test_check_resumable_accepts_matching_running_run(conn, golden_record):
    run_id = start_run(
        conn, make_samples(golden_record, 1).values(), dataset_version="golden_v1", repeat_no=1, mode="offline",
        settings=fake_settings(),
    )

    check_resumable(conn, run_id, mode="offline", dataset_version="golden_v1", settings=fake_settings())


@pytest.mark.db
@pytest.mark.parametrize(
    ("finished", "mode", "version", "message"),
    [
        (True, "rag", "golden_v1", "RUNNING"),
        (False, "baseline", "golden_v1", "mode"),
        (False, "rag", "golden_v2", "dataset_version"),
    ],
)
def test_check_resumable_rejects_mismatch(conn, golden_record, finished, mode, version, message):
    run_id = new_run(conn, make_samples(golden_record, 1), mode="rag")
    if finished:
        finish_run(conn, run_id, "FAILED")
        conn.commit()

    with pytest.raises(ValueError, match=message):
        check_resumable(conn, run_id, mode=mode, dataset_version=version, settings=fake_settings())


@pytest.mark.db
def test_check_resumable_rejects_unknown_run(conn):
    with pytest.raises(ValueError, match="없음"):
        check_resumable(conn, UUID(int=0), mode="rag", dataset_version="golden_v1", settings=fake_settings())


# ---- run 전체 흐름 (임시 DB, 응답·채점은 가짜) ----


@pytest.fixture
def wired(monkeypatch, eval_schema_url, golden_record):
    """run()이 임시 DB와 가짜 응답·채점을 쓰도록 연결한다. samples·fake는 테스트가 바꿀 수 있다."""
    state = SimpleNamespace(samples=list(make_samples(golden_record, 2).values()), fake=Recorder())
    monkeypatch.setattr(runner, "get_settings", lambda: fake_settings(database_url=eval_schema_url))
    monkeypatch.setattr(runner, "load_evaluation_samples", lambda path, conn: state.samples)
    monkeypatch.setattr(runner, "load_golden", lambda path: state.samples)

    @asynccontextmanager
    async def fake_workers(settings, mode):
        yield state.fake.workers()

    monkeypatch.setattr(runner, "_workers", fake_workers)
    return state


@pytest.mark.db
def test_run_repeats_as_separate_runs(wired, eval_schema_url):
    outcomes = runner.run(golden_path=GOLDEN_PATH, repeat=2, offline=True)

    assert [o.status for o in outcomes] == ["SUCCEEDED", "SUCCEEDED"]
    with psycopg.connect(eval_schema_url) as c:
        runs = [run_row(c, o.run_id) for o in outcomes]
    assert [(r["mode"], r["repeat_no"], r["dataset_version"]) for r in runs] == [
        ("offline", 1, "golden_v1"),
        ("offline", 2, "golden_v1"),
    ]


@pytest.mark.db
def test_run_stops_repeating_when_run_is_incomplete(wired):
    wired.fake = Recorder(fail_answer={"single-000001"})

    outcomes = runner.run(golden_path=GOLDEN_PATH, repeat=3)

    assert [(o.status, o.pending_answer) for o in outcomes] == [("RUNNING", 1)]


@pytest.mark.db
def test_run_without_samples_creates_no_run(wired, eval_schema_url):
    wired.samples = []
    with psycopg.connect(eval_schema_url) as c:
        before = fetch(c, "SELECT COUNT(*) AS n FROM ragas_evaluation_run")[0]["n"]

    with pytest.raises(ValueError, match="평가할 샘플"):
        runner.run(golden_path=GOLDEN_PATH, repeat=1)

    with psycopg.connect(eval_schema_url) as c:
        assert fetch(c, "SELECT COUNT(*) AS n FROM ragas_evaluation_run")[0]["n"] == before


@pytest.mark.db
def test_run_resume_finishes_incomplete_run(wired):
    wired.fake = Recorder(fail_answer={"single-000002"})
    (first,) = runner.run(golden_path=GOLDEN_PATH, repeat=1)
    wired.fake = Recorder()

    (resumed,) = runner.run(golden_path=GOLDEN_PATH, repeat=1, resume=str(first.run_id))

    assert resumed == RunOutcome(first.run_id, "SUCCEEDED", 0, 0)
    assert wired.fake.answered == ["single-000002"]


@pytest.mark.db
def test_run_resume_fails_when_sample_missing_from_golden(wired):
    wired.fake = Recorder(fail_answer={"single-000002"})
    (first,) = runner.run(golden_path=GOLDEN_PATH, repeat=1)
    wired.samples = wired.samples[:1]

    with pytest.raises(ValueError, match="single-000002"):
        runner.run(golden_path=GOLDEN_PATH, repeat=1, resume=str(first.run_id))


@pytest.mark.db
def test_resume_rejects_golden_with_different_question(conn, golden_record):
    samples = make_samples(golden_record, 1)
    run_id = new_run(conn, samples)
    edited = {"single-000001": samples["single-000001"].model_copy(update={"question": "바뀐 질문"})}
    fake = Recorder()

    with pytest.raises(ValueError, match="single-000001"):
        asyncio.run(execute(conn, run_id, "rag", edited, fake.workers()))

    assert fake.answered == []
    assert get_run(conn, run_id).status == "RUNNING"


def test_connect_error_hides_connection_string():
    settings = fake_settings(database_url="postgresql://user:s3cret-pw@localhost:1/test_db?bad_option=x")

    with pytest.raises(RuntimeError) as exc:
        runner._connect(settings)

    assert "s3cret-pw" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


@pytest.mark.db
@pytest.mark.parametrize(
    ("overrides", "changed"),
    [
        ({"judge_model": "other-judge"}, "judge_model"),
        ({"embedding_model": "other-emb"}, "embedding_model"),
        ({"baseline_model": "other-baseline"}, "rag_chat_model"),
    ],
)
def test_check_resumable_rejects_changed_models(conn, golden_record, overrides, changed):
    run_id = start_run(
        conn, make_samples(golden_record, 1).values(), dataset_version="golden_v1", repeat_no=1, mode="baseline",
        settings=fake_settings(),
    )
    settings = SimpleNamespace(**{**vars(fake_settings()), **overrides})

    with pytest.raises(ValueError, match=changed):
        check_resumable(conn, run_id, mode="baseline", dataset_version="golden_v1", settings=settings)


def rag_http(statuses, rag_payload):
    """응답 상태 코드를 차례로 돌려주는 가짜 RAG 서버. calls에 호출 횟수를 남긴다."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        status = statuses[min(len(calls), len(statuses)) - 1]
        return httpx.Response(status, json=rag_payload if status == 200 else {})

    return httpx.AsyncClient(base_url="http://rag.test", transport=httpx.MockTransport(handler)), calls


@pytest.mark.parametrize(("statuses", "expected_calls"), [([503, 200], 2), ([503, 503, 200], 3)])
def test_rag_call_retries_server_errors(golden_record, rag_payload, statuses, expected_calls):
    http, calls = rag_http(statuses, rag_payload)

    result = asyncio.run(runner.call_rag_with_retry(http, GoldenSample.model_validate(golden_record), 0))

    assert result.answer == rag_payload["answer"]
    assert len(calls) == expected_calls


@pytest.mark.parametrize(("statuses", "expected_calls"), [([503], 3), ([400], 1)])
def test_rag_call_gives_up_after_retries_or_on_client_error(golden_record, rag_payload, statuses, expected_calls):
    http, calls = rag_http(statuses, rag_payload)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(runner.call_rag_with_retry(http, GoldenSample.model_validate(golden_record), 0))

    assert len(calls) == expected_calls


def test_rag_call_does_not_retry_contract_violation(golden_record, rag_payload):
    http, calls = rag_http([200], {"answer": "계약 위반 응답"})

    with pytest.raises(ValueError, match="계약 위반"):
        asyncio.run(runner.call_rag_with_retry(http, GoldenSample.model_validate(golden_record), 0))

    assert len(calls) == 1


@pytest.mark.db
def test_rag_settings_change_during_run_marks_failed(conn, golden_record):
    samples = make_samples(golden_record, 2)
    run_id = new_run(conn, samples)
    asyncio.run(execute(conn, run_id, "rag", samples, Recorder(fail_answer={"single-000002"}).workers()))
    redeployed = Recorder()

    async def answer_with_new_model(sample):
        return rag_answer(sample, chat_model="new-chat")

    with pytest.raises(ValueError, match="chat_model"):
        asyncio.run(execute(conn, run_id, "rag", samples, Workers(answer_with_new_model, redeployed.score)))

    assert get_run(conn, run_id).status == "FAILED"
    answer = fetch(conn, "SELECT answer FROM ragas_evaluation_samples WHERE run_id = %s AND sample_id = %s",
                   [run_id, "single-000002"])[0]["answer"]
    assert answer is None



@pytest.mark.db
def test_start_run_records_scoring_prompt_in_every_mode(conn, golden_record):
    run_id = start_run(
        conn, make_samples(golden_record, 1).values(), dataset_version="golden_v1", repeat_no=1, mode="rag",
        settings=fake_settings(),
    )

    assert run_row(conn, run_id)["metadata"] == {"scoring_prompt": "answer_relevancy-ko-v1+answer_coverage-v1"}


@pytest.mark.db
def test_check_resumable_rejects_run_scored_with_other_prompt(conn, golden_record):
    run_id = new_run(conn, make_samples(golden_record, 1), mode="rag")

    with pytest.raises(ValueError, match="scoring_prompt"):
        check_resumable(conn, run_id, mode="rag", dataset_version="golden_v1", settings=fake_settings())


@pytest.mark.db
def test_check_resumable_rejects_run_started_before_answer_coverage(conn, golden_record):
    # 커버리지 추가 전 run을 재개하면 남은 샘플에만 커버리지가 채워진다
    run_id = insert_run(
        conn, dataset_version="golden_v1", repeat_no=1, mode="rag", judge_model="test-judge",
        embedding_model="test-emb", metadata={"scoring_prompt": "answer_relevancy-ko-v1"},
    )
    conn.commit()

    with pytest.raises(ValueError, match="scoring_prompt"):
        check_resumable(conn, run_id, mode="rag", dataset_version="golden_v1", settings=fake_settings())
