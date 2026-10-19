"""Deduplication and reciprocal rank fusion.

The two arms score on incomparable scales — BM25 returns an unbounded positive score, kNN
returns a cosine distance — so they are fused by *rank*, not by score. RRF (Cormack et al.,
2009) is what the pipeline specifies and what this module implements:

    score(d) = Σ_arms 1 / (k + rank_arm(d))

with ``rank`` 1-based and ``k`` = 60 by default, which flattens the contribution of the top few
positions enough that a document ranked highly by only one arm does not automatically win.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

#: Standard RRF constant. Also the value the pipeline config carries as ``RAG_RRF_K``.
DEFAULT_RRF_K = 60

#: Which arm's copy of a duplicated document is kept. Purely a tie-breaker, so that two runs
#: over the same input keep the same one; lexical evidence wins because it is the copy whose
#: terms actually matched the query.
ARM_PRIORITY = {"bm25": 0, "vector": 1, "seed": 2, "rerank": 3}


@dataclass(frozen=True, slots=True)
class RankedHit:
    """A hit as one arm returned it: an id, the arm, and its 1-based rank within that arm."""

    item_id: str
    arm: str
    rank: int
    text: str = ""
    payload: Mapping[str, object] | None = None


def deduplicate(hits: Iterable[RankedHit]) -> list[RankedHit]:
    """Collapse duplicate hits, keeping one copy of each.

    Two things are deduplicated, and the second one matters:

    * the same ``item_id`` returned by both arms — the same document is not two documents, and
      fusing it twice would inflate its RRF score relative to a document found by one arm only;
    * two ids whose text is identical after normalization — a duplicate that exists under two
      ids still occupies two of the three to five sourced slots handed to the caller, which is
      a way of answering the same thing twice while looking like corroboration.
    """
    ordered = sorted(hits, key=lambda hit: (hit.rank, ARM_PRIORITY.get(hit.arm, 99), hit.item_id))
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    kept: list[RankedHit] = []
    for hit in ordered:
        if hit.item_id in seen_ids:
            continue
        text_key = _text_key(hit)
        if text_key and text_key in seen_texts:
            continue
        seen_ids.add(hit.item_id)
        if text_key:
            seen_texts.add(text_key)
        kept.append(hit)
    return kept


def _text_key(hit: RankedHit) -> str:
    if not hit.text:
        return ""
    return " ".join(hit.text.split()).lower()


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[str]] | Sequence[Sequence[str]],
    *,
    k: int = DEFAULT_RRF_K,
    weights: Mapping[str, float] | None = None,
) -> list[tuple[str, float]]:
    """Fuse ranked id lists into one ranking, highest fused score first.

    ``rankings`` may be a mapping of arm name to ranked ids (preferred) or a plain sequence of
    ranked id lists. Ties are broken by id so the result is a function of its input alone —
    a retriever whose ordering wobbles between identical requests is not reproducible, and its
    evaluation numbers mean nothing.
    """
    if k <= 0:
        raise ValueError(f"RRF k must be positive, got {k}")
    arms: Mapping[str, Sequence[str]]
    if isinstance(rankings, Mapping):
        arms = rankings
    else:
        arms = {f"arm{index}": ids for index, ids in enumerate(rankings)}

    scores: dict[str, float] = {}
    for arm_name, ids in arms.items():
        weight = 1.0 if weights is None else float(weights.get(arm_name, 1.0))
        for position, item_id in enumerate(ids):
            rank = position + 1
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda entry: (-entry[1], entry[0]))


def rrf_scores(
    rankings: Mapping[str, Sequence[str]], *, k: int = DEFAULT_RRF_K
) -> dict[str, float]:
    """Fused scores as a mapping, for callers that need to look a document's score up."""
    return dict(reciprocal_rank_fusion(rankings, k=k))
