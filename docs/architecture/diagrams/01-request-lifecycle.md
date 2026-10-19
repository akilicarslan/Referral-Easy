# 1 · Request lifecycle — command plane, SSE and coordinator dispatch

Spec: §5, §7, §9, §12 · Code: `services/gateway/src/referraleasy_gateway/`
(`api.py`, `commands.py`, `events.py`, `surfaces.py`, `dispatch.py`, `release.py`)

[← back to the index](README.md)

## The four write operations

`commands.py` is the only module that changes control state. Each operation has a different
authority behind it.

```mermaid
flowchart LR
    subgraph Routes["POST /v1/workflows/{id}/…"]
        A["/actions<br/>card tap (system:*)"]
        M["/messages<br/>free text"]
        T["/transitions<br/>admin only"]
        B["/bookings<br/>booking write"]
    end

    A --> PA["policy_rule row"] --> WA["workflow.payload<br/>(the answer)"]
    M --> GA["guardrail pipeline"] --> WM["workflow.payload<br/>(the message)"]
    T --> AA["admin session"] --> WS["workflow.state"]
    B --> BA["kill switch + policy +<br/>guardrails + approval"] --> WB["booking row +<br/>workflow.state"]
```

## A free-text message, end to end

```mermaid
sequenceDiagram
    autonumber
    participant W as Web client
    participant API as gateway api.py
    participant C as CommandService
    participant L as Command ledger (PG)
    participant G as Guardrail seam
    participant S as Control-state port (PG)
    participant E as EventLog / SSE
    participant D as CoordinationService
    participant A as agent-care_coordinator

    W->>API: POST /messages {command_id, expected_state_version, text}
    API->>API: verify patient session, drop untrusted user_id
    API->>C: submit_message()
    C->>S: load workflow, scoped to the patient (404 if not theirs)
    C->>L: begin_command(command_id, request hash)
    alt replay of a completed command
        L-->>C: stored response
        C-->>W: same body, byte for byte — no work done
    end
    C->>C: expected_state_version == row.state_version? (else RE002)
    C->>E: agent_progress "reading"
    C->>G: screen(text, workflow_state, short_reply_expected)
    G-->>C: GuardrailOutcome {decision, category, verdicts[]}
    C->>E: per-layer verdicts
    alt block (not emergency)
        C-->>W: 422 input_blocked — nothing recorded
    else block, category = emergency
        C->>S: route to HUMAN_HANDOFF
        C->>E: notice (approved fixed safety text) + terminal needs_input
        C-->>W: 200 routed_to HUMAN_HANDOFF
    else clarify
        C->>S: append message, committed=false
        C-->>W: 202 ambiguous_input
    else allow
        C->>S: append message and pending continuation (version check)
        C->>L: complete_command(response)
        C-->>W: 202 {committed: true, dispatched}
        D->>S: claim committed job with owner and lease
        D-)A: POST /tasks — envelope, source inputs and execution plan
        A--)D: outcome, validated specialist results and budget use
        D->>C: apply transition and card through CommandService
        C->>S: persist state and current surface
        D->>S: finish only this owner's command
    end
    E--)W: SSE frames (workflow_state · surface_update · agent_progress · partial_text · notice · terminal)
```

## The SSE stream and surfaces

```mermaid
flowchart TB
    subgraph Producers
        Turn["Turn<br/>progress · partial · notice<br/>workflow_state · surface · terminal"]
        Disp["CoordinationService<br/>(leased continuation worker)"]
    end

    subgraph Log["EventLog — process-local dict[workflow_id → events]"]
        Seq["seq: monotonic per workflow"]
        Id["event_id = sha256(workflow_id, seq, type, payload)[:32]"]
        Auth["AUTHORITATIVE: workflow_state · surface_update · terminal"]
        NonAuth["NOT state: partial_text · agent_progress · notice"]
    end

    Producers --> Log
    Log -- "GET /events<br/>Last-Event-ID replay" --> SSE["text/event-stream<br/>(no event: name — onmessage only)"]
    SSE --> Store["apps/web surface-store"]
    Log -- "committed_events() only" --> Fold["fold into surface state"]
```

Standard coordination jobs and current cards persist in PostgreSQL. Historic SSE events remain
process-local. A separate execution service needs shared event delivery before it can serve the
same live stream. See [ADR-0041](../../adr/ADR-0041-autonomous-coordination.md).

## Coordinator dispatch internals

```mermaid
flowchart LR
    Req["accepted message or card answer"] --> Q[("workflow.payload.coordination_job")]
    Q -- "command receipt committed; lease available" --> Wk["CoordinationService"]
    Rel["release.py<br/>RELEASE_MANIFEST_ID or<br/>registry/releases/latest.json"] --> Env
    Pol["allowed_actions_for(state)<br/>from the policy table"] --> Env
    Env["AgentTaskRequest<br/>workflow · command · state version · release<br/>policy actions · inherited deadline and quotas<br/>minimum specialist inputs · bounded execution plan"]
    Wk --> Env --> Post["httpx POST<br/>A2A_BASE_URL{role=care_coordinator}/tasks<br/>Bearer service token + traceparent"]
    Post -- "validated result" --> Note["transition and card through CommandService"]
    Post -- "error / timeout" --> Fail["reconcile original booking key<br/>or offer handoff summary for consent"]
```

`configured` is false unless there is a URL, a token **and** a release id; an unconfigured gateway
says "no coordinator is configured" to the patient rather than implying someone is acting.

The bounded in-memory dispatch queue remains for general model tasks. The standard workflow uses
the stored continuation path shown above. It requires a continuously running container.
