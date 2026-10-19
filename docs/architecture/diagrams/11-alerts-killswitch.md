# 11 · Alerts, the mock email outbox and the kill switch

Spec: §16 · Code: `packages/alerts/src/referraleasy_alerts/`, `services/gateway/.../killswitch.py`,
`services/gateway/.../admin.py`, `/v1/admin/{alerts,control}`

[← back to the index](README.md)

## Alerting, as designed and as wired

```mermaid
flowchart LR
    OS[("otel-events-*")] -. "query on a schedule" .-> Sch["scheduler<br/>compose profile pending — exits 64"]:::gap
    Sch -.-> Rules
    subgraph Pkg["packages/alerts"]
        Rules["rules.py — 4 pure rules"]:::unwired
        Dedup["dedup.py — fire once,<br/>cooldown, resolution is its own event"]:::unwired
        Outbox["outbox.py — mock email<br/>composes + records, never sends"]:::unwired
        Rules --> Dedup --> Outbox
    end
    Rules -- "rule list + thresholds" --> Admin["gateway /v1/admin/alerts"]:::built
    Admin --> UI["admin Alerts page<br/>'No rule has been evaluated'"]:::built

    classDef built fill:#e8f4ea,stroke:#2e7d32,color:#1b1b1b
    classDef unwired fill:#fff8e1,stroke:#f9a825,color:#1b1b1b,stroke-dasharray:4 3
    classDef gap fill:#f5f5f5,stroke:#9e9e9e,color:#616161,stroke-dasharray:2 4
```

The rules, the dedup logic and the outbox are implemented and tested. The admin page shows the rule
list and thresholds, and states that nothing has been evaluated, because the scheduler that would
query OpenSearch does not exist yet.

## The four rules

```mermaid
flowchart TB
    R1["TerminalFailureRate<br/>terminal > 20% with ≥ 20 invocations in 300 s<br/>(terminal = error · blocked · fallback · partial; empty is NOT)"]
    R2["SustainedLatency<br/>p95 attempt > 2.5 s for three windows"]
    R3["CircuitBreakerOpen"]
    R4["QualityDrop<br/>with enough samples"]
```

## Rule states: `unmeasured` is not `ok`

```mermaid
stateDiagram-v2
    [*] --> unmeasured: sample below the rule's minimum
    [*] --> ok: enough data, condition false
    [*] --> firing: enough data, condition true
    ok --> firing
    firing --> resolved_event: condition clears → its own event
    resolved_event --> ok
    firing --> firing: within cooldown → no new mail
    unmeasured --> ok
    unmeasured --> firing
```

## The admin kill switch (wired)

```mermaid
sequenceDiagram
    autonumber
    participant Op as Admin
    participant GW as gateway
    participant KS as KillSwitch (in-process)
    participant P as Patient

    Op->>GW: POST /v1/admin/control/booking-writes {engage, reason}
    GW->>KS: engage(reason, by, now)
    P->>GW: POST /bookings
    GW->>KS: engaged?
    KS-->>GW: yes
    GW-->>P: refused — booking writes are off
    P->>GW: GET /help, directory guidance
    GW-->>P: still served (read-only)
    Op->>GW: release
    Note over GW: An in-flight booking with an unknown outcome<br/>is still reconciled — the switch never erases one.
```

This switch stops all booking writes. The release registry has a different one, which stops a
*pinned release* (see [09-release-registry.md](09-release-registry.md)), and that one is not wired.
