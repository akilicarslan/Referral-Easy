"""The retrieval evaluation: the metric arithmetic, and the dataset it is computed over.

An evaluation whose maths is wrong reports a number nobody can check. So the aggregates are
pinned against values computed by hand, the dataset is checked to ask for entities that exist, and
the seed rung is evaluated offline — where its numbers can be reproduced without a cluster.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from referraleasy_rag.degradation import LadderState, Rung
from referraleasy_rag.evaluation import (
    EvalQuery,
    EvalReport,
    QueryOutcome,
    evaluate,
    evaluate_seed_only,
    load_eval_queries,
)
from referraleasy_rag.retriever import Filters, RetrievalResult, RetrievedChunk
from referraleasy_rag.seed import SeedCorpus


def _outcome(
    query_id: str,
    *,
    first_rank: int | None,
    recall: Mapping[int, float],
    expected: Sequence[str] = ("e1",),
    degraded: bool = False,
) -> QueryOutcome:
    return QueryOutcome(
        query_id=query_id,
        query=f"query {query_id}",
        kind="lexical",
        ranked_entity_ids=tuple(expected),
        expected_entity_ids=tuple(expected),
        rung=str(Rung.HYBRID_RERANK if not degraded else Rung.HYBRID_RRF),
        degraded=degraded,
        first_relevant_rank=first_rank,
        recall_at_k=recall,
    )


# --------------------------------------------------------------------- metric arithmetic


def test_reciprocal_rank_is_the_inverse_of_the_first_relevant_position() -> None:
    assert _outcome("q", first_rank=1, recall={}).reciprocal_rank == 1.0
    assert _outcome("q", first_rank=2, recall={}).reciprocal_rank == 0.5
    assert _outcome("q", first_rank=4, recall={}).reciprocal_rank == 0.25
    assert _outcome("q", first_rank=None, recall={}).reciprocal_rank == 0.0
    assert _outcome("q", first_rank=None, recall={}).hit is False


def test_the_aggregates_match_a_hand_computed_report() -> None:
    """Three queries: a hit at rank 1, a hit at rank 2, and a miss.

    MRR        = (1 + 1/2 + 0) / 3                       = 0.5
    hit_rate   = 2 / 3                                   = 0.667
    recall@1   = (1.0 + 0.0 + 0.0) / 3                   = 0.333
    recall@3   = (1.0 + 1.0 + 0.0) / 3                   = 0.667
    recall@5   = (1.0 + 1.0 + 0.0) / 3                   = 0.667
    """
    report = EvalReport(
        label="hand-computed",
        k_values=(1, 3, 5),
        outcomes=(
            _outcome("q1", first_rank=1, recall={1: 1.0, 3: 1.0, 5: 1.0}),
            _outcome("q2", first_rank=2, recall={1: 0.0, 3: 1.0, 5: 1.0}),
            _outcome("q3", first_rank=None, recall={1: 0.0, 3: 0.0, 5: 0.0}, degraded=True),
        ),
    )

    assert report.mrr == pytest.approx(0.5)
    assert report.hit_rate == pytest.approx(2 / 3)
    assert report.recall_at_k == {
        1: pytest.approx(1 / 3),
        3: pytest.approx(2 / 3),
        5: pytest.approx(2 / 3),
    }
    assert report.degraded_outcomes == 1
    assert "MRR=0.500" in report.headline()
    assert "recall@1=0.333" in report.headline()
    assert "(3 queries, 1 degraded)" in report.headline()


def test_an_empty_report_scores_zero_rather_than_dividing_by_zero() -> None:
    report = EvalReport(label="empty", k_values=(1, 3), outcomes=())
    assert report.mrr == 0.0
    assert report.hit_rate == 0.0
    assert report.recall_at_k == {1: 0.0, 3: 0.0}


def test_recall_is_measured_against_every_expected_entity() -> None:
    """A query expecting two providers and finding one is half right, not entirely right."""
    report = EvalReport(
        label="multi",
        k_values=(3,),
        outcomes=(_outcome("q", first_rank=1, recall={3: 0.5}, expected=("e1", "e2")),),
    )
    assert report.recall_at_k[3] == pytest.approx(0.5)


def test_the_report_serializes_its_per_query_detail() -> None:
    report = EvalReport(
        label="serialized",
        k_values=(1, 3),
        outcomes=(_outcome("q1", first_rank=1, recall={1: 1.0, 3: 1.0}),),
        rungs={str(Rung.HYBRID_RRF): 1},
    )
    payload = report.to_dict()
    assert payload["mrr"] == 1.0
    assert payload["recall_at_k"] == {"1": 1.0, "3": 1.0}
    assert payload["rungs"] == {"hybrid_rrf": 1}
    assert payload["per_query"][0]["first_relevant_rank"] == 1
    assert payload["per_query"][0]["expected_entity_ids"] == ["e1"]


# --------------------------------------------------------------------- the dataset


def test_the_dataset_loads_and_is_internally_consistent() -> None:
    queries = load_eval_queries()
    assert len(queries) >= 8, "a handful of queries cannot distinguish two configurations"
    assert len({query.query_id for query in queries}) == len(queries)
    for query in queries:
        assert query.query.strip()
        assert query.expected_entity_ids
        assert query.kind in {"lexical", "paraphrase", "filtered", "unspecified", "seed"}


def test_the_dataset_exercises_filters_and_paraphrases() -> None:
    """All-lexical queries would flatter the keyword arm and prove nothing about the vector one."""
    queries = load_eval_queries()
    assert sum(1 for query in queries if query.kind == "paraphrase") >= 3
    assert sum(1 for query in queries if not query.filters.is_empty) >= 3


def test_every_expected_entity_exists_in_the_corpus(seed: SeedCorpus) -> None:
    """A dataset that asks for a provider nobody has indexed scores zero forever."""
    known = {document.entity_id for document in seed.documents}
    for query in load_eval_queries():
        missing = sorted(set(query.expected_entity_ids) - known)
        assert missing == [], f"{query.query_id} expects entities absent from the corpus: {missing}"


def test_the_dataset_parses_filters_into_the_shared_filter_object() -> None:
    filtered = [query for query in load_eval_queries() if not query.filters.is_empty]
    assert filtered
    for query in filtered:
        assert query.filters.to_clause()
    clause = next(query.filters for query in filtered if query.filters.languages).to_clause()
    assert clause["languages"]


def test_a_malformed_dataset_entry_is_an_error(tmp_path: Path) -> None:
    broken = tmp_path / "eval.json"
    broken.write_text('{"queries": [{"id": "q1", "query": "x"}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="expected_entity_ids"):
        load_eval_queries(broken)


# --------------------------------------------------------------------- evaluation runs


@dataclass
class _ScriptedRetriever:
    """A retriever that returns a scripted result, so the metrics can be checked in isolation."""

    results: Mapping[str, tuple[RetrievedChunk, ...]] = field(default_factory=dict)
    rung: Rung = Rung.HYBRID_RERANK
    reason: str = "scripted"

    def search(
        self,
        query: str,
        *,
        filters: Filters | None = None,
        top_k: int | None = None,
    ) -> RetrievalResult:
        ladder = LadderState()
        if self.rung is not Rung.HYBRID_RERANK:
            ladder.degrade(self.rung, self.reason)
        return RetrievalResult(
            query=query,
            results=self.results.get(query, ()),
            ladder=ladder,
            filters_applied=filters.to_clause() if filters is not None else {},
        )


def _chunk(entity_id: str, rank: int) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"{entity_id}:{rank}",
        entity_id=entity_id,
        entity_type="provider",
        source_id=f"provider_directory:{entity_id}",
        knowledge_version="kv-test",
        text=f"{entity_id} chunk {rank}",
        score=1.0 / rank,
        rank=rank,
    )


def test_evaluation_counts_distinct_entities_not_chunks() -> None:
    """Two chunks of one provider are one entity retrieved, not two — otherwise a pipeline that
    repeats itself scores as if it had found more."""
    retriever = _ScriptedRetriever(
        results={"a query": (_chunk("e1", 1), _chunk("e1", 2), _chunk("e2", 3))}
    )
    report = evaluate(
        retriever,  # type: ignore[arg-type]
        [EvalQuery(query_id="q1", query="a query", expected_entity_ids=("e2",))],
        label="dedup",
        k_values=(1, 3),
    )
    outcome = report.outcomes[0]
    assert outcome.ranked_entity_ids == ("e1", "e2")
    assert outcome.first_relevant_rank == 2, "e2 is second *entity*, not third chunk"
    assert outcome.recall_at_k == {1: 0.0, 3: 1.0}


def test_evaluation_tallies_which_rungs_answered() -> None:
    retriever = _ScriptedRetriever(rung=Rung.HYBRID_RRF, reason="reranker_unavailable")
    queries = [
        EvalQuery(query_id="q1", query="one", expected_entity_ids=("e1",)),
        EvalQuery(query_id="q2", query="two", expected_entity_ids=("e2",)),
    ]
    report = evaluate(retriever, queries, label="degraded", k_values=(1, 3))  # type: ignore[arg-type]
    assert report.rungs == {str(Rung.HYBRID_RRF): 2}
    assert report.degraded_outcomes == 2
    assert report.mrr == 0.0, "nothing was returned, and a miss scores zero rather than raising"


def test_evaluation_passes_the_query_filters_through() -> None:
    retriever = _ScriptedRetriever(results={"filtered query": (_chunk("e1", 1),)})
    queries = [
        EvalQuery(
            query_id="q1",
            query="filtered query",
            expected_entity_ids=("e1",),
            filters=Filters(languages=("es",)),
        )
    ]
    report = evaluate(retriever, queries, label="filtered", k_values=(1,))  # type: ignore[arg-type]
    assert report.mrr == 1.0


def test_the_seed_rung_is_evaluated_offline(seed: SeedCorpus, blocked_sockets: None) -> None:
    """Sockets blocked: these numbers are reproducible on a machine with no network at all."""
    queries = load_eval_queries()
    report = evaluate_seed_only(seed, queries, top_k=5)

    assert len(report.outcomes) == len(queries)
    assert set(report.rungs) == {str(Rung.SEED_FALLBACK)}
    assert report.degraded_outcomes == len(queries), "every seed answer is a degraded answer"
    assert report.hit_rate > 0.0, "the last rung has to be useful, or it may as well not exist"
    assert report.mrr > 0.0
    assert "seed-only" in report.label
    for outcome in report.outcomes:
        assert outcome.rung == str(Rung.SEED_FALLBACK)
        assert outcome.degraded is True


def test_the_seed_rung_is_deterministic_across_runs(seed: SeedCorpus) -> None:
    queries = load_eval_queries()
    first = evaluate_seed_only(seed, queries, top_k=5).to_dict()
    second = evaluate_seed_only(seed, queries, top_k=5).to_dict()
    assert first == second


def test_evaluation_reports_an_honest_zero_when_nothing_is_found() -> None:
    retriever = _ScriptedRetriever()
    queries = [EvalQuery(query_id="q1", query="unfindable", expected_entity_ids=("e1",))]
    report = evaluate(retriever, queries, label="empty", k_values=(1, 3))  # type: ignore[arg-type]
    assert report.mrr == 0.0
    assert report.hit_rate == 0.0
    assert report.recall_at_k == {1: 0.0, 3: 0.0}
    assert report.to_dict()["per_query"][0]["ranked_entity_ids"] == []


def test_a_scripted_outcome_keeps_the_degradation_it_was_given() -> None:
    retriever = _ScriptedRetriever(rung=Rung.BM25_ONLY, reason="embeddings_unavailable")
    queries = [EvalQuery(query_id="q1", query="x", expected_entity_ids=("e1",))]
    report = evaluate(retriever, queries, label="lexical only", k_values=(1,))  # type: ignore[arg-type]
    assert report.rungs == {str(Rung.BM25_ONLY): 1}
    assert report.to_dict()["rungs"] == {"bm25_only": 1}


def test_the_dataset_is_package_data() -> None:
    from referraleasy_rag.config import eval_dataset_path

    assert eval_dataset_path().is_file()
    payload: Any = eval_dataset_path().read_text(encoding="utf-8")
    assert "queries" in payload
