# 모드별 RAGAS 지표 채점 (검색 지표 3개 + Answer Relevancy + 할루시네이션 비교 지표 2개 + Answer Coverage)
import asyncio
import logging
import math
import re
from collections.abc import Mapping
from numbers import Real
from types import MappingProxyType
from typing import Annotated, Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field
from ragas.embeddings import OpenAIEmbeddings
from ragas.llms import llm_factory
from ragas.metrics.collections import (
    AnswerRelevancy,
    ContextPrecision,
    ContextRecall,
    FactualCorrectness,
    Faithfulness,
)
from ragas.metrics.collections.answer_relevancy.util import (
    AnswerRelevanceInput,
    AnswerRelevanceOutput,
    AnswerRelevancePrompt,
)
from ragas.metrics.collections.factual_correctness.util import (
    ClaimDecompositionInput,
    ClaimDecompositionOutput,
    ClaimDecompositionPrompt,
    NLIStatementInput,
    NLIStatementOutput,
    NLIStatementPrompt,
)
from ragas.metrics.result import MetricResult

logger = logging.getLogger(__name__)

Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
METRIC_NAMES = (
    "context_precision",
    "context_recall",
    "faithfulness",
    "answer_relevancy",
    "factual_correctness",
    "reference_faithfulness",
    "answer_coverage",
)
# baseline은 검색을 하지 않으므로 검색 결과가 필요한 지표를 계산하지 않는다 (PRD "평가 지표" 모드별 표)
Mode = Literal["rag", "baseline", "offline"]
METRICS_BY_MODE = MappingProxyType(
    {
        "rag": METRIC_NAMES,
        "baseline": ("answer_relevancy", "factual_correctness", "reference_faithfulness", "answer_coverage"),
        "offline": METRIC_NAMES,
    }
)
MODES = tuple(METRICS_BY_MODE)
# 검색 결과로 재는 지표. RAG가 근거 없이 답한 응답(짧은 답변)에는 계산하지 않는다
CONTEXT_METRICS = ("context_precision", "context_recall", "faithfulness")
# ragas 0.4.3은 모델 버전을 정수로만 읽어 gpt-5.6-terra 같은 소수점 버전을 GPT-5 이상으로 보지 못한다.
# 그러면 지원하지 않는 max_tokens를 보내 400이 나므로 GPT-5 이상은 직접 추론 모델 인자로 맞춘다
GPT_VERSION = re.compile(r"gpt-(\d+)(?:\.\d+)?(?:-|$)")
# ragas 문서 권장: GPT-5 계열은 추론 토큰 때문에 구조화 출력에 4096 이상 필요
REASONING_MAX_COMPLETION_TOKENS = 4096
# 채점 프롬프트 버전. 바꾸면 run.metadata.scoring_prompt로 구분하고 다른 버전 run과 섞어 비교하지 않는다
SCORING_PROMPT_VERSION = "answer_relevancy-ko-v1+answer_coverage-v1"
# AnswerRelevancy의 코사인 유사도 평균은 1.0000000000000002처럼 범위를 미세하게 넘을 수 있다
SCORE_TOLERANCE = 1e-9
# answer_coverage는 정답 정보를 버리지 않고(coverage) 사실 하나씩(atomicity) 쪼개야 누락을 잡는다.
# factual_correctness는 기존 run과 비교하려고 ragas 기본값(low/low)을 유지한다
COVERAGE_ATOMICITY = "high"
COVERAGE_COVERAGE = "high"


class ScoreResult(BaseModel):
    """샘플 1건의 지표 7개. 실패한 지표는 None으로 두고 errors에 지표명 → 사유를 남긴다.
    모드에서 제외된 지표도 None이지만 errors에는 남기지 않는다."""

    model_config = ConfigDict(frozen=True)

    context_precision: Score | None = None
    context_recall: Score | None = None
    faithfulness: Score | None = None
    answer_relevancy: Score | None = None
    factual_correctness: Score | None = None
    reference_faithfulness: Score | None = None
    answer_coverage: Score | None = None
    errors: dict[str, str] = Field(default_factory=dict)


class KoreanAnswerRelevancePrompt(AnswerRelevancePrompt):
    """AnswerRelevancy 기본 프롬프트(영어)는 한국어 답변에서 영어 질문을 만들어 원래 질문과의 유사도가 낮아진다.
    실행마다 같은 문구가 되도록 LLM 번역(adapt) 대신 고정 한국어 프롬프트를 쓴다."""

    language = "korean"
    instruction = """주어진 답변으로 답할 수 있는 질문을 하나 만들고, 답변이 회피성인지 판단하세요.
질문은 반드시 한국어로 작성하세요.
답변이 회피적이거나 모호하거나 애매하면 noncommittal을 1, 구체적이면 0으로 주세요.
회피성 답변 예: "모르겠습니다", "확실하지 않습니다", "상황에 따라 다릅니다"."""
    examples = [
        (
            AnswerRelevanceInput(response="청년 월세 지원은 만 19세부터 34세까지 신청할 수 있습니다."),
            AnswerRelevanceOutput(question="청년 월세 지원은 몇 살까지 신청할 수 있나요?", noncommittal=0),
        ),
        (
            AnswerRelevanceInput(
                response="신청은 복지로 누리집에서 온라인으로 하며, 임대차계약서와 주민등록등본을 제출해야 합니다."
            ),
            AnswerRelevanceOutput(question="온라인 신청은 어디서 하고 어떤 서류를 내야 하나요?", noncommittal=0),
        ),
        (
            AnswerRelevanceInput(
                response="2027년에 새로 생기는 지원 사업의 신청 조건은 아직 발표되지 않아 알 수 없습니다."
            ),
            AnswerRelevanceOutput(question="2027년에 새로 생기는 지원 사업의 신청 조건은 무엇인가요?", noncommittal=1),
        ),
    ]


class NoClaimsError(ValueError):
    """정답에서 주장이 하나도 나오지 않아 비율을 계산할 수 없음"""


class VerdictCountMismatchError(ValueError):
    """판정 수가 주장 수와 달라 어느 주장이 맞았는지 알 수 없음"""


class AnswerCoverage:
    """정답(reference)의 주장 중 답변에서 추론되는 비율. 정답 밖 덧붙임은 감점하지 않는다.
    ragas FactualCorrectness의 프롬프트를 그대로 쓰되, 내부 함수 대신 프롬프트를 직접 호출한다 (PRD "Answer Coverage")."""

    name = "answer_coverage"

    def __init__(self, llm: Any) -> None:
        self.llm = llm
        self.prompt = ClaimDecompositionPrompt()
        self.nli_prompt = NLIStatementPrompt()

    async def ascore(self, response: str, reference: str) -> MetricResult:
        decomposition = ClaimDecompositionInput(
            response=reference, atomicity=COVERAGE_ATOMICITY, coverage=COVERAGE_COVERAGE
        )
        claims = (await self.llm.agenerate(self.prompt.to_string(decomposition), ClaimDecompositionOutput)).claims
        if not claims:
            raise NoClaimsError
        verification = NLIStatementInput(context=response, statements=claims)
        verdicts = (await self.llm.agenerate(self.nli_prompt.to_string(verification), NLIStatementOutput)).statements
        if len(verdicts) != len(claims):
            raise VerdictCountMismatchError
        return MetricResult(value=sum(v.verdict == 1 for v in verdicts) / len(claims))


def make_llm(model: str, client: AsyncOpenAI) -> Any:
    """ragas 구조화 출력 LLM. GPT-5 이상은 max_completion_tokens·temperature 1.0으로 보낸다."""
    llm = llm_factory(model, client=client)
    matched = GPT_VERSION.search(model.lower())
    if matched and int(matched.group(1)) >= 5:
        # model_args는 호출마다 복사돼 요청 인자가 되므로 생성 직후 한 번 바꾼다
        llm.model_args.pop("max_tokens", None)
        llm.model_args.pop("top_p", None)
        llm.model_args.update(max_completion_tokens=REASONING_MAX_COMPLETION_TOKENS, temperature=1.0)
    return llm


def build_metrics(client: AsyncOpenAI, judge_model: str, embedding_model: str) -> dict[str, Any]:
    llm = make_llm(judge_model, client)
    embeddings = OpenAIEmbeddings(client=client, model=embedding_model)
    return {
        "context_precision": ContextPrecision(llm=llm),
        "context_recall": ContextRecall(llm=llm),
        "faithfulness": Faithfulness(llm=llm),
        "answer_relevancy": _korean_answer_relevancy(llm, embeddings),
        "factual_correctness": FactualCorrectness(llm=llm),
        "reference_faithfulness": Faithfulness(llm=llm, name="reference_faithfulness"),
        "answer_coverage": AnswerCoverage(llm),
    }


def _korean_answer_relevancy(llm: Any, embeddings: Any) -> AnswerRelevancy:
    metric = AnswerRelevancy(llm=llm, embeddings=embeddings)
    metric.prompt = KoreanAnswerRelevancePrompt()
    return metric


async def score_sample(
    metrics: Mapping[str, Any],
    *,
    question: str,
    answer: str,
    retrieved_contexts: list[str] | None,
    reference: str,
    reference_contexts: list[str],
    mode: Mode = "rag",
) -> ScoreResult:
    """retrieved_contexts가 None이면 검색하지 않은 응답이라 CONTEXT_METRICS를 빼고 채점한다 ([]은 검색 결과 0건)."""
    if mode not in METRICS_BY_MODE:
        raise ValueError(f"알 수 없는 mode: {mode!r} (허용: {sorted(METRICS_BY_MODE)})")
    names = tuple(n for n in METRICS_BY_MODE[mode] if retrieved_contexts is not None or n not in CONTEXT_METRICS)
    missing = [name for name in names if name not in metrics]
    if missing:
        raise ValueError(f"지표 객체 누락: {missing}")
    inputs = {
        "context_precision": {"user_input": question, "retrieved_contexts": retrieved_contexts, "reference": reference},
        "context_recall": {"user_input": question, "retrieved_contexts": retrieved_contexts, "reference": reference},
        "faithfulness": {"user_input": question, "response": answer, "retrieved_contexts": retrieved_contexts},
        "answer_relevancy": {"user_input": question, "response": answer},
        "factual_correctness": {"response": answer, "reference": reference},
        # 검색 결과 대신 정책 원문 기준으로 충실도를 재서 검색이 없는 baseline과 같은 기준으로 비교한다
        "reference_faithfulness": {
            "user_input": question,
            "response": answer,
            "retrieved_contexts": reference_contexts,
        },
        "answer_coverage": {"response": answer, "reference": reference},
    }
    outcomes = await asyncio.gather(*(metrics[name].ascore(**inputs[name]) for name in names), return_exceptions=True)

    scores: dict[str, float | None] = {}
    errors: dict[str, str] = {}
    for name, outcome in zip(names, outcomes):
        # 취소·종료 신호는 채점 실패가 아니므로 그대로 올린다
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, Exception):
            # 예외 메시지에는 프롬프트·응답 내용이 섞일 수 있어 클래스명만 남긴다
            value, error = None, type(outcome).__name__
        else:
            value, error = _check_value(getattr(outcome, "value", None))
        scores[name] = value
        if error:
            errors[name] = error
            logger.warning("채점 실패: metric=%s error=%s", name, error)
    return ScoreResult(**scores, errors=errors)


def _check_value(value: Any) -> tuple[float | None, str | None]:
    """0점과 실패를 구분하기 위해 0 ~ 1 실수만 점수로 인정한다."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None, "InvalidValue"
    if math.isnan(value):
        return None, "NaN"
    if not -SCORE_TOLERANCE <= value <= 1 + SCORE_TOLERANCE:
        return None, "InvalidValue"
    return min(max(float(value), 0.0), 1.0), None
