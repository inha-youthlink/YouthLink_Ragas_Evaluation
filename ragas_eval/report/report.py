# 평가 테이블을 읽어 성적표·모드별 비교를 마크다운으로 results/<dataset_version>/에 저장
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ragas_eval.config import get_settings
from ragas_eval.scorer.scorer import METRIC_NAMES

DEFAULT_OUT_DIR = "results"
DB_CONNECT_TIMEOUT_SECONDS = 10
LOW_SAMPLE_COUNT = 10
QUESTION_MAX_CHARS = 60
ANSWER_MAX_CHARS = 200
# baseline 대비 rag 개선폭 (PRD "베이스라인 모드" 비교 지표 포함 7개 전부)
COMPARE_MODES = ("baseline", "rag")
# 정책을 좁히는 순서대로 (단계, 표시 이름, metadata.retrieval 키). MRR은 역순위의 평균
RETRIEVAL_ROWS = (
    ("1차 필터", "filter_hit", "filter_hit"),
    ("검색", "retrieval_hit", "retrieval_hit"),
    *(("검색", f"hit_at_{k}", f"hit_at_{k}") for k in (1, 3, 5, 10)),
    ("검색", "MRR", "reciprocal_rank"),
    ("후보", "candidate_hit", "candidate_hit"),
    ("카드", "card_hit", "card_hit"),
)
# 라우팅 보고서의 유형 표시 순서 (PRD "라우팅 평가")
ROUTING_INTENTS = ("policy_list", "policy_detail", "eligibility", "comparison", "chitchat", "out_of_scope", "unclear")
DROP_CAUSES = ("정상", "카드 선택", "후보 선택", "검색", "1차 필터", "판단 불가")

SELECT_RUN = "SELECT * FROM ragas_evaluation_run WHERE run_id = %(run_id)s"

# 같은 데이터셋의 완료된 run을 모드별로 평균낸다 (반복 실행 노이즈 완화)
SELECT_MODE_AVERAGES = sql.SQL(
    """
    SELECT mode, COUNT(*) AS runs, {averages}
    FROM ragas_evaluation_run
    WHERE dataset_version = %(dataset_version)s AND status = 'SUCCEEDED'
      AND metadata->>'scoring_prompt' IS NOT DISTINCT FROM %(scoring_prompt)s
    GROUP BY mode
    ORDER BY mode
"""
).format(
    averages=sql.SQL(", ").join(
        sql.SQL("ROUND(AVG({col}), 3) AS {col}").format(col=sql.Identifier(f"avg_{name}")) for name in METRIC_NAMES
    )
)

SELECT_SAMPLES = sql.SQL(
    """
    SELECT sample_id, policy_no, question, answer, {metrics}, latency_ms, metadata->'errors' AS errors,
           metadata->'retrieval' AS retrieval,
           metadata->'routing' AS routing
    FROM ragas_evaluation_samples
    WHERE run_id = %(run_id)s
    ORDER BY sample_id
"""
).format(metrics=sql.SQL(", ").join(sql.Identifier(name) for name in METRIC_NAMES))


class ReportData(NamedTuple):
    run: Mapping[str, Any]
    mode_averages: Sequence[Mapping[str, Any]]
    samples: Sequence[Mapping[str, Any]]


def report(run_id: UUID, out_dir: str | Path = DEFAULT_OUT_DIR) -> Path:
    settings = get_settings()
    try:
        conn = psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=DB_CONNECT_TIMEOUT_SECONDS)
    except psycopg.Error as e:
        # DSN 파싱 오류 메시지에 접속 문자열 일부가 남을 수 있어 에러 종류만 남긴다
        raise RuntimeError(f"DB 연결 실패: {type(e).__name__}") from None
    with conn:
        conn.read_only = True
        data = load_report_data(conn, run_id)
    path = report_path(data.run, Path(out_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data), encoding="utf-8")
    return path


def load_report_data(conn: psycopg.Connection, run_id: UUID) -> ReportData:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SELECT_RUN, {"run_id": run_id})
        run = cur.fetchone()
        if run is None:
            raise ValueError(f"run이 없음: {run_id}")
        # 채점 프롬프트가 다른 run은 점수 기준이 달라 함께 평균내지 않는다
        params = {
            "dataset_version": run["dataset_version"],
            "scoring_prompt": (run["metadata"] or {}).get("scoring_prompt"),
        }
        cur.execute(SELECT_MODE_AVERAGES, params)
        mode_averages = cur.fetchall()
        cur.execute(SELECT_SAMPLES, {"run_id": run_id})
        samples = cur.fetchall()
    return ReportData(run=run, mode_averages=mode_averages, samples=samples)


def report_path(run: Mapping[str, Any], out_dir: Path) -> Path:
    return out_dir / run["dataset_version"] / f"{run['dataset_version']}_{run['mode']}_r{run['repeat_no']}_{run['run_id']}.md"


def render(data: ReportData) -> str:
    run = data.run
    if run["mode"] == "routing":
        return _render_routing(data)
    sections = [
        f"# RAGAS 평가 결과: {run['dataset_version']} / {run['mode']} / 반복 {run['repeat_no']}",
        _summary(run, data.samples),
        _conditions(run),
        _averages(run),
        _retrieval(data.samples),
        _mode_comparison(run["dataset_version"], data.mode_averages),
        _progress(data.samples),
        _low_samples(data.samples),
    ]
    return "\n\n".join(section for section in sections if section) + "\n"


def _render_routing(data: ReportData) -> str:
    """PRD "라우팅 평가": 채점 LLM이 없어 RAGAS 지표 구간 대신 유형 정확도·혼동 행렬·틀린 샘플을 보인다."""
    run = data.run
    sections = [
        f"# 라우팅 평가 결과: {run['dataset_version']} / {run['mode']} / 반복 {run['repeat_no']}",
        _summary(run, data.samples),
        _routing_conditions(run),
        _routing_accuracy(run),
        _routing_progress(data.samples),
        _routing_wrong_samples(data.samples),
    ]
    return "\n\n".join(section for section in sections if section) + "\n"


def _summary(run: Mapping[str, Any], samples: Sequence[Mapping[str, Any]]) -> str:
    finished = run["sample_count"] is not None
    status = run["status"] if finished else f"{run['status']} (집계 전)"
    sample_count = run["sample_count"] if finished else f"{len(samples)} (집계 전)"
    period = f"{_time(run['started_at'])} ~ {_time(run['finished_at'])}"
    rows = [["run_id", run["run_id"]], ["상태", status], ["실행 시각", period], ["샘플 수", sample_count]]
    return _table(["항목", "값"], rows)


def _conditions(run: Mapping[str, Any]) -> str:
    rows = [
        ["답변 모델", run["rag_chat_model"]],
        ["검색 방식", run["rag_retriever"]],
        ["top_k", run["rag_top_k"]],
        ["채점 모델", run["judge_model"]],
        ["임베딩 모델", run["embedding_model"]],
        ["프롬프트 버전", (run["metadata"] or {}).get("prompt_version")],
        ["채점 프롬프트", (run["metadata"] or {}).get("scoring_prompt")],
    ]
    return "## 실행 조건\n\n" + _table(["항목", "값"], [[k, _text(v)] for k, v in rows])


def _routing_conditions(run: Mapping[str, Any]) -> str:
    rows = [
        ["답변 모델", run["rag_chat_model"]],
        ["검색 방식", run["rag_retriever"]],
        ["top_k", run["rag_top_k"]],
        ["프롬프트 버전", (run["metadata"] or {}).get("prompt_version")],
    ]
    return "## 실행 조건\n\n" + _table(["항목", "값"], [[k, _text(v)] for k, v in rows])


def _routing_accuracy(run: Mapping[str, Any]) -> str:
    summary = (run["metadata"] or {}).get("routing")
    if not summary:
        return "## 라우팅 정확도\n\n집계 전"
    rows = [
        ["정확도", _score(summary.get("accuracy"))],
        ["분석 실패율", _score(summary.get("fallback_rate"))],
        ["채점된 샘플 수", summary.get("sample_count", "-")],
        ["분석 프롬프트 버전", _text(", ".join(summary.get("analysis_prompt_versions") or []))],
    ]
    by_intent = summary.get("by_intent") or {}
    confusion = summary.get("confusion") or {}
    # 정해진 7개 밖의 유형이 나와도 표에서 빠지지 않게 뒤에 붙인다
    seen = {*by_intent, *confusion, *(p for row in confusion.values() for p in row)}
    intents = [*ROUTING_INTENTS, *sorted(seen - set(ROUTING_INTENTS))]
    accuracy_rows = []
    for intent in intents:
        counts = by_intent.get(intent, {"total": 0, "correct": 0})
        rate = _score(counts["correct"] / counts["total"]) if counts["total"] else "-"
        accuracy_rows.append([intent, counts["total"], counts["correct"], rate])
    matrix_rows = [[e, *(confusion.get(e, {}).get(p, 0) for p in intents)] for e in intents]
    return "\n\n".join(
        [
            "## 라우팅 정확도",
            _table(["항목", "값"], rows),
            "### 유형별 정확도 (하위 질문 1개인 샘플만)",
            _table(["유형", "샘플 수", "정답 수", "정확도"], accuracy_rows),
            "### 혼동 행렬 (행: 기대 유형, 열: 응답 유형, 하위 질문 1개인 샘플만)",
            _table(["기대 \\ 예측", *intents], matrix_rows),
        ]
    )


def _routing_progress(samples: Sequence[Mapping[str, Any]]) -> str:
    answered = [s for s in samples if s["answer"] is not None]
    unscorable = [s for s in answered if (s.get("routing") or {}).get("intent_correct") is None]
    latencies = [s["latency_ms"] for s in samples if s["latency_ms"] is not None]
    rows = [
        ["미응답", len(samples) - len(answered)],
        ["채점 불가(analysis 없음)", len(unscorable)],
        ["평균 응답 시간(ms)", round(sum(latencies) / len(latencies)) if latencies else "-"],
    ]
    return "## 응답 현황\n\n" + _table(["항목", "값"], rows)


def _routing_wrong_samples(samples: Sequence[Mapping[str, Any]]) -> str:
    wrong = [(s, s["routing"]) for s in samples if (s.get("routing") or {}).get("intent_correct") == 0]
    rows = [
        [
            s["sample_id"],
            _text(s["question"], QUESTION_MAX_CHARS),
            _text(", ".join(r.get("expected_intent") or [])),
            _text(", ".join(r.get("predicted_intent") or [])),
            _text(r.get("fallback_reason")),
        ]
        for s, r in wrong
    ]
    headers = ["sample_id", "질문", "기대 유형", "응답 유형", "fallback_reason"]
    return f"## 틀린 샘플 ({len(wrong)}건)\n\n" + (_table(headers, rows) if rows else "틀린 샘플 없음")


def _averages(run: Mapping[str, Any]) -> str:
    rows = [[name, _score(run[f"avg_{name}"])] for name in METRIC_NAMES]
    return "## 지표 평균\n\n" + _table(["지표", "평균"], rows)


def _retrieval(samples: Sequence[Mapping[str, Any]]) -> str:
    """PRD "검색 적중률 평가": 단계별 적중률과 정답 정책이 빠진 단계별 샘플 수. 검색 없는 run은 생략."""
    metrics = [s["retrieval"] for s in samples if s.get("retrieval")]
    if not metrics:
        return ""
    rows = []
    for stage, name, key in RETRIEVAL_ROWS:
        # NULL(판단할 수 없음)은 0점이 아니라 평균에서 뺀다
        values = [m[key] for m in metrics if m.get(key) is not None]
        rows.append([stage, name, _score(sum(values) / len(values)) if values else "-", len(values)])
    causes = [_drop_cause(m) for m in metrics]
    title = f"## 검색 적중률 (검색 결과가 저장된 샘플 {len(metrics)}건)"
    return "\n\n".join(
        [
            title,
            _table(["단계", "지표", "평균", "계산 대상 샘플"], rows),
            "### 정답 정책이 빠진 단계",
            _table(["원인", "샘플 수"], [[cause, causes.count(cause)] for cause in DROP_CAUSES]),
        ]
    )


def _drop_cause(m: Mapping[str, Any]) -> str:
    # 바깥 단계부터 본다. 카드에 있으면 정상, 아니면 처음으로 빠진 단계가 원인 (PRD 원인 표)
    if m.get("card_hit") == 1:
        return "정상"
    if m.get("card_hit") == 0 and m.get("candidate_hit") == 1:
        return "카드 선택"
    if m.get("candidate_hit") == 0 and m.get("retrieval_hit") == 1:
        return "후보 선택"
    if m.get("retrieval_hit") == 0:
        return "1차 필터" if m.get("filter_hit") == 0 else "검색"
    return "판단 불가"


def _mode_comparison(dataset_version: str, mode_averages: Sequence[Mapping[str, Any]]) -> str:
    title = f"## 모드별 비교 ({dataset_version}, 같은 채점 프롬프트의 완료된 run 평균)"
    if not mode_averages:
        return f"{title}\n\n완료된 run 없음"
    by_mode = {row["mode"]: row for row in mode_averages}
    headers = ["지표", *(f"{mode} ({row['runs']}회)" for mode, row in by_mode.items())]
    compare = all(mode in by_mode for mode in COMPARE_MODES)
    if compare:
        headers.append("rag - baseline")
    rows = []
    for name in METRIC_NAMES:
        key = f"avg_{name}"
        row = [name, *(_score(averages.get(key)) for averages in by_mode.values())]
        if compare:
            row.append(_diff(by_mode["baseline"].get(key), by_mode["rag"].get(key)))
        rows.append(row)
    return f"{title}\n\n" + _table(headers, rows)


def _progress(samples: Sequence[Mapping[str, Any]]) -> str:
    latencies = [s["latency_ms"] for s in samples if s["latency_ms"] is not None]
    rows = [
        ["미응답", sum(1 for s in samples if s["answer"] is None)],
        ["채점 실패 지표가 있는 샘플", sum(1 for s in samples if s["errors"])],
        ["평균 응답 시간(ms)", round(sum(latencies) / len(latencies)) if latencies else "-"],
    ]
    return "## 응답·채점 현황\n\n" + _table(["항목", "값"], rows)


def _low_samples(samples: Sequence[Mapping[str, Any]]) -> str:
    # 채점 실패(NULL)를 먼저, 그다음 factual_correctness 낮은 순
    answered = [s for s in samples if s["answer"] is not None]
    ranked = sorted(answered, key=lambda s: (s["factual_correctness"] is not None, s["factual_correctness"] or 0))
    headers = [
        "sample_id",
        "policy_no",
        "질문",
        "답변",
        "factual_correctness",
        "answer_coverage",
        "reference_faithfulness",
        "실패 지표",
    ]
    rows = [
        [
            s["sample_id"],
            s["policy_no"],
            _text(s["question"], QUESTION_MAX_CHARS),
            _text(s["answer"], ANSWER_MAX_CHARS),
            _score(s["factual_correctness"]),
            _score(s["answer_coverage"]),
            _score(s["reference_faithfulness"]),
            _text(", ".join(f"{k}:{v}" for k, v in (s["errors"] or {}).items())),
        ]
        for s in ranked[:LOW_SAMPLE_COUNT]
    ]
    title = f"## 점수 낮은 샘플 (factual_correctness 낮은 순, 최대 {LOW_SAMPLE_COUNT}건)"
    return f"{title}\n\n" + (_table(headers, rows) if rows else "응답한 샘플 없음")


def _table(headers: Sequence[Any], rows: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(map(str, headers)) + " |", "|" + " --- |" * len(headers)]
    lines += ["| " + " | ".join(map(str, row)) + " |" for row in rows]
    return "\n".join(lines)


def _score(value: Decimal | float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _diff(baseline: Decimal | None, rag: Decimal | None) -> str:
    return "-" if baseline is None or rag is None else f"{rag - baseline:+.3f}"


def _text(value: Any, max_chars: int | None = None) -> str:
    """표 셀용: 줄바꿈·연속 공백을 한 칸으로, 표를 깨는 | 는 이스케이프한다."""
    if value is None or value == "":
        return "-"
    text = " ".join(str(value).split())
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text.replace("|", "\\|")


def _time(value: Any) -> str:
    return value.isoformat(timespec="seconds") if value else "-"
