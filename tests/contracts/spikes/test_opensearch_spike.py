"""Spike S-08: OpenSearch k-NN, hybrid BM25 + k-NN, and the OTel metrics question.

Three things, all against a **running** node:

1. k-NN is available and maps an ``EMBEDDING_DIMENSIONS``-wide vector (ADR-0006/0008).
2. The raw ``knn`` clause works *inside* a ``hybrid`` query. ADR-0008 records this as
   the main unverified item: the vendor example uses the ``neural`` clause, which
   needs an ML Commons model deployed inside OpenSearch, and ReferralEasy computes
   its embeddings externally on Vertex.
3. **Native OTel metrics ingestion does not exist** (ADR-0008), with the adopted
   numeric-log-event fallback demonstrated end to end.

Needs a running node at OPENSEARCH_URL. Start it with ``make up``. The client is not
part of the workspace yet, so run this one standalone (note ``python -m pytest``):

    uv run --with opensearch-py==3.2.0 python -m pytest tests/contracts/spikes -m spike
"""

from __future__ import annotations

import contextlib
import os
import uuid
from typing import Any

import pytest
from _spike_env import require_env, require_module

pytestmark = pytest.mark.spike

ADR = "ADR-0008-opensearch-single-node-hybrid-search-and-otel-metrics.md"

# Candidate OTLP/HTTP metrics ingest routes. Probing several is deliberate: the point
# is to falsify "no native ingestion", not to confirm a path we chose. Any 2xx here
# means ADR-0008 is wrong and must be revised before the next demo.
_OTLP_METRICS_ROUTES = ("/_otlp/v1/metrics", "/v1/metrics", "/_plugins/_otel/v1/metrics")


def _client() -> Any:
    url = require_env(
        "OPENSEARCH_URL",
        purpose="locates the single-node OpenSearch instance these checks run against",
        where="set by .env.example (http://opensearch:9200); start the stack with `make up`",
        adr=ADR,
    )
    opensearchpy = require_module("opensearchpy", distribution="opensearch-py==3.2.0", adr=ADR)
    return opensearchpy.OpenSearch(
        hosts=[url],
        timeout=float(os.environ.get("SPIKE_HTTP_TIMEOUT_S", "30")),
        max_retries=0,
    )


def _reachable(client: Any) -> dict[str, Any]:
    try:
        info = client.info()
    except Exception as exc:
        pytest.fail(
            "OPENSEARCH NOT REACHABLE.\n\n"
            f"OPENSEARCH_URL={os.environ.get('OPENSEARCH_URL')!r}\n"
            f"Error: {type(exc).__name__}: {exc}\n\n"
            "This spike needs a running node - it is not a unit test.\n\n"
            "    make up          # compose profile core\n"
            "    make down        # when finished\n\n"
            f"See docs/adr/{ADR}.",
            pytrace=False,
        )
    return dict(info)


def test_node_version_and_knn_plugin() -> None:
    """Record the node version and prove k-NN is really installed."""
    client = _client()
    info = _reachable(client)
    version = info.get("version", {}).get("number")

    cat = require_module("opensearchpy", distribution="opensearch-py==3.2.0", adr=ADR)
    plugins = client.cat.plugins(format="json", h="component,version")
    names = sorted(str(p.get("component", "")) for p in plugins)
    assert names, "no plugins reported - is this really an OpenSearch node?"

    print(f"\n[OPENSEARCH] version={version}")
    print(f"[OPENSEARCH] plugins={names}")
    assert version, "the node reported no version"

    knn = [n for n in names if "knn" in n.replace("-", "").lower()]
    if not knn:
        pytest.fail(
            f"k-NN PLUGIN NOT PRESENT on this node (version {version}).\n\n"
            f"Reported plugins: {names}\n\n"
            "ADR-0008 pins OpenSearch 3.9.0, whose official build manifest lists k-NN at "
            "tags/3.9.0.0 as a bundled component. A node without it means the image or the "
            "compose service is not the pinned distribution.",
            pytrace=False,
        )
    assert cat is not None


def test_knn_index_and_vector_search() -> None:
    """A knn_vector field at EMBEDDING_DIMENSIONS width accepts data and is searchable."""
    raw_dimensions = require_env(
        "EMBEDDING_DIMENSIONS",
        purpose="the vector width the directory index must be mapped with",
        where=".env.example pins 768; it must equal the width ADR-0006 pins for "
        "gemini-embedding-001 (output_dimensionality)",
        adr=ADR,
    )
    dimensions = int(raw_dimensions)
    client = _client()
    _reachable(client)

    index = f"spike-knn-{uuid.uuid4().hex[:8]}"
    mapping = {
        "settings": {"index": {"knn": True, "number_of_replicas": 0}},
        "mappings": {
            "properties": {
                "text": {"type": "text"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dimensions,
                    "method": {"name": "hnsw", "space_type": "cosinesimil"},
                },
            }
        },
    }

    try:
        client.indices.create(index=index, body=mapping)

        def vector(*axes: tuple[int, float]) -> list[float]:
            """A vector with weight on the given axes.

            Deliberately NOT a constant vector. `cosinesimil` is scale-invariant, so every
            constant vector is parallel to every other and scores 1.0 against it — an assertion
            that "near" wins would then pass without comparing anything. It also cannot be all
            zeros: k-NN rejects a zero vector under cosine similarity outright.
            """
            v = [0.0] * dimensions
            for axis, weight in axes:
                v[axis % dimensions] = weight
            return v

        client.index(
            index=index, id="near", body={"text": "cardiology", "embedding": vector((0, 1.0))}
        )
        client.index(
            index=index, id="far", body={"text": "dermatology", "embedding": vector((1, 1.0))}
        )
        client.indices.refresh(index=index)

        try:
            result = client.search(
                index=index,
                body={
                    "size": 1,
                    # Mostly aligned with "near", barely with "far".
                    "query": {
                        "knn": {"embedding": {"vector": vector((0, 0.99), (1, 0.01)), "k": 1}}
                    },
                },
            )
        except Exception as exc:
            pytest.fail(
                "THE RAW `knn` QUERY CLAUSE WAS REJECTED.\n\n"
                f"Error: {type(exc).__name__}: {exc}\n\n"
                "ADR-0008 depends on this clause for the externally-computed (Vertex) "
                "embedding path. If the field must instead be queried another way, record "
                f"the working shape in docs/adr/{ADR} and update this spike.",
                pytrace=False,
            )

        hits = result["hits"]["hits"]
        assert hits, "k-NN search returned no hits"
        assert hits[0]["_id"] == "near", (
            f"k-NN returned the wrong nearest document: {hits[0]['_id']!r}. The vector "
            "space settings (space_type) do not match the data being written."
        )
    finally:
        client.indices.delete(index=index, ignore=[404])


def test_hybrid_bm25_plus_knn_query() -> None:
    """The full ADR-0008 hybrid shape: search pipeline + `hybrid` query + `knn` clause."""
    dimensions = int(
        require_env(
            "EMBEDDING_DIMENSIONS",
            purpose="vector width for the hybrid index",
            where=".env.example pins 768 (ADR-0006)",
            adr=ADR,
        )
    )
    client = _client()
    _reachable(client)

    index = f"spike-hybrid-{uuid.uuid4().hex[:8]}"
    pipeline = f"spike-pipeline-{uuid.uuid4().hex[:8]}"

    def vector(*axes: tuple[int, float]) -> list[float]:
        """A vector with weight on the given axes. See the note in the k-NN test above:
        constant vectors are useless under `cosinesimil` and an all-zero vector is rejected."""
        v = [0.0] * dimensions
        for axis, weight in axes:
            v[axis % dimensions] = weight
        return v

    mapping = {
        "settings": {"index": {"knn": True, "number_of_replicas": 0}},
        "mappings": {
            "properties": {
                "text": {"type": "text"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dimensions,
                    "method": {"name": "hnsw", "space_type": "cosinesimil"},
                },
            }
        },
    }
    pipeline_body = {
        "description": "Spike S-08: hybrid BM25 + k-NN (ADR-0008)",
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {"weights": [0.3, 0.7]},
                    },
                }
            }
        ],
    }

    try:
        client.indices.create(index=index, body=mapping)
        client.transport.perform_request("PUT", f"/_search/pipeline/{pipeline}", body=pipeline_body)
        client.index(
            index=index,
            id="cardio",
            body={"text": "urgent cardiology review", "embedding": vector((0, 1.0))},
        )
        client.index(
            index=index,
            id="derm",
            body={"text": "routine dermatology review", "embedding": vector((1, 1.0))},
        )
        client.indices.refresh(index=index)

        body = {
            "size": 2,
            "query": {
                "hybrid": {
                    "queries": [
                        {"match": {"text": {"query": "urgent cardiology"}}},
                        {"knn": {"embedding": {"vector": vector((0, 0.99), (1, 0.01)), "k": 2}}},
                    ]
                }
            },
        }
        try:
            result = client.search(index=index, body=body, params={"search_pipeline": pipeline})
        except Exception as exc:
            pytest.fail(
                "HYBRID BM25 + KNN FAILED.\n\n"
                f"Error: {type(exc).__name__}: {exc}\n\n"
                "ADR-0008 records this as its central unverified item: the vendor example "
                "combines `neural` (ML Commons, model deployed *inside* OpenSearch) with "
                "`match`, while this project embeds externally on Vertex and therefore "
                "needs the raw `knn` clause inside `hybrid.queries`.\n\n"
                "If this cannot be made to work, the documented fallback is two separate "
                "searches plus application-side score fusion, and the ADR must be amended.",
                pytrace=False,
            )

        hits = result["hits"]["hits"]
        assert hits, "hybrid query returned no hits"
        assert hits[0]["_id"] == "cardio", (
            f"hybrid ranking put {hits[0]['_id']!r} first; expected 'cardio'. Weights "
            "(0.3 lexical / 0.7 vector) or the normalization technique need review."
        )
        assert "_score" in hits[0]
    finally:
        # Cleanup must never be able to mask the outcome under test. A failure here previously
        # replaced the real assertion error with a misleading one about a DELETE parameter.
        # The pipeline endpoint also rejects `ignore_unavailable`, so it is not sent at all.
        with contextlib.suppress(Exception):
            client.indices.delete(index=index, ignore=[404])
        with contextlib.suppress(Exception):
            client.transport.perform_request("DELETE", f"/_search/pipeline/{pipeline}")


def test_no_native_otel_metrics_ingestion() -> None:
    """Falsify ADR-0008's claim that the node has no OTLP metrics ingest endpoint.

    Any 2xx from a probed route means native ingestion exists and the ADR is wrong.
    """
    client = _client()
    _reachable(client)

    results: dict[str, int] = {}
    for route in _OTLP_METRICS_ROUTES:
        try:
            response = client.transport.perform_request(
                "POST",
                route,
                body={},
                params={"error_trace": "false"},
            )
            del response
            results[route] = 200
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "info", None), "status", None
            )
            results[route] = int(status) if isinstance(status, int) else -1

    print(f"\n[OTEL METRICS INGEST PROBE] {results}")

    accepted = {route: status for route, status in results.items() if 200 <= status < 300}
    if accepted:
        pytest.fail(
            "NATIVE OTLP METRICS INGESTION APPEARS TO EXIST.\n\n"
            f"These routes answered 2xx: {accepted}\n\n"
            "ADR-0008 states the 3.9.0 node has no OTLP metrics *ingest* endpoint (only an "
            "OTLP *exporter* plugin), and the project's fallback to numeric-log-event "
            "metrics is justified by that. Re-verify and amend the ADR before relying on "
            "the fallback.",
            pytrace=False,
        )

    assert all(status != -1 for status in results.values()), (
        f"a probe produced no status code at all: {results}. The probe is inconclusive."
    )


def test_numeric_log_event_metrics_fallback_works() -> None:
    """Demonstrate the adopted fallback: metrics as numeric fields, aggregated in place."""
    client = _client()
    _reachable(client)

    index = f"spike-metrics-{uuid.uuid4().hex[:8]}"
    try:
        client.indices.create(
            index=index,
            body={
                "settings": {"number_of_replicas": 0},
                "mappings": {
                    "properties": {
                        "@timestamp": {"type": "date"},
                        "metric.name": {"type": "keyword"},
                        "metric.value": {"type": "double"},
                        "synthetic": {"type": "boolean"},
                    }
                },
            },
        )
        for offset, value in enumerate((1.0, 2.0, 3.0, 4.0)):
            client.index(
                index=index,
                body={
                    "@timestamp": f"2026-10-14T10:0{offset}:00Z",
                    "metric.name": "agent.attempt.duration_ms",
                    "metric.value": value,
                    "synthetic": True,
                },
            )
        client.indices.refresh(index=index)

        result = client.search(
            index=index,
            body={
                "size": 0,
                "query": {"term": {"metric.name": "agent.attempt.duration_ms"}},
                "aggs": {"value": {"stats": {"field": "metric.value"}}},
            },
        )
        stats = result["aggregations"]["value"]
        assert stats["count"] == 4, f"expected 4 metric documents, aggregated {stats['count']}"
        assert stats["avg"] == pytest.approx(2.5), f"unexpected mean: {stats['avg']}"
        print(f"\n[FALLBACK METRICS] stats={stats}")
    finally:
        client.indices.delete(index=index, ignore=[404])
