<div align="center">

# ReferralEasy

### Multi-agent referral coordination, engineered for failure.

**Google ADK · A2A · A2UI · Hybrid RAG · OpenSearch · Guardrails · Durable workflows**

https://github.com/user-attachments/assets/24517357-e5ea-45a1-b4d0-4db36b714ea3

**[▶ Watch the 60-second product film](docs/media/referraleasy-showcase-enterprise.mp4)**

Guided referrals, specialist coordination, explicit consent and recovery. Synthetic demo.

[Architecture](#architecture) · [Agents](#multi-agent-design) · [Reliability](#bounded-execution-and-failure-recovery) · [Evidence](#engineering-evidence) · [Run locally](#run-locally)

</div>

ReferralEasy turns a fragmented referral process—provider search, insurance checks, paperwork and
appointment booking—into a guided patient journey. Seven specialist agents coordinate the work;
the patient chooses the provider and appointment, reviews the exact request, and approves the write.

This is a **GenAI systems engineering project**. Its central design principle is that
model reasoning operates inside explicit authority, time and state boundaries. Agent collaboration,
retrieval quality, user interaction and distributed correctness each have their own contracts.

> **Demo scope:** All patient, provider, hospital, insurance and document records are synthetic.
> This project does not provide clinical decisions or connect to real healthcare systems.

The demo runs on localhost and provides fixture sessions without login. Read the
[security and publication notes](SECURITY.md) before running it or sharing source.

## Screenshots

Live browser captures from the local synthetic demo. Click a thumbnail to open the full-size image.

<table>
  <tr>
    <td align="center"><a href="docs/media/referral-create.jpg"><img src="docs/media/referral-create-thumb.jpg" width="260" alt="Create a demo referral with a sample patient" /></a><br /><sub>Create a referral</sub></td>
    <td align="center"><a href="docs/media/referral-journey.jpg"><img src="docs/media/referral-journey-thumb.jpg" width="260" alt="Guided patient referral and provider choices" /></a><br /><sub>Patient referral</sub></td>
  </tr>
  <tr>
    <td align="center"><a href="docs/media/admin-dashboard.jpg"><img src="docs/media/admin-dashboard-thumb.jpg" width="260" alt="Admin dashboard with measured events and outcome bars" /></a><br /><sub>Admin dashboard</sub></td>
    <td align="center"><a href="docs/media/component-gallery.jpg"><img src="docs/media/component-gallery-thumb.jpg" width="260" alt="Interactive gallery with provider selection and recovery states" /></a><br /><sub>Component gallery</sub></td>
  </tr>
</table>

## What this project demonstrates

| Engineering area          | Design demonstrated here                                                                                                                                      |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Multi-agent systems**   | Seven separately served specialists; parallel independent work; ordered local sub-agents; role-scoped tools and minimized task context.                       |
| **Google ADK**            | Real ADK agents and `Runner` execution, `AgentTool` sub-agents, schema-validated results and a `BaseLlm` wrapper that charges model calls to the turn budget. |
| **A2A (Agent-to-Agent)**  | Agent Cards, authenticated service calls, typed task/result envelopes and inherited execution limits across remote delegation.                                |
| **A2UI (Agent-to-UI)**    | Eight catalog components streamed over SSE; server-issued actions, explicit consent, event deduplication and workflow-version checks.                         |
| **RAG and vector search** | OpenSearch BM25 + vector kNN, reciprocal rank fusion, a reranker adapter, source provenance, structured-record precedence and an explicit degradation ladder. |
| **Safety and authority**  | Three-stage input screening, PII masking, default-deny tool policy, bound approval and source text treated as untrusted data.                                 |
| **Failure engineering**   | Deadlines, call quotas, bounded retries, cancellation, idempotency, uncertain-write reconciliation, leases and stale-worker rejection.                        |
| **LLMOps**                | Distributed traces, per-layer guardrail events, a nine-page admin dashboard, versioned prompts/skills, release manifests and evaluation contracts.            |

The standard referral journey uses deterministic orchestration over governed specialist tools.
Model-driven tasks use ADK. Both paths enforce tool policy and execution budgets. The hybrid RAG
package is separately tested; connecting it to the live matching tool is an explicit remaining step.

## The patient journey

1. **Capture the referral.** Validate the source and identify missing information.
2. **Check coverage and find candidates.** Run independent plan coverage and provider matching together.
3. **Respect the patient's choices.** Check the selected doctor's network and the selected hospital's requirements.
4. **Prepare the referral.** Surface missing documents and collect the information needed to proceed.
5. **Review and approve.** Present the selected slot and exact booking request before any write.
6. **Report the real outcome.** Show a confirmed receipt only after confirmation; otherwise reconcile or offer a consented human handoff.

Patient answers create durable continuation work. A replacement worker can resume accepted input
after a restart. Current cards are rebuilt from workflow state, so a refreshed page can recover the
review, receipt or handoff view.

## Architecture

```mermaid
flowchart TB
    Patient[Patient] --> UI["Lit + TypeScript client<br/>A2UI component catalog"]
    UI -->|Commands and consent| Gateway["FastAPI gateway<br/>Auth · guardrails · policy · workflow"]
    Gateway -->|SSE events and cards| UI
    Gateway <--> DB[("PostgreSQL<br/>State · approvals · continuations · outbox")]
    Gateway -->|Authenticated A2A tasks| Coordinator[Care Coordinator]
    Coordinator --> Intake[Referral Intake]
    Coordinator --> Coverage[Coverage and Network]
    Coordinator --> Matching[Provider Matching]
    Coordinator --> Readiness[Referral Readiness]
    Coordinator --> Scheduling[Scheduling]
    Coordinator --> Recovery[Recovery and Handoff]
    Matching --> Local["ADK model path<br/>Candidate Researcher → Constraint Analyst"]
    Intake & Coverage & Matching & Readiness & Scheduling & Recovery --> Tools["Governed tools<br/>Role allowlist + state policy + budget"]
    Tools --> Mock["Synthetic hospital API<br/>Directory · coverage · documents · appointments"]
    DB <--> Workers["Workers<br/>Outbox · reconciliation · human queue"]
    RAG["Hybrid RAG package<br/>BM25 + kNN → RRF → rerank"] --> Vector[(OpenSearch vector index)]
    Matching -.->|Integration pending| RAG
    Gateway --> Telemetry["OpenTelemetry<br/>Traces and structured events"]
    Tools --> Telemetry
    Workers --> Telemetry
    Telemetry --> Analytics[(OpenSearch telemetry indices)]
    Analytics --> Admin[Nine-page admin dashboard]
```

**Authority lives outside the model.** PostgreSQL and application policy govern transitions and
writes. The coordinator can delegate but cannot book. Scheduling owns booking tools, and still
needs the correct workflow state and patient approval.

Explore the [system diagrams](docs/architecture/diagrams/README.md),
[architecture specification](ReferralEasy_Architecture_v3_EN.md) and
[architecture decision records](docs/adr/).

## Multi-agent design

| Agent                  | Responsibility                                             | Boundary                                                   |
| ---------------------- | ---------------------------------------------------------- | ---------------------------------------------------------- |
| **Care Coordinator**   | Compose specialist work and collect results.               | Delegation only; no booking tools.                         |
| **Referral Intake**    | Validate referral input, location and preferences.         | Extracts and clarifies; does not make clinical decisions.  |
| **Coverage & Network** | Check plans, provider networks and coverage rules.         | Coverage evidence does not guarantee payment.              |
| **Provider Matching**  | Find and compare candidates against patient constraints.   | Hard constraints remain binding; the patient selects.      |
| **Referral Readiness** | Check hospital requirements and document completeness.     | Missing prerequisites stay visible.                        |
| **Scheduling**         | List slots, submit approved requests and look up outcomes. | A successful HTTP response alone cannot confirm a booking. |
| **Recovery & Handoff** | Classify failures and prepare a path to human help.        | Handoff requires consent to the exact summary.             |

There are two collaboration levels:

- **Between services:** A2A tasks carry typed inputs, evidence and execution context. The gateway
  chooses legal workflow steps; the coordinator runs coverage and matching concurrently only when
  those operations are independent.
- **Within an ADK matching turn:** `candidate_researcher` builds a pool, then `constraint_analyst`
  compares candidates. Both are `AgentTool` sub-agents with tools restricted to the parent's
  capabilities and model calls charged to the same turn budget. Their order is prompt-directed;
  tool permissions are enforced in code.

Versioned [skill packages](packages/skills/) supply task guidance without granting permissions.
The runtime owns the tool allowlist.

Read the [ADK assembly](services/agents/referraleasy_agents/assembly.py),
[local sub-agents](services/agents/referraleasy_agents/subagents.py) and
[standard workflow executor](services/agents/referraleasy_agents/workflow.py).

## Bounded execution and failure recovery

An agent loop must have a measurable stopping condition. The runtime applies configurable defaults
to each turn:

| Limit                                        |                              Default |
| -------------------------------------------- | -----------------------------------: |
| Turn deadline                                |                           30 seconds |
| Model attempt timeout                        | 3 seconds, clamped to remaining time |
| Attempts per retry site, including the first |                                    3 |
| Delegations / model calls / tool calls       |                          8 / 12 / 16 |
| HTTP tool timeout / fallback timeout         |              1.5 seconds / 3 seconds |

Local delegation shares the parent's counters and absolute deadline. Remote calls receive the
remaining deadline and quotas, which the receiving service clamps to its own limits. These are
execution bounds; globally atomic quota accounting across independent remote branches is not claimed.
Model calls are charged before execution, including calls that fail. Fallback work also spends budget.

| Failure                                     | System response                                                                                                                                                            |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Repeated transient failure**              | Bounded attempts with exponential backoff; honor `Retry-After` without waiting past the deadline. Transport retries are disabled so retry layers cannot multiply attempts. |
| **Valid empty result or policy refusal**    | Return the typed outcome; repeating the same request cannot turn a refusal into permission.                                                                                |
| **Runaway reasoning or delegation**         | Exhaust the model/tool/delegation quota or deadline and exit with a typed failure. Cancellation propagates.                                                                |
| **Booking timeout after a possible commit** | Preserve the original idempotency key and look up that request. The standard workflow does not blindly resend `create_booking`.                                            |
| **Duplicate tap or command delivery**       | Return the command ledger's stored response; preserve approval and request identity.                                                                                       |
| **Worker crash or expired claim**           | Reclaim pending work with a new token. Expired or replaced owners cannot complete or record stale results.                                                                 |
| **Unavailable retrieval dependency**        | Descend through an explicit fallback ladder, record why, and retain a degraded-result flag.                                                                                |
| **No safe automated resolution**            | Ask for approval of the handoff summary and persist a real human-queue record.                                                                                             |

The safety invariants include **no booking without bound approval**, **no silent constraint
relaxation**, **no stale-owner writes**, and **no presentation of `PENDING_REVIEW` as confirmed**.
The booking kill switch blocks writes while help and inspection remain available.

See [budget enforcement](packages/agent-runtime/src/referraleasy_agent_runtime/budget.py),
[retry policy](packages/agent-runtime/src/referraleasy_agent_runtime/retries.py),
[durable continuations](docs/architecture/durable-continuation-lifecycle.md) and
[booking reconciliation](docs/architecture/diagrams/05-booking-reconciliation.md).

## Retrieval, grounding and vector storage

The [hybrid RAG package](packages/rag/) implements a provider-directory retrieval pipeline:

```mermaid
flowchart LR
    Query[Query and metadata filters] --> Lexical[BM25 lexical search]
    Query --> Embedding[Query embedding]
    Embedding --> Vector[OpenSearch vector kNN]
    Lexical --> Fusion[Reciprocal rank fusion]
    Vector --> Fusion
    Fusion --> Rerank[Reranker adapter]
    Rerank --> Authority[Source and authority checks]
    Authority --> Result["Sourced result<br/>Provenance · conflicts · degradation"]
```

- **Indexing:** Entity-aware chunks retain source IDs, knowledge versions and timestamps. The
  configured embedding path uses Vertex `gemini-embedding-001` at 768 dimensions; offline runs use
  deterministic embeddings.
- **Retrieval:** BM25 and kNN use consistent metadata filters; reciprocal rank fusion merges the
  candidate lists before the optional rerank stage.
- **Grounding:** Structured coverage records take precedence over conflicting prose. Coordinates
  determine distance. Retrieved text cannot establish live appointment availability or confirmation.
- **Graceful degradation:** Hybrid + rerank → hybrid RRF → BM25 → versioned seed data → human handoff.
  Transitions only move downward, retain reasons and never manufacture a source.

**Integration status:** OpenSearch retrieval has dedicated integration tests. The running agent's
`search_directory` tool currently calls the mock API and labels its results `static_seed`. The live
rerank provider contract remains unresolved; the pipeline and fallback behavior are implemented.
See the [RAG wiring and pipeline diagrams](docs/architecture/diagrams/07-hybrid-rag.md).

## Guardrails and interaction safety

Input screening combines **regex rules → a decision model → a local classifier**. The pipeline masks
identifiers before model screening, produces closed decision enums, records layer availability and
uses a corpus-backed threshold record. An unavailable dependency does not grant write permission.
The local classifier supports a verified ONNX artifact or an explicitly named offline encoder.

Screening is backed by separate enforcement: service-supplied identity, role allowlists,
state-dependent policy, schema validation and approval bound to the patient, workflow, state version,
payload hash and expiry. Documents and retrieved passages remain data rather than instructions.
The standalone output guardrail package is tested but is not yet integrated; active agent decision
checks already reject unsupported confirmation and payment-guarantee wording.

The frontend renders eight **A2UI catalog components** over SSE, including hospital/doctor/slot
pickers, a booking review, receipts and handoff cards. It accepts only catalog components and
server-issued actions. Stable instance IDs, event deduplication and state-version checks protect the
action lifecycle. Durable surface projection repairs current cards after a restart; historical SSE
replay is still process-local.

See [guardrail architecture](docs/architecture/diagrams/02-guardrails.md),
[A2UI and action lifecycle](docs/architecture/diagrams/12-web-a2ui.md) and
[surface restart recovery](docs/architecture/workflow-surface-projection.md).

## Engineering evidence

The repository pairs architectural claims with executable checks. Useful starting points:

| Claim                                                      | Evidence to inspect                                                                                                                             |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Independent specialist calls actually overlap              | [Barrier-based workflow tests](tests/agents/test_workflow_execution.py)                                                                         |
| Delegation preserves inherited limits                      | [Remote budget tests](tests/agent_runtime/test_inherited_budget.py)                                                                             |
| Sub-agents cannot expand authority                         | [Matching sub-agent tests](tests/agents/test_matching_subagents.py) and [forbidden capabilities](tests/agents/test_forbidden_capabilities.py)   |
| Consent precedes booking; replay does not duplicate writes | [Autonomous journey tests](tests/e2e/test_autonomous_coordination.py) and [approval binding](tests/gateway/test_approval_binding.py)            |
| Crashes and stale claims cannot complete work incorrectly  | [Continuation lifecycle tests](tests/gateway/test_coordination_continuations.py) and [surface recovery](tests/gateway/test_surface_recovery.py) |
| Retrieval quality and degradation are explicit             | [OpenSearch retrieval tests](tests/rag/test_retrieval_integration.py) and [degradation tests](tests/rag/test_degradation.py)                    |
| UI retries preserve command identity                       | [Action retry tests](apps/web/test/action-retry.test.ts)                                                                                        |
| Missing measurements stay missing                          | [Admin read tests](tests/gateway/test_admin_reads.py) and [evaluation CLI tests](tests/eval/test_dataset_and_cli.py)                            |

**Recorded local verification, October 19, 2026:** 2,978 Python unit/contract tests passed with two
existing fixture skips; 295 TypeScript tests passed across web, mock API and contracts. The final
memory-backed E2E run passed all 24 cases in each of two fresh iterations. Browser checks covered
both confirmed and hospital-review outcomes, consent, reload and original-request lookup.
These are the [recorded results](docs/architecture/durable-continuation-lifecycle.md#browser-checks-and-current-results),
not a claim of a new CI run. The final PostgreSQL rerun remains pending in that record.

OpenTelemetry connects gateway and agent requests through propagated trace context. The admin
dashboard exposes traces, evaluations, guardrail events, feedback, faults, alerts and the human queue.
Missing data is reported as unavailable. Offline fixture output is marked synthetic; it is not
presented as measured model quality, cost or latency.

The evaluation package contains **44 cases across eight scenarios and 14 deterministic rules**.
It reports unmeasured coverage until actual replay records are supplied. There is no completed
replay harness or published quality benchmark yet.

## Run locally

Run commands from the repository root. The sequence below starts PostgreSQL and applies migrations
**before** starting the gateway, builds the current source, and includes the frontend's `showcase`
profile. Plain `make demo` starts only `core` and can reuse old images.

### 1. Install and check the tools

Use **Python 3.12**, **Node.js 22+**, **pnpm 12.14.0** (pinned in
[package.json](package.json)), **uv 0.13.1** (the CI/image pin), and **Docker Compose v2**.
Start Docker Desktop, or your Docker Engine, before continuing.

```bash
node --version
npm install --global pnpm@12.14.0
pnpm --version
docker compose version
docker info
```

For a standalone uv installation on macOS/Linux, install the pinned version into a known location:

```bash
curl -LsSf https://astral.sh/uv/0.13.1/install.sh \
  | env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
uv python install 3.12
```

The `PATH` change above applies to the current shell; keep `~/.local/bin` on your shell's `PATH` for
future sessions. If `uv self update` reports a different installer directory, run `type -a uv` to
find competing copies and reinstall at the intended location as above. Homebrew-managed copies
are updated with `brew upgrade uv`; updating one copy does not change which copy `PATH` selects.
See the [uv installer documentation](https://github.com/astral-sh/uv/blob/main/docs/reference/installer.md).

### 2. Install dependencies and configure `.env`

```bash
make bootstrap
pnpm --filter @referraleasy/contracts build
```

`make bootstrap` creates `.env` from [.env.example](.env.example) only when it is missing, then
installs Python and Node dependencies. Building the TypeScript contracts also prepares the host
for Vite development and tests; otherwise imports can fail with
`Failed to resolve entry for package "@referraleasy/contracts"`.

For a new offline setup, edit these values in `.env`:

```dotenv
DATABASE_URL=postgresql://referraleasy:referraleasy@localhost:55433/referraleasy
POSTGRES_HOST_PORT=55433
GATEWAY_URL=http://localhost:8080
WEB_URL=http://localhost:3000
MODEL_BACKEND=offline
EMBEDDING_BACKEND=offline
RERANK_BACKEND=offline
OPENROUTER_API_KEY=offline-fixture-not-a-real-key
```

Port **55433** was used for this local walkthrough to avoid other PostgreSQL installations on 5432.
You may use another free port, but `DATABASE_URL`, `POSTGRES_HOST_PORT`, and the migration command
must agree. The key above is a non-secret fixture used only with the offline backends: the agent
settings currently require a nonempty key even when no provider call is made.

**Offline mode starts the stack and supports fixture inspection, but does not enable booking
writes.** The decision-model guardrail deliberately stays unavailable offline. Use the component
gallery for a preview without credentials; configure the live model path below for an interactive
referral journey. A healthy container does not prove that guardrail screening or a provider call
will succeed. Do not commit `.env` or real credentials.

### 3. Start PostgreSQL, migrate, build, and launch

```bash
docker compose --profile core up -d --wait postgres

uv run python scripts/migrate.py \
  --database-url postgresql://referraleasy:referraleasy@localhost:55433/referraleasy

uv run python scripts/validate_env.py

docker compose --profile core --profile showcase build \
  gateway agent-care_coordinator worker mock-api web

uv run python scripts/demo.py --profile core --profile showcase
```

All seven specialists share the `referraleasy/agents:dev` image, so building `agent-care_coordinator`
builds their common image once. The demo command starts the services, applies OpenSearch templates,
checks migrations and fixture data, indexes the directory, and probes the selected profiles.
On a fresh database, starting the whole stack before migrating can leave the gateway unhealthy and
prevent the demo script from ever reaching its migration step.

| Surface | Local address |
| --- | --- |
| Create referral / patient app | [localhost:3000](http://localhost:3000/) |
| Component gallery | [localhost:3000/demo.html](http://localhost:3000/demo.html) |
| Admin dashboard | [localhost:3000/admin.html](http://localhost:3000/admin.html) |
| Gateway health | [localhost:8080/healthz](http://localhost:8080/healthz) |
| Mock hospital API health | [localhost:4010/health](http://localhost:4010/health) |
| OpenSearch health | [localhost:9200/_cluster/health](http://localhost:9200/_cluster/health) |

### 4. Create a referral in the browser

With the live model/guardrail configuration below ready, open
[localhost:3000](http://localhost:3000/), choose a sample patient, and click **Create referral**.
The form creates a fresh synthetic workflow, opens its patient session, submits the initial request,
and takes you directly to provider choices. No workflow IDs or terminal commands are needed.
The available samples include cardiology and neurology referrals, with language preferences loaded
from their fixture records. Do not enter real patient information.

A failed initial dispatch keeps the created workflow and its original command ID in the page;
**Retry starting referral** retries that same request. Keep the page open while retrying.
Creation uses the gateway’s existing demo-only session and workflow routes. If the demo gateway
or required model guardrail is unavailable, the form reports the failure instead of inventing choices.

To reopen an existing workflow, expand **Open a demo referral with an existing ID** and enter its
workflow ID and matching patient reference. The browser keeps the patient session in tab-local
storage so a reload restores the current question. Share or bookmark the referral URL only for
this synthetic local demo; another tab may need to open a fresh demo session.

Lakeview Regional Medical Center uses the `instant_confirm` fixture path; Ravenswood Specialty
Surgical Center uses `review_required`. A review-required request remains `PENDING_REVIEW` and
must not appear confirmed. Three unresolved status checks lead to an offer of human handoff;
a queue record is created only after the patient approves the handoff summary.

For a preview without a gateway, use [the component gallery](http://localhost:3000/demo.html).
Its filters group provider choices, booking outcomes, and recovery states. Gallery controls only
log local activity; they do not create workflows or send requests to hospitals.

### Live model and guardrail configuration

For the interactive journey, set `MODEL_BACKEND=live` in `.env`, replace the offline fixture key
with your own `OPENROUTER_API_KEY`, and set `AGENT_MODEL_ID` to a verified tool-capable OpenRouter
model ID. The shipped guardrail pin is `GUARDRAIL_DECISION_MODEL=openai/gpt-5-nano`; keep the local
classifier model and calibrated threshold aligned with [.env.example](.env.example).
The local classifier can remain offline while the decision model uses OpenRouter.

You can keep `EMBEDDING_BACKEND=offline` and `RERANK_BACKEND=offline` while testing the patient
journey. Their live backends need their own validated provider configuration. Before enabling any
live backend, resolve **every** `CHANGEME` value: the environment validator checks even unrelated
declared pins. Replace the patient/admin signing secrets and A2A token with local-only values.
If Vertex, evaluation, or LangSmith are disabled, clear their unused placeholder fields rather
than inventing provider settings; set `LANGSMITH_ENABLED=false` when tracing is disabled.

```bash
uv run python scripts/validate_env.py
docker compose --profile core --profile showcase up -d --wait
```

The second command recreates services whose environment changed. Real provider calls can incur
charges. Keep service ports on localhost. See [.env.example](.env.example) and the
[Compose guide](infra/compose/README.md) for the complete environment contract.

### Development, health checks, and common startup failures

For frontend changes with hot reload, keep the backend stack running, stop its web container, and
start Vite on the same port:

```bash
docker compose stop web
pnpm --filter @referraleasy/contracts build
pnpm web:dev
```

The gallery at `/demo.html` also works with Vite alone, without Docker or a gateway. If you use
containerized frontend development instead, rebuild after source changes:

```bash
docker compose --profile core --profile showcase build web
docker compose --profile core --profile showcase up -d --no-deps --wait web
```

After backend changes, repeat the image-build command in step 3 and start the stack again.
`docker compose restart` alone does not rebuild source into an existing image.

| Symptom | Check / fix |
| --- | --- |
| PostgreSQL connection refused on 5432 | This walkthrough uses 55433. Match the migration URL to `.env`, and start `postgres` with `--wait`. |
| PostgreSQL authentication fails on another project's server | Confirm the published port with `docker compose port postgres 5432`; keep both `.env` port values aligned. |
| `web NO PROBE: WEB_URL is not set` | Add `WEB_URL=http://localhost:3000` to an existing `.env`. Updating `.env.example` does not update existing files. |
| No frontend on port 3000 | Include both `--profile core --profile showcase`; the web service belongs to `showcase`. |
| Old missing-workflow screen or a stream `Invalid URL` error | Rebuild the web image from current source, recreate the container, and reload. Open a real workflow using both IDs. |
| Sample workflow returns not found | The example ID is from another local database. Create a fresh workflow with the browser form above. |
| Session expired | Click **Open demo session** again using the same workflow ID and patient reference. |
| Agent startup says `OPENROUTER_API_KEY is not set` in offline mode | Set the non-secret offline fixture key shown in step 2, keeping all three backends offline. |
| Booking is pending or not confirmed | A review-required fixture can remain pending. Status checks look up the original request; they do not submit another booking. |
| Mock API reports `offline: true`, `llm_calls: 0` | Expected: this service simulates hospitals and never calls a model. It does not describe the agent or guardrail model backend. |
| OpenSearch is `yellow` on one node | All primary shards are available, but some replicas cannot be allocated on that node. This does not explain a frontend startup failure. |

```bash
docker compose ps
docker compose logs --tail=100 gateway web
uv run python scripts/health.py --profile core --profile showcase
make down
```

`make down` stops the stack and preserves named volumes. Do not delete volumes to fix a frontend
problem: doing so removes the local workflows, booking state, and indexes you may want to inspect.
The [local demo runbook](docs/runbooks/local-demo.md) contains additional troubleshooting.

### Tests

```bash
make test                 # Python unit and contract tests
pnpm test                 # TypeScript workspace tests
make e2e REPEAT=2         # Fresh mock API + temporary database per iteration
make test-integration     # Requires TEST_DATABASE_URL pointing to a dedicated test database
make test-integration-rag # Requires a reachable OpenSearch instance
make eval                 # Reports unmeasured cases unless RUNS=<capture> is supplied
make down
```

Database-backed tests clear test tables: use a dedicated migrated test database.
`make help` lists the remaining commands. [CI](.github/workflows/ci.yml) also checks types,
contracts, lint and secrets; the OpenSearch integration suite is currently run separately.

## Scope and remaining work

The standard synthetic referral journey, durable continuations, consented booking/handoff, safety
policy and current-card recovery are implemented and locally tested. Remaining platform work is
explicit:

- Connect the hybrid retriever to the matching service and resolve the live rerank provider contract.
- Integrate the standalone output guardrail with agent-held source evidence.
- Add evaluation replay capture, scenario execution and measured model-quality reports.
- Complete per-conversation release pinning, canary routing and CI image publishing.
- Persist historical SSE replay and general model-task dispatch for multi-replica operation.
- Complete scheduled alert execution and operator queue actions in the dashboard.

The deployment design targets Cloud Run; this repository does not claim a production deployment.
See [ADR-0040](docs/adr/ADR-0040-cloud-run-design.md) and
[ADR-0041](docs/adr/ADR-0041-autonomous-coordination.md) for execution boundaries and tradeoffs.

## Explore the repository

```text
apps/web/                  Patient A2UI client and admin dashboard
services/gateway/          Commands, guardrails, workflow policy, SSE and continuations
services/agents/           Seven A2A services, ADK assembly and local sub-agents
services/workers/          Outbox, ownership, reconciliation and human handoff
services/mock-api/         Synthetic hospital integrations and fault injection
packages/agent-runtime/   Budgets, retries, model adapters, tool gates and skills
packages/rag/             Hybrid retrieval, vector indexing and degradation
packages/guardrails/      Input layers, corpus evaluation and output checks
packages/contracts/       JSON Schema → Pydantic and Zod
packages/telemetry/        Trace propagation, events, masking and query contracts
packages/release-registry/ Versioned component hashes and release manifests
packages/eval/            Dataset, deterministic rules and evaluation CLI
registry/                 Agent, prompt and release versions
migrations/               PostgreSQL invariants and durable state
docs/                     Decisions, diagrams, runbooks and verification records
```

For a deeper review, start with the [architecture-to-code map](docs/architecture/README.md),
[coordination acceptance evidence](docs/architecture/autonomous-coordination.md),
[implementation work plan](IMPLEMENTATION_TODO.md) and
[historical implementation notes](docs/implementation-notes.md).

Showcase video: [MP4](docs/media/referraleasy-showcase-enterprise.mp4) ·
[media details and attribution](docs/media/README.md).
