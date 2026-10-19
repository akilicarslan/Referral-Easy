# 6 · Human in the loop

Spec: §11 · ADR-0035 · Code: `services/workers/src/referraleasy_workers/hitl.py`,
`packages/domain/src/referraleasy_domain/handoff.py`, `migrations/011_handoff.sql`,
`services/gateway/.../human_queue.py` (read), `python -m referraleasy_workers handoff …` (act)

[← back to the index](README.md)

## Triggers and reason classes

```mermaid
flowchart LR
    subgraph Triggers
        t1["no hospital / specialist"]
        t2["sources conflict"]
        t3["readiness cannot be resolved"]
        t4["unknown booking drags on"]
        t5["patient asks for a human"]
        t6["recovery finds no reliable option"]
        t7["guardrail: emergency"]
        t8["RAG ladder: no source at all"]
    end
    subgraph Reason["HandoffReasonClass"]
        r1["no_match"]
        r2["conflicting_sources"]
        r3["missing_document"]
        r4["prolonged_unknown_booking"]
        r5["patient_requested"]
        r6["no_reliable_recovery"]
        r7["technical_outage"]
    end
    t1 --> r1
    t2 --> r2
    t3 --> r3
    t4 --> r4
    t5 --> r5
    t6 --> r6
    t8 --> r1
```

An emergency routes the workflow to `HUMAN_HANDOFF` straight from the gateway, with the approved
fixed safety text. It does not go through the worker's consent flow.

For standard coordination, the gateway stores `payload.handoff_draft` and shows the exact summary.
The card asks the patient to share it. `system:capture_consent` then queues that draft through
`ControlState.queue_handoff`. It creates no booking approval. The existing worker queue API and
operator lifecycle remain available.

## Ticket lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant WF as Workflow (automation owner)
    participant P as Patient
    participant HQ as hitl.HumanQueue
    participant DB as handoff table (PG)
    participant Op as Operator (CLI)
    participant GW as gateway /v1/admin/human-queue

    WF->>WF: write payload.handoff<br/>patient-visible summary + sources
    WF->>P: HumanHandoffCard — shows the summary to be shared
    P->>WF: consents (consent_given_at)
    alt no consent
        HQ--xDB: ConsentRequiredError — no row written
    end
    HQ->>DB: INSERT handoff (open) — an open row IS in the queue
    HQ->>WF: → HUMAN_HANDOFF
    P-->>P: ticket ref + contact details<br/>(never "an operator is with you")
    GW->>DB: read queue, oldest first (admin page)
    Op->>DB: handoff claim — compare-and-set
    DB->>WF: human_owner_id set — automation writes refused (RE003)
    Note over DB: a second operator's claim fails
    Op->>Op: draft alternatives / missing-document note
    Op->>DB: handoff resolve <id> --return-state X
    DB->>DB: handoff_resolve(): return_state allow-listed (RE004 otherwise)<br/>notes → resolution_note, audited
    DB->>WF: human owner released → back to a safe state
    Note over WF: final booking approval is still required —<br/>a resolution never grants consent
```

## Handoff state

```mermaid
stateDiagram-v2
    [*] --> open: consent given
    open --> claimed: operator claim (CAS)
    claimed --> open: release
    claimed --> resolved: resolve + allow-listed return state
    resolved --> [*]
```

## Ownership while a human holds the ticket

```mermaid
flowchart LR
    Claim["handoff_claim"] --> HO["workflow.human_owner_id = operator"]
    HO --> Guard{"workflow_guard:<br/>writer is human?"}
    Guard -- "agent / worker / gateway automation" --> Refuse["RE003 refused"]
    Guard -- "operator" --> Allow["write allowed"]
    Resolve["handoff_resolve"] --> Return["state ∈ {INTAKE, PROVIDER_MATCHING,<br/>READINESS_CHECK, AWAITING_SLOT_CHOICE, RECOVERY}"]
    Return --> Auto["automation owns again"]
```

> **Gap (README #6):** the admin Human Queue page *reads* the queue. Claiming, releasing and
> resolving are terminal commands (`python -m referraleasy_workers handoff …`), so there is only one
> implementation of §11's claim rules and the database's compare-and-set decides who wins.
