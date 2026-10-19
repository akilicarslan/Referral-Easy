# ADR-0006 — Embeddings: `gemini-embedding-001` at 768 dimensions, with asymmetric task types

- **Status:** Accepted (model id and dimensions pinned). Live behaviour is **UNRESOLVED —
  blocked on Vertex credentials** (see ADR-0009); the pin itself is CONFIRMED against vendor docs.
- **Date:** 2026-10-14
- **Phase:** 0 (spike S-06)
- **Decides:** `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL_ID`, `EMBEDDING_DIMENSIONS`, and the
  document-vs-query embedding rule.

## Question

Is `gemini-embedding-001` at **768** dimensions real and supported, and what is the
document-versus-query task-type distinction?

## What was checked

| # | Command / URL | Result |
|---|---|---|
| 1 | `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/embeddings/get-text-embeddings` (read to the end of the page) | `gemini-embedding-001` default output is **3072** dimensions ("up to 3072"); max sequence length **2048 tokens**. Smaller output is a documented storage/efficiency option. `text-embedding-005` and `text-multilingual-embedding-002` are the "up to 768" models — **not** this one. |
| 2 | `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/embeddings/task-types` | the exact `task_type` values, and the document/query rule quoted below |
| 3 | `curl -s .../googleapis/python-genai/main/google/genai/types.py` (887,544 bytes) | the request field itself: `output_dimensionality: Optional[int]` — *"Reduced dimension for the output embedding. If set, excessive values in the output embedding are truncated from the end. Supported by newer models since 2024 only. You cannot set this value if using the earlier model (`models/embedding-001`)."* |
| 4 | same file | `title` field — *"Title for the text. Only applicable when TaskType is `RETRIEVAL_DOCUMENT`."* |
| 5 | `curl -s https://pypi.org/pypi/google-genai/json` | `2.30.1`, `requires_python >=3.10` |

**Asymmetry rule (quoted):** "To get the best performance, you must use different task
types to generate embeddings for your corpus and your queries" — embed the corpus with
`RETRIEVAL_DOCUMENT` and user queries with `RETRIEVAL_QUERY`.

`task_type` values available: `RETRIEVAL_QUERY`, `RETRIEVAL_DOCUMENT`,
`CODE_RETRIEVAL_QUERY`, `QUESTION_ANSWERING`, `FACT_VERIFICATION`, `CLASSIFICATION`,
`CLUSTERING`, `SEMANTIC_SIMILARITY`.

## Decision

- **`EMBEDDING_PROVIDER=vertex`**, **`EMBEDDING_MODEL_ID=gemini-embedding-001`** (unchanged
  from `.env.example`), **`EMBEDDING_DIMENSIONS=768`**, passed as
  `output_dimensionality=768`.
- **`google-genai==2.30.1`** — CONFIRMED (PyPI latest).
- **Two call paths, one entry point.** The embedding port exposes
  `embed_documents(texts)` and `embed_queries(texts)` — never a bare `embed()`. The task
  type is bound to the *method*, not to a call-site argument, because the failure mode
  (forgetting to switch task types on the query path) silently degrades retrieval quality
  with no error at all. A bare `embed()` would make that mistake one keyword away.
- **One model, one dimension, both sides.** Documents and queries must be embedded by the
  same model at the same dimension, or cosine similarity is meaningless. 768 is chosen for
  index size; it is propagated to OpenSearch as `knn_vector.dimension: 768` (ADR-0008) so
  a mismatch fails at index time.
- **Chunking constraint that follows from check 1: 2048 tokens maximum sequence length.**
  The directory chunker must respect it. Combined with the `autoTruncate` flag documented
  on the same page (set it `false` so over-limit input **fails** instead of being silently
  cut), an over-long chunk is a loud error rather than a quietly truncated embedding.

## What remains unverified

1. **768 specifically is asserted, not demonstrated.** The docs show the default (3072) and
   say "up to 3072"; the SDK docstring says excessive values are truncated from the end,
   which implies any smaller value is legal. **No 768 example for `gemini-embedding-001`
   was found.** The spike asserts `len(vector) == 768` and fails loudly otherwise.
2. **Whether truncated MRL output must be re-normalised** before cosine similarity against
   another truncated vector. Not verified for this model; it is a well-known pitfall of
   truncated embeddings and is *not* assumed away here. The spike compares relative
   ordering with and without re-normalisation and records the result.
3. **Live call behaviour in general is blocked on credentials.** `EMBEDDING_PROVIDER=vertex`
   needs `VERTEX_PROJECT_ID`, which does not exist in this environment. The embedding spike
   therefore cannot run at all yet — it is listed as blocked in ADR-0009, not as passing.
4. **Batch size and quota limits**, and whether a batch request has a per-item length cap.
5. **Whether `title` materially improves directory retrieval** — it is a documented
   quality lever for `RETRIEVAL_DOCUMENT`, but this corpus has no titles worth passing.

## Consequences

- The embedding pin and the OpenSearch vector dimension are now linked by a documented,
  testable invariant rather than by two independent literals.
- Retrieval quality regressions caused by querying with document embeddings are
  structurally prevented (see the two-method port above).
