# ADR-0008 — OpenSearch: single-node version, hybrid BM25+kNN shape, and OTel metrics ingestion

- **Status:** Accepted and **spike-verified against a live node (2026-10-14)**. Version, k-NN
  availability, the external-embedding hybrid query shape and the metrics fallback are all
  CONFIRMED by `tests/contracts/spikes/test_opensearch_spike.py` (5/5 passing).
  **Native OTel metrics ingestion is NOT available — the documented fallback is adopted.**
- **Date:** 2026-10-14
- **Phase:** 0 (spike S-08)

## Question

Which OpenSearch version runs single-node, is k-NN available, what is the exact hybrid
BM25 + k-NN query shape — and **explicitly**: does that version natively ingest OTel
*metrics*, or must the project fall back to numeric-log-event metrics?

## What was checked

| # | Command / URL | Result |
|---|---|---|
| 1 | `curl -s "https://hub.docker.com/v2/repositories/opensearchproject/opensearch/tags?page_size=25&ordering=last_updated"` | `latest` = **3.9.0** (2026-09-29); `3.8.0` (2026-09-15); 2.x line: `2.19.6` (2026-09-15) |
| 2 | `curl -s .../opensearch-build/main/manifests/3.9.0/opensearch-3.9.0.yml` | build manifest, `build.version: 3.9.0`; components include **k-NN `ref: tags/3.9.0.0`** and **neural-search `ref: tags/3.9.0.0`** |
| 3 | `curl -s https://pypi.org/pypi/opensearch-py/json` | client `3.2.0`, `requires_python <4,>=3.10` |
| 4 | `.../documentation-website/main/_query-dsl/compound/hybrid.md` | `hybrid` query DSL: `queries[]` (max 5), `filter`, `pagination_depth`; requires a search pipeline |
| 5 | `.../_vector-search/ai-search/hybrid-search/index.md` | search pipeline shape, index mapping (`index.knn: true`, `knn_vector`), the working hybrid request body |
| 6 | `gh search code "otlp" --repo opensearch-project/OpenSearch` | **exporter only** — `io.opentelemetry.exporter.otlp.metrics.OtlpGrpcMetricExporter` used by `plugins/telemetry-otel` |
| 7 | `gh api "search/repositories?q=org:opensearch-project+opentelemetry"` | none; the only `otel`-named repo is `genai-observability-sdk-py` (a tracing SDK, not an ingest plugin) |
| 8 | `gh api repos/opensearch-project/data-prepper/releases/latest` | `2.16.0`; `otel_metrics_source` plugin exists, HTTP service added in 2.15.0 |

## Decision

- **`opensearchproject/opensearch:3.9.0`**, single node, security plugin disabled for the
  local demo profile. `2.19.6` is the recorded fallback if a dependency needs the 2.x line.
- **k-NN: available.** It is a bundled component of the official 3.9.0 build
  (`tags/3.9.0.0`), not an optional add-on. `neural-search` 3.9.0.0 ships alongside it.
- **Client:** `opensearch-py==3.2.0`.
- **Directory RAG index:** `index.knn: true`, embedding field mapped as `knn_vector` with
  `dimension: 768` (must match ADR-0006 exactly — a mismatch is a hard index error, which
  is the good outcome), text field mapped as `text`, both in the same index so the hybrid
  query can combine them in one request.

### Hybrid query shape (verified)

Search pipeline (created once):

```json
PUT /_search/pipeline/referraleasy-hybrid
{ "phase_results_processors": [
  { "normalization-processor": {
      "normalization": { "technique": "min_max" },
      "combination":   { "technique": "arithmetic_mean", "parameters": { "weights": [0.3, 0.7] } } } } ] }
```

Query (`?search_pipeline=referraleasy-hybrid`):

```json
{ "query": { "hybrid": { "queries": [
      { "match":  { "text":     { "query": "<q>" } } },
      { "neural": { "embedding": { "query_text": "<q>", "model_id": "<deployed>", "k": 5 } } } ] } } }
```

Constraints established from the docs — these are the ones that will bite:

- `hybrid` is **top-level only**. It cannot be nested inside `function_score`,
  `constant_score`, `script_score`, `boosting`, or multilevel combinations, and doing so
  "may produce a runtime error or silently bypass the normalization pipeline". Silent
  bypass is the dangerous case.
- A `bool.should` is **not** a substitute: it scores by plain Boolean addition and skips
  the search pipeline entirely.
- `queries` accepts **at most 5** clauses.
- `rescore` runs **per subquery at shard level** before normalization — not on the merged
  result set as with standard queries.
- `pagination_depth` bounds how deep each subquery may go; required when `from > 0`.
  Paginating past it silently truncates candidates, so it must be set from the requested
  page depth, not left at a default.
- Pre-filtering uses the hybrid query's top-level `filter`; `post_filter` is post-scoring.
- Since 3.5: `min_score` is applied *after* normalization; hybrid works on indexes with
  >512 shards without configuration; `_msearch` does not auto-handle batched reduction.

### OTel metrics ingestion — **NOT native; fallback adopted**

The project required this be *verified, not assumed*. Verified finding: **the OpenSearch
3.9.0 node does not accept OTLP metrics.** The only OTLP code in the distribution is an
**exporter** — the `telemetry-otel` plugin uses `OtlpGrpcMetricExporter` to push
OpenSearch's *own* internal telemetry outward. No OTLP receiver/ingest endpoint exists in
the core, and no organisation repo provides one.

Therefore, per the project's own rule:

> **Adopted fallback: numeric-log-event metrics.** Services emit metrics as structured
> documents with numeric fields (e.g. `@timestamp`, `metric.name`, `metric.value`,
> `attributes.*`) into `referraleasy-metrics-*`, and the demo's metric views aggregate them
> with `stats`/`percentiles`/`date_histogram`. This needs no additional service and no
> assumption about a feature that does not exist.

The alternative path — should a real OTLP pipeline ever be required — is Data Prepper
`otel_metrics_source` (2.16.0), which lands OTel metrics into OpenSearch as documents
anyway, at the cost of another container. It is recorded here, **not** adopted.

## What remains unverified

1. ~~**The raw `knn` clause inside a `hybrid.queries` array.**~~ **RESOLVED 2026-10-14 — it
   works, and the fallback is not needed.** The documentation's verified example uses a
   `neural` query, which requires an ML Commons model deployed *inside* OpenSearch.
   ReferralEasy computes embeddings **externally** (Vertex, ADR-0006), so it needs the raw
   `knn` vector clause instead — a combination the vendor example does not cover.
   `test_hybrid_bm25_plus_knn_query` now drives the full shape against a live 3.9.0 node:
   a normalisation search pipeline plus a top-level `hybrid` query whose `queries` array is
   `[{match}, {knn}]`, with a `knn_vector` mapping at 768 dimensions and an externally-computed
   query vector. It returns ranked hits with scores, ranked correctly. **The single-request
   guarantee holds and two-searches-plus-application-fusion is not needed.**

   Two things the spike had to get right, both recorded because they are easy to repeat:
   a `cosinesimil` index **rejects an all-zero vector outright**, and constant vectors are
   useless as fixtures — cosine similarity is scale-invariant, so every constant vector is
   parallel to every other and scores 1.0, meaning a ranking assertion built on them passes
   without comparing anything. The spike now writes vectors with weight on distinct axes.
2. **`dimension: 768` enforcement.** k-NN rejects a mismatched vector at index time; the
   spike asserts the error rather than trusting it.
3. **Whether the `telemetry-otel` plugin is enabled by default** in the 3.9.0 image, and
   whether it can be disabled to keep the demo's topology honest.
4. **Single-node k-NN behaviour** — no replica shards, so k-NN graph build behaviour with
   `index.knn` defaults (which assume replicas) is unverified.
5. **`otel_metrics_source` schema mapping** (which `ss4o_metrics-*` template it writes) —
   not read, because the path is not adopted.

## Consequences

- The dashboard/metrics requirement is satisfiable today with zero new components and zero
  unverified features.
- The "is native OTLP metrics ingest supported?" question is answered **no**, with the
  evidence recorded, so it cannot resurface later as an assumption.
- Spike `tests/contracts/spikes/test_opensearch_spike.py` covers three things against a
  running node: k-NN index creation + raw vector search; the full hybrid request above; and
  an explicit probe/report that no native OTLP metrics ingest exists, followed by a
  demonstration of the adopted numeric-log-event fallback (index a numeric metric document,
  run a `stats` aggregation over it).
