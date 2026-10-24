# RAG 질문 분석의 유형이 기대 유형과 같은지 채점하고 run 단위로 집계 (채점 LLM 없음)
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any


def score_routing(expected_intent: Sequence[str], analysis: Mapping[str, Any] | None) -> dict[str, Any]:
    """PRD "라우팅 평가" intent_correct.

    분석 실패(fallback_reason)는 유형이 비어 0점, 응답에 analysis가 없으면 판단할 수 없어 None이다.
    """
    if analysis is None:
        return {"predicted_intent": None, "intent_correct": None, "fallback_reason": None, "prompt_version": None}
    predicted = [sub["intent"] for sub in analysis.get("sub_queries") or []]
    return {
        "predicted_intent": predicted,
        # 하위 질문 순서는 LLM 출력마다 달라질 수 있어 개수까지 포함한 묶음으로 비교한다
        "intent_correct": int(Counter(predicted) == Counter(expected_intent)),
        "fallback_reason": analysis.get("fallback_reason"),
        "prompt_version": analysis.get("prompt_version"),
    }


def summarize_routing(rows: Sequence[tuple[Sequence[str], Mapping[str, Any]]]) -> dict[str, Any]:
    """(기대 유형, score_routing 결과) 목록을 run 집계로 만든다. 판단할 수 없는 샘플(None)은 뺀다."""
    scored = [(expected, score) for expected, score in rows if score["intent_correct"] is not None]
    by_intent: dict[str, dict[str, int]] = {}
    confusion: dict[str, dict[str, int]] = {}
    # 유형별 정확도·혼동 행렬은 기대·응답 모두 하위 질문이 1개인 샘플만 센다 (PRD "라우팅 평가")
    for expected, score in scored:
        if len(expected) != 1 or len(score["predicted_intent"]) != 1:
            continue
        actual, predicted = expected[0], score["predicted_intent"][0]
        counts = by_intent.setdefault(actual, {"total": 0, "correct": 0})
        counts["total"] += 1
        counts["correct"] += int(actual == predicted)
        row = confusion.setdefault(actual, {})
        row[predicted] = row.get(predicted, 0) + 1
    total = len(scored)
    return {
        "sample_count": total,
        "accuracy": sum(score["intent_correct"] for _, score in scored) / total if total else None,
        "fallback_rate": sum(1 for _, score in scored if score["fallback_reason"]) / total if total else None,
        "by_intent": by_intent,
        "confusion": confusion,
        # 분석 프롬프트가 run 도중 바뀌었는지 보이도록 전부 남긴다
        "analysis_prompt_versions": sorted({score["prompt_version"] for _, score in scored if score["prompt_version"]}),
    }
