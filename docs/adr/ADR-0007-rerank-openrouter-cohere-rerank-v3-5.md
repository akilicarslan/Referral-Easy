# ADR-0007 — Rerank provider: OpenRouter `/api/v1/rerank` with `cohere/rerank-v3.5`

- **Status:** Accepted; the **model id is UNRESOLVED** (not independently verifiable without a key).
- **Date:** 2026-10-14
- **Phase:** 0 (spike S-07)
- **Decides:** `RERANK_PROVIDER`, `RERANK_MODEL_ID`, and the exact wire shape the rerank client implements.

## Question

What is the request and response shape of the OpenRouter rerank endpoint for
`cohere/rerank-v3.5`, and is the model id real?

## What was checked

| # | Command / URL | Result |
|---|---|---|
| 1 | `curl -s https://openrouter.ai/openapi.json` | HTTP 200, 2,678,048 bytes — a machine-readable spec |
| 2 | parsed `paths."/rerank".post` from that spec | `operationId: createRerank`, `summary: Submit a rerank request` |
| 3 | `curl -s https://openrouter.ai/api/v1/models` then filtered in Python | 462 models; **zero** contain `rerank` |
| 4 | `POST https://openrouter.ai/api/v1/rerank` with no `Authorization` | HTTP 401 `{"error":{"message":"No cookie auth credentials found","code":401}}` |
| 5 | `grep` of the spec for rerank model ids | the only rerank id in the spec is `cohere/rerank-v3.5` (4 occurrences, all in `example` blocks) |

**Important distinction.** Checks 1–2 establish that the *endpoint exists* and what its
*shape* is. Check 3 shows rerank models are **not** enumerable from any public
machine-readable source (the `/models` catalogue does not include them, and there is no
`/rerank/models` path). Check 4 shows the route is auth-gated, which is **not** the same
as the model id being valid. Therefore `cohere/rerank-v3.5` is the id the vendor's own
spec examples use, and nothing more — that is the honest state of the pin.

## Decision

- **`RERANK_PROVIDER=openrouter`**, **`RERANK_MODEL_ID=cohere/rerank-v3.5`** — carried over
  from `.env.example`, consistent with the only model id OpenRouter's own spec references.
  Marked **UNRESOLVED** until spike S-07 returns a 200 for that id with a real key.
- No vendor SDK. The endpoint is one JSON POST; the client is `httpx` behind the project's
  rerank port.

### Request

```
POST https://openrouter.ai/api/v1/rerank
Authorization: Bearer $OPENROUTER_API_KEY
Content-Type: application/json
```

| Field | Required | Type | Notes (verbatim from the spec) |
|---|---|---|---|
| `model` | yes | string | the rerank model to use |
| `query` | yes | string | the search query to rerank documents against |
| `documents` | yes | array | each item is a plain string, **or** an object with `text` and/or `image` (at least one required) |
| `top_n` | no | integer | "Number of most relevant documents to return"; `minimum: 1` |
| `provider` | no | object | provider routing preferences |
| `session_id` | no | string | groups related requests for observability |
| `trace` | no | object | request tracing passthrough |
| `user` | no | string | end-user id |

### Response (200)

| Field | Required | Notes |
|---|---|---|
| `results` | yes | array, "sorted by relevance" |
| `model` | yes | model used |
| `id` | no | "ORID format" |
| `provider` | no | e.g. `"Cohere"` |
| `usage` | no | `{ "search_units": int, "total_tokens": int }` |

Each `results[]` item has exactly three required fields:

| Field | Type | Meaning |
|---|---|---|
| `index` | integer | **index of the document in the original input list** — this is the join key back to the caller's documents |
| `relevance_score` | number | relevance of the document to the query |
| `document` | object | the original input echoed back, e.g. `{"text": "..."}` |

### Documented error codes

`400` malformed input · `401` auth · `402` insufficient credits · `403` forbidden ·
`404` unknown resource · `413` payload too large · `429` rate limited · `500` ·
`502` upstream failure · `503` · `524` edge timeout · `529` provider overloaded.

`402` and `429` are the two a portfolio demo will actually hit; both must surface as a
degraded (not silent) path, so the caller falls back to BM25 order with an explicit flag.

## What remains unverified

1. **The model id is unproven** (see above). Only a live 200 proves it.
2. **`relevance_score` scale and calibration are undocumented.** Nothing in the spec states
   a range, whether it is a normalised similarity or a raw logit, or whether it is
   model-specific. **Any rerank cut-off threshold must be measured, not assumed** — the
   same discipline ADR-0005 applies to the guardrail threshold.
3. **The `document` echo shape.** The spec says the echo preserves "text and/or image",
   but every example shows only `{"text": ...}`. Whether a plain-string input is echoed as
   a string or wrapped in an object is unverified — the client must not assume and must key
   off `index`.
4. **`top_n > len(documents)`** behaviour is unspecified.
5. **Whether `cohere/rerank-v3.5` is served by multiple upstreams** (the response has a
   `provider` field, implying it can vary) and whether they agree on `relevance_score`
   semantics.

## Consequences

- The rerank port is defined by this ADR; adding a second provider later means
  implementing the same port, not changing call sites.
- Spike `tests/contracts/spikes/test_rerank_openrouter_spike.py` asserts the shape above
  (required request fields accepted, `results[].index` round-trips into the input list,
  `relevance_score` is a float, results are sorted descending) and fails loudly naming
  `OPENROUTER_API_KEY` when the key is absent.
- Threshold defaults stay unset (`CHANGEME_MEASURED_THRESHOLD` is a *guardrail* pin; the
  rerank cut-off arrives with the measured value from the same run) until S-07 produces
  numbers.
