"""RRF correctness, on an example computed by hand, and deduplication."""

from __future__ import annotations

import pytest

from referraleasy_rag.rrf import (
    DEFAULT_RRF_K,
    RankedHit,
    deduplicate,
    reciprocal_rank_fusion,
    rrf_scores,
)


def test_rrf_matches_a_hand_computed_example() -> None:
    """Two arms, three documents, scores worked out on paper.

    With k = 60:
      doc-a: rank 1 in arm one, rank 3 in arm two  -> 1/61 + 1/63
      doc-b: rank 2 in arm one only                -> 1/62
      doc-c: rank 3 in arm one, rank 1 in arm two  -> 1/63 + 1/61
      doc-d: rank 2 in arm two only                -> 1/62
    a and c therefore tie exactly, and b and d tie exactly; both ties are broken by id, which is
    what makes the ordering a function of the input rather than of dictionary iteration order.
    """
    fused = reciprocal_rank_fusion(
        {"arm_one": ["doc-a", "doc-b", "doc-c"], "arm_two": ["doc-c", "doc-d", "doc-a"]}
    )
    scores = dict(fused)
    assert scores["doc-a"] == pytest.approx(1 / 61 + 1 / 63)
    assert scores["doc-c"] == pytest.approx(1 / 61 + 1 / 63)
    assert scores["doc-a"] == pytest.approx(scores["doc-c"])
    assert scores["doc-b"] == pytest.approx(1 / 62)
    assert scores["doc-d"] == pytest.approx(1 / 62)
    assert [item_id for item_id, _ in fused] == ["doc-a", "doc-c", "doc-b", "doc-d"]


def test_rrf_k_is_the_documented_constant() -> None:
    assert DEFAULT_RRF_K == 60
    default = reciprocal_rank_fusion({"a": ["x", "y"]})
    explicit = reciprocal_rank_fusion({"a": ["x", "y"]}, k=DEFAULT_RRF_K)
    assert default == explicit


def test_rrf_is_order_stable_for_ties() -> None:
    """Ties break by id, so two identical requests cannot return different orderings."""
    first = reciprocal_rank_fusion({"a": ["z", "m"], "b": ["m", "z"]})
    second = reciprocal_rank_fusion({"a": ["z", "m"], "b": ["m", "z"]})
    assert first == second
    assert [item_id for item_id, _ in first] == ["m", "z"]


def test_rrf_accepts_a_bare_sequence_of_rankings() -> None:
    fused = reciprocal_rank_fusion([["a", "b"], ["b", "a"]])
    assert dict(fused)["a"] == dict(fused)["b"]


def test_rrf_rejects_a_non_positive_k() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        reciprocal_rank_fusion({"a": ["x"]}, k=0)


def test_rrf_scores_helper_returns_a_mapping() -> None:
    scores = rrf_scores({"a": ["x", "y"]})
    assert scores["x"] == pytest.approx(1 / 61)
    assert scores["y"] == pytest.approx(1 / 62)


def test_deduplicate_collapses_the_same_id_from_both_arms() -> None:
    """The same document found by both arms is one document, not two.

    Fusing a duplicate twice inflates its score relative to a document found once, which is how
    a single weak signal ends up outranking agreement between both arms.
    """
    hits = [
        RankedHit(item_id="doc-a", arm="bm25", rank=1),
        RankedHit(item_id="doc-a", arm="vector", rank=2),
        RankedHit(item_id="doc-b", arm="vector", rank=1),
    ]
    kept = deduplicate(hits)
    assert [hit.item_id for hit in kept] == ["doc-a", "doc-b"]
    # Rank is a rank *within* an arm, so the two copies are not comparable by it; arm priority
    # settles it, and lexical evidence wins because it is the copy whose terms matched.
    assert kept[0].arm == "bm25"


def test_deduplicate_prefers_the_better_rank_within_the_same_arm_priority() -> None:
    hits = [
        RankedHit(item_id="doc-a", arm="vector", rank=7),
        RankedHit(item_id="doc-a", arm="vector", rank=2),
    ]
    kept = deduplicate(hits)
    assert [hit.rank for hit in kept] == [2]


def test_deduplicate_collapses_identical_text_under_two_ids() -> None:
    """Two ids carrying the same text would otherwise occupy two of the sourced slots."""
    hits = [
        RankedHit(item_id="doc-a", arm="bm25", rank=2, text="Elena Marquez  +13125550110"),
        RankedHit(item_id="doc-b", arm="vector", rank=1, text="Elena   Marquez +13125550110"),
    ]
    kept = deduplicate(hits)
    assert len(kept) == 1
    assert kept[0].item_id == "doc-b"


def test_deduplicate_keeps_different_text_under_different_ids() -> None:
    hits = [
        RankedHit(item_id="doc-a", arm="bm25", rank=1, text="one fact"),
        RankedHit(item_id="doc-b", arm="vector", rank=2, text="another fact"),
    ]
    assert len(deduplicate(hits)) == 2


def test_deduplicate_is_deterministic_across_arm_ordering() -> None:
    forward = deduplicate(
        [
            RankedHit(item_id="doc-a", arm="bm25", rank=1, text="same text"),
            RankedHit(item_id="doc-b", arm="vector", rank=1, text="same text"),
        ]
    )
    backward = deduplicate(
        [
            RankedHit(item_id="doc-b", arm="vector", rank=1, text="same text"),
            RankedHit(item_id="doc-a", arm="bm25", rank=1, text="same text"),
        ]
    )
    assert [hit.item_id for hit in forward] == [hit.item_id for hit in backward] == ["doc-a"]
