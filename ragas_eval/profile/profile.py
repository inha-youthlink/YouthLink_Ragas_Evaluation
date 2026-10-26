# golden 샘플에 정답 정책의 시도·나이 조건에 맞는 프로필을 채워 다음 버전 후보를 만든다
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg

from ragas_eval.config import get_settings
from ragas_eval.dataset.dataset import GoldenProfile, GoldenSample, load_golden
from ragas_eval.generate.generate import write_jsonl

SELECT_POLICY_SCOPE = """
    SELECT p.policy_no, p.min_age, p.max_age,
           ARRAY(SELECT DISTINCT left(r.region_code, 2) FROM policy_region r
                 WHERE r.policy_no = p.policy_no ORDER BY 1) AS sido_codes
    FROM policy p
    WHERE p.policy_no = ANY(%(policy_nos)s)
"""

# RAG region 테이블의 시도 코드와 질문에서 그 시도를 가리키는 이름 (2026-07 전남·광주 통합 반영)
SIDO_NAMES: dict[str, tuple[str, ...]] = {
    "11": ("서울",),
    "12": ("광주", "전남", "전라남도"),
    "26": ("부산",),
    "27": ("대구",),
    "28": ("인천",),
    "30": ("대전",),
    "31": ("울산",),
    "36": ("세종",),
    "41": ("경기",),
    "43": ("충북", "충청북도"),
    "44": ("충남", "충청남도"),
    "47": ("경북", "경상북도"),
    "48": ("경남", "경상남도"),
    "50": ("제주",),
    "51": ("강원",),
    "52": ("전북", "전라북도"),
}
# 경기도 광주시와 광주광역시를 함께 가리켜 시도를 정할 수 없음 (RAG conditions.AMBIGUOUS_NAMES)
AMBIGUOUS_PLACES = ("광주시",)
# 시군구 목록이 없어 이름 꼴로만 찾음. 여러 시도에 걸친 정책에서 시도를 고를 수 없을 때 검수로 돌리는 데만 씀
PLACE_LIKE = re.compile(r"[가-힣]{1,6}(?:시|군|구)(?![가-힣])")

DEFAULT_AGE = 25
# API가 "제한 없음"으로 채운 나이 (PRD "데이터셋 생성" 원천 텍스트와 같은 기준)
NO_MIN_AGE, NO_MAX_AGE = 1, 99
# 앞이 숫자면 "120대", "100대 기업"처럼 나이가 아님
DECADE = re.compile(r"(?<!\d)([1-9])0대\s*(초반|중반|후반)?")
DECADE_OFFSETS = {"초반": 2, "중반": 5, "후반": 8, "": 5}
EXPLICIT_AGE = re.compile(r"\d{1,2}\s*(?:살|세)")
# 정답이 "자격이 안 된다"인 샘플은 프로필을 붙이면 1차 필터가 정답 정책을 빼므로 붙이지 않는다
NOT_ELIGIBLE = re.compile(r"대상이 아[니닙님]|해당하지 않|해당되지 않|신청할 수 없|자격이 없")


@dataclass(frozen=True)
class PolicyScope:
    min_age: int | None
    max_age: int | None
    sido_codes: tuple[str, ...]


@dataclass(frozen=True)
class ProfiledSample:
    sample: GoldenSample
    # 검수할 이유. 있으면 reviewed = false로 내려 검수 전에는 평가에 쓰이지 않게 한다
    reasons: list[str]


@dataclass(frozen=True)
class ProfileSummary:
    written: int
    flagged: list[ProfiledSample]


def profile(golden_path: str | Path, out_path: str | Path) -> ProfileSummary:
    if Path(out_path).resolve() == Path(golden_path).resolve():
        raise ValueError("입력 golden 파일에는 쓰지 않음. --out을 다른 경로로 지정")
    samples = load_golden(golden_path)
    scopes = _fetch_scopes({s.policy_no for s in samples})
    results = fill_profiles(samples, scopes)
    write_jsonl((r.sample.model_dump(mode="json") for r in results), out_path)
    return ProfileSummary(written=len(results), flagged=[r for r in results if r.reasons])


def fill_profiles(samples: Sequence[GoldenSample], scopes: Mapping[str, PolicyScope]) -> list[ProfiledSample]:
    # 여러 시도에 걸친 정책은 샘플 순서로 시도를 돌려 써서 한 지역에 몰리지 않게 한다
    return [_fill(sample, scopes.get(sample.policy_no), index) for index, sample in enumerate(samples)]


def _fill(sample: GoldenSample, scope: PolicyScope | None, index: int) -> ProfiledSample:
    if scope is None:
        return _result(sample, None, None, ["DB에 정책이 없어 프로필을 비움"])
    if NOT_ELIGIBLE.search(sample.ground_truth):
        return _result(sample, None, None, ["정답이 자격 없음 판정이라 프로필을 비움"])
    region_code, region_reasons = _choose_region(sample.question, scope.sido_codes, index)
    age, age_reasons = _choose_age(sample.question, scope)
    return _result(sample, region_code, age, region_reasons + age_reasons)


# 지역·나이만 바꾸고 손으로 넣은 다른 프로필 필드는 그대로 둔다
def _result(sample: GoldenSample, region_code: str | None, age: int | None, reasons: list[str]) -> ProfiledSample:
    new_profile = sample.profile.model_copy(update={"region_code": region_code, "age": age})
    updates = {"profile": new_profile} | ({"reviewed": False} if reasons else {})
    return ProfiledSample(sample.model_copy(update=updates), reasons)


def _choose_region(question: str, sido_codes: Iterable[str], index: int) -> tuple[str | None, list[str]]:
    known = [code for code in sido_codes if code in SIDO_NAMES]
    if not known:
        return None, ["정책 지역에 RAG가 아는 시도가 없어 지역을 비움"]
    if ambiguous := [place for place in AMBIGUOUS_PLACES if place in question]:
        return None, [f"질문의 '{ambiguous[0]}'은 시도를 정할 수 없어 지역을 비움"]

    mentioned = sorted({code for code, names in SIDO_NAMES.items() if any(name in question for name in names)})
    if mentioned:
        if len(mentioned) == 1 and mentioned[0] in known:
            return f"{mentioned[0]}000", []
        return None, [f"질문의 시도({', '.join(mentioned)})가 정책 시도와 다르거나 여러 개라 지역을 비움"]
    if len(known) == 1:
        return f"{known[0]}000", []
    if place := PLACE_LIKE.search(question):
        return None, [f"질문의 '{place.group()}'가 어느 시도인지 알 수 없어 지역을 비움"]
    return f"{known[index % len(known)]}000", []


def _choose_age(question: str, scope: PolicyScope) -> tuple[int | None, list[str]]:
    low = scope.min_age if scope.min_age is not None and scope.min_age > NO_MIN_AGE else None
    high = scope.max_age if scope.max_age is not None and scope.max_age < NO_MAX_AGE else None
    limits = f"{low if low is not None else ''}~{high if high is not None else ''}세"
    if low is not None and high is not None and low > high:
        return None, [f"정책 나이({limits})의 하한이 상한보다 커 나이를 비움"]
    decades = DECADE.findall(question)
    if len({tens for tens, _ in decades}) > 1:
        return None, ["질문에 나이대가 여러 개라 나이를 비움"]

    decade = DECADE.search(question)
    target = int(decade.group(1)) * 10 + DECADE_OFFSETS[decade.group(2) or ""] if decade else DEFAULT_AGE
    age = min(max(target, low if low is not None else target), high if high is not None else target)

    if decade and age != target:
        said = decade.group().strip()
        if age // 10 != int(decade.group(1)):
            return None, [f"질문의 '{said}'가 정책 나이({limits})를 벗어나 나이를 비움"]
        return age, [f"질문의 '{said}'를 정책 나이({limits})에 맞춰 {age}세로 둠"]
    if explicit := EXPLICIT_AGE.findall(question):
        # "만 19세 ~ 39세"처럼 자격 범위일 때가 많아 사용자 나이인지 사람이 정한다 (다르면 RAG가 되물음)
        return age, [f"질문의 '{', '.join(explicit)}'가 사용자 나이면 프로필 나이({age})를 맞춰야 함"]
    return age, []


def _fetch_scopes(policy_nos: Iterable[str]) -> dict[str, PolicyScope]:
    # policy는 SELECT만 한다. 읽기 전용 세션으로 열어 실수로 쓰는 것도 막는다
    with psycopg.connect(get_settings().database_url.get_secret_value()) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(SELECT_POLICY_SCOPE, {"policy_nos": list(policy_nos)})
            return {
                policy_no: PolicyScope(min_age, max_age, tuple(sido_codes))
                for policy_no, min_age, max_age, sido_codes in cur.fetchall()
            }
