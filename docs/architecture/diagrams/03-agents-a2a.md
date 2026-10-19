# 3 · Agents, the A2A task contract and tool authority

Spec: §3, §5, §6, §8 · ADR-0002, ADR-0003, ADR-0030, ADR-0033 · Code: `packages/agent-runtime/`,
`services/agents/referraleasy_agents/`, `packages/skills/`, `packages/contracts/schemas/task.json`

[← back to the index](README.md)

## Agent topology

One coordinator and six specialists. They run from one image, and `AGENT_ROLE` picks the role.
The coordinator's only tool is `delegate_task`. Only Scheduling can write bookings.

The standard journey uses `execution_mode=workflow`. Its source-driven operations run through the
same tools and policy gates. Only plan coverage and matching run in parallel. Each remote workflow
call inherits the deadline and remaining quotas, and the parent charges returned usage. The ADK
sequence below describes model tasks. See [ADR-0041](../../adr/ADR-0041-autonomous-coordination.md).

```mermaid
flowchart TB
    GW["gateway<br/>CoordinatorDispatch"] -- "POST /tasks" --> CC

    CC["Care Coordinator<br/>tools: delegate_task"]

    CC -- "delegate_task" --> RI["Referral Intake<br/>validate_referral · resolve_location<br/>patient_preferences"]
    CC -- "delegate_task" --> CN["Coverage & Network<br/>check_plan · check_provider_network<br/>get_coverage_rules"]
    CC -- "delegate_task" --> PM["Provider Matching<br/>find_candidates · search_directory"]
    CC -- "delegate_task" --> RR["Referral Readiness<br/>get_referral_requirements<br/>inspect_document · validate_referral_package"]
    CC -- "delegate_task" --> SC["Scheduling — sole booking writer<br/>list_slots · lookup_request<br/>create_booking · cancel_booking · reschedule_booking"]
    CC -- "delegate_task" --> RH["Recovery & Handoff<br/>classify_error · create_handoff<br/>search_directory"]

    subgraph PMsub["inside Provider Matching — ADK AgentTools, not containers"]
        CR["Candidate Researcher<br/>builds the pool, keeps provenance"]
        CA["Constraint Analyst<br/>why each candidate fits or not<br/>which alternative to ask about"]
        Code["matching_code.py<br/>distance · filtering · uniqueness · ranking<br/>(code, never a model score)"]
        CR --> CA
        CR -.-> Code
    end
    PM --- PMsub

    RI & CN & PM & RR & SC & RH --> API["mock-api :4010"]
```

## The A2A task: envelope and result

```mermaid
classDiagram
    class AgentTaskRequest {
        task_envelope: TaskEnvelope
        command_id
        role input document (schema per role, extra=forbid)
    }
    class TaskEnvelope {
        workflow_id
        command_id
        target role
        current_state_version
        constraints
        evidence_refs[]
        allowed_actions[]  computed from the policy table
        deadline
        call_budget  model 12 / tool 16 / delegation 8
        release_manifest_id
        parent_task_id
    }
    class AgentTaskResult {
        outcome: ResultOutcome
        protocol_state: A2A TaskState
        evidence_ids[]
        reason_code
        next_allowed_actions[]
    }
    class ResultOutcome {
        <<enumeration>>
        completed
        needs_input
        no_match
        retryable_error
        terminal_error
        outcome_unknown
    }
    AgentTaskRequest --> TaskEnvelope
    AgentTaskResult --> ResultOutcome
```

`contracts/a2a.py` maps each application outcome onto an A2A protocol task state; the custom enum
is never presented as a protocol state. A turn that ends in `outcome_unknown` is HTTP 200 with
protocol state `failed` **and** the outcome beside it.

> **Transport today:** each agent serves `POST /tasks`, `GET /healthz` and `GET /manifest` from the
> runtime's FastAPI app, and publishes an immutable Agent Card (`cards.py`). ADR-0003 pins
> `a2a-sdk`, but no module imports it yet; ADK↔a2a-sdk compatibility is recorded as unverified.

## One specialist turn (`handler.py`)

```mermaid
sequenceDiagram
    autonumber
    participant Caller as Coordinator / gateway
    participant App as runtime app.py
    participant H as TaskHandler
    participant Sk as Skill loader
    participant ADK as ADK Runner + Agent
    participant TS as ToolSet / tool gate
    participant PG as PostgreSQL policy_assert
    participant API as mock-api

    Caller->>App: POST /tasks (Bearer service token, traceparent)
    App->>App: verify token at transport, drop payload user_id
    App->>App: inherit envelope deadline and remaining caps
    App->>H: handle(request, budget)
    H->>H: 1 split payload: envelope + command_id vs role input
    H->>H: 2 validate input schema (before any model call)
    H->>Sk: 3 select skills for role + state, load bodies
    Sk-->>H: effective skill list (written to the trace)
    H->>ADK: 4 open session keyed by workflow + command
    H->>ADK: 5 run one turn
    loop each tool call the model makes
        ADK->>TS: call tool
        TS->>TS: allowlist (roles.py)
        TS->>PG: policy_assert(role, tool, state)
        PG-->>TS: allow / RE-deny
        TS->>API: HTTP, retries=0, timeout from remaining deadline
        API-->>TS: data, wrapped as provenance-tagged evidence
    end
    ADK-->>H: model answer
    H->>H: 6 validate output schema, refuse payment guarantees<br/>and unconfirmed-as-confirmed, recompute next_allowed_actions
    H->>H: 7 project outcome (suspend on needs_input, never re-run to reword)
    H-->>App: AgentTaskResult
    App-->>Caller: envelope {outcome, protocol state, validated output, trace}
```

## Tool authority — two checks, in a fixed order

```mermaid
flowchart LR
    Call["tool call<br/>(role, tool, workflow_state)"] --> AL{"1 · on this role's<br/>allowlist?"}
    AL -- no --> TNA["ToolNotAllowed<br/>no DB round-trip"]
    AL -- yes --> PB{"policy backend<br/>available?"}
    PB -- no --> PU["PolicyUnavailable<br/>(default deny)"]
    PB -- yes --> PA{"2 · policy_assert<br/>row exists?"}
    PA -- no --> PR["PolicyRefused"]
    PA -- yes --> Lease["take a CallLease<br/>from the TurnBudget"] --> Run["run the tool"]
```

A skill grants **no** tool permission. The manifest schema has no field that could carry one, and
the loader refuses a mutated manifest.

## Budget and retries (§8)

```mermaid
flowchart TB
    Root["Root TurnBudget<br/>TURN_DEADLINE_MS 30 000<br/>model 12 · tool 16 · delegation 8"]
    Root -- "delegate() — same deadline,<br/>same counters" --> Child1["Specialist budget"]
    Child1 -- "delegate()" --> Sub["Sub-agent budget"]
    Root --> Lease["each attempt lease:<br/>min(3000 ms, time left in the turn)"]

    subgraph Retry["retries.py — the only retry owner"]
        R1["attempt 1"] --> R2["attempt 2<br/>backoff 150–500 ms"] --> R3["attempt 3"]
        R3 -- "3rd transient failure" --> Rec["one-way to RECOVERY<br/>same specialist not called again"]
    end

    Ex["BudgetExhausted<br/>names which cap ran out"] --> OU["create_booking in flight?<br/>→ outcome_unknown → reconciliation"]
```

HTTP clients run with retries set to zero (`http.py`), so SDK, HTTP and A2A retries cannot
multiply the budget.

## Skills

```mermaid
flowchart LR
    subgraph Pkg["packages/skills/<name>/"]
        SK["SKILL.md<br/>instructions"]
        MF["manifest<br/>cheap selection metadata, version, hash"]
        RF["references/<br/>read-only"]
    end
    Sel["select_skills(role, state)"] --> MF
    Sel --> Load["load_instructions — only selected ones"] --> SK
    Load --> Prompt["agent instruction"]
    Sel --> Trace["effective skill list → trace + release manifest"]
```

The eight packages are referral-intake, coverage-interpretation, constraint-negotiation,
directory-grounding, referral-readiness, booking-confirmation, uncertain-booking-reconciliation and
handoff-triage.

## Model providers

```mermaid
flowchart LR
    MB{"MODEL_BACKEND"} -- offline --> OM["offline_model.py<br/>deterministic BaseLlm, no socket<br/>output flagged synthetic"]
    MB -- live --> LL["ADK LiteLlm → OpenRouter<br/>AGENT_MODEL_ID pinned"]
    LL -. alternative .-> VX["Vertex Gemini Flash"]
```
