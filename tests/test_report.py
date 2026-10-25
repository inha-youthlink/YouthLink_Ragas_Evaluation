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


def clarify_row(sample_id, reason):
    unscored = {name: None for name in ("answer_relevancy", "reference_faithfulness", "answer_coverage")}
    return sample_row(sample_id, None, answer="되묻기 문구", handler="clarify", clarify_reason=reason, **unscored)


def test_render_counts_clarify_samples_apart_from_low_samples():
    samples = [
        sample_row("single-000001", Decimal("0.900")),
        clarify_row("single-000002", "condition"),
        clarify_row("single-000003", "policy"),
    ]

    text = render(report_data(samples=samples))

    clarify = text.split("## 되묻기 응답 (채점 제외 2건)")[1].split("\n## ")[0]
    assert "| 조건 되묻기 (condition) | 1 |" in clarify and "| 정책 되묻기 (policy) | 1 |" in clarify
    assert "single-000002" in clarify and "single-000003" in clarify
    low = text.split("## 점수 낮은 샘플")[1]
    assert "single-000002" not in low and "single-000001" in low


def test_render_without_clarify_has_no_clarify_section():
    text = render(report_data())

    assert "## 되묻기 응답" not in text


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


def retrieval(card, candidate, retrieved, filtered=None, rank=None):
    hits = {f"hit_at_{k}": None if retrieved is None else int(rank is not None and rank <= k) for k in (1, 3, 5, 10)}
    reciprocal = None if retrieved is None else (1 / rank if rank else 0.0)
    return {
        "card_hit": card,
        "candidate_hit": candidate,
        "retrieval_hit": retrieved,
        "filter_hit": filtered,
        "rank": rank,
        **hits,
        "reciprocal_rank": reciprocal,
    }


def rag_report_data(*retrievals):
    samples = [
        sample_row(f"single-00000{i}", Decimal("0.500"), retrieval=r) for i, r in enumerate(retrievals, start=1)
    ]
    return report_data(run=run_row(mode="rag"), samples=samples)


def test_render_shows_retrieval_rates_by_stage():
    data = rag_report_data(retrieval(1, 1, 1, filtered=1, rank=1), retrieval(0, 1, 1, rank=4))

    text = render(data)

    section = text.split("## 검색 적중률")[1].split("\n## ")[0]
    assert "| 1차 필터 | filter_hit | 1.000 | 1 |" in section
    assert "| 검색 | retrieval_hit | 1.000 | 2 |" in section
    assert "| 검색 | hit_at_1 | 0.500 | 2 |" in section
    assert "| 검색 | hit_at_5 | 1.000 | 2 |" in section
    assert "| 검색 | MRR | 0.625 | 2 |" in section
    assert "| 후보 | candidate_hit | 1.000 | 2 |" in section
    assert "| 카드 | card_hit | 0.500 | 2 |" in section


def test_render_counts_samples_by_dropped_stage():
    data = rag_report_data(
        retrieval(1, 1, 1, rank=1),
        retrieval(0, 1, 1, rank=2),
        retrieval(0, 0, 1, rank=3),
        retrieval(0, 0, 0, filtered=1),
        retrieval(0, 0, 0, filtered=0),
        retrieval(None, None, None),
    )

    text = render(data)

    section = text.split("## 검색 적중률")[1].split("\n## ")[0]
    for cause in ("정상", "카드 선택", "후보 선택", "검색", "1차 필터", "판단 불가"):
        assert f"| {cause} | 1 |" in section


def test_render_without_retrieval_metrics_has_no_retrieval_section():
    text = render(report_data())

    assert "## 검색 적중률" not in text


def routing_run(**overrides):
    summary = {
        "sample_count": 4,
        "accuracy": 0.5,
        "fallback_rate": 0.25,
        "by_intent": {"policy_list": {"total": 2, "correct": 1}, "chitchat": {"total": 1, "correct": 1}},
        "confusion": {"policy_list": {"policy_list": 1, "policy_detail": 1}, "chitchat": {"chitchat": 1}},
        "analysis_prompt_versions": ["analyze_v3", "analyze_v4"],
    }
    metadata = {"prompt_version": "rag-v1", "routing": summary}
    return run_row(**{"mode": "routing", "metadata": metadata, **overrides})


def routing_sample(sample_id, expected, predicted, correct, fallback=None, **overrides):
    routing = {
        "expected_intent": expected,
        "predicted_intent": predicted,
        "intent_correct": correct,
        "fallback_reason": fallback,
        "prompt_version": "analyze_v3",
    }
    return sample_row(sample_id, None, **{"policy_no": None, "routing": routing, **overrides})


def routing_report_data(**overrides):
    samples = [
        routing_sample("route-000001", ["policy_list"], ["policy_list"], 1),
        routing_sample("route-000002", ["policy_list"], ["policy_detail"], 0, question="주택 | 정책\n알려줘"),
        routing_sample("route-000003", ["comparison", "eligibility"], [], 0, fallback="llm_error"),
        routing_sample("route-000004", ["chitchat"], None, None),
    ]
    return report_data(**{"run": routing_run(), "samples": samples, **overrides})


def test_render_routing_uses_routing_title_and_skips_ragas_sections():
    text = render(routing_report_data())

    assert text.startswith("# 라우팅 평가 결과: golden_v1 / routing / 반복 2")
    for ragas_only in ("## 지표 평균", "## 모드별 비교", "## 점수 낮은 샘플", "## 검색 적중률", "채점 모델"):
        assert ragas_only not in text
    assert "| 프롬프트 버전 | rag-v1 |" in text


def test_render_routing_accuracy_section_reads_run_summary():
    text = render(routing_report_data())

    section = text.split("## 라우팅 정확도")[1].split("\n### ")[0]
    assert "| 정확도 | 0.500 |" in section
    assert "| 분석 실패율 | 0.250 |" in section
    assert "| 채점된 샘플 수 | 4 |" in section
    assert "| 분석 프롬프트 버전 | analyze_v3, analyze_v4 |" in section


def test_render_routing_before_aggregation_shows_pending_without_breakdowns():
    run = routing_run(status="RUNNING", finished_at=None, sample_count=None, metadata={"prompt_version": "rag-v1"})

    text = render(routing_report_data(run=run))

    section = text.split("## 라우팅 정확도")[1].split("\n## ")[0]
    assert "집계 전" in section
    assert "### 유형별 정확도" not in text and "### 혼동 행렬" not in text


def test_render_routing_accuracy_by_intent_in_fixed_order_with_empty_intents():
    text = render(routing_report_data())

    section = text.split("### 유형별 정확도")[1].split("\n### ")[0]
    assert "| policy_list | 2 | 1 | 0.500 |" in section
    assert "| chitchat | 1 | 1 | 1.000 |" in section
    assert "| policy_detail | 0 | 0 | - |" in section
    order = ["policy_list", "policy_detail", "eligibility", "comparison", "chitchat", "out_of_scope", "unclear"]
    positions = [section.index(f"| {intent} |") for intent in order]
    assert positions == sorted(positions)


def test_render_routing_confusion_matrix_rows_expected_columns_predicted():
    text = render(routing_report_data())

    heading, section = text.split("### 혼동 행렬")[1].split("\n\n", 1)
    assert "하위 질문 1개" in heading
    header = section.splitlines()[0]
    assert header.startswith("| 기대 \\ 예측 | policy_list | policy_detail |") and header.endswith("| unclear |")
    assert "| policy_list | 1 | 1 | 0 | 0 | 0 | 0 | 0 |" in section
    assert "| chitchat | 0 | 0 | 0 | 0 | 1 | 0 | 0 |" in section


def test_render_routing_lists_only_wrong_samples_with_intents_and_fallback():
    text = render(routing_report_data())

    wrong = text.split("## 틀린 샘플")[1]
    assert "route-000001" not in wrong and "route-000004" not in wrong
    assert "| route-000002 | 주택 \\| 정책 알려줘 | policy_list | policy_detail | - |" in wrong
    assert "| route-000003 | route-000003 질문 | comparison, eligibility | - | llm_error |" in wrong


def test_render_routing_without_wrong_samples_says_none():
    samples = [routing_sample("route-000001", ["policy_list"], ["policy_list"], 1)]

    text = render(routing_report_data(samples=samples))

    assert "틀린 샘플 없음" in text.split("## 틀린 샘플")[1]


def test_render_routing_progress_counts_unanswered_and_unscorable_samples():
    samples = [
        routing_sample("route-000001", ["policy_list"], ["policy_list"], 1),
        routing_sample("route-000002", ["chitchat"], None, None),
        routing_sample("route-000003", ["chitchat"], None, None, answer=None),
    ]

    text = render(routing_report_data(samples=samples))

    assert "| 미응답 | 1 |" in text
    assert "| 채점 불가(analysis 없음) | 1 |" in text


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
def test_load_report_data_reads_retrieval_metrics(eval_conn, golden_record):
    version = f"golden_v{uuid4().int % 10**9}"
    run_id = insert_run(
        eval_conn, dataset_version=version, repeat_no=1, mode="rag", judge_model="test-judge", embedding_model="test-emb"
    )
    sample = GoldenSample.model_validate(golden_record)
    insert_samples(eval_conn, run_id, [sample])
    metrics = retrieval(1, 1, 1, rank=1)
    result = RagResult(answer="답변", retrieved_contexts=[], chunks=[])
    update_answer(eval_conn, run_id, sample.sample_id, result, retrieval=metrics)

    data = load_report_data(eval_conn, run_id)

    assert data.samples[0]["retrieval"] == metrics


@pytest.mark.db
def test_load_report_data_reads_routing_metrics(eval_conn, golden_record):
    version = f"golden_v{uuid4().int % 10**9}"
    run_id = insert_run(
        eval_conn, dataset_version=version, repeat_no=1, mode="routing", judge_model="test-judge", embedding_model="test-emb"
    )
    sample = GoldenSample.model_validate(golden_record)
    insert_samples(eval_conn, run_id, [sample])
    eval_conn.execute(
        "UPDATE ragas_evaluation_samples SET metadata = jsonb_build_object('routing', %s::jsonb) WHERE run_id = %s",
        ['{"intent_correct": 0, "expected_intent": ["chitchat"]}', run_id],
    )

    data = load_report_data(eval_conn, run_id)

    assert data.samples[0]["routing"] == {"intent_correct": 0, "expected_intent": ["chitchat"]}


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
