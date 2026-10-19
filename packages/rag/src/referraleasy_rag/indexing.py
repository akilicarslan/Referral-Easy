"""Building the index — the step most likely to look successful while doing nothing.

An empty index answers every query with zero hits and no error, which reads as "no matching
directory entries" rather than "the indexer never ran". So every step here is checked and
counted: the index is created rather than assumed, the embedding count must equal the chunk
count, bulk failures are raised rather than summarised away, and the number of documents the
index actually holds is read back from the cluster and reported.

Idempotency is a property of the document ids, not of a cleanup pass: a chunk id is a
deterministic hash of the entity, the chunker version and the text, so re-indexing the same
corpus overwrites its own documents. The prune step only removes documents from an *earlier*
run (a previous chunker or corpus version), which is what keeps a re-run from accumulating
stale rows while leaving the current run's documents untouched.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from referraleasy_rag.chunking import (
    CHUNKER_VERSION,
    Chunk,
    chunk_directory,
    corpus_version,
    run_id,
    validate_name_phone_pairing,
)
from referraleasy_rag.config import Settings
from referraleasy_rag.contracts import (
    load_hospitals,
    load_locations,
    load_providers,
)
from referraleasy_rag.degradation import CallLedger
from referraleasy_rag.embeddings import EmbeddingProvider
from referraleasy_rag.opensearch_index import (
    ClusterClient,
    IndexReport,
    bulk_body,
    mapping,
)


class IndexingError(RuntimeError):
    """The index could not be built correctly. Always fatal: a half-built index lies."""


def build_chunks(settings: Settings) -> list[Chunk]:
    """Load the fixtures, chunk them, and refuse to continue if a grounding rule is broken."""
    providers = load_providers(settings.fixtures_dir)
    hospitals = load_hospitals(settings.fixtures_dir)
    locations = load_locations(settings.fixtures_dir)
    chunks = chunk_directory(providers, hospitals, locations)
    problems = validate_name_phone_pairing(chunks)
    if problems:
        raise IndexingError(
            "entity chunking broke the name <-> phone relation, refusing to index: "
            + "; ".join(problems[:5])
        )
    return chunks


def ensure_index(
    client: ClusterClient, *, index_name: str, dimensions: int, recreate: bool = False
) -> bool:
    """Create the index if it does not exist. Returns True when it was created."""
    exists = bool(client.indices.exists(index=index_name))
    if exists and recreate:
        client.indices.delete(index=index_name)
        exists = False
    if not exists:
        client.indices.create(index=index_name, body=mapping(dimensions))
        return True
    return False


def embed_chunks(embedder: EmbeddingProvider, chunks: Sequence[Chunk]) -> list[list[float]]:
    """Embed every chunk, asserting the width before anything is written."""
    vectors = embedder.embed_documents([chunk.text for chunk in chunks])
    if len(vectors) != len(chunks):
        raise IndexingError(f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks")
    for vector in vectors:
        if len(vector) != embedder.dimensions:
            raise IndexingError(
                f"embedder returned a {len(vector)}-dimension vector but declares "
                f"{embedder.dimensions}"
            )
    return vectors


def prune_previous_runs(client: ClusterClient, *, index_name: str, current_run: str) -> int:
    """Delete documents belonging to an earlier run. Returns the number deleted."""
    response = client.delete_by_query(
        index=index_name,
        body={"query": {"bool": {"must_not": [{"term": {"run_id": current_run}}]}}},
        params={"refresh": "true", "conflicts": "proceed"},
    )
    deleted = response.get("deleted") if isinstance(response, Mapping) else None
    return deleted if isinstance(deleted, int) else 0


def bulk_index(
    client: ClusterClient, *, index_name: str, documents: Sequence[Mapping[str, Any]]
) -> int:
    """Index documents with ``_id = chunk_id``. Returns the number accepted by the cluster."""
    if not documents:
        return 0
    response = client.bulk(
        body=bulk_body(documents, index=index_name),
        index=index_name,
        params={"refresh": "true"},
    )
    if not isinstance(response, Mapping):
        raise IndexingError(f"bulk response was not a mapping: {response!r}")
    if response.get("errors") is True:
        failures = [
            item
            for item in _item_list(response)
            if _item_status(item) is not None and (_item_status(item) or 0) >= 300
        ]
        raise IndexingError(
            f"{len(failures)} of {len(documents)} documents failed to index; first error: "
            f"{_first_error(failures)}"
        )
    return len(documents)


def _item_list(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = response.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def _item_status(item: Mapping[str, Any]) -> int | None:
    for value in item.values():
        if isinstance(value, Mapping) and isinstance(value.get("status"), int):
            status = value.get("status")
            return status if isinstance(status, int) else None
    return None


def _first_error(failures: Sequence[Mapping[str, Any]]) -> str:
    for item in failures:
        for value in item.values():
            if isinstance(value, Mapping):
                error = value.get("error")
                if isinstance(error, Mapping):
                    return str(error.get("reason") or error.get("type") or error)
                if isinstance(error, str):
                    return error
    return "unknown error"


def document_count(client: ClusterClient, *, index_name: str) -> int:
    """Documents the cluster actually holds — read back, not assumed from what was sent."""
    response = client.count(index=index_name)
    count = response.get("count")
    if not isinstance(count, int):
        raise IndexingError(f"count response did not carry a document count: {response!r}")
    return count


def index_directory(
    settings: Settings,
    *,
    client: ClusterClient,
    embedder: EmbeddingProvider,
    index_name: str | None = None,
    recreate: bool = False,
    prune: bool = True,
    ledger: CallLedger | None = None,
    reporter: list[str] | None = None,
) -> IndexReport:
    """Build the directory index. Idempotent: the same corpus yields the same document count."""
    started = time.perf_counter()
    name = index_name or settings.index_name
    chunks = build_chunks(settings)
    version = corpus_version(chunks)
    current_run = run_id(version)
    if reporter is not None:
        reporter.append(f"chunked {len(chunks)} chunks (corpus_version={version})")

    created = ensure_index(
        client, index_name=name, dimensions=embedder.dimensions, recreate=recreate
    )
    vectors = embed_chunks(embedder, chunks)
    documents = [
        chunk.to_document(run_id=current_run, corpus_version=version, embedding=vector)
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    accepted = bulk_index(client, index_name=name, documents=documents)
    pruned = prune_previous_runs(client, index_name=name, current_run=current_run) if prune else 0
    total = document_count(client, index_name=name)

    entities: dict[str, int] = {}
    for chunk in chunks:
        entities[chunk.entity_type] = entities.get(chunk.entity_type, 0) + 1

    return IndexReport(
        index_name=name,
        created=created,
        dimensions=embedder.dimensions,
        knowledge_version=version,
        chunker_version=CHUNKER_VERSION,
        embedding_model=embedder.describe(),
        chunks_built=len(chunks),
        documents_indexed=accepted,
        documents_pruned=pruned,
        documents_in_index=total,
        entities_by_type=entities,
        elapsed_seconds=time.perf_counter() - started,
    )


__all__ = [
    "IndexingError",
    "build_chunks",
    "bulk_index",
    "document_count",
    "embed_chunks",
    "ensure_index",
    "index_directory",
    "prune_previous_runs",
]
