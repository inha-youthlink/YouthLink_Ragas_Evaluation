# golden·routing jsonl 로드, reviewed 필터, source_updated_at 변경 검사
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

import psycopg
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

# RAG 질문 분석 유형 7종 (YouthLink_RAG app/schemas/analysis.py Intent)
Intent = Literal["policy_list", "policy_detail", "eligibility", "comparison", "chitchat", "out_of_scope", "unclear"]
# RAG가 질문 하나를 나누는 하위 질문 최대 개수 (YouthLink_RAG analyzer.MAX_SUB_QUERIES)
MAX_SUB_QUERIES = 3

SELECT_SOURCE_UPDATED_AT = """
    SELECT policy_no, source_updated_at
    FROM policy
    WHERE policy_no = ANY(%(policy_nos)s)
"""


class GoldenProfile(BaseModel):
    """RAG에 보내는 프로필 (YouthLink_RAG app/schemas/pipeline.py Profile).

    RAG는 모르는 키를 무시해 오타가 필터 없는 검색으로 조용히 바뀌므로 여기서 막는다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    age: int | None = Field(default=None, ge=0)
    gender: str | None = None
    # 법정 시군구 코드 5자리. 시도 전체는 끝 3자리 000
    region_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    marriage_status_code: str | None = None
    # 원 단위 (공개 API의 만 원 단위와 다름)
    annual_income: int | None = Field(default=None, ge=0)
    major_code: str | None = None
    job_code: str | None = None
    school_code: str | None = None
    special_target_code: str | None = None

    def to_request(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class GoldenSample(BaseModel):
    """golden jsonl 한 줄. 길이 제한은 ragas_evaluation_samples 컬럼과 맞춘다."""

    model_config = ConfigDict(frozen=True)

    sample_id: str = Field(min_length=1, max_length=50)
    policy_no: str = Field(min_length=1, max_length=30)
    question: str = Field(min_length=1)
    ground_truth: str = Field(min_length=1)
    reference_contexts: list[str] = Field(min_length=1)
    synthesizer_name: str
    profile: GoldenProfile = Field(default_factory=GoldenProfile)
    source_updated_at: AwareDatetime | None
    reviewed: bool = False


class RoutingSample(BaseModel):
    """routing jsonl 한 줄. expected_intent는 하위 질문마다 1개 (PRD "라우팅 평가" 데이터셋)."""

    model_config = ConfigDict(frozen=True)

    sample_id: str = Field(min_length=1, max_length=50)
    question: str = Field(min_length=1)
    expected_intent: list[Intent] = Field(min_length=1, max_length=MAX_SUB_QUERIES)
    reviewed: bool = False
    # 검수용 판정 근거. 채점에 쓰지 않는다
    note: str | None = None


Sample = TypeVar("Sample", GoldenSample, RoutingSample)


def latest_dataset(kind: Literal["golden", "routing"], directory: str | Path = "datasets") -> Path:
    """확정 데이터셋 {kind}_vN.jsonl 중 N이 가장 큰 파일. 후보 파일(*_candidates.jsonl)은 고르지 않는다."""
    pattern = re.compile(rf"{kind}_v(\d+)\.jsonl")
    versions = {int(m.group(1)): p for p in Path(directory).iterdir() if (m := pattern.fullmatch(p.name))}
    if not versions:
        raise FileNotFoundError(f"{directory}에 확정 데이터셋({kind}_vN.jsonl)이 없음")
    return versions[max(versions)]


def load_golden(path: str | Path) -> list[GoldenSample]:
    return _load_jsonl(path, GoldenSample)


def load_routing(path: str | Path) -> list[RoutingSample]:
    return _load_jsonl(path, RoutingSample)


def _load_jsonl(path: str | Path, model: type[Sample]) -> list[Sample]:
    samples: list[Sample] = []
    seen: set[str] = set()
    # utf-8-sig: 편집기가 붙인 BOM 허용, split("\n"): 본문 속 U+2028 등에서 레코드가 쪼개지지 않게
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8-sig").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            sample = model.model_validate_json(line)
        except ValidationError as e:
            # 원문(input_value)이 traceback에 남지 않도록 위치·종류만 남기고 체인을 끊는다
            details = ", ".join(
                f"{'.'.join(map(str, err['loc'])) or '(줄 전체)'}:{err['type']}" for err in e.errors(include_input=False)
            )
            raise ValueError(f"{path} {line_no}번째 줄 검증 실패: {details}") from None
        if sample.sample_id in seen:
            raise ValueError(f"{path} {line_no}번째 줄 sample_id 중복: {sample.sample_id}")
        seen.add(sample.sample_id)
        samples.append(sample)
    return samples


def reviewed_only(samples: Iterable[Sample]) -> list[Sample]:
    return [s for s in samples if s.reviewed]


def fetch_source_updated_at(conn: psycopg.Connection, policy_nos: Iterable[str]) -> dict[str, datetime | None]:
    policy_nos = list(policy_nos)
    if not policy_nos:
        return {}
    with conn.cursor() as cur:
        cur.execute(SELECT_SOURCE_UPDATED_AT, {"policy_nos": policy_nos})
        return {policy_no: updated_at for policy_no, updated_at in cur.fetchall()}


def split_by_freshness(
    samples: Iterable[GoldenSample], current: Mapping[str, datetime | None]
) -> tuple[list[GoldenSample], list[GoldenSample]]:
    """DB에 정책이 없거나 source_updated_at이 다르면 stale로 분류한다."""
    fresh: list[GoldenSample] = []
    stale: list[GoldenSample] = []
    for sample in samples:
        is_fresh = sample.policy_no in current and current[sample.policy_no] == sample.source_updated_at
        (fresh if is_fresh else stale).append(sample)
    return fresh, stale


def load_evaluation_samples(path: str | Path, conn: psycopg.Connection) -> list[GoldenSample]:
    samples = reviewed_only(load_golden(path))
    current = fetch_source_updated_at(conn, {s.policy_no for s in samples})
    fresh, stale = split_by_freshness(samples, current)
    for sample in stale:
        reason = "변경으로" if sample.policy_no in current else "삭제로"
        logger.warning("정책 %s 샘플 제외: sample_id=%s policy_no=%s", reason, sample.sample_id, sample.policy_no)
    if stale:
        logger.warning("제외 샘플 %d건 / 전체 %d건", len(stale), len(samples))
    return fresh
