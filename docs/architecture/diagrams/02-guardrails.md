# 2 · Guardrails — three input layers, an output check, and authorization

Spec: §9 · ADR-0005, ADR-0038 · Code: `packages/guardrails/` (the layers and the measurement),
`services/gateway/src/referraleasy_gateway/guardrails.py` (the seam), `packages/agent-runtime/.../tools.py`
(tool authority)

[← back to the index](README.md)

## Where guardrails sit

Input screening is **one** of several defences. The others (tool allowlist, minimum context,
sources treated as data, server-side policy) are what actually stop prompt injection.

```mermaid
flowchart LR
    In["Patient text"] --> IG["Input guardrail<br/>3 layers"]:::built
    IG --> Cmd["Command plane"]:::built
    Cmd --> Agent["Agent turn"]:::built
    Agent --> TG["Tool authority<br/>allowlist → policy_assert"]:::built
    TG --> Tools["Mock API tools"]:::built
    Agent --> DC["Decision checks<br/>no payment guarantee,<br/>no unconfirmed-as-confirmed"]:::built
    Cmd --> OG["Output guardrail<br/>before a card reaches the patient"]:::unwired
    OG --> Card["A2UI card"]:::built

    classDef built fill:#e8f4ea,stroke:#2e7d32,color:#1b1b1b
    classDef unwired fill:#fff8e1,stroke:#f9a825,color:#1b1b1b,stroke-dasharray:4 3
```

`OutputGuardrail` exists and is tested in `packages/guardrails/output.py`, but nothing in
`services/` calls it yet. The equivalent wording checks that *are* live run inside the agent handler
(`referraleasy_agents/decisions.py`).

## The input pipeline — `three_stage` profile

```mermaid
flowchart TD
    Msg["message + workflow_state<br/>+ short_reply_expected"] --> Mask["mask_pii() — once, here<br/>email · phone · digit runs ≥ 7"]
    Mask --> L1

    subgraph L1["Layer 1 — regex (layer_regex.py)"]
        direction TB
        N["Unicode normalise · length cap"]
        P["explicit attack patterns<br/>injection · exfiltration"]
        Em["emergency phrases"]
        Pr["profanity: refused only when<br/>there is no request signal"]
        Sh["anchored short reply<br/>'evet', date, ZIP — valid if a question is open"]
    end

    L1 -->|"emergency"| EM["short-circuit:<br/>APPROVED_EMERGENCY_RESPONSE<br/>→ HUMAN_HANDOFF"]
    L1 -->|"block"| STOP["block — later layers not called"]
    L1 -->|"allow / clarify"| L2

    subgraph L2["Layer 2 — decision model (layer_decision_model.py)"]
        direction TB
        U["refuse to send if identifiers<br/>survived masking"]
        OR["OpenRouter, pinned<br/>GUARDRAIL_DECISION_MODEL"]
        Parse["parse into closed enums<br/>allow | block | clarify + category<br/>unparseable = not a decision"]
    end

    L2 -->|"block"| STOP
    L2 -->|"allow / clarify"| L3

    subgraph L3["Layer 3 — local classifier (layer_classifier.py)"]
        direction TB
        Enc["encoder rung, named — never substituted<br/>OnnxEncoder: all-MiniLM-L6-v2, hash-verified<br/>or offline/hashed-char-ngram-v1"]
        Thr["ThresholdRecord<br/>method · corpus hash · confusion matrix"]
        Band["inside uncertainty band → clarify<br/>(similarity ≠ probability)"]
    end

    L3 --> Combine["combine<br/>first non-allow decides<br/>a later block beats a clarify<br/>allow never overrides"]
    STOP --> Combine
    Combine --> Out["GuardrailOutcome<br/>decision · category · reason<br/>verdicts[] · unavailable[] · latency"]
```

## The `conditional` profile

Same order, minus calls that cannot change the answer. It is measured against `three_stage` rather
than assumed better.

```mermaid
flowchart LR
    L1["Layer 1 verdict"] --> Q{"decisive by construction?<br/>attack · emergency · oversized/empty<br/>· full short-reply match with a question open"}
    Q -- yes --> Done["stop — skipped layers recorded"]
    Q -- no --> L2["Layer 2"] --> L3["Layer 3"] --> Done2["combined decision"]
```

## Closed dependency: an unavailable layer is not a permission

```mermaid
flowchart TD
    Build["build_layers(GuardrailSettings)<br/>at startup"] --> LS{"every layer has<br/>something behind it?"}
    LS -- no --> Closed["LayerSet.closed<br/>names the layer and why"]
    Closed --> H["/healthz reports it"]
    Closed --> Screen["screen() → outcome.unavailable ≠ ∅<br/>decision forced to clarify"]
    Screen --> Book{"write path?"}
    Book -- "POST /bookings" --> R503["require_complete() raises<br/>503 guardrails_not_configured"]
    Book -- "GET /help" --> Help["static help card still served"]
    LS -- yes --> Ready["all three layers run"]
```

## How each outcome reaches the patient

```mermaid
stateDiagram-v2
    [*] --> Screened
    Screened --> Blocked: block, not emergency
    Screened --> Emergency: category = emergency
    Screened --> Clarify: clarify / incomplete
    Screened --> Allowed: allow
    Blocked --> [*]: 422, nothing recorded
    Emergency --> [*]: 200, fixed safety text, HUMAN_HANDOFF
    Clarify --> [*]: 202, recorded, committed=false
    Allowed --> [*]: 202, recorded, dispatched to coordinator
```

## Authorization, beside the guardrail

```mermaid
flowchart LR
    subgraph Sessions["auth.py — separate by construction"]
        PS["patient session<br/>scoped to own workflows (404 otherwise)"]
        AS["admin session<br/>transitions · control · admin reads"]
        SV["service token<br/>agent ↔ agent, gateway → coordinator"]
    end
    subgraph Tool["Every agent tool call — tools.py"]
        A1["1 · allowlist<br/>roles.py ROLE_TOOLS"] --> A2["2 · policy_assert(role, tool, state)<br/>PostgreSQL, default deny"]
    end
    subgraph Approval["Booking approval — approval.py"]
        AP["patient + workflow + state_version<br/>+ payload hash + expiry"]
    end
```

Only Scheduling holds `create_booking` / `cancel_booking` / `reschedule_booking`, and the Coordinator
holds only `delegate_task`. `roles.py` asserts both at import time.

## Measurement

`python -m referraleasy_guardrails.evaluation` runs the labelled corpus through each layer and both
profiles and reports per-layer false positives/negatives, added latency and cost. The admin
Guardrails page reads live per-layer decisions from `otel-events-*`.
