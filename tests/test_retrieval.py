# 검색 적중률 지표(카드·후보·검색·필터 적중, Hit@k, 역순위) 계산 테스트
import pytest

from ragas_eval.retrieval.retrieval import compute_retrieval_metrics

GOLD = "TEST-0001"


def chunk(policy_no, score):
    return {"chunk_id": f"chunk-{policy_no}-{score}", "policy_no": policy_no, "score": score}


def metadata(**overrides):
    base = {
        "retrieved_chunks": [chunk(GOLD, 0.9), chunk("TEST-0002", 0.8)],
        "cards": {"candidate_nos": [GOLD, "TEST-0002"]},
        "card_policy_nos": [GOLD],
        "filtered_policy_nos": [GOLD, "TEST-0002"],
    }
    return base | overrides


def test_gold_policy_surviving_every_stage_hits_all():
    result = compute_retrieval_metrics(GOLD, metadata())

    assert (result["card_hit"], result["candidate_hit"], result["retrieval_hit"], result["filter_hit"]) == (1, 1, 1, 1)
    assert result["rank"] == 1
    assert result["reciprocal_rank"] == 1.0


def test_gold_policy_dropped_at_cards_misses_only_card():
    result = compute_retrieval_metrics(GOLD, metadata(card_policy_nos=["TEST-0002"]))

    assert (result["card_hit"], result["candidate_hit"], result["retrieval_hit"]) == (0, 1, 1)


def test_gold_policy_dropped_at_candidates_misses_candidate_and_card():
    result = compute_retrieval_metrics(
        GOLD, metadata(cards={"candidate_nos": ["TEST-0002"]}, card_policy_nos=["TEST-0002"])
    )

    assert (result["card_hit"], result["candidate_hit"], result["retrieval_hit"]) == (0, 0, 1)


def test_gold_policy_not_retrieved_has_no_rank_and_zero_hits():
    result = compute_retrieval_metrics(
        GOLD,
        metadata(
            retrieved_chunks=[chunk("TEST-0002", 0.8)],
            cards={"candidate_nos": ["TEST-0002"]},
            card_policy_nos=["TEST-0002"],
        ),
    )

    assert result["retrieval_hit"] == 0
    assert result["rank"] is None
    assert result["reciprocal_rank"] == 0.0
    assert [result[f"hit_at_{k}"] for k in (1, 3, 5, 10)] == [0, 0, 0, 0]


def test_policy_rank_counts_each_policy_once_in_score_order():
    chunks = [
        chunk("TEST-0002", 0.95),
        chunk("TEST-0002", 0.94),
        chunk("TEST-0003", 0.93),
        chunk(GOLD, 0.92),
    ]

    result = compute_retrieval_metrics(GOLD, metadata(retrieved_chunks=chunks))

    assert result["rank"] == 3
    assert result["reciprocal_rank"] == pytest.approx(1 / 3)
    assert [result[f"hit_at_{k}"] for k in (1, 3, 5, 10)] == [0, 1, 1, 1]


def test_split_search_chunks_are_merged_by_score_not_response_order():
    local_then_national = [chunk("TEST-0002", 0.70), chunk(GOLD, 0.60), chunk("TEST-0003", 0.90)]

    result = compute_retrieval_metrics(GOLD, metadata(retrieved_chunks=local_then_national))

    assert result["rank"] == 3


def test_equal_scores_keep_response_order():
    chunks = [chunk("TEST-0002", 0.8), chunk(GOLD, 0.8)]

    result = compute_retrieval_metrics(GOLD, metadata(retrieved_chunks=chunks))

    assert result["rank"] == 2


def test_rank_beyond_ten_misses_every_hit_at_k():
    chunks = [chunk(f"TEST-1{i:03d}", 1 - i / 100) for i in range(10)] + [chunk(GOLD, 0.1)]

    result = compute_retrieval_metrics(GOLD, metadata(retrieved_chunks=chunks))

    assert result["rank"] == 11
    assert result["hit_at_10"] == 0
    assert result["reciprocal_rank"] == pytest.approx(1 / 11)


def test_empty_retrieval_leaves_retrieval_and_candidate_metrics_null():
    result = compute_retrieval_metrics(GOLD, metadata(retrieved_chunks=[]))

    assert result["retrieval_hit"] is None
    assert result["candidate_hit"] is None
    assert result["rank"] is None
    assert result["reciprocal_rank"] is None
    assert [result[f"hit_at_{k}"] for k in (1, 3, 5, 10)] == [None, None, None, None]
    assert result["card_hit"] == 1


@pytest.mark.parametrize("missing, metric", [("cards", "candidate_hit"), ("card_policy_nos", "card_hit")])
def test_response_without_cards_leaves_card_metrics_null(missing, metric):
    data = metadata()
    del data[missing]

    result = compute_retrieval_metrics(GOLD, data)

    assert result[metric] is None


def test_old_rag_without_filtered_list_leaves_filter_hit_null():
    data = metadata()
    del data["filtered_policy_nos"]

    result = compute_retrieval_metrics(GOLD, data)

    assert result["filter_hit"] is None


@pytest.mark.parametrize("filtered, expected", [(None, None), ([], 0), (["TEST-0002"], 0), ([GOLD], 1)])
def test_filter_hit_checks_gold_in_filtered_list(filtered, expected):
    result = compute_retrieval_metrics(GOLD, metadata(filtered_policy_nos=filtered))

    assert result["filter_hit"] == expected


def test_metadata_without_any_trace_detail_returns_all_null():
    result = compute_retrieval_metrics(GOLD, {})

    assert all(value is None for value in result.values())
