"""Indexing against a live OpenSearch. Skipped when no cluster is reachable.

The unit tests prove the pipeline's logic; only a real cluster can prove the mapping is accepted,
the kNN field is really a kNN field, and re-indexing the same corpus does not double the document
count. Uses its own index name and removes it afterwards, so it cannot disturb anything else in the
running compose stack.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from referraleasy_rag.chunking import Chunk, corpus_version
from referraleasy_rag.client import build_client, probe_opensearch
from referraleasy_rag.config import Settings
from referraleasy_rag.embeddings import HashingEmbeddingProvider
from referraleasy_rag.indexing import document_count, index_directory
from referraleasy_rag.opensearch_index import build_count_query, mapping

pytestmark = pytest.mark.integration

TEST_INDEX = "referraleasy-directory-chunks-integration"


@pytest.fixture(scope="module")
def cluster(settings: Settings) -> Iterator[Any]:
    """A live client, or a clean skip with the URLs that were tried."""
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


def _index(settings: Settings, cluster: Any, *, recreate: bool) -> Any:
    """Build the test index with the offline embedder: no model call, same width, same mapping."""
    return index_directory(
        settings,
        client=cluster,
        embedder=HashingEmbeddingProvider(dimensions=settings.embedding_dimensions),
        index_name=TEST_INDEX,
        recreate=recreate,
    )


def test_the_mapping_is_accepted_and_carries_both_arms(settings: Settings, cluster: Any) -> None:
    _index(settings, cluster, recreate=True)
    body = cluster.indices.get_mapping(index=TEST_INDEX)
    properties = body[TEST_INDEX]["mappings"]["properties"]

    assert properties["text"]["type"] == "text"
    assert properties["embedding"]["type"] == "knn_vector"
    assert properties["embedding"]["dimension"] == settings.embedding_dimensions
    # The filter fields the arms need, or a filtered query would be rejected at runtime.
    for field in ("languages", "specialties", "hospital_ids", "cities", "entity_type"):
        assert properties[field]["type"] == "keyword"
    assert mapping(settings.embedding_dimensions)["settings"]["index"]["knn"] is True


def test_indexing_twice_does_not_double_the_documents(
    settings: Settings, cluster: Any, chunks: list[Chunk]
) -> None:
    """The property the indexer's own summary claims: the same corpus, the same count."""
    first = _index(settings, cluster, recreate=True)
    second = _index(settings, cluster, recreate=False)

    assert first.documents_indexed == len(chunks)
    assert first.documents_in_index == len(chunks)
    assert second.documents_indexed == len(chunks)
    assert second.documents_pruned == 0, "deterministic ids mean nothing from the same run to prune"
    assert second.documents_in_index == len(chunks)
    assert document_count(cluster, index_name=TEST_INDEX) == len(chunks)
    assert second.knowledge_version == corpus_version(chunks)


def test_the_summary_counts_match_the_cluster(
    settings: Settings, cluster: Any, chunks: list[Chunk]
) -> None:
    report = _index(settings, cluster, recreate=True)

    assert report.dimensions == settings.embedding_dimensions
    assert report.chunker_version
    assert "synthetic" in report.embedding_model, "the offline embedder says so in its description"
    for entity_type, expected in report.entities_by_type.items():
        response = cluster.count(
            index=TEST_INDEX, body=build_count_query({"entity_type": [entity_type]})
        )
        assert response["count"] == expected
    assert sum(report.entities_by_type.values()) == len(chunks)


def test_both_arms_execute_against_the_cluster(settings: Settings, cluster: Any) -> None:
    """A mapping can be accepted and a query still be rejected; so both are actually run."""
    from referraleasy_rag.opensearch_index import (
        build_bm25_query,
        build_knn_query,
        hits_from_response,
    )

    _index(settings, cluster, recreate=True)
    vector = [0.0] * settings.embedding_dimensions
    vector[0] = 1.0
    filters = {"languages": ["es"]}

    lexical = cluster.search(index=TEST_INDEX, body=build_bm25_query("cardiologist", filters, 5))
    nearest = cluster.search(index=TEST_INDEX, body=build_knn_query(vector, filters, 5))

    assert hits_from_response(lexical), "the lexical arm found nothing; the query is wrong"
    assert hits_from_response(nearest), "the filtered kNN arm found nothing"
    for hit in hits_from_response(lexical) + hits_from_response(nearest):
        assert hit["_id"]
        assert "embedding" not in hit["chunk"], "vectors are excluded from _source"


def test_a_synthetic_marker_is_indexed(settings: Settings, cluster: Any) -> None:
    """Every fixture document is synthetic, and the index says so — an answer must be able to.

    Counted with a raw term query rather than through :func:`build_count_query`: ``synthetic`` is
    deliberately not a user-facing filter (nobody asks a directory for synthetic records), but it
    still has to be a real, queryable field.
    """
    _index(settings, cluster, recreate=True)
    response = cluster.count(index=TEST_INDEX, body={"query": {"term": {"synthetic": True}}})
    assert response["count"] > 0
