# 12 · Web client, A2UI and the admin dashboard

Spec: §12 · ADR-0004, ADR-0027 · Code: `apps/web/src/` (`a2ui/`, `stream/`, `components/`,
`admin/`), `packages/contracts/schemas/sse.json`, `services/gateway/.../surfaces.py`

[← back to the index](README.md)

## Patient client

```mermaid
flowchart TB
    subgraph Web["apps/web — Lit + TypeScript, one bundle"]
        App["app/referral-app.ts"]
        SSE["stream/sse-client.ts<br/>EventSource.onmessage<br/>replay on seq gap (Last-Event-ID)"]
        Store["stream/surface-store.ts<br/>dedup by event_id<br/>terminal carries the outcome,<br/>stream end is not one"]
        Cmd["stream/command.ts<br/>CommandRegistry — one command_id per tap,<br/>expected_state_version attached"]
        Adapter["a2ui/adapter.ts — THE A2UI boundary<br/>only file importing @a2ui/* (v0.9, pkg 0.13.0)<br/>rebuilds the whole tree keyed by instance_id"]
        subgraph Cards["8-component catalog"]
            c1["HospitalPicker"]
            c2["DoctorPicker"]
            c3["SlotPicker"]
            c4["PatientDetailsForm"]
            c5["BookingReview"]
            c6["AppointmentReceipt"]
            c7["DirectoryFallbackCard"]
            c8["HumanHandoffCard"]
        end
    end

    GW["gateway"] -- "GET /v1/workflows/{id}/events" --> SSE --> Store --> Adapter --> Cards
    Cards -- "server-issued action on the instance" --> Cmd -- "POST /actions · /messages · /bookings" --> GW
```

## Card lifecycle — why a re-render can't re-book

```mermaid
sequenceDiagram
    autonumber
    participant GW as gateway surfaces.py
    participant S as surface-store
    participant A as A2UI adapter
    participant U as Patient

    GW->>S: surface_update {instance_id, component, props,<br/>workflow_version, action}
    S->>S: seen event_id? → no-op
    S->>A: apply (reconcile by instance_id)
    A->>U: render card
    U->>A: tap
    A->>S: AdapterAction
    S->>GW: POST /actions {command_id, instance_id, action,<br/>expected_state_version}
    GW->>GW: instance.action == submitted action?<br/>(a patient can only send an action the server issued)
    GW->>GW: command ledger — replay returns the stored response
    Note over U,GW: Re-rendering or replaying the stream re-applies identical events.<br/>A repeated tap re-sends the same command_id. Neither books twice.
```

## SSE event types

```mermaid
flowchart LR
    subgraph Authoritative["authoritative — folded into state"]
        e1["workflow_state"]
        e2["surface_update"]
        e3["terminal"]
    end
    subgraph Informational["never committed as state"]
        e4["agent_progress"]
        e5["partial_text"]
        e6["notice"]
    end
```

Progress latency and final-answer latency are measured on two separate clocks (`timing.py`) and are
never reported as one number.

## Admin dashboard (`/admin`)

```mermaid
flowchart LR
    Router["admin/router.ts"] --> P1["Overview"]
    Router --> P2["Agents & Versions<br/>+ registry"]
    Router --> P3["Trace Explorer"]
    Router --> P4["Evaluations"]
    Router --> P5["Guardrails"]
    Router --> P6["Feedback"]
    Router --> P7["Fault Scenarios<br/>§20's eight"]
    Router --> P8["Human Queue"]
    Router --> P9["Alerts + kill switch"]
    P1 & P2 & P3 & P4 & P5 & P6 & P7 & P8 & P9 --> API["admin/api.ts → gateway /v1/admin/*<br/>(admin session)"]
```

Every chart reads a real index or table. When a series has no data, the page says so instead of
drawing a placeholder: "no fabricated demo charts" is one of the repo's `[!]` invariants.
