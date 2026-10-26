# golden 샘플 프로필 채우기(시도·나이 선택, 검수 표시) 테스트
import pytest

from ragas_eval.dataset.dataset import GoldenSample, load_golden
from ragas_eval.profile import profile
from ragas_eval.profile.profile import PolicyScope, fill_profiles

SEOUL_ONLY = PolicyScope(min_age=19, max_age=34, sido_codes=("11",))
NATIONWIDE = PolicyScope(min_age=None, max_age=None, sido_codes=("11", "26", "41"))


def sample(golden_record, question="테스트 월세 지원 알려주세요", ground_truth="월 20만원을 지원합니다.", **overrides):
    return GoldenSample.model_validate(
        {**golden_record, "question": question, "ground_truth": ground_truth, **overrides}
    )


def fill_one(golden_record, scope, **kwargs):
    (result,) = fill_profiles([sample(golden_record, **kwargs)], {"TEST-0001": scope})
    return result


def test_single_sido_policy_gets_sido_wide_code_and_default_age(golden_record):
    result = fill_one(golden_record, SEOUL_ONLY)

    assert result.sample.profile.to_request() == {"region_code": "11000", "age": 25}
    assert result.reasons == []
    assert result.sample.reviewed is True


def test_question_sido_is_used_when_policy_covers_it(golden_record):
    result = fill_one(golden_record, NATIONWIDE, question="부산 금정구 청년 지원 알려주세요")

    assert result.sample.profile.region_code == "26000"
    assert result.reasons == []


def test_question_sido_outside_policy_leaves_region_empty(golden_record):
    result = fill_one(golden_record, SEOUL_ONLY, question="부산 청년 월세 지원 알려주세요")

    assert result.sample.profile.region_code is None
    assert result.sample.reviewed is False
    assert len(result.reasons) == 1


def test_ambiguous_place_leaves_region_empty(golden_record):
    result = fill_one(golden_record, NATIONWIDE, question="광주시 청년 지원 알려주세요")

    assert result.sample.profile.region_code is None
    assert result.sample.reviewed is False


def test_multi_sido_policy_with_unknown_place_leaves_region_empty(golden_record):
    result = fill_one(golden_record, NATIONWIDE, question="금정구 청년 지원 알려주세요")

    assert result.sample.profile.region_code is None
    assert result.sample.reviewed is False


def test_multi_sido_policy_rotates_sido_by_sample_order(golden_record):
    samples = [sample(golden_record, sample_id=f"single-00000{i}") for i in range(4)]

    results = fill_profiles(samples, {"TEST-0001": NATIONWIDE})

    assert [r.sample.profile.region_code for r in results] == ["11000", "26000", "41000", "11000"]
    assert all(r.reasons == [] for r in results)


@pytest.mark.parametrize(
    ("question", "expected"),
    [("20대 후반 구직자인데 지원 알려주세요", 28), ("20대 초반인데 지원 알려주세요", 22), ("20대인데 지원 알려주세요", 25)],
)
def test_age_follows_question_decade(golden_record, question, expected):
    result = fill_one(golden_record, SEOUL_ONLY, question=question)

    assert result.sample.profile.age == expected
    assert result.reasons == []


@pytest.mark.parametrize(("min_age", "max_age", "expected"), [(29, 39, 29), (15, 24, 24), (0, 120, 25), (None, None, 25)])
def test_default_age_is_clamped_to_policy_range(golden_record, min_age, max_age, expected):
    scope = PolicyScope(min_age=min_age, max_age=max_age, sido_codes=("11",))

    result = fill_one(golden_record, scope)

    assert result.sample.profile.age == expected


@pytest.mark.parametrize(("question", "expected"), [("30대 후반인데 지원 받을 수 있나요?", 34), ("40대인데 받을 수 있나요?", None)])
def test_question_decade_outside_policy_range_is_flagged(golden_record, question, expected):
    result = fill_one(golden_record, SEOUL_ONLY, question=question)

    assert result.sample.profile.age == expected
    assert result.sample.reviewed is False


@pytest.mark.parametrize("question", ["120대 한정 모집인데 받을 수 있나요?", "100대 기업 인턴도 되나요?"])
def test_number_before_decade_is_not_an_age(golden_record, question):
    result = fill_one(golden_record, SEOUL_ONLY, question=question)

    assert result.sample.profile.age == 25
    assert result.reasons == []


def test_several_decades_leave_age_empty(golden_record):
    result = fill_one(golden_record, SEOUL_ONLY, question="20대와 30대 모두 받을 수 있나요?")

    assert result.sample.profile.age is None
    assert result.sample.reviewed is False


@pytest.mark.parametrize(("min_age", "max_age", "expected"), [(1, 99, 25), (30, 20, None)])
def test_age_limit_edges(golden_record, min_age, max_age, expected):
    scope = PolicyScope(min_age=min_age, max_age=max_age, sido_codes=("11",))

    result = fill_one(golden_record, scope)

    assert result.sample.profile.age == expected
    assert result.sample.reviewed is (expected is not None)


def test_other_profile_fields_are_kept(golden_record):
    result = fill_one(golden_record, SEOUL_ONLY, profile={"job_code": "0013003", "age": 40})

    assert result.sample.profile.to_request() == {"job_code": "0013003", "region_code": "11000", "age": 25}


def test_explicit_age_in_question_is_flagged(golden_record):
    result = fill_one(golden_record, SEOUL_ONLY, question="만 19세 ~ 34세면 지원 받을 수 있나요?")

    assert result.sample.profile.age == 25
    assert result.sample.reviewed is False
    assert len(result.reasons) == 1


def test_not_eligible_answer_gets_no_profile(golden_record):
    result = fill_one(golden_record, SEOUL_ONLY, ground_truth="미취업자 대상이라 창업자는 신청 대상이 아닙니다.")

    assert result.sample.profile.to_request() == {}
    assert result.sample.reviewed is False


def test_missing_policy_gets_no_profile(golden_record):
    (result,) = fill_profiles([sample(golden_record)], {})

    assert result.sample.profile.to_request() == {}
    assert result.sample.reviewed is False


def test_policy_without_known_sido_leaves_region_empty(golden_record):
    scope = PolicyScope(min_age=19, max_age=34, sido_codes=("99",))

    result = fill_one(golden_record, scope)

    assert result.sample.profile.region_code is None
    assert result.sample.profile.age == 25
    assert result.sample.reviewed is False


def test_profile_writes_candidates_and_keeps_other_fields(tmp_path, monkeypatch, golden_record):
    golden = tmp_path / "golden_v1.jsonl"
    golden.write_text(sample(golden_record).model_dump_json() + "\n", encoding="utf-8")
    out = tmp_path / "golden_profile_candidates.jsonl"
    monkeypatch.setattr(profile, "_fetch_scopes", lambda policy_nos: {"TEST-0001": SEOUL_ONLY})

    summary = profile.profile(golden, out)

    (written,) = load_golden(out)
    assert written.profile.to_request() == {"region_code": "11000", "age": 25}
    assert written.question == sample(golden_record).question
    assert summary.written == 1
    assert summary.flagged == []


def test_profile_refuses_input_file_as_output(tmp_path, monkeypatch, golden_record):
    golden = tmp_path / "golden_profile_candidates.jsonl"
    golden.write_text(sample(golden_record).model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(profile, "_fetch_scopes", lambda policy_nos: {"TEST-0001": SEOUL_ONLY})

    with pytest.raises(ValueError, match="입력 golden"):
        profile.profile(golden, golden)


def test_profile_refuses_confirmed_dataset_as_output(tmp_path, monkeypatch, golden_record):
    golden = tmp_path / "golden_v1.jsonl"
    golden.write_text(sample(golden_record).model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(profile, "_fetch_scopes", lambda policy_nos: {"TEST-0001": SEOUL_ONLY})

    with pytest.raises(ValueError, match="확정 데이터셋"):
        profile.profile(golden, tmp_path / "golden_v2.jsonl")
