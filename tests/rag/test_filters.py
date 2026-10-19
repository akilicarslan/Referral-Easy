"""Filters must reach both arms, identically.

The failure this file guards against does not look like a failure: the vector arm quietly
ignores the specialty the user asked for, both arms return results, and the answer is a
cardiologist when the caller asked for a dermatologist. The filter is therefore a property of the
query builder — both arms receive the same mapping object — and the assertions here are about
equality of clauses rather than about "a filter is present somewhere".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from referraleasy_rag.opensearch_index import (
    FILTERABLE_KEYWORD_FIELDS,
    build_bm25_query,
    build_knn_query,
    filters_clause,
)
from referraleasy_rag.retriever import Filters
from referraleasy_rag.seed import SeedCorpus

CARDIOLOGY_SPANISH = Filters(
    specialty="cardiology",
    languages=("es",),
    hospital_ids=("20000000-0000-4000-8000-000000000003",),
    cities=("Chicago",),
    entity_types=("provider",),
)


def test_both_arms_carry_the_identical_filter_clause() -> None:
    clause = CARDIOLOGY_SPANISH.to_clause()
    bm25 = build_bm25_query("spanish speaking cardiologist", clause, 20)
    knn = build_knn_query([0.0] * 768, clause, 20)

    bm25_filter = bm25["query"]["bool"]["filter"]
    knn_filter = knn["query"]["bool"]["filter"]
    assert bm25_filter != [], "the filtered query must actually carry a clause"
    assert bm25_filter == knn_filter

    # The kNN clause repeats the same clause, so the k nearest *matching* neighbours come back.
    # Without it the arm retrieves k global neighbours and discards almost all of them.
    knn_inner = knn["query"]["bool"]["must"][0]["knn"]["embedding"]["filter"]["bool"]["filter"]
    assert knn_inner == bm25_filter


def test_an_empty_filter_produces_no_clause_in_either_arm() -> None:
    assert filters_clause(Filters().to_clause()) == []
    bm25 = build_bm25_query("anything", Filters().to_clause(), 20)
    knn = build_knn_query([0.0] * 768, Filters().to_clause(), 20)
    assert bm25["query"]["bool"]["filter"] == []
    assert knn["query"]["bool"]["filter"] == []
    assert "filter" not in knn["query"]["bool"]["must"][0]["knn"]["embedding"]


def test_the_retriever_hands_the_same_clause_to_both_arms(
    build_retriever: Callable[..., Any], two_providers: list[dict[str, Any]]
) -> None:
    """End to end, through the retriever: one clause, two search bodies, no second assembly."""
    harness = build_retriever(hits=two_providers)
    harness.retriever.search("spanish speaking cardiologist", filters=CARDIOLOGY_SPANISH)

    client = harness.client
    assert len(client.bodies) == 2, "the retriever must query both arms"
    assert client.bm25_body["query"]["bool"]["filter"] == client.knn_body["query"]["bool"]["filter"]
    assert client.bm25_body["query"]["bool"]["filter"] != []


def test_the_arms_would_differ_if_the_filters_were_dropped(
    build_retriever: Callable[..., Any], two_providers: list[dict[str, Any]]
) -> None:
    """Negative control: without filters there is no clause, so the equality above is not vacuous."""
    harness = build_retriever(hits=two_providers)
    harness.retriever.search("spanish speaking cardiologist")
    client = harness.client
    assert client.bm25_body["query"]["bool"]["filter"] == []
    assert client.knn_body["query"]["bool"]["filter"] == []


def test_filters_use_only_filterable_keyword_fields() -> None:
    """A key the mapping does not carry would be rejected by the cluster, not by us."""
    for key in CARDIOLOGY_SPANISH.to_clause():
        assert key in FILTERABLE_KEYWORD_FIELDS


def test_an_unknown_filter_is_refused_rather_than_ignored() -> None:
    """Silently dropping a filter widens the search the user narrowed."""
    with pytest.raises(ValueError, match="unknown filter"):
        filters_clause({"favourite_colour": "green"})


def test_blank_filter_values_are_dropped_not_matched_against_empty_strings() -> None:
    assert filters_clause({"languages": [], "cities": "", "specialties": None}) == []


def test_filter_values_are_stringified_consistently_for_both_arms() -> None:
    clause = filters_clause({"languages": ("es", "pl")})
    assert clause == [{"terms": {"languages": ["es", "pl"]}}]
    bm25 = build_bm25_query("q", {"languages": ("es", "pl")}, 5)
    knn = build_knn_query([0.0] * 768, {"languages": ("es", "pl")}, 5)
    assert bm25["query"]["bool"]["filter"] == knn["query"]["bool"]["filter"] == clause


def test_the_seed_rung_reads_the_same_clause_with_the_same_meaning(seed: SeedCorpus) -> None:
    """The degraded rung must not widen the search: same keys, same all-must-match semantics."""
    clause = Filters(languages=("es",)).to_clause()
    hits = seed.search("cardiology", filters=clause, top_k=10)
    assert hits, "the fixture corpus has Spanish-speaking cardiologists"
    for hit in hits:
        assert "es" in hit.document.filter_values("languages")


def test_the_seed_rung_excludes_what_the_filter_excludes(seed: SeedCorpus) -> None:
    """Negative control: an impossible filter must empty the result, not be ignored."""
    unfiltered = seed.search("cardiology", top_k=10)
    filtered = seed.search("cardiology", filters={"languages": ["xx"]}, top_k=10)
    assert unfiltered
    assert filtered == []


def test_the_seed_rung_applies_filters_before_scoring(seed: SeedCorpus) -> None:
    """A provider who matches the query but not the filter must not appear at all."""
    hits = seed.search(
        "cardiologist",
        filters={"hospital_ids": ["20000000-0000-4000-8000-000000000003"]},
        top_k=20,
    )
    assert hits
    for hit in hits:
        assert "20000000-0000-4000-8000-000000000003" in hit.document.filter_values("hospital_ids")
