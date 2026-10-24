# 평가 결과 마크다운 성적표 생성 테스트 (임시 PostgreSQL, 실제 LLM 호출 없음)
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import SecretStr

from ragas_eval.dataset.dataset import GoldenSample
from ragas_eval.rag_client.rag_client import RagResult
from ragas_eval.report import report
from ragas_eval.report.report import ReportData, load_report_data, render, report_path
from ragas_eval.repository.repository import finish_run, insert_run, insert_samples, update_answer, update_scores
from ragas_eval.scorer.scorer import ScoreResult

KST = timezone(timedelta(hours=9))
RUN_ID = UUID("00000000-0000-0000-0000-00000000abcd")


def run_row(**overrides):
    row = {
        "run_id": RUN_ID,
        "dataset_version": "golden_v1",
        "repeat_no": 2,
        "mode": "baseline",
        "status": "SUCCEEDED",
        "rag_chat_model": "test-chat",
        "rag_retriever": "none",
        "rag_top_k": None,
        "judge_model": "test-judge",
        "embedding_model": "test-emb",
        "sample_count": 2,
        "avg_context_precision": None,
        "avg_context_recall": None,
        "avg_faithfulness": None,
        "avg_answer_relevancy": Decimal("0.812"),
        "avg_factual_correctness": Decimal("0.400"),
        "avg_reference_faithfulness": Decimal("0.550"),
        "avg_answer_coverage": Decimal("0.900"),
        "started_at": datetime(2026, 10, 3, 10, 0, tzinfo=KST),
        "finished_at": datetime(2026, 10, 3, 10, 5, tzinfo=KST),
        "metadata": {"prompt_version": "baseline-v0", "scoring_prompt": "answer_relevancy-ko-v1"},
    }
    return {**row, **overrides}


def sample_row(sample_id, factual, **overrides):
    row = {
        "sample_id": sample_id,
        "policy_no": "TEST-0001",
        "question": f"{sample_id} 질문",
        "answer": f"{sample_id} 답변",
        "context_precision": None,
        "context_recall": None,
        "faithfulness": None,
        "answer_relevancy": Decimal("0.800"),
        "factual_correctness": factual,
        "reference_faithfulness": Decimal("0.500"),
        "answer_coverage": Decimal("0.750"),
        "latency_ms": 1200,
        "errors": None,
    }
    return {**row, **overrides}


def report_data(**overrides):
    fields = {
        "run": run_row(),
        "mode_averages": [
            {
                "mode": "baseline",
                "runs": 3,
                "avg_answer_relevancy": Decimal("0.800"),
                "avg_factual_correctness": Decimal("0.400"),
                "avg_reference_faithfulness": Decimal("0.500"),
                "avg_answer_coverage": Decimal("0.300"),
            },
            {
                "mode": "rag",
                "runs": 3,
                "avg_answer_relevancy": Decimal("0.900"),
                "avg_factual_correctness": Decimal("0.700"),
                "avg_reference_faithfulness": Decimal("0.850"),
                "avg_answer_coverage": Decimal("0.950"),
            },
        ],
        "samples": [sample_row("single-000001", Decimal("0.900")), sample_row("single-000002", Decimal("0.100"))],
    }
    return ReportData(**{**fields, **overrides})


# ---- render (순수 함수) ----


def test_render_shows_run_conditions_and_averages():
    text = render(report_data())

    assert text.startswith("# RAGAS 평가 결과: golden_v1 / baseline / 반복 2")
    assert str(RUN_ID) in text and "SUCCEEDED" in text
    assert "| answer_relevancy | 0.812 |" in text
    assert "| answer_coverage | 0.900 |" in text
    assert "| context_precision | - |" in text
    assert "baseline-v0" in text and "test-judge" in text
    assert "| 채점 프롬프트 | answer_relevancy-ko-v1 |" in text


def test_render_compares_modes_with_rag_minus_baseline():
    text = render(report_data())

    assert "| factual_correctness | 0.400 | 0.700 | +0.300 |" in text
    assert "| reference_faithfulness | 0.500 | 0.850 | +0.350 |" in text
    assert "| answer_coverage | 0.300 | 0.950 | +0.650 |" in text


def test_render_without_both_modes_has_no_difference_column():
    averages = [{"mode": "offline", "runs": 1, "avg_factual_correctness": Decimal("0.950")}]

    text = render(report_data(mode_averages=averages))

    assert "rag - baseline" not in text
    assert "| factual_correctness | 0.950 |" in text


def test_render_lists_lowest_samples_first_with_pending_counts():
    samples = [
        sample_row("single-000001", Decimal("0.900")),
        sample_row("single-000002", Decimal("0.100"), errors={"faithfulness": "TimeoutError"}),
        sample_row("single-000003", None, answer=None),
    ]

    text = render(report_data(samples=samples))

    low = text.split("## 점수 낮은 샘플")[1]
    assert low.index("single-000002") < low.index("single-000001")
    assert "single-000003" not in low
    assert "| 미응답 | 1 |" in text and "| 채점 실패 지표가 있는 샘플 | 1 |" in text
    assert "TimeoutError" in low


def test_render_shows_answer_coverage_in_low_samples():
    samples = [sample_row("single-000001", Decimal("0.100"), answer_coverage=Decimal("1.000"))]

    text = render(report_data(samples=samples))

    low = text.split("## 점수 낮은 샘플")[1]
    assert "| factual_correctness | answer_coverage | reference_faithfulness |" in low
    assert "| 0.100 | 1.000 | 0.500 |" in low


def test_render_shows_truncated_answer_in_low_samples():
    samples = [sample_row("single-000001", Decimal("0.100"), answer="가" * 250)]

    text = render(report_data(samples=samples))

    low = text.split("## 점수 낮은 샘플")[1]
    assert "| 답변 |" in low
    assert "가" * 200 + "…" in low and "가" * 201 not in low


def test_render_escapes_table_breaking_characters():
    samples = [sample_row("single-000001", Decimal("0.100"), question="소득 | 조건\n알려줘")]

    text = render(report_data(samples=samples))

    assert "소득 \\| 조건 알려줘" in text


def test_render_marks_unfinished_run():
    text = render(report_data(run=run_row(status="RUNNING", finished_at=None, sample_count=None)))

    assert "RUNNING" in text and "집계 전" in text


def test_report_path_names_file_by_run_conditions(tmp_path):
    path = report_path(run_row(), tmp_path)

    assert path == tmp_path / "golden_v1" / f"golden_v1_baseline_r2_{RUN_ID}.md"


# ---- DB 조회 (임시 DB) ----


def seed_run(conn, golden_record, *, dataset_version, mode, factual, finished=True):
    run_id = insert_run(
        conn,
        dataset_version=dataset_version,
        repeat_no=1,
        mode=mode,
        judge_model="test-judge",
        embedding_model="test-emb",
    )
    sample = GoldenSample.model_validate(golden_record)
    insert_samples(conn, run_id, [sample])
    update_answer(conn, run_id, sample.sample_id, RagResult(answer="답변", retrieved_contexts=[], chunks=[]))
    update_scores(conn, run_id, sample.sample_id, ScoreResult(factual_correctness=factual, answer_relevancy=0.5))
    if finished:
        finish_run(conn, run_id, "SUCCEEDED")
    return run_id


@pytest.mark.db
def test_load_report_data_reads_run_samples_and_mode_averages(eval_conn, golden_record):
    version = f"golden_v{uuid4().int % 10**9}"
    run_id = seed_run(eval_conn, golden_record, dataset_version=version, mode="rag", factual=0.8)
    seed_run(eval_conn, golden_record, dataset_version=version, mode="rag", factual=0.6)
    seed_run(eval_conn, golden_record, dataset_version=version, mode="baseline", factual=0.2)
    seed_run(eval_conn, golden_record, dataset_version=version, mode="baseline", factual=0.9, finished=False)

    data = load_report_data(eval_conn, run_id)

    assert (data.run["run_id"], data.run["status"]) == (run_id, "SUCCEEDED")
    averages = {row["mode"]: row for row in data.mode_averages}
    assert (averages["rag"]["runs"], averages["rag"]["avg_factual_correctness"]) == (2, Decimal("0.700"))
    assert (averages["baseline"]["runs"], averages["baseline"]["avg_factual_correctness"]) == (1, Decimal("0.200"))
    assert [s["sample_id"] for s in data.samples] == [golden_record["sample_id"]]
    assert data.samples[0]["errors"] is None


@pytest.mark.db
def test_load_report_data_rejects_unknown_run(eval_conn):
    with pytest.raises(ValueError, match="run이 없음"):
        load_report_data(eval_conn, UUID(int=0))


@pytest.mark.db
def test_report_writes_markdown_file(eval_schema_url, golden_record, monkeypatch, tmp_path):
    with psycopg.connect(eval_schema_url) as conn:
        run_id = seed_run(conn, golden_record, dataset_version="golden_v1", mode="offline", factual=1.0)
        conn.commit()
    monkeypatch.setattr(report, "get_settings", lambda: SimpleNamespace(database_url=SecretStr(eval_schema_url)))

    path = report.report(run_id, out_dir=tmp_path / "results")

    assert path == tmp_path / "results" / "golden_v1" / f"golden_v1_offline_r1_{run_id}.md"
    assert "# RAGAS 평가 결과: golden_v1 / offline / 반복 1" in path.read_text(encoding="utf-8")

@pytest.mark.db
def test_load_report_data_reads_answer_coverage(eval_conn, golden_record):
    version = f"golden_v{uuid4().int % 10**9}"
    run_id = seed_run(eval_conn, golden_record, dataset_version=version, mode="rag", factual=0.2, finished=False)
    update_scores(
        eval_conn, run_id, golden_record["sample_id"], ScoreResult(factual_correctness=0.2, answer_coverage=1.0)
    )
    finish_run(eval_conn, run_id, "SUCCEEDED")

    data = load_report_data(eval_conn, run_id)

    assert data.samples[0]["answer_coverage"] == Decimal("1.000")
    assert data.run["avg_answer_coverage"] == Decimal("1.000")
    (rag,) = data.mode_averages
    assert rag["avg_answer_coverage"] == Decimal("1.000")


@pytest.mark.db
def test_mode_averages_only_include_runs_with_same_scoring_prompt(eval_conn, golden_record):
    version = f"golden_v{uuid4().int % 10**9}"
    korean = seed_run(eval_conn, golden_record, dataset_version=version, mode="baseline", factual=0.8)
    english = seed_run(eval_conn, golden_record, dataset_version=version, mode="baseline", factual=0.2)
    for run_id, prompt in ((korean, "answer_relevancy-ko-v1"), (english, None)):
        if prompt:
            eval_conn.execute(
                "UPDATE ragas_evaluation_run SET metadata = jsonb_build_object('scoring_prompt', %s::text) WHERE run_id = %s",
                [prompt, run_id],
            )

    data = load_report_data(eval_conn, korean)

    (baseline,) = data.mode_averages
    assert (baseline["runs"], baseline["avg_factual_correctness"]) == (1, Decimal("0.800"))
