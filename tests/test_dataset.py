# GoldenSample 타입 검증과 golden jsonl 로드·필터·변경 검사 테스트
import json
import logging
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from pydantic import ValidationError

from ragas_eval.dataset import dataset
from ragas_eval.dataset.dataset import (
    GoldenSample,
    RoutingSample,
    fetch_source_updated_at,
    load_evaluation_samples,
    load_golden,
    load_routing,
    reviewed_only,
    split_by_freshness,
)

KST = timezone(timedelta(hours=9))
UPDATED_AT = datetime(2026, 9, 30, tzinfo=KST)


def test_latest_dataset_picks_highest_version_numerically(tmp_path):
    for name in ["golden_v2.jsonl", "golden_v10.jsonl", "golden_v9.jsonl", "golden_candidates.jsonl", "routing_v11.jsonl"]:
        (tmp_path / name).write_text("", encoding="utf-8")

    assert dataset.latest_dataset("golden", tmp_path) == tmp_path / "golden_v10.jsonl"
    assert dataset.latest_dataset("routing", tmp_path) == tmp_path / "routing_v11.jsonl"


def test_latest_dataset_fails_without_confirmed_file(tmp_path):
    (tmp_path / "golden_candidates.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="golden_vN"):
        dataset.latest_dataset("golden", tmp_path)


def write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return path


def test_golden_record_parses_into_sample(golden_record):
    sample = GoldenSample.model_validate(golden_record)

    assert sample.sample_id == "single-000001"
    assert sample.reference_contexts == ["지원 대상: 만 19세 ~ 34세 청년"]
    assert sample.source_updated_at == datetime(2026, 9, 30, tzinfo=timezone(timedelta(hours=9)))
    assert sample.reviewed is True


def test_missing_reviewed_and_profile_use_defaults(golden_record):
    record = {k: v for k, v in golden_record.items() if k not in ("reviewed", "profile")}

    sample = GoldenSample.model_validate(record)

    assert sample.reviewed is False
    assert sample.profile.to_request() == {}


def test_profile_sends_only_filled_fields(golden_record):
    record = {**golden_record, "profile": {"age": 25, "region_code": "11000", "job_code": None}}

    sample = GoldenSample.model_validate(record)

    assert sample.profile.to_request() == {"age": 25, "region_code": "11000"}


@pytest.mark.parametrize(
    "profile",
    [
        {"region": "11000"},
        {"region_code": "11"},
        {"region_code": "서울"},
        {"age": -1},
        {"annual_income": -1},
    ],
)
def test_invalid_profile_fails(golden_record, profile):
    with pytest.raises(ValidationError):
        GoldenSample.model_validate({**golden_record, "profile": profile})


def test_null_source_updated_at_is_allowed(golden_record):
    sample = GoldenSample.model_validate({**golden_record, "source_updated_at": None})

    assert sample.source_updated_at is None


def test_naive_source_updated_at_fails(golden_record):
    with pytest.raises(ValidationError):
        GoldenSample.model_validate({**golden_record, "source_updated_at": "2026-09-30T00:00:00"})


@pytest.mark.parametrize(
    "field",
    ["sample_id", "policy_no", "question", "ground_truth", "reference_contexts", "synthesizer_name", "source_updated_at"],
)
def test_missing_required_field_fails(golden_record, field):
    record = {k: v for k, v in golden_record.items() if k != field}

    with pytest.raises(ValidationError):
        GoldenSample.model_validate(record)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sample_id", "x" * 51),
        ("policy_no", "x" * 31),
        ("question", ""),
        ("ground_truth", ""),
        ("reference_contexts", []),
    ],
)
def test_invalid_value_fails(golden_record, field, value):
    with pytest.raises(ValidationError):
        GoldenSample.model_validate({**golden_record, field: value})


def test_assigning_field_fails(golden_record):
    sample = GoldenSample.model_validate(golden_record)

    with pytest.raises(ValidationError):
        sample.question = "바뀐 질문"


def test_load_golden_reads_samples_in_order(tmp_path, golden_record):
    path = write_jsonl(tmp_path / "golden.jsonl", [golden_record, {**golden_record, "sample_id": "single-000002"}])

    samples = load_golden(path)

    assert [s.sample_id for s in samples] == ["single-000001", "single-000002"]
    assert samples[0].question == "테스트 청년 월세 지원은 몇 살까지 신청할 수 있나요?"


def test_load_golden_skips_blank_lines(tmp_path, golden_record):
    path = tmp_path / "golden.jsonl"
    path.write_text("\n" + json.dumps(golden_record, ensure_ascii=False) + "\r\n\n", encoding="utf-8")

    samples = load_golden(path)

    assert len(samples) == 1


def test_load_golden_accepts_utf8_bom(tmp_path, golden_record):
    path = tmp_path / "golden.jsonl"
    path.write_text(json.dumps(golden_record, ensure_ascii=False) + "\n", encoding="utf-8-sig")

    assert [s.sample_id for s in load_golden(path)] == ["single-000001"]


def test_load_golden_keeps_unicode_line_separator_in_record(tmp_path, golden_record):
    path = write_jsonl(tmp_path / "golden.jsonl", [{**golden_record, "ground_truth": "첫 줄 둘째 줄"}])

    assert load_golden(path)[0].ground_truth == "첫 줄 둘째 줄"


def test_load_golden_invalid_line_fails_with_line_number(tmp_path, golden_record):
    path = write_jsonl(tmp_path / "golden.jsonl", [golden_record, {**golden_record, "sample_id": "single-000002", "question": ""}])

    with pytest.raises(ValueError, match="2번째 줄 검증 실패: question:string_too_short"):
        load_golden(path)


def test_load_golden_error_message_excludes_line_content(tmp_path, golden_record):
    path = write_jsonl(tmp_path / "golden.jsonl", [{**golden_record, "policy_no": "비밀-" + "x" * 40}])

    with pytest.raises(ValueError) as exc_info:
        load_golden(path)

    assert "비밀-" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_load_golden_duplicate_sample_id_fails(tmp_path, golden_record):
    path = write_jsonl(tmp_path / "golden.jsonl", [golden_record, golden_record])

    with pytest.raises(ValueError, match="single-000001"):
        load_golden(path)


def test_reviewed_only_drops_unreviewed(golden_record):
    reviewed = GoldenSample.model_validate(golden_record)
    unreviewed = GoldenSample.model_validate({**golden_record, "sample_id": "single-000002", "reviewed": False})

    assert reviewed_only([reviewed, unreviewed]) == [reviewed]


@pytest.mark.parametrize(
    ("sample_value", "current", "is_fresh"),
    [
        pytest.param("2026-09-30T00:00:00+09:00", {"TEST-0001": UPDATED_AT}, True, id="same"),
        pytest.param("2026-09-29T15:00:00+00:00", {"TEST-0001": UPDATED_AT}, True, id="same-instant-other-tz"),
        pytest.param("2026-09-30T00:00:00+09:00", {"TEST-0001": UPDATED_AT + timedelta(days=1)}, False, id="changed"),
        pytest.param("2026-09-30T00:00:00+09:00", {"TEST-0001": None}, False, id="db-null"),
        pytest.param(None, {"TEST-0001": UPDATED_AT}, False, id="sample-null"),
        pytest.param(None, {"TEST-0001": None}, True, id="both-null"),
        pytest.param("2026-09-30T00:00:00+09:00", {}, False, id="policy-deleted"),
    ],
)
def test_freshness_depends_on_db_source_updated_at(golden_record, sample_value, current, is_fresh):
    sample = GoldenSample.model_validate({**golden_record, "source_updated_at": sample_value})
    expected = ([sample], []) if is_fresh else ([], [sample])

    result = split_by_freshness([sample], current)

    assert result == expected


def test_load_evaluation_samples_returns_fresh_reviewed_and_warns_stale(tmp_path, golden_record, monkeypatch, caplog):
    records = [
        golden_record,
        {**golden_record, "sample_id": "single-000002", "policy_no": "TEST-0002"},
        {**golden_record, "sample_id": "single-000003", "reviewed": False},
        {**golden_record, "sample_id": "single-000004", "policy_no": "TEST-DELETED"},
    ]
    path = write_jsonl(tmp_path / "golden.jsonl", records)
    requested = []

    def fake_fetch(conn, policy_nos):
        requested.append(sorted(policy_nos))
        return {"TEST-0001": UPDATED_AT, "TEST-0002": UPDATED_AT + timedelta(days=1)}

    monkeypatch.setattr(dataset, "fetch_source_updated_at", fake_fetch)

    with caplog.at_level(logging.WARNING, logger=dataset.__name__):
        samples = load_evaluation_samples(path, conn=None)

    assert [s.sample_id for s in samples] == ["single-000001"]
    assert requested == [["TEST-0001", "TEST-0002", "TEST-DELETED"]]
    assert "정책 변경으로 샘플 제외: sample_id=single-000002" in caplog.text
    assert "정책 삭제로 샘플 제외: sample_id=single-000004" in caplog.text
    assert "제외 샘플 2건 / 전체 3건" in caplog.text


@pytest.mark.db
def test_fetch_source_updated_at_reads_requested_policies(test_database_url):
    with psycopg.connect(test_database_url) as conn, conn.transaction(force_rollback=True):
        conn.execute("CREATE TEMP TABLE policy (policy_no VARCHAR(30) PRIMARY KEY, source_updated_at TIMESTAMPTZ)")
        conn.execute(
            "INSERT INTO pg_temp.policy VALUES (%s, %s), (%s, %s), (%s, %s)",
            ["TEST-0001", UPDATED_AT, "TEST-0002", None, "TEST-OTHER", UPDATED_AT],
        )

        current = fetch_source_updated_at(conn, ["TEST-0001", "TEST-0002", "TEST-MISSING"])

    assert current == {"TEST-0001": UPDATED_AT, "TEST-0002": None}


# ---- 라우팅 데이터셋 ----


def routing_record(**overrides):
    record = {
        "sample_id": "route-000001",
        "question": "테스트 정책 신청 방법 알려줘",
        "expected_intent": ["policy_detail"],
        "reviewed": True,
    }
    return {**record, **overrides}


def test_routing_record_parses_with_optional_note():
    sample = RoutingSample.model_validate(routing_record(note="기준 자체를 물음"))

    assert sample.expected_intent == ["policy_detail"]
    assert sample.note == "기준 자체를 물음"
    assert RoutingSample.model_validate(routing_record()).note is None


def test_routing_record_keeps_sub_query_intents_in_order():
    sample = RoutingSample.model_validate(routing_record(expected_intent=["policy_detail", "policy_list"]))

    assert sample.expected_intent == ["policy_detail", "policy_list"]


@pytest.mark.parametrize(
    "intents",
    [[], ["unknown_intent"], ["policy_list"] * 4],
    ids=["empty", "unknown", "more-than-max-sub-queries"],
)
def test_invalid_expected_intent_fails(intents):
    with pytest.raises(ValidationError):
        RoutingSample.model_validate(routing_record(expected_intent=intents))


@pytest.mark.parametrize("field", ["sample_id", "question", "expected_intent"])
def test_missing_routing_field_fails(field):
    record = {k: v for k, v in routing_record().items() if k != field}

    with pytest.raises(ValidationError):
        RoutingSample.model_validate(record)


def test_empty_routing_question_fails():
    with pytest.raises(ValidationError):
        RoutingSample.model_validate(routing_record(question=""))


def test_load_routing_reads_samples_and_rejects_duplicates(tmp_path):
    path = write_jsonl(tmp_path / "routing.jsonl", [routing_record(), routing_record(sample_id="route-000002")])
    duplicate = write_jsonl(tmp_path / "dup.jsonl", [routing_record(), routing_record()])

    samples = load_routing(path)

    assert [s.sample_id for s in samples] == ["route-000001", "route-000002"]
    with pytest.raises(ValueError, match="2번째 줄 sample_id 중복"):
        load_routing(duplicate)


def test_load_routing_invalid_line_fails_with_line_number(tmp_path):
    path = write_jsonl(tmp_path / "routing.jsonl", [routing_record(), routing_record(expected_intent=["nope"])])

    with pytest.raises(ValueError, match="2번째 줄 검증 실패"):
        load_routing(path)


def test_reviewed_only_drops_unreviewed_routing_samples():
    samples = [
        RoutingSample.model_validate(routing_record()),
        RoutingSample.model_validate(routing_record(sample_id="route-000002", reviewed=False)),
    ]

    assert [s.sample_id for s in reviewed_only(samples)] == ["route-000001"]


def test_confirmed_routing_v1_file_is_valid():
    samples = load_routing("datasets/routing_v1.jsonl")

    assert len(samples) == 90
    assert all(s.reviewed for s in samples)
