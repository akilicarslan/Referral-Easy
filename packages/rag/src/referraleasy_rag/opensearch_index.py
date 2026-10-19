"""The OpenSearch index: one index, two retrieval arms, one request.

Text and the 768-dimension kNN vector live in the *same* index on purpose. Splitting them into
two indices would make the hybrid query two round trips, two filter sets and two chances for the
filters to differ between the arms — and the pipeline requires the arms to take identical
filters (language, specialty, hospital), which is a property of the query builder here, not of
the caller's discipline.

:func:`filters_clause` is the single source of the filter clause, and both
:func:`build_bm25_query` and :func:`build_knn_query` call it. A test asserts the two bodies carry
the same filter, because "the arms drifted apart" is a silent failure: both return results, both
look plausible, and the vector arm quietly ignores the specialty the user asked for.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

#: Index fields that participate in lexical scoring, with their boosts. The phone number is
#: heavily boosted: when a query contains one, that is almost never a topical interest.
BM25_FIELDS: tuple[str, ...] = (
    "contact_phone^6",
    "display_name^4",
    "text^3",
    "hospital_names^2",
    "specialties^2",
    "subspecialties^2",
    "cities",
)

#: Every keyword field the filters may touch. Kept here so the mapping and the builder cannot
#: disagree: :func:`mapping` is generated from this list.
FILTERABLE_KEYWORD_FIELDS: tuple[str, ...] = (
    "entity_type",
    "specialty",
    "specialties",
    "languages",
    "hospital_ids",
    "cities",
    "states",
    "source_id",
    "knowledge_version",
    "corpus_version",
    "run_id",
)

#: Metadata fields the chunker may emit. Asserted against by a test, so a new metadata key that
#: nobody added to the mapping is caught at test time instead of being silently unsearchable.
DOCUMENT_METADATA_FIELDS: tuple[str, ...] = (
    "display_name",
    "specialty",
    "specialties",
    "subspecialties",
    "languages",
    "hospital_ids",
    "hospital_names",
    "cities",
    "states",
    "postal_code",
    "location_id",
    "contact_phone",
    "credentials",
    "adapter_type",
    "accepts_new_patients",
    "accessibility_features",
    "synthetic",
    "geo_lat",
    "geo_lon",
)


class SearchClient(Protocol):
    """The slice of the OpenSearch client this package searches through.

    Structural, so a fake is trivial to write — which matters, because the degradation ladder can
    only be tested by making things fail on purpose.
    """

    def search(self, *, index: str, body: Mapping[str, Any]) -> Mapping[str, Any]: ...


class IndicesClient(Protocol):
    """Index lifecycle operations, as `opensearch-py` namespaces them."""

    def exists(self, *, index: str) -> Any: ...

    def create(self, *, index: str, body: Mapping[str, Any]) -> Any: ...

    def delete(self, *, index: str, params: Mapping[str, Any] | None = ...) -> Any: ...

    def refresh(self, *, index: str) -> Any: ...


class ClusterClient(SearchClient, Protocol):
    """Adds the cluster operations the indexer needs. Signature-compatible with `opensearch-py`.

    ``params=`` is how `opensearch-py` 3.x takes per-request options such as ``refresh`` — they
    are not keyword arguments of the method — so the protocol carries them the same way rather
    than inventing a nicer API that the real client does not have.
    """

    indices: IndicesClient

    def bulk(
        self,
        *,
        body: Sequence[Mapping[str, Any]],
        index: str,
        params: Mapping[str, Any] | None = ...,
    ) -> Any: ...

    def count(self, *, index: str) -> Any: ...

    def delete_by_query(
        self, *, index: str, body: Mapping[str, Any], params: Mapping[str, Any] | None = ...
    ) -> Any: ...


class IndexUnavailableError(RuntimeError):
    """OpenSearch could not be reached. The third rung — the seed corpus — handles this."""


def is_outage(exc: BaseException) -> bool:
    """True when an exception means "the cluster is not answering", false when it means "bug".

    The distinction is what keeps the ladder honest. A dead node, a timeout and a refused
    connection are outages: retrieval degrades to the seed corpus. A 400 from a malformed query
    is *not* an outage — degrading there would hide a broken query behind a working-looking
    answer, which is precisely the failure this pipeline is built to avoid.

    Order matters here: `opensearchpy.exceptions.ConnectionError` *is* a `TransportError`, so the
    specific connection failures are tested first. Checking `TransportError` first — as an earlier
    version of this function did — classifies every refused connection as "not an outage", and the
    ladder then never descends for the one failure it exists to handle.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status >= 500
    info = getattr(exc, "info", None)
    if isinstance(info, Mapping):
        nested = info.get("status")
        if isinstance(nested, int):
            return nested >= 500
    try:  # pragma: no cover - import guard, opensearch-py is a hard dependency
        from opensearchpy.exceptions import ConnectionError as OSConnectionError
        from opensearchpy.exceptions import ConnectionTimeout, TransportError

        if isinstance(exc, (OSConnectionError, ConnectionTimeout)):
            return True
        if isinstance(exc, TransportError):
            return False
    except ImportError:  # pragma: no cover
        pass
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


@dataclass(frozen=True, slots=True)
class IndexReport:
    """What an index build actually did. Printed by the indexer; never inferred."""

    index_name: str
    created: bool
    dimensions: int
    knowledge_version: str
    chunker_version: str
    embedding_model: str
    chunks_built: int
    documents_indexed: int
    documents_pruned: int
    documents_in_index: int
    entities_by_type: Mapping[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    def lines(self) -> list[str]:
        entities = ", ".join(
            f"{key}={value}" for key, value in sorted(self.entities_by_type.items())
        )
        return [
            f"index              {self.index_name} ({'created' if self.created else 'reused'})",
            f"documents indexed  {self.documents_indexed} ({entities})",
            f"documents pruned   {self.documents_pruned} (documents from an earlier run)",
            f"documents in index {self.documents_in_index} after refresh",
            f"vector dimensions  {self.dimensions}",
            f"knowledge_version  {self.knowledge_version}",
            f"chunker_version    {self.chunker_version}",
            f"embedding model    {self.embedding_model}",
            f"elapsed            {self.elapsed_seconds:.2f}s",
        ]


def mapping(dimensions: int) -> dict[str, Any]:
    """Index mapping: one text field, one kNN field, keyword fields for the filters."""
    properties: dict[str, Any] = {
        "chunk_id": {"type": "keyword"},
        "entity_id": {"type": "keyword"},
        "text": {"type": "text"},
        "display_name": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
        },
        "hospital_names": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "subspecialties": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "contact_phone": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 64}},
        },
        "credentials": {"type": "text"},
        "postal_code": {"type": "keyword"},
        "location_id": {"type": "keyword"},
        "adapter_type": {"type": "keyword"},
        "accepts_new_patients": {"type": "boolean"},
        "accessibility_features": {"type": "keyword"},
        "synthetic": {"type": "boolean"},
        "updated_at": {"type": "date"},
        "ordinal": {"type": "integer"},
        "geo_lat": {"type": "float"},
        "geo_lon": {"type": "float"},
        "embedding": {
            "type": "knn_vector",
            "dimension": dimensions,
            "method": {
                "name": "hnsw",
                "space_type": "cosinesimil",
                "engine": "lucene",
                "parameters": {"ef_construction": 128, "m": 16},
            },
        },
    }
    for name in FILTERABLE_KEYWORD_FIELDS:
        properties.setdefault(name, {"type": "keyword"})
    return {
        "settings": {
            "index": {
                "knn": True,
                "knn.algo_param.ef_search": 100,
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
        },
        "mappings": {"properties": properties},
    }


def filters_clause(filters: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Translate a filter mapping into OpenSearch clauses.

    Term (not terms-across-fields) semantics, applied to keyword fields only: a filter says what
    must be true of the entity, so it belongs in the query *filter* context where it does not
    contribute to the score. A "soft" filter that merely re-ranks would let a document the user
    excluded rank first.
    """
    if not filters:
        return []
    clauses: list[dict[str, Any]] = []
    for name in sorted(filters):
        value = filters[name]
        if value is None or value == "" or value == []:
            continue
        if name not in FILTERABLE_KEYWORD_FIELDS:
            raise ValueError(
                f"unknown filter {name!r}; filterable fields are {FILTERABLE_KEYWORD_FIELDS}"
            )
        values = [item for item in (value if isinstance(value, (list, tuple, set)) else [value])]
        if not values:
            continue
        clauses.append({"terms": {name: [str(item) for item in values]}})
    return clauses


def build_bm25_query(query: str, filters: Mapping[str, Any] | None, size: int) -> dict[str, Any]:
    """Lexical arm. Takes the same ``filters`` object as the vector arm, by construction."""
    return {
        "_source": {"excludes": ["embedding"]},
        "size": size,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": list(BM25_FIELDS),
                            "type": "best_fields",
                        }
                    }
                ],
                "filter": filters_clause(filters),
            }
        },
    }


def build_knn_query(
    vector: Sequence[float], filters: Mapping[str, Any] | None, size: int
) -> dict[str, Any]:
    """Vector arm. Same ``filters`` object, placed inside the kNN clause as well as the bool.

    The filter appears inside ``knn`` because that is what makes the k nearest *matching*
    neighbours come back; putting it only on the outer bool would retrieve k global neighbours
    and then discard nearly all of them, silently shrinking the arm.
    """
    clause: list[dict[str, Any]] = filters_clause(filters)
    knn: dict[str, Any] = {"vector": [float(value) for value in vector], "k": size}
    if clause:
        knn["filter"] = {"bool": {"filter": clause}}
    return {
        "_source": {"excludes": ["embedding"]},
        "size": size,
        "query": {
            "bool": {
                "must": [{"knn": {"embedding": knn}}],
                "filter": clause,
            }
        },
    }


def build_count_query(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Count documents matching a filter, used by the indexer's summary."""
    clause = filters_clause(filters)
    return {"query": {"bool": {"filter": clause}} if clause else {"match_all": {}}}


def hits_from_response(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize a search response into plain hit dicts (``{"_id", "_score", "chunk": {...}}``)."""
    hits = response.get("hits")
    if not isinstance(hits, Mapping):
        raise IndexUnavailableError("search response carried no hits object")
    raw = hits.get("hits")
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        source = entry.get("_source")
        score = entry.get("_score")
        normalized.append(
            {
                "_id": entry.get("_id"),
                "_score": float(score) if isinstance(score, (int, float)) else None,
                "chunk": dict(source) if isinstance(source, Mapping) else {},
            }
        )
    return normalized


def bulk_body(
    documents: Iterable[Mapping[str, Any]], *, index: str, id_field: str = "chunk_id"
) -> list[dict[str, Any]]:
    """Build an NDJSON bulk body.

    ``_id`` comes from the chunk id, which is a deterministic hash of the entity, the chunker
    version and the text. Re-indexing the same corpus therefore *overwrites* rather than
    appends, which is the property the idempotency check tests for.
    """
    body: list[dict[str, Any]] = []
    for document in documents:
        identifier = document.get(id_field)
        if not isinstance(identifier, str) or not identifier:
            raise ValueError(f"document without a usable {id_field}: {document.get('chunk_id')!r}")
        body.append({"index": {"_index": index, "_id": identifier}})
        body.append(dict(document))
    return body


__all__ = [
    "BM25_FIELDS",
    "DOCUMENT_METADATA_FIELDS",
    "FILTERABLE_KEYWORD_FIELDS",
    "ClusterClient",
    "IndexReport",
    "IndexUnavailableError",
    "SearchClient",
    "build_bm25_query",
    "build_count_query",
    "build_knn_query",
    "bulk_body",
    "filters_clause",
    "hits_from_response",
    "mapping",
]
