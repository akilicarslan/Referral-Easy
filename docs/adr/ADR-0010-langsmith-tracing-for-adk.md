# ADR-0010 — LangSmith tracing for ADK

- **Status:** **UNRESOLVED — blocked on `LANGSMITH_API_KEY`.** The integration path is
  identified and version-pinned; end-to-end emission is unproven.
- **Date:** 2026-10-14
- **Phase:** 0 (spike S-10)
- **Decides:** how ADK traces reach LangSmith.

## Question

What is the tracing integration path from Google ADK to LangSmith — and what is needed to
prove it works?

## What was checked

| # | Command / URL | Result |
|---|---|---|
| 1 | `curl -s https://pypi.org/pypi/langsmith/json` → `gh api repos/langchain-ai/langsmith-sdk/releases/latest` | `langsmith==0.14.8` on both — CONFIRMED |
| 2 | `curl -s https://pypi.org/pypi/openinference-instrumentation-google-adk/json` | `1.0.4`, `requires_python <3.15,>=3.10` — a maintained ADK instrumentor exists |
| 3 | `curl -s https://pypi.org/pypi/google-adk/2.12.0/json` (`requires_dist`) | ADK already depends on `opentelemetry-api`/`-sdk` (1.39–1.42) and its `all`/`otel-gcp` extras pull OTLP exporters and `opentelemetry-instrumentation-google-genai` |
| 4 | LangChain's OTEL announcement + the per-framework LangSmith integration guides + OpenRouter's LangSmith broadcast docs | OTLP traces endpoint `https://api.smith.langchain.com/otel/v1/traces` (EU: `https://eu.api.smith.langchain.com/otel/v1/traces`); auth via headers `x-api-key: <LANGSMITH_API_KEY>` and `Langsmith-Project: <project>` |

## Decision

- **Path: ADK's native OpenTelemetry span tree → OTLP exporter → LangSmith's OTLP endpoint.**
  ADK already emits OTel spans (check 3), so the transport is the *existing* telemetry
  pipeline, not a second one. `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` carries the
  **full** URL `https://api.smith.langchain.com/otel/v1/traces` — the base-endpoint form
  (`.../otel`, with `/v1/traces` appended by the SDK) is a known source of
  misconfiguration.
- **Auth headers:** `x-api-key` (**not** `Authorization: Bearer`) and `Langsmith-Project`.
  `Langsmith-Project` is a value the environment does not define today — `.env.example` has
  no `LANGSMITH_PROJECT`. **Adding it is required**; it is listed here because
  `.env.example` is outside this ADR's edit scope. The spike requires it explicitly and
  names it when missing, rather than defaulting a project name and scattering traces
  somewhere unexpected.
- **GenAI semantic conventions come from the OpenInference ADK instrumentor
  (`openinference-instrumentation-google-adk==1.0.4`)**, which adds prompt/completion and
  token-usage attributes that a bare span tree lacks. ADK's spans remain the structure.
- **`langsmith==0.14.8`** is pinned for the readback client used by the spike.
- **Offline profile:** `LANGSMITH_ENABLED=true` with `MODEL_BACKEND=offline` is a
  contradiction — the offline profile is defined as **zero network calls**. The offline
  path therefore does not export, and `scripts/validate_env.py` **warns** (does not fail,
  because that combination is the shipped default in `.env.example`) that the flag is not
  honoured offline. Silently exporting would break the offline guarantee; silently
  ignoring the flag without saying so would hide it.

## What is needed to resolve it

1. `LANGSMITH_API_KEY` — from the LangSmith workspace settings.
2. `LANGSMITH_PROJECT` — added to `.env.example` and `.env` (owner: whoever owns that file).
3. Network egress to `api.smith.langchain.com`.
4. Then the spike confirms: a span arrives **and** the run is readable back.

## What remains unverified

1. **Whether the OpenInference ADK instrumentor and ADK's own spans coexist without
   duplicating or orphaning spans.** Both attach to the same process; this is the most
   likely way the integration produces a messy trace tree, and it is unmeasured.
2. **Whether the emitted spans satisfy LangSmith's expectations** (`langsmith.span.kind`,
   `gen_ai.*` attributes) such that they render as LLM/chain/tool runs rather than generic
   spans.
3. **Readback, not just a 200.** LangSmith's OTLP endpoint acknowledges with HTTP 200 and an
   **empty body**; the transport reports success to an exporter that cannot tell ingestion
   from acceptance. **The spike must verify by reading the run back**, which is why
   `langsmith==0.14.8` is pinned here. A green exporter log is not evidence.
4. **Self-hosted vs SaaS endpoint shape** — the full-path form differs; only the SaaS path
   was verified.
5. **Ingestion latency** — how long a trace takes to become readable, which determines the
   spike's poll timeout.
6. **Sampling interaction with `EVAL_SAMPLE_RATE=0.01`** — trace volume vs eval volume are
   separate knobs, and nothing has confirmed they are independent.

## Consequences

- The tracing path reuses infrastructure the project already runs (the OTel collector), so
  no new service is introduced to reach LangSmith.
- Offline runs provably stay offline: a flag that cannot be honoured is reported, not
  quietly ignored.
