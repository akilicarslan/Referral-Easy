# Architecture diagrams

Mermaid diagrams of the system **as it is built**, drawn from the code rather than from the plan.
Where the plan ([`../../../ReferralEasy_Architecture_v3.md`](../../../ReferralEasy_Architecture_v3.md))
and the code disagree, the diagrams follow the code and mark the difference.

## Legend

Every diagram uses the same three styles:

```mermaid
flowchart LR
    A["Built and wired<br/>(runs in the stack)"]:::built
    B["Built, not wired<br/>(package + tests, no caller in services/)"]:::unwired
    C["Declared only<br/>(compose `pending` profile / plan)"]:::gap
    classDef built fill:#e8f4ea,stroke:#2e7d32,color:#1b1b1b
    classDef unwired fill:#fff8e1,stroke:#f9a825,color:#1b1b1b,stroke-dasharray:4 3
    classDef gap fill:#f5f5f5,stroke:#9e9e9e,color:#616161,stroke-dasharray:2 4
```

## Feature diagrams

| # | Feature | Spec | File |
| --- | --- | --- | --- |
| 1 | Request lifecycle: command plane, SSE, coordinator dispatch | §5, §7, §12 | [01-request-lifecycle.md](01-request-lifecycle.md) |
| 2 | Three-layer guardrails + output guardrail | §9 | [02-guardrails.md](02-guardrails.md) |
| 3 | Agents, A2A task contract, tool authority | §3, §5, §6, §8 | [03-agents-a2a.md](03-agents-a2a.md) |
| 4 | Workflow state machine and policy table | §4, §7 | [04-workflow-state-machine.md](04-workflow-state-machine.md) |
| 5 | Booking, idempotency and reconciliation | §2.5, §7, §17 | [05-booking-reconciliation.md](05-booking-reconciliation.md) |
| 6 | Human in the loop | §11 | [06-hitl.md](06-hitl.md) |
| 7 | Hybrid RAG and the degradation ladder | §10 | [07-hybrid-rag.md](07-hybrid-rag.md) |
| 8 | Telemetry: OTel → Data Prepper → OpenSearch | §15 | [08-telemetry.md](08-telemetry.md) |
| 9 | Release registry and the version gate | §13 | [09-release-registry.md](09-release-registry.md) |
| 10 | Evaluation | §14 | [10-evaluation.md](10-evaluation.md) |
| 11 | Alerts and the kill switch | §16 | [11-alerts-killswitch.md](11-alerts-killswitch.md) |
| 12 | Web client, A2UI and the admin dashboard | §12 | [12-web-a2ui.md](12-web-a2ui.md) |

---

## System overview

The whole platform on one page: who calls whom, and where state lives.

```mermaid
flowchart TB
    subgraph Clients["Clients — apps/web (one bundle)"]
        Patient["Patient client<br/>/  · A2UI renderer · 8 cards"]:::built
        AdminUI["Admin dashboard<br/>/admin · 9 pages"]:::built
    end

    subgraph GW["services/gateway — FastAPI"]
        Auth["Sessions<br/>patient ≠ admin"]:::built
        Cmd["Command plane<br/>actions · messages · transitions · bookings"]:::built
        Guard["Guardrail seam<br/>3 layers, §9"]:::built
        Policy["Policy check<br/>§4 table"]:::built
        Events["EventLog + SSE<br/>surfaces, replay by event id"]:::built
        Dispatch["CoordinationService<br/>durable jobs, leased worker"]:::built
        AdminAPI["Admin read API<br/>+ kill switch"]:::built
    end

    subgraph Agents["services/agents — one image, AGENT_ROLE × 7"]
        Coord["Care Coordinator<br/>delegate_task only"]:::built
        Spec["6 specialists<br/>Intake · Coverage · Matching<br/>Readiness · Scheduling · Recovery"]:::built
    end

    subgraph Workers["services/workers"]
        Outbox["outbox --watch<br/>(the running container)"]:::built
        Recon["reconcile · handoff<br/>(one-shot CLI)"]:::built
    end

    Mock["services/mock-api — Fastify/TS<br/>hospitals · providers · coverage · documents<br/>availability · appointments · notifications<br/>2 hospital adapters · seeded fault engine"]:::built

    subgraph Stores["State"]
        PG[("PostgreSQL 18<br/>workflow · command ledger · policy_rule<br/>approval · booking · outbox · handoff<br/>release_registry")]:::built
        OS[("OpenSearch 3 — single node<br/>otel-traces-* · otel-events-*<br/>evaluation · directory-vector")]:::built
    end

    subgraph Obs["Telemetry pipeline"]
        Col["OTel Collector"]:::built
        DP["Data Prepper"]:::built
    end

    LLM["Model providers<br/>OpenRouter (LiteLLM) · Vertex<br/>offline profile = canned"]:::built
    Pending["scheduler · local-guardrail<br/>compose profile `pending`"]:::gap

    Patient -- "REST commands" --> Cmd
    Patient -- "SSE stream" --> Events
    AdminUI --> AdminAPI
    Cmd --> Auth
    Cmd --> Guard
    Cmd --> Policy
    Cmd --> Events
    Cmd -- "committed message or answer" --> Dispatch
    Dispatch -- "POST /tasks<br/>§5 envelope + traceparent" --> Coord
    Coord -- "delegate_task<br/>POST /tasks" --> Spec
    Spec -- "tool calls" --> Mock
    Spec -- "policy_assert" --> PG
    Coord --> LLM
    Spec --> LLM
    Guard -- "layer 2" --> LLM
    Cmd --> PG
    Policy --> PG
    Outbox --> PG
    Outbox --> Mock
    Recon --> PG
    Recon --> Mock
    AdminAPI --> OS

    GW -. "OTLP/HTTP" .-> Col
    Agents -. "OTLP/HTTP" .-> Col
    Col --> DP --> OS

    classDef built fill:#e8f4ea,stroke:#2e7d32,color:#1b1b1b
    classDef unwired fill:#fff8e1,stroke:#f9a825,color:#1b1b1b,stroke-dasharray:4 3
    classDef gap fill:#f5f5f5,stroke:#9e9e9e,color:#616161,stroke-dasharray:2 4
```

### Deployment topology (compose profiles)

```mermaid
flowchart LR
    subgraph core["profile: core — make up"]
        direction TB
        postgres[("postgres :5432")]
        opensearch[("opensearch :9200")]
        otel["otel-collector :4318"]
        prepper["data-prepper"]
        mockapi["mock-api :4010"]
        gateway["gateway :8080"]
        worker["worker<br/>(no port · pgrep probe)"]
        subgraph agents["agents image × 7 (:8080 each)"]
            a1["agent-care_coordinator"]
            a2["agent-referral_intake"]
            a3["agent-coverage_network"]
            a4["agent-provider_matching"]
            a5["agent-referral_readiness"]
            a6["agent-scheduling"]
            a7["agent-recovery_handoff"]
        end
    end
    subgraph showcase["profile: showcase"]
        web["web :3000"]
    end
    subgraph dashboards["profile: dashboards"]
        osd["opensearch-dashboards"]
    end
    subgraph pending["profile: pending — never started"]
        scheduler["scheduler"]:::gap
        localguard["local-guardrail"]:::gap
    end

    web --> gateway
    gateway --> postgres & opensearch & mockapi & otel
    agents --> postgres & mockapi & otel
    worker --> postgres & mockapi
    otel --> prepper --> opensearch
    osd --> opensearch

    classDef gap fill:#f5f5f5,stroke:#9e9e9e,color:#616161,stroke-dasharray:2 4
```

**There is no message broker.** The work queue is a PostgreSQL table read with
`SELECT … FOR UPDATE SKIP LOCKED`, and the single OpenSearch node is both the vector store and the
telemetry store (ADR-0023, ADR-0008).

### Package dependency map

```mermaid
flowchart BT
    contracts["packages/contracts<br/>JSON Schema → Pydantic + Zod"]
    domain["packages/domain<br/>workflow · policy · commands · approval · handoff"]
    telemetry["packages/telemetry"]
    runtime["packages/agent-runtime<br/>budget · retries · tools gate · skills · A2A app"]
    skills["packages/skills<br/>8 skill packages"]
    guardrails["packages/guardrails"]
    rag["packages/rag"]:::unwired
    registry["packages/release-registry"]
    alerts["packages/alerts"]
    evalpkg["packages/eval"]

    gateway["services/gateway"]
    agents["services/agents"]
    workers["services/workers"]

    domain --> contracts
    runtime --> contracts & domain & telemetry
    guardrails --> contracts
    rag --> contracts
    registry --> contracts
    alerts --> telemetry
    evalpkg --> contracts & telemetry

    gateway --> domain & guardrails & telemetry & alerts & contracts
    agents --> runtime & skills & domain
    workers --> runtime & domain & telemetry

    classDef unwired fill:#fff8e1,stroke:#f9a825,color:#1b1b1b,stroke-dasharray:4 3
```

`packages/rag` has no importer in `services/` yet: the Matching agent's `search_directory` calls
the mock API's `/directory/search` and labels every answer `static_seed`. See
[07-hybrid-rag.md](07-hybrid-rag.md).
