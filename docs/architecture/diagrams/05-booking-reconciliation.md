# 5 · Booking, idempotency and reconciliation

Spec: §2.5, §7, §9, §17 · ADR-0021, ADR-0035 · Code: `services/gateway/.../commands.py` (`book`),
`services/gateway/.../approval.py`, `services/mock-api/src/` (`modules/appointments.ts`, `adapters/`,
`faults/`), `services/workers/src/referraleasy_workers/` (`booking.py`, `reconcile.py`, `outbox.py`)

[← back to the index](README.md)

## The booking write: five gates before the row

```mermaid
flowchart TD
    Req["POST /v1/workflows/{id}/bookings<br/>command_id · approval_id<br/>expected_state_version · booking_payload"] --> KS{"kill switch<br/>engaged?"}
    KS -- yes --> K503["refused — read-only guidance still served"]
    KS -- no --> Rep{"command_id<br/>already completed?"}
    Rep -- yes --> Replay["stored response — no second booking"]
    Rep -- no --> V{"1 · state_version<br/>matches?"}
    V -- no --> RE002["409 RE002"]
    V -- yes --> P{"2 · policy:<br/>scheduling:create_booking<br/>in current state?"}
    P -- no --> PD["403 — allowed only in BOOKING_IN_FLIGHT"]
    P -- yes --> G{"3 · guardrails<br/>complete + allow?"}
    G -- "layer missing" --> G503["503 guardrails_not_configured"]
    G -- "block" --> G422["422 — booking not attempted"]
    G -- allow --> H{"payload hash ==<br/>the one consented to?"}
    H -- no --> CV["contract violation"]
    H -- yes --> A{"4 · approval binding<br/>patient + workflow + state_version−1<br/>+ payload hash + unexpired"}
    A -- no --> AD["refused"]
    A -- yes --> W["write booking row<br/>UNIQUE(approval_id) — 5th gate, the database's"]
    W --> T["transition the workflow in the same transaction"]
```

## Booking lifecycle (§2.5)

HTTP 200 never means `CONFIRMED`. Only the booking's own `state` is authoritative.

```mermaid
stateDiagram-v2
    [*] --> REQUESTED
    REQUESTED --> CONFIRMED: instant-confirm hospital
    REQUESTED --> PENDING_REVIEW: review-required hospital
    PENDING_REVIEW --> CONFIRMED: hospital accepts
    PENDING_REVIEW --> REJECTED: hospital declines
    REQUESTED --> REJECTED: x-vendor-decision: decline
    CONFIRMED --> CANCEL_PENDING: cancel requested
    CANCEL_PENDING --> CANCELLED
    REJECTED --> [*]
    CANCELLED --> [*]
    note right of PENDING_REVIEW
        REQUESTED and PENDING_REVIEW are never
        presented to the patient as confirmed.
    end note
```

## The mock hospital API (§17)

```mermaid
flowchart LR
    Client["agent tool / worker"] -- "idempotency-key header (≥ 8 chars)" --> Fault

    subgraph Mock["services/mock-api — Fastify + Zod"]
        Fault["fault middleware<br/>seed + scenario_id + request sequence"]
        Mods["modules<br/>locations · hospitals · providers · coverage<br/>referral-requirements · documents · evidence<br/>availability · directory-search · appointments · notifications"]
        Svc["booking service<br/>same key + same payload → same booking<br/>same key + different payload → reject<br/>slot uniqueness"]
        Reg["adapter registry"]
        IC["instant-confirm adapter"]
        RRq["review-required adapter"]
        Norm["NormalisedBooking<br/>state · external_reference · verified<br/>verification_method · vendor_status"]
        Out[("hospital DB:<br/>booking + notification outbox<br/>one transaction")]
        Fault --> Mods --> Svc --> Reg
        Reg --> IC --> Norm
        Reg --> RRq --> Norm
        Svc --> Out
    end

    subgraph Faults["injectable faults"]
        f["latency · rate_limit_429 · service_unavailable_503<br/>malformed_schema · empty_valid_result · stale_slot_409<br/>timeout_before_commit · timeout_after_commit<br/>duplicate_event · stale_coverage_evidence · notification_failure"]
    end
    Faults -.-> Fault
```

## Unknown outcome → reconciliation, under the same key

The "timeout after commit" scenario: the booking exists, but the response was lost.

```mermaid
sequenceDiagram
    autonumber
    participant S as Scheduling / worker
    participant API as mock-api
    participant WF as workflow (PG)
    participant R as BookingReconciler
    participant Q as Human queue

    S->>API: POST /appointments (key K)
    API->>API: commit booking + outbox row
    API--xS: timeout_after_commit — response lost
    S->>WF: BOOKING_IN_FLIGHT → BOOKING_UNVERIFIED
    Note over S: never mint a second key —<br/>ensure_same_key() refuses one
    R->>API: lookup_request(K)
    alt lookup fails / times out
        R->>R: backoff (honours Retry-After), retry with key K
    end
    alt found, CONFIRMED / PENDING_REVIEW
        R->>WF: resolved_booked → BOOKED (state as reported)
    else definitively absent
        R->>WF: resolved_not_booked → BOOKING_IN_FLIGHT (may re-send under K)
    else deadline reached first
        R->>WF: pending — stay BOOKING_UNVERIFIED, re-run later
    else budget exhausted / different key returned
        R->>Q: escalated — handoff reason prolonged_unknown_booking
    end
```

`reconcile.py` is the only retry owner for lookups. It is deliberately not wrapped in
`run_with_retries`, because nested loops would turn 3 × 3 attempts into 9.

## Worker pipeline: two replicas, one command

```mermaid
flowchart TD
    Claim["claim workflow<br/>owner · lease · generation"] --> Resume["read + verify checkpoint digest<br/>start at the step after the last proven one"]
    Resume --> Step{"external step's command_id<br/>= uuid5(workflow, key, step)<br/>already recorded?"}
    Step -- yes --> Ledger["replay the ledger outcome,<br/>then lookup — never re-send"]
    Step -- no --> Act["act once"]
    Act --> CP["write checkpoint<br/>with the owner/generation/version read"]
    Ledger --> CP
    CP -- "lease expired → matches no row" --> Late["typed refusal, nothing written"]
    CP --> Adv["advance state"]
    Adv -- "3rd transient failure" --> Rec["RecoveryPlanner → RECOVERY (one-way)"]
```

## Notifications never roll a booking back

```mermaid
flowchart LR
    BK["booking transaction<br/>booking row + outbox row"] --> OB[("outbox table")]
    OB -- "outbox_claim()<br/>FOR UPDATE SKIP LOCKED" --> OW["OutboxWorker<br/>one attempt per claim, HTTP retries=0"]
    OW -- delivered --> Done["settle: delivered"]
    OW -- failed --> Retry["settle: pending + next_attempt_at<br/>attempt_count++"]
    Retry -- "budget spent" --> Dead["dead — never claimable again"]
    OW -. "never touches" .-> BK
```
