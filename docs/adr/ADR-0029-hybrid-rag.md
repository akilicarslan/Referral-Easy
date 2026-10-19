# ADR-0029 — Directory retrieval: two arms, RRF, rerank, and a degradation ladder that is always visible

- **Status:** Accepted and **verified against the live stack on 2026-10-14**: the indexer was run
  twice against a running OpenSearch 3.9.0 (`28 → 28` documents), the retrieval evaluation was run
  against real 768-dimension embeddings, and the four lower rungs of the ladder were exercised by
  tests that make the failure happen for real. **The reranker itself is UNRESOLVED** (§Rerank): this
  workspace cannot get a 200 from it, and that is recorded rather than papered over.
- **Date:** 2026-10-14
- **Phase:** 2 (directory RAG)
- **Depends on:** ADR-0006 (embedding model), ADR-0007 (rerank endpoint), ADR-0008 (OpenSearch),
  ADR-0024 (fixtures)

## Question

How does the directory answer a question — "who is a Spanish-speaking cardiologist near Naperville,
and do they take my plan?" — **without ever stating something the data does not support**, and what
does it do, visibly, when a component it depends on is not there?

This is the retrieval half of the referral agent. It is a portfolio project, so the requirement is
not only "return good results": it is "be able to prove the answers were grounded, and be able to
prove it when the pipeline is degraded." Those two are the same requirement.

## What was checked

| # | Probe | Result |
|---|---|---|
| 1 | `POST https://openrouter.ai/api/v1/embeddings` with `{"model": "google/gemini-embedding-001", "input": [...], "dimensions": 768}` | **200**, every vector **768 wide** |
| 2 | Same request with `output_dimensionality: 768` (the field ADR-0006 pins for the Google SDK) instead of `dimensions` | the field is **ignored** by this endpoint; the width comes back at the model default |
| 3 | `POST https://openrouter.ai/api/v1/rerank` with `cohere/rerank-v3.5` (ADR-0007's pin) | **HTTP 404** — `"0 endpoints out of 1 requested are available matching your guardrail restrictions and data policy…"` |
| 4 | OpenSearch `3.9.0` at `http://localhost:9200` | reachable, single node, `knn_vector` accepted with `dimension: 768` (ADR-0008) |
| 5 | `uv run python scripts/index_directory.py` (twice, live embeddings) | `documents indexed 28` / `documents in index 28` both times, `documents pruned 0` |
| 6 | 28 fixture entities (`provider=14, hospital=7, location=7`) through the chunker | **28 chunks** — one per entity, longest 248 tokens |

## Decision

### One index, two arms, one filter object

The text and the vector live in the **same index** (`referraleasy-directory-chunks`), so a hybrid
question is one round trip per arm with one filter set. `filters_clause()` is the only place a filter
becomes a query, and both `build_bm25_query()` and `build_knn_query()` call it. A test asserts the two
bodies carry the *same* clause, because arm drift is a silent failure: both arms return results, both
look plausible, and the vector arm quietly ignores the specialty the user asked for.

The filter goes **inside the `knn` clause as well as** on the outer `bool`, so the k nearest *matching*
neighbours come back. Filtering only outside would retrieve k global neighbours and then discard
almost all of them, shrinking the arm without ever failing.

Filters are term-level and in query *filter* context (they do not contribute to the score). A "soft"
filter that merely re-ranks would let a document the user excluded rank first.

### Entity-based chunking, with the name↔phone relation protected

Chunks are built per **entity**, not per page: one provider record produces the atoms that belong
together (name + credentials + specialty + **phone** + sites), so a chunk can never hold a phone
number separated from the name it belongs to. `chunk_provider()` raises `ChunkError` if the pairing
would break, and a test asserts the invariant survives chunking of the whole fixture corpus.

- `MAX_CHUNK_TOKENS = 384` (target), `HARD_MAX_CHUNK_TOKENS = 2048`.
- 2048 is ADR-0006's `autoTruncate: false` sequence limit: an over-long *atom* raises `ChunkError`
  rather than being silently truncated, because a silently truncated embedding is a silently missing
  fact.
- Chunk metadata (`entity_id`, `source_id`, `knowledge_version`, `updated_at`, …) may not override
  reserved document fields — the filter/interop fields win, and a test asserts it.
- **Slots are deliberately not indexed.** A slot is booking state; the directory must not be able to
  assert availability. It answers "who", "where", "which languages", "which plans" — never "are they
  free on Tuesday".

### Fusion: dedup, then RRF, `k = 60`

Each arm returns `arm_top_k = 20`; identical documents are **deduplicated per arm before fusion**
(preferring the better rank), then combined by reciprocal rank fusion `1/(k + rank)`. RRF is used
rather than score normalisation because BM25 scores and cosine similarities are not comparable and
normalising them is a tuned constant pretending to be a measurement. A test pins the fusion against a
hand-computed example, so the arithmetic is checkable rather than asserted.

The fused list is truncated to `rerank_candidates = 10` before reranking; the final answer is
`result_top_k = 5` chunks, and fewer than `min_result_top_k` is reported as "thin evidence" in the
notes.

### Distance comes from coordinates, never from a score

`geo.haversine_km()` computes distance from the indexed `geo_lat`/`geo_lon` (mean Earth radius
`6371.0088 km`). It is never derived from a retrieval score, and an unknown distance is `None` — not a
sentinel, not a large number, not "0 km". A test swaps two coordinates (Chicago ↔ Tokyo) to show that
range validation cannot catch every bad pair; the honest answer there is that the pipeline reports
what the data said.

### Authority: a structured record outranks retrieved text

Coverage ("does this provider take plan X at hospital Y") comes from the structured record, and
`authority.py` makes the conflict **visible** instead of resolving it quietly:

- If a retrieved blurb claims in-network and the coverage row says otherwise, the structured record
  wins, **both sides are returned**, and the conflict is flagged on the result.
- If there is no structured record, the pipeline returns **no answer** — it does not guess from prose,
  and it does not infer one provider's coverage from another's.
- Availability and booking claims ("same business day", "instant confirmation", "slots available",
  "walk-ins welcome", "guaranteed appointment") are **stripped from the grounding text** before it can
  be used as evidence, sentence by sentence, and the removal is reported — so the directory cannot
  assert what it does not own.

### The degradation ladder

Every retrieval reports the rung it came from, and a descent is only ever downwards:

| Rung | Reached when | What the caller gets |
|---|---|---|
| `hybrid_rerank` | everything worked | reranked top 3–5, fully cited |
| `hybrid_rrf` | reranker unavailable, or rerank disabled | the same candidates in RRF order, flagged |
| `bm25_only` | embeddings unavailable, or the index dies after the query was embedded | lexical results only, flagged |
| `seed_fallback` | OpenSearch unreachable from the start | the committed JSON corpus, deterministic, flagged |
| `human_handoff` | nothing found at all | no answer, `requires_human = true` |

`ALLOWED_TRANSITIONS` encodes the descent, `LadderViolation` makes an illegal transition a bug rather
than a mood, and each rung carries its reason string (`reranker_unavailable`, `embeddings_unavailable`,
`opensearch_unavailable`, `index_lost_mid_retrieval`, `no_source_found`, …). "Degraded" is a
first-class value in the result object, not a log line.

Two properties are enforced rather than hoped for:

- **Skipping is allowed; climbing is not.** A dead index goes straight to `seed_fallback`. Nothing
  ever returns to a rung it left.
- **The seed rung makes no call of any kind.** A `CallLedger` records every outbound call — model
  calls counted *per text*, not per request, so batching cannot satisfy "no LLM call" while making
  one — and a test runs the seed rung with sockets blocked at the process level. The ledger also
  reports the number of outbound *requests* separately, because "embedded 28 texts" and "made 1 HTTP
  request" are different facts.

### The seed corpus

`data/seed_directory.v1.json` is committed package data, built from the fixtures by the **same
chunker** (so the degraded corpus and the indexed corpus agree by construction — a test asserts the
committed file is byte-identical to a fresh build, and that the index's chunk ids are a superset of
the seed's).

- **No vectors.** A seed with vectors would be stale the moment the model changed, and it would let
  the last rung make a model call.
- **Deterministic scoring** — tokenisation, light stemming and per-field weights in pure Python. Same
  query, same order, no network.
- **Query terms that appear in more than half the corpus are dropped**
  (`MAX_QUERY_TERM_DOCUMENT_FRACTION = 0.5`). This came out of a test: a nonsense query matched every
  document because the stop word "at" has document frequency 28/28. A word that is in every document
  cannot be evidence for any of them; without the cutoff the last rung returns confident nonsense.

### Embeddings: OpenRouter, not Vertex — and why that is a deviation, not a change of mind

ADR-0006 pins `EMBEDDING_PROVIDER=vertex` with `gemini-embedding-001` at 768 dimensions. In this
environment the Vertex credentials are unresolved, and retrieval cannot be built against an adapter
nobody can run. The **model and the width are unchanged**; only the transport is:

- `POST https://openrouter.ai/api/v1/embeddings`, `google/gemini-embedding-001`, 768 dimensions via
  the `dimensions` field. Probe 2 above is the reason the field name differs from ADR-0006's
  `output_dimensionality`: this endpoint ignores the latter.
- The pin stays in `.env` as the recorded intent. `build_embedding_provider()` selects the adapter;
  the `EmbeddingProvider` port is what the rest of the pipeline sees, so swapping the transport back
  is a change of one factory, not of one pipeline.
- **No task-type asymmetry on this endpoint.** ADR-0006 keeps `embed_documents()` and `embed_queries()`
  as separate methods because a provider that supports task types must not let a call site forget to
  switch. OpenRouter's embeddings endpoint takes no task type, so both paths send identical
  preprocessing (`PREPROCESSING_VERSION = text-norm-1`). The two methods are kept — the seam is what
  prevents the mistake — and a test asserts documents and queries are preprocessed identically, since
  an asymmetry here would degrade retrieval silently.
- The offline provider (`HashingEmbeddingProvider`) exists so the pipeline can be exercised with no
  network. It is **flagged synthetic** on every document it touches, and the ledger treats it as
  neither a network nor a model call (it is a local hash), so an offline test cannot accidentally
  satisfy or violate the "seed made no call" claim.

### Rerank: implemented faithfully, and currently unavailable in this workspace

The client implements ADR-0007's contract exactly (`model`, `query`, `documents`, `top_n` in;
`results[].index` / `relevance_score` as the join key back to our candidates out) and raises a single
`RerankUnavailableError` for every failure mode. On this workspace the endpoint returns the 404 in
probe 3, so:

- the **live** path always descends to `hybrid_rrf`, and says `reranker_unavailable`;
- the rerank *step* is proved by unit tests with a scripted reranker (ordering is really applied, and
  the RRF ordering is really kept when it fails);
- the live 404 is what exercises the rung for real. A provider that is genuinely unavailable is a
  better test of this rung than a stub, and there is no code path that pretends otherwise.

The evaluation prints rerank-on and rerank-off, which currently score **identically** — and it prints
a note saying so, because a reader would otherwise assume the reranker had been measured.

### Idempotency

`chunk_id = sha256(chunker_version, entity_id, ordinal, knowledge_version, text)`. Indexing is a bulk
request whose `_id` is that hash, so re-indexing **overwrites** rather than appends; `run_id` /
`corpus_version` identify a run and documents from an earlier run are pruned. `scripts/index_directory.py`
prints the counts it actually observed (`documents indexed`, `documents pruned`, `documents in index
after refresh`) and exits non-zero on failure, so "it worked" is a printed number, not an assumption.
Verified: two consecutive runs against the live cluster both reported **28 documents, 0 pruned**.

### Configuration

Everything is read from `.env` (never printed, never hardcoded): `OPENROUTER_API_KEY`,
`EMBEDDING_PROVIDER`, `EMBEDDING_MODEL_ID`, `EMBEDDING_DIMENSIONS`, `DIRECTORY_INDEX_NAME`,
`RERANK_PROVIDER`, `RERANK_MODEL_ID`, and the `RAG_*` knobs (`RAG_ARM_TOP_K=20`,
`RAG_RERANK_CANDIDATES=10`, `RAG_RESULT_TOP_K=5`, `RAG_RRF_K=60`, `RAG_EMBEDDING_BATCH_SIZE=32`,
`RAG_REQUEST_TIMEOUT_SECONDS=60`). When `OPENSEARCH_URL` is not set explicitly the client tries a
small host-only fallback list (`localhost:9200`, `127.0.0.1:9200`), because the compose service name
does not resolve from the host; an explicitly configured URL gets **exactly** that URL and no
fallback, so a typo in a real environment fails loudly instead of silently reading a local node.

## Evidence

```
$ OPENSEARCH_URL=http://localhost:9200 uv run python scripts/index_directory.py     # run 1
index              referraleasy-directory-chunks (reused)
documents indexed  28 (hospital=7, location=7, provider=14)
documents pruned   0 (documents from an earlier run)
documents in index 28 after refresh
vector dimensions  768
knowledge_version  directory-chunker-1+21v+6cefb9624388
embedding model    google/gemini-embedding-001 (768d, live, preprocessing text-norm-1)
model calls        28 text(s) over 1 outbound request(s) (embedding.document)
                                                                                     # run 2: identical, 28 and 28
$ OPENSEARCH_URL=http://localhost:9200 uv run pytest tests/rag -v
171 passed                                                                           # 12 are @pytest.mark.integration
$ OPENSEARCH_URL=http://127.0.0.1:9 uv run pytest tests/rag -q
159 passed, 12 skipped                                                               # integration skips cleanly
$ uv run ruff check packages/rag scripts/index_directory.py   → All checks passed!
$ uv run mypy packages/rag                                    → Success: no issues found in 17 source files
```

Retrieval evaluation (10 queries; 4 lexical, 6 paraphrase, 4 filtered), live embeddings:

| Configuration | MRR | recall@1 | recall@3 | recall@5 | hit_rate |
|---|---|---|---|---|---|
| hybrid, rerank on (reranker 404 → `hybrid_rrf`) | 0.950 | 0.733 | 1.000 | 1.000 | 1.000 |
| hybrid, rerank off (`hybrid_rrf`) | 0.950 | 0.733 | 1.000 | 1.000 | 1.000 |
| seed only (no index, **no model call at all**) | 0.650 | 0.383 | 0.750 | 0.950 | 1.000 |

The seed-only row is the one that matters: the last rung finds every query's entity in the top 5 and
half of them at rank 1, offline and deterministically.

## What remains unverified

1. **Rerank has never returned a 200 here** (probe 3), so its live effect on ranking is unmeasured.
   Only its failure path and its stubbed effect are tested.
2. **Vertex is never exercised** (ADR-0006's pin). The OpenRouter adapter is what runs.
3. **k-NN arm quality is unmeasured in isolation.** The evaluation measures the hybrid result; there
   is no per-arm ablation, so "the vector arm contributes X" is not a number this ADR can give.
4. **28 documents is not a corpus.** Every number here is honest and tiny; there is no test of
   behaviour at 10⁵ documents, and `ef_search`/HNSW parameters are untuned defaults.
5. **Latency and concurrency are unmeasured.** `_post()` is a fresh `httpx.Client` per call, which is
   correct but not economical at volume.
6. **The integration tests run with the offline embedder**, so they prove the index, the arms and the
   filters against a real cluster — not the live embedding path. That path is covered by the
   evaluation run above and by the unit tests.
7. **Seed-vs-index agreement is asserted on the fixture corpus only.**

## Consequences

- A degraded answer is always available and always labelled: a user never receives an unmarked guess,
  and an operator can count how often each rung answered.
- The seed corpus is a second code path over the same data, so it must be rebuilt (and its test
  re-run) whenever the chunker or the fixtures change — enforced by the byte-identical rebuild test,
  not by discipline.
- The filters live in one function, so adding a filter field is a one-line change plus the arm-drift
  test that already exists.
- The rerank rung is demonstrated by a real provider failure, which means the demo shows the ladder
  working without anyone having to break anything on purpose.
- **Cost of the deviation:** two embedding transports exist until Vertex credentials land, and the
  field-name difference (`dimensions` vs `output_dimensionality`) has to be remembered if the
  transport is switched back.
