# 정답 정책이 1차 필터 → 검색 → 후보 → 카드 중 어디까지 남았는지 계산 (채점 LLM 없음)
from collections.abc import Mapping
from typing import Any

HIT_AT_K = (1, 3, 5, 10)


def compute_retrieval_metrics(policy_no: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    """samples.metadata에 저장된 trace 원본으로 PRD "검색 적중률 평가" 지표를 계산한다.

    적중은 1/0, 출처 키가 없거나 판단할 수 없으면 None이다 (0점과 구분).
    """
    chunks = metadata.get("retrieved_chunks") or []
    searched = bool(chunks)
    rank = _policy_rank(policy_no, chunks) if searched else None

    cards = metadata.get("cards")
    candidate_nos = cards.get("candidate_nos") if isinstance(cards, Mapping) else None
    card_policy_nos = metadata.get("card_policy_nos")
    filtered_policy_nos = metadata.get("filtered_policy_nos")

    return {
        "card_hit": _contains(card_policy_nos, policy_no),
        # 검색이 없으면 후보도 검색 결과에서 나오지 않으므로 함께 NULL
        "candidate_hit": _contains(candidate_nos, policy_no) if searched else None,
        "retrieval_hit": int(rank is not None) if searched else None,
        # null = 필터를 걸지 않음, 키 없음 = 목록을 주기 전 RAG
        "filter_hit": _contains(filtered_policy_nos, policy_no),
        "rank": rank,
        **{f"hit_at_{k}": int(rank is not None and rank <= k) if searched else None for k in HIT_AT_K},
        "reciprocal_rank": (1 / rank if rank else 0.0) if searched else None,
    }


def _policy_rank(policy_no: str, chunks: list[Mapping[str, Any]]) -> int | None:
    # 지역·전국 분리 검색은 두 결과가 이어 붙어 오므로 점수로 다시 정렬한다. 동점은 응답 순서 유지 (안정 정렬)
    ordered = sorted(chunks, key=lambda c: c["score"], reverse=True)
    policies = list(dict.fromkeys(c["policy_no"] for c in ordered))
    return policies.index(policy_no) + 1 if policy_no in policies else None


def _contains(policy_nos: list[str] | None, policy_no: str) -> int | None:
    return None if policy_nos is None else int(policy_no in policy_nos)
