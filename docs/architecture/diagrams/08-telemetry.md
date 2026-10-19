# 8 · Telemetry: OTel → Data Prepper → OpenSearch

Spec: §15 · ADR-0008, ADR-0032 · Code: `packages/telemetry/`, `infra/otel/`,
`infra/data-prepper/pipelines.yaml`, `infra/opensearch/{index-templates,ism-policies}/`,
`services/gateway/.../admin.py`

[← back to the index](README.md)

## Pipeline

OpenSearch 3.9.0 does not ingest native OTLP metrics (ADR-0008), so every measurement is sent as a
numeric **log event**. There is no Prometheus.

```mermaid
flowchart LR
    subgraph Emitters["configure_telemetry() — one call per service"]
        GW["gateway<br/>server spans · turn latency<br/>guardrail_decision · api_failure"]
        AG["agents × 7<br/>agent_trigger · latency.invocation.ms<br/>dimension agent.role"]
        WK["workers<br/>outbox · reconcile · hitl"]
        MK["mock-api"]
    end

    subgraph Proc["in-process"]
        Mask1["masking.py<br/>PII + secrets before export"]
        Iso["isolation.py<br/>bounded, never blocks a booking"]
    end

    subgraph Col["OTel Collector :4318"]
        ML["memory_limiter<br/>(refusals visible on :8888)"]
        TM["transform/mask_pii<br/>redaction/scrub"]
        BT["batch"]
        ML --> TM --> BT
    end

    subgraph DP["Data Prepper"]
        TP["trace-pipeline"]
        EP["event-pipeline"]
    end

    subgraph OS["OpenSearch"]
        TI[("otel-traces-YYYY.MM.DD<br/>7-day ISM")]
        EI[("otel-events-YYYY.MM.DD<br/>7-day ISM")]
        EV[("evaluation index<br/>30-day ISM")]
        DV[("directory-vector<br/>— never written by telemetry")]
    end

    Emitters --> Proc -- "OTLP/HTTP<br/>traces + logs" --> Col
    BT -- traces --> TP --> TI
    BT -- logs --> EP --> EI
    TI & EI & EV --> Q["query.py<br/>typed query layer"] --> Admin["gateway /v1/admin/*"] --> UI["admin dashboard"]
    OSD["OpenSearch Dashboards<br/>(profile: dashboards)"] --> OS
```

## Identity: propagate, don't correlate

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant G as gateway (server span)
    participant D as dispatch worker thread
    participant C as coordinator (server span)
    participant S as specialist (server span)

    B->>G: POST /messages
    G->>G: start SERVER span (trace T)
    G->>D: enqueue job + traceparent captured on the request thread
    D->>C: POST /tasks — traceparent: T / span G
    C->>C: child span of G — same trace T
    C->>S: delegate_task — traceparent: T / span C
    S->>S: child span of C
    Note over G,S: A2A task_id / context_id / message_id are span attributes,<br/>never equated with trace_id. Async worker/HITL waits start a new trace + span link.
```

## One event = one measurement

```mermaid
classDiagram
    class TelemetryEvent {
        event.id   derived hash of the logical identity (dedup key)
        event.name
        metric.name
        metric.value
        metric.unit   count | ms | usd | score | tokens
        outcome.class   success_full | success_empty | success_partial | error | blocked | fallback
        dimensions   agent.role · agent.version · release · model · traffic
    }
    class EventName {
        <<enumeration>>
        agent_trigger
        attempt
        retry
        outcome
        api_failure
        latency
        model_usage
        hitl_wait
        hitl_resolve
        guardrail_decision
        eval_score
        feedback
        state_transition
    }
    TelemetryEvent --> EventName
```

Counts use `cardinality(event.id)`, not `doc_count`, so a re-export or a double emission counts
once. Distributions use `percentiles(metric.value)`. The three latencies (`latency.attempt.ms`,
`latency.invocation.ms`, `latency.turn.ms`) are separate series, and timeouts stay in them.

## What the admin dashboard reads

```mermaid
flowchart LR
    Q["query.py"] --> O["Overview<br/>counts · outcomes · latencies · index health"]
    Q --> A["Agents & Versions<br/>agent.role / version in use"]
    Q --> T["Trace Explorer<br/>recent traces · one trace's spans"]
    Q --> E["Evaluations<br/>scores per run + metric, with sample counts"]
    Q --> G["Guardrails<br/>per-layer decisions"]
    Q --> F["Feedback<br/>raw count · like rate · participation"]
    Q --> AL["Alerts<br/>rules · thresholds · kill switch"]
    PG[("PostgreSQL")] --> HQ["Human Queue"]
    FX["fixtures/scenarios.json"] --> SC["Fault Scenarios"]
```

Telemetry and evaluation failures never roll back a business transaction. The critical audit stays
in PostgreSQL.
