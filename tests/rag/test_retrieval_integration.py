"""Retrieval against a live OpenSearch. Skipped when no cluster is reachable.

Everything here runs with the offline embedder and no reranker, so the test needs a cluster and
nothing else — no API key, no model call, and no flakiness from a remote provider. What it proves
is what only a real cluster can: both arms execute, the filters narrow both of them the same way,
and the ladder reports the rung the answer actually came from.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from referraleasy_rag.chunking import Chunk
from referraleasy_rag.client import build_client, probe_opensearch
from referraleasy_rag.config import Settings
from referraleasy_rag.embeddings import HashingEmbeddingProvider
from referraleasy_rag.indexing import index_directory
from referraleasy_rag.rerank import NullRerankClient
from referraleasy_rag.retriever import Filters, HybridRetriever
from referraleasy_rag.seed import SeedCorpus

pytestmark = pytest.mark.integration

TEST_INDEX = "referraleasy-directory-chunks-retrieval"

HOSPITAL_PRAIRIE_RIDGE = "20000000-0000-4000-8000-000000000003"
PROVIDER_ELENA = "30000000-0000-4000-8000-000000000001"
CHICAGO = (41.8781, -87.6298)


@pytest.fixture(scope="module")
def cluster(settings: Settings) -> Iterator[Any]:
    probe = probe_opensearch(settings)
    if not probe.reachable:
        pytest.skip(
            "OpenSearch is not reachable: "
            + ", ".join(f"{attempt.url} ({attempt.detail})" for attempt in probe.attempts)
        )
    client = build_client(settings, url=probe.url)
    yield client
    if client.indices.exists(index=TEST_INDEX):
        client.indices.delete(index=TEST_INDEX, params={"ignore_unavailable": "true"})


@pytest.fixture(scope="module")
def retriever(settings: Settings, cluster: Any, seed: SeedCorpus) -> HybridRetriever:
    """Indexed once, then queried by every test in the module."""
    index_directory(
        settings,
        client=cluster,
        embedder=HashingEmbeddingProvider(dimensions=settings.embedding_dimensions),
        index_name=TEST_INDEX,
        recreate=True,
    )
    return HybridRetriever(
        settings,
        client=cluster,
        embedder=HashingEmbeddingProvider(dimensions=settings.embedding_dimensions),
        reranker=NullRerankClient(),
        seed=seed,
        index_name=TEST_INDEX,
        rerank_enabled=False,
    )


def test_a_live_hybrid_query_uses_both_arms(retriever: HybridRetriever) -> None:
    result = retriever.search("Elena Marquez cardiologist phone number", top_k=5)

    assert result.results, "the live index holds Elena Marquez"
    assert result.rung.value == "hybrid_rrf", "the reranker is disabled for this run"
    assert result.degraded is True
    assert result.ladder.ledger.model_calls == 0, "the offline embedder is not a model"
    assert "opensearch.bm25" in result.ladder.ledger.kinds()
    assert "opensearch.knn" in result.ladder.ledger.kinds()
    assert result.results[0].entity_id == PROVIDER_ELENA
    assert any("bm25" in chunk.arms for chunk in result.results)


def test_every_result_is_citable(retriever: HybridRetriever) -> None:
    result = retriever.search("cardiologist", top_k=5)
    for chunk in result.results:
        assert chunk.citation == f"{chunk.source_id}#{chunk.chunk_id}"
        assert chunk.knowledge_version
        assert chunk.text
        assert chunk.corpus_version, "the indexed corpus version comes back with the document"


def test_filters_narrow_both_arms_on_a_live_cluster(retriever: HybridRetriever) -> None:
    filters = Filters(languages=("es",), hospital_ids=(HOSPITAL_PRAIRIE_RIDGE,))
    result = retriever.search("who speaks Spanish", filters=filters, top_k=10)

    assert result.filters_applied == {
        "languages": ["es"],
        "hospital_ids": [HOSPITAL_PRAIRIE_RIDGE],
    }
    assert result.results
    for chunk in result.results:
        assert HOSPITAL_PRAIRIE_RIDGE in chunk.metadata.get("hospital_ids", []), (
            "a document the user excluded came back, so a filter did not reach an arm"
        )
        assert "es" in chunk.metadata.get("languages", [])


def test_a_filter_nobody_satisfies_returns_nothing_rather_than_ignoring_it(
    retriever: HybridRetriever,
) -> None:
    result = retriever.search("cardiologist", filters=Filters(languages=("xx",)), top_k=5)
    assert result.results == ()
    assert result.requires_human is True
    # The ladder descends for the caller's own configuration too (`rerank_disabled`), so the
    # assertion is on the *last* reason: the one the pipeline ended on, which must be the handoff.
    assert result.ladder.reasons[-1] == "no_source_found"
    assert result.ladder.rung.value == "human_handoff"


def test_distance_comes_from_the_indexed_coordinates(retriever: HybridRetriever) -> None:
    result = retriever.search("hospital", near=CHICAGO, top_k=5)

    assert result.results
    assert all(chunk.distance_km is not None for chunk in result.results)
    ordered = result.sorted_by_distance(CHICAGO)
    distances = [chunk.distance_km for chunk in ordered]
    assert distances == sorted(distances)
    assert ordered[0].distance_km is not None
    assert ordered[0].distance_km < 50.0, "these are Chicago-area distances"


def test_the_ladder_reports_the_rung_it_took(retriever: HybridRetriever) -> None:
    result = retriever.search("dermatologist", top_k=3)
    payload = result.to_dict()
    assert payload["ladder"]["rung"] == "hybrid_rrf"
    assert payload["ladder"]["degraded"] is True
    assert payload["ladder"]["degradations"][0]["reason"] == "rerank_disabled"
    assert payload["citations"] == result.citations


def test_the_seed_rung_agrees_with_the_index_on_the_same_question(
    settings: Settings, retriever: HybridRetriever, chunks: list[Chunk]
) -> None:
    """A degraded answer must come from the same corpus, or degrading changes the facts."""
    from referraleasy_rag.indexing import build_chunks

    live = retriever.search("Elena Marquez cardiologist", top_k=5)
    offline = SeedCorpus.load(settings.seed_path).search("Elena Marquez cardiologist", top_k=5)

    assert live.results[0].entity_id == offline[0].document.entity_id
    indexed = {chunk.chunk_id for chunk in build_chunks(settings)}
    assert {hit.document.chunk_id for hit in offline} <= indexed
    assert len(chunks) == len(indexed)


def test_an_outage_mid_query_is_survived_by_the_live_seed_rung(
    settings: Settings, seed: SeedCorpus
) -> None:
    """A client pointed at a closed port: the pipeline answers from the seed corpus instead."""
    dead = build_client(settings, url="http://127.0.0.1:9")
    retriever = HybridRetriever(
        settings,
        client=dead,
        embedder=HashingEmbeddingProvider(dimensions=settings.embedding_dimensions),
        reranker=NullRerankClient(),
        seed=seed,
        index_name=TEST_INDEX,
        rerank_enabled=False,
    )
    result = retriever.search("Elena Marquez cardiologist", top_k=5)

    assert result.rung.value == "seed_fallback"
    assert result.results, "the seed corpus answers when the cluster will not"
    assert result.ladder.seed_is_silent is True
    assert result.ladder.ledger.model_calls == 0
