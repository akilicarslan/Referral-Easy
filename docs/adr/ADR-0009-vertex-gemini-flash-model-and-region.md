# ADR-0009 — Vertex AI Gemini Flash: model id and region

- **Status:** **UNRESOLVED — blocked on GCP access.** No model id is pinned and none is
  guessed.
- **Date:** 2026-10-14
- **Phase:** 0 (spike S-09)
- **Decides:** `VERTEX_PROJECT_ID`, `VERTEX_LOCATION`, `VERTEX_AGENT_MODEL_ID`.

## Question

Which Gemini Flash model id and which region does the Vertex AI path use?

## What was checked

| # | Command / URL | Result |
|---|---|---|
| 1 | environment inspection | `VERTEX_PROJECT_ID` is empty; no GCP credentials exist in this environment |
| 2 | `gcloud` / Vertex model enumeration | **not attempted** — there is no project to enumerate against. Inventing a probe would produce a guess, not a finding. |
| 3 | `curl -s https://openrouter.ai/api/v1/models` | Google Flash-family ids exist in the **OpenRouter** catalogue (`google/gemini-3.8-flash`, `google/gemini-3.7-flash`, `google/gemini-3.5-flash`, `google/gemini-3.1-flash-lite`, …; 1,048,576-token context) |

**Why check 3 does not resolve this ADR.** OpenRouter's catalogue is a *router's* catalogue.
It tells you what OpenRouter will proxy, **not** what a given Vertex AI project in a given
region will serve. Vertex model ids, regional availability and version lifecycles are
project- and location-specific — a router listing cannot substitute for `gcloud` output.
Treating it as a pin would be a confidently wrong version, which is worse than an honest
`UNRESOLVED`.

## Decision

- **`VERTEX_AGENT_MODEL_ID=CHANGEME_FLASH_MODEL`** — unchanged.
- **`VERTEX_LOCATION=CHANGEME_SUPPORTED_REGION`** — unchanged.
- **`VERTEX_PROJECT_ID=`** — empty.

## What is needed to resolve it

1. A GCP project with the **Vertex AI API enabled**.
2. An identity with `roles/aiplatform.user` — either
   `gcloud auth application-default login`, or a service-account key referenced by
   `GOOGLE_APPLICATION_CREDENTIALS`.
3. Then, mechanically:
   - enumerate what the project actually serves in each candidate region (the
     publisher-models listing for `publishers/google/models`, per region);
   - confirm the chosen Flash id supports **function calling** and **streaming**, since ADK
     depends on both (ADR-0002);
   - **choose one region that serves both** the Flash model and `gemini-embedding-001`
     (ADR-0006) — a region that serves one but not the other produces a failure that only
     appears at run time, in the embedding path, far from the model that was pinned;
   - record the id, the region, the date, and the fact that Vertex model ids are
     **versioned and retired** — so this pin carries an expiry, unlike the library pins.
4. Environment needed by the spike: `VERTEX_PROJECT_ID`, `VERTEX_LOCATION`,
   `VERTEX_AGENT_MODEL_ID`, plus `GOOGLE_APPLICATION_CREDENTIALS` **or** application-default
   credentials.

## What remains unverified

Everything about Vertex: model availability, regional availability, quota, latency, whether
the Flash tier is callable at all from a fresh project, and whether the demo's
`AGENT_MAX_ATTEMPTS` / `AGENT_ATTEMPT_TIMEOUT_MS` budget (3 s per attempt) is realistic for
this model in this region. **None of these may be assumed.**

## Consequences

- ADR-0006 (embeddings) and ADR-0009 (agent model) share this blocker: one set of
  credentials unblocks both spikes, which is why they should be run together.
- Nothing in the demo depends on the pin: with `MODEL_BACKEND=offline` /
  `EMBEDDING_BACKEND=offline` the offline profile serves canned responses and the missing
  Vertex configuration is never read.
- `scripts/validate_env.py` is the enforcement point: with `EMBEDDING_BACKEND=live` and
  `VERTEX_LOCATION` still `CHANGEME_SUPPORTED_REGION`, it exits non-zero and names the
  variable.
