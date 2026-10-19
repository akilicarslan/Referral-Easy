# ReferralEasy — Implementation TODO

Derived from `ReferralEasy_Architecture_v3.md` (v3.0, 2026-10-14). The architecture doc is the source of
truth; section references like `§7` point into it. This file only orders the work and makes it checkable.

**Status:** greenfield — nothing implemented yet.

Legend: `[ ]` task · `[!]` invariant that must hold and be covered by a test · `→` exit criterion.

---

## Invariants (apply to every phase)

These are the release blockers from §14 and the Definition of Done in §21. Each one needs at least one
automated test before the phase that introduces the behavior is closed.

- [!] No booking without a valid approval bound to patient + workflow + state_version + payload hash + expiry (§9).
- [!] No double booking: slot uniqueness in the mock hospital DB, same idempotency key on retry (§7, §17).
- [!] On `outcome_unknown`, reconcile with the **same** key; never mint a second booking key (§7).
- [!] A stale worker (expired lease / old generation) cannot write workflow state (§7).
- [!] Cross-user tool access is rejected; `user_id` in payloads is never trusted (§5, §9).
- [!] Hard constraints are never relaxed silently; preferences change only on explicit patient choice (§2.1, §2.4).
- [!] `PENDING_REVIEW` / `REQUESTED` are never shown as confirmed; HTTP 200 ≠ confirmed (§2.5).
- [!] Documents and retrieved text are data, never instructions (§2.3, §9).
- [!] Guardrail does not block short valid replies ("evet", dates, ZIP codes) in the right state (§9).
- [!] Guardrail dependency down → no booking writes; static help card only (§9).
- [!] Telemetry, LangSmith, eval and notification failures never roll back or block a booking (§7, §15, §19).
- [!] No fabricated demo charts; every admin number comes from a real trace/event (§12).
- [!] `<pinned-...>` values in the env contract are placeholders until a Phase 0 spike pins them (§19).

---

## Phase 0 — Bootstrap and risk spikes

Goal: lock versions and resolve every unknown that would invalidate later phases. Each spike ends with a
short ADR in `docs/adr/` and a smoke test that stays in `tests/contracts/`.

### 0.1 Repo skeleton (§22)
- [x] `git init`, `.gitignore`, monorepo layout exactly as §22
- [x] Python toolchain (uv) for gateway, agents, workers; Node/TS (pnpm) for `mock-api` and `apps/web`
- [x] Lockfiles committed; pinned base images
- [x] `.env.example` copied from §19; secrets never committed (pre-commit secret scan)
- [x] `Makefile` targets stubbed: `demo`, `up`, `down`, `migrate`, `seed`, `index`, `test`, `eval`
  — **no stubs remain, but `eval` cannot complete a run.** `demo` is now a real eight-step
  orchestrator (`scripts/demo.py`, Phase 8.3), and `migrate`, `index`, `seed`, `health` and `test`
  were already real; `seed` turned out not to be a loader at all, so see the note under Phase 8.
  `eval` runs a real CLI, and it exits 3 without `registry/releases/latest.json`, which does not
  exist because building one needs an image digest and Docker is not running. The command is honest
  about that rather than reporting a score of nothing.
- [x] `compose.yaml` skeleton with profiles `core`, `showcase`, `dashboards`
- [x] CI skeleton: lint, type-check, unit tests, contract tests
- [x] ADR-0001: language/tooling choices

### 0.2 Spikes (each pins a value or a version)
- [ ] **S1 ADK + LiteLlm → OpenRouter**: choose `AGENT_MODEL_ID`; prove tool calling, structured output schema, streaming, cancellation (§8) — **NOT DONE**: the spike exists and names all four capabilities; the pin is still a `CHANGEME_` value (ADR-0002)
- [ ] **S1b Vertex Gemini Flash** alternative: pin `VERTEX_AGENT_MODEL_ID`, `VERTEX_LOCATION`
- [ ] **S2 ADK A2A exposure**: Agent Card, mount in FastAPI ASGI vs separate port, client call, A2A task lifecycle states, auth header verification; pin ADK + a2a-sdk versions (§5) — **PARTIAL**: `a2a-sdk==1.2.2` and `google-adk==2.12.0` are pinned and in use across all seven services, the card shape and task states are recorded (ADR-0003), and `tests/agents/test_agent_cards.py` covers the cards against the pinned SDK. What the ADR records as UNVERIFIED, and what the credential-gated spike never proved, is an ADK↔a2a-sdk round trip
- [x] **S3 A2UI**: pin renderer/adapter version; stream a custom component over SSE; confirm a custom catalog works (§12) — pinned `@a2ui/lit 0.13.0` + `@a2ui/web_core 0.13.0`, eight custom components in one catalog, streamed over SSE. ADR-0004 still records custom-catalog authoring as UNVERIFIED; what proves it is `apps/web/test/render.test.ts` and `test/adapter-boundary.test.ts`, not a spike under `tests/contracts/`
- [x] **S4 Guardrail decision model**: pin `GUARDRAIL_DECISION_MODEL` on OpenRouter; measure latency (§9) — `openai/gpt-5-nano`, measured 5.39–5.44 s at 476 prompt tokens (ADR-0038)
- [ ] **S5 Local guardrail classifier**: pick CPU/ONNX encoder approach; record artifact hash and download steps; pin `GUARDRAIL_LOCAL_MODEL` (§9) — **PARTIAL**: the artifact is chosen, hashed (`data/artifacts.json`, `all-MiniLM-L6-v2` qint8, sha256 `4278337f…`) and the threshold measured on a calibration/held-out split — but the 23 MB artifact is not present in this environment, so the threshold record is for the *offline lexical rung* and `.env.example` ships that rung's name, not the encoder's. ADR-0005's status is still UNRESOLVED
- [ ] **S6 Embeddings**: `gemini-embedding-001` at 768 dims; document vs query task types (§8) — **PARTIAL**: pinned and documented (ADR-0006), the spike covers both task types, and the live run is blocked on Vertex credentials
- [ ] **S7 Rerank**: OpenRouter `/api/v1/rerank` with `cohere/rerank-v3.5` smoke test (§8) — **PARTIAL**: the model id is pinned and the wire shape recorded (ADR-0007); only a live 200 would prove the id resolves, and there is no key
- [x] **S8 OpenSearch**: pin single-node version; k-NN + BM25 hybrid query; OTel traces via Data Prepper; check whether native OTel **metrics** ingestion works → otherwise metrics as numeric log events (§15) — 3.9.0 pinned, spike CONFIRMED 5/5, and the answer was **no**: measurements travel as log events (ADR-0008)
- [ ] **S9 LangSmith + ADK** tracing smoke (§14)
- [ ] **S10 Eval judge**: pin `EVAL_JUDGE_MODEL_ID`

→ No `<pinned-...>` placeholder left in `.env.example`; every pin has a dated ADR and a passing smoke test.

**Not met, and the gap is five pins.** The literal `<pinned-…>` text is gone, but five functional
`CHANGEME_*` values remain: `AGENT_MODEL_ID` (S1), `VERTEX_AGENT_MODEL_ID` and `VERTEX_LOCATION`
(S1b), `LANGSMITH_PROJECT` (S9) and `EVAL_JUDGE_MODEL_ID` (S10). `scripts/validate_env.py` exists
precisely to refuse a `live` run while any of them is unresolved, so this is a *stated* state rather
than a silent one — but it is still open, and three of the five are blocked on credentials or access
rather than on work: S1b and S6 need a GCP project, S9 needs a LangSmith key, S10 needs a judge model
decision (ADR-0011), and S7 needs a live rerank call. **S5's pin resolves to a different model than
its ADR names** — `.env.example` carries the offline lexical rung while `data/artifacts.json` pins the
ONNX encoder whose threshold is recorded as UNMEASURED in this environment — which is the one
remaining item that is neither done nor blocked on anyone but this repository.

---

## Phase 1 — Contracts, domain, state machine, mock API (§21 step 1)

Goal: the deterministic core every later phase builds on. No LLM involved.

### 1.1 `packages/contracts` (§4, §5)
- [x] Task envelope: `workflow_id`, `command_id`, goal, `current_state_version`, constraints, `evidence_refs`, `allowed_actions`, `deadline`, `call_budget`, `release_manifest_id`, `parent_task_id`
- [x] Result envelope: `completed | needs_input | no_match | retryable_error | terminal_error | outcome_unknown` + `evidence_ids` + typed payload
- [x] Adapter mapping app results → standard A2A task states (never expose the custom enum as protocol state)
- [x] Decision output: `action`, `evidence_ids`, `missing_fields`, `next_allowed_actions`, `reason_code`
- [x] Patient preferences: hard constraint vs soft preference; mandatory constraints (specialty, approval, valid slot) (§2.1)
- [x] Coverage result: `accepts_insurance` vs `in_network_for_plan` vs `unknown`; referral / prior-auth requirement (§2.2)
- [x] Booking lifecycle: `REQUESTED, PENDING_REVIEW, CONFIRMED, REJECTED, CANCEL_PENDING, CANCELLED` (§2.5)
- [x] Single schema source → Pydantic (Python) + Zod/JSON Schema (TS); schemas versioned

### 1.2 Synthetic fixtures (`fixtures/`)
- [x] Locations with coordinates; hospitals with IANA timezone and adapter type
- [x] Providers: specialty, languages, accepts new patients, accessibility, first/return visit durations
- [x] Coverage plans, plan-level network records, referral/prior-auth rules, one deliberately stale/contradictory record
- [x] Referral requirements per hospital/plan/visit type
- [x] Document text fixtures (referral letter, prior report, auth reference), including one with an injection attempt
- [x] Synthetic patients; fixture data shaped to make all eight §20 scenarios reproducible

### 1.3 PostgreSQL (§7)
- [x] Migrations for: workflow, session, task, approval, booking_request, outbox, handoff, release registry, audit, command dedup
- [x] Owner/lease + monotonic generation columns on active work
- [x] Atomic state transitions with optimistic version check; no long-held transactions

### 1.4 State machine and policy (§4, §7)
- [x] Workflow state machine
- [x] Policy table: which specialist/tool is allowed in which state (the coordinator cannot override it)
- [x] Approval binding (patient + workflow + state_version + payload hash + expiry); any data change invalidates it
- [x] `command_id` dedup on replay

**Done** — `packages/domain` (`policy.py`, `workflow.py`, `commands.py`, `outbox.py`, `handoff.py`),
176 tests, ADR-0026. The database owns the rules and the client only translates its refusals: every
invariant is a constraint or a trigger, and `tests/domain` proves it by trying to write past it.

Two things worth carrying forward:
- **92 of those tests need a real database** and skip without `TEST_DATABASE_URL`. Use
  `make test-integration`; CI has its own job with a Postgres service and fails if they skip.
- **RE003 (`HumanOwnedError`) is not reachable through the client.** `workflow_claim`/`workflow_advance`
  require `human_owner_id IS NULL` inside the compare-and-set, so a human-owned row matches *no* row
  and refuses as RE001 before the guard trigger runs. The trigger is live — a raw UPDATE trips it —
  but a caller cannot distinguish "you lost the lease" from "a human owns this" by exception type.

### 1.5 Mock API — Fastify/TypeScript (§17)
- [x] Modules: locations, hospitals, providers, coverage, referral-requirements, documents, availability, appointments, notifications
- [x] Zod + OpenAPI contracts generated from the same schemas
- [x] Two hospital adapters, `instant-confirm` and `review-required`, with different payloads normalized to the internal contract
- [x] Idempotency: same key + same payload → same booking; same key + different payload → reject
- [x] Slot uniqueness constraint; booking + notification outbox written in one transaction
- [x] `lookup_request`, cancel, and reschedule endpoints
- [x] Slots stored in UTC, displayed in hospital timezone
- [x] API failure distinguished from a genuine "no specialist" result
- [x] Fault middleware: latency, 429 + Retry-After, 503, malformed schema, empty valid result, stale slot/409, before-commit timeout, after-commit timeout, duplicate event, stale coverage evidence, notification failure
- [x] Faults reproducible from `MOCK_FAULT_SEED` + `scenario_id` + request sequence; random mode is seeded too

**Done** — `services/mock-api`, **94 tests**, ADR-0022/ADR-0024 for the external-system boundary.

The window into this phase that mattered most: the mock API owns the **external** systems (hospitals,
providers, slots, plans, documents, coverage) and seeds itself from `fixtures/` at startup. There is
deliberately no `hospital`/`slot`/`plan` table in PostgreSQL — a `make seed` that inserted them would
invent tables the schema refuses to have and create a second authority for a fact this service
already owns. That is why `make seed` verifies rather than loads.

Its most useful behaviour is one it makes easy to get wrong in the opposite direction: it reports a
`builtin` dataset when it cannot read `fixtures/`, which is self-consistent and healthy-looking but
shares no id with `scenarios.json`. A demo running on it would complete end to end while naming
hospitals that do not exist. `make seed` now fails loudly on exactly that state.


### 1.6 Tests
- [x] Contract tests for every schema (Python ↔ TS round trip)
- [x] Idempotency, slot uniqueness and concurrent-booking tests
- [x] State machine: illegal transitions rejected; optimistic version conflict

**Done** — `tests/e2e/test_scripted_booking.py`, 16 tests, `make e2e`. One booking driven end to end
by a scripted HTTP client with no LLM anywhere, under all eleven fault types. `make e2e` restarts the
mock API on a clean database first: the suite shares one store, so a second run against surviving
state is not the same test.

→ A scripted (no-LLM) client can drive one booking end to end against the mock API, under every fault type, without violating any invariant.

---

## Phase 2 — Gateway, web app, eight A2UI components (§21 step 2)

Goal: the normal patient selection flow, driven by a **scripted coordinator stub** so this phase can run in
parallel with Phase 3.

### 2.1 Gateway — FastAPI (§5, §9, §12)
- [x] Separate patient and admin sessions; local JWT fixtures validated for real
- [x] REST command endpoints; every action validated server-side against workflow version
- [x] SSE stream with event IDs and replay; re-rendering never creates a second booking
- [x] Partial agent output never committed as final state
- [x] Progress latency measured separately from final response latency

**Done** — `services/gateway`, 147 tests, ADR-0031.

"Replay creates no second booking" is asserted as **exactly one booking row across replays**, not as
the client's discipline. Partial output cannot become final state structurally: only
`{surface_update, workflow_state, terminal}` are authoritative, so non-authoritative frames can stream
indefinitely and the folded state never moves. The policy test **re-parses migration 014 on every
run** and compares it against the gateway's transcription, so the two cannot drift silently — that
check exists because eighteen transition edges *were* silently missing from the transcription.

**Real gaps, deliberately left visible:**

- **The PostgreSQL control profile is implemented** (it was the notable gap). `PostgresControlState`
  satisfies the same eight-method `ControlState` port, `refuse_unimplemented_profiles` is **removed**
  rather than narrowed, and the gateway now `ping()`s before serving so a wrong DSN or an unmigrated
  database is a refusal to start — the database is asked, not a hard-coded list consulted.
  **101 of the 159 test functions are parameterised over both backends**, and with a database **98
  tests hit the real server with zero skips**; without one, 152 pass and 7 skip. The 10 tests that
  cannot run in the postgres profile are **deselected, never skipped**, each with a docstring naming
  the guarantee the schema enforces differently, and a test that fails if one is marked without being
  documented. That boundary is itself a test — no assertion was parameterised into a weaker form.
- **§12's event log is still not persisted, and there is no table for it.** No migration creates one
  (`audit` is the tamper-evident chain, not the transport log) and the agent did not invent one.
  Replay therefore survives a client disconnect but not a gateway restart. Tested behaviourally in
  both profiles; the "no table exists" half is by inspection of `migrations/`, not by assertion.
- **JWT is hand-rolled** on stdlib `hmac`/`hashlib` (PyJWT is not a dependency). Exercised for real by
  every route, but there is no JWKS/RS256, key rotation, or revocation beyond expiry.
- **The gateway Dockerfile has never been built or run.**
- **The web client could not open the authenticated stream.** `EventSource` cannot send headers, and
  the stream route requires a patient session. The gateway accepts `?token=` in the demo profile for
  exactly this; `apps/web` now passes a token through `SseClientOptions.token` (tested, and
  `buildUrl` at last has tests — it had none), but **the app still never obtains a session**, so the
  browser-to-gateway stream remains unexercised end to end.

### 2.2 Web — `apps/web` (§12)
- [x] A2UI adapter (version pinned in S3)
- [x] Catalog: `HospitalPicker`, `DoctorPicker`, `SlotPicker`, `PatientDetailsForm`, `BookingReview`, `AppointmentReceipt`, `DirectoryFallbackCard`, `HumanHandoffCard`
- [x] Coverage, preference and readiness shown as section/field variants of these cards — no new UI system per feature
- [x] `PatientDetailsForm` is a real form, validated on the backend; also hosts synthetic document selection
- [x] Order: hospitals → doctors at the chosen hospital → slots; no auto-selected slot, no pre-booking (§4)
- [x] Each component instance carries workflow version and server-validated action

**Done** — `apps/web` (Vite + Lit), 70 tests, ADR-0027. Build and container verified: the nginx
upstream is a variable so a missing gateway costs one 502 on an API call rather than killing the UI.

**Not done here:** the Playwright e2e below is *not* written. The client is verified under jsdom and
over HTTP against the built image; jsdom has no `EventSource`, so **the SSE happy path has no test
at all** — only its failure path. A real browser pass is still owed, and so is the end-to-end booking
against the gateway once the gateway can serve it.

---

## Phase 3 — ADK agents, sub-agents, skills, registry, A2A (§21 step 3)

### 3.1 `packages/agent-runtime`
- [x] Model factory from env (LiteLlm → OpenRouter, or Vertex), per-agent override recorded in the manifest
- [x] Shared deadline/budget context (remaining time, model/tool/delegation counters)
- [x] Result envelope helpers and A2A state adapter
- [x] Tool allowlist and scope enforcement per agent; workflow ownership check on every tool call
- [x] HTTP-level service auth verification
- [x] Skill loader: select only the required skills; write the effective skill list to the trace
- [x] Suspend on `needs_input` / HITL without holding an open LLM call
- [x] Single image, role chosen by `AGENT_ROLE` (§18)

**Done** — `packages/agent-runtime`, 225 tests (9 of them live-provider spikes), ADR-0030.

The A2A mapping is not re-implemented: `results.py` imports the projection from `packages/contracts`
and a test asserts **object identity**, plus three AST guards that the runtime's own source contains
no protocol-state literal, no local outcome table and no local projection — each with a planted
violation proving the guard can fail. The trap is pinned: `outcome_unknown` → `failed` (terminal)
with `requires_reconciliation=True`, the application outcome always carried beside the protocol state.

`TurnBudget` is the single authority: 3000 ms per attempt, ≤3 attempts **including the first**,
counted per call site, 30 s turn deadline, 8 delegations / 12 model calls / 16 tool calls, with leases
shrinking to the remaining turn. A delegation — and a grandchild — shares the parent's absolute
deadline and counters. Exhaustion is typed: `retryable_error` normally, but `outcome_unknown` when it
happens at or after a booking write attempt, which is what prevents a double booking.

**Unverified:** no live A2A peer and no real Postgres here — the A2A client is exercised over
httpx/ASGI doubles and `SqlPolicyBackend` against a recording `execute` (asserting it calls
`SELECT policy_assert(role, tool, state)` and translates RE006); the real `policy_assert` behaviour
is the database package's integration tests. No ADK `Runner`/`Agent` wiring lives here — the
specialists own that assembly (see the ADR for phase 3.2).

**A note on budgets worth keeping:** §8's 3000 ms attempt limit is a *fault-injection* bound, not a
performance guarantee, and a user's thinking time is not inside it.

### 3.2 Specialist agents (§3 table)

> **Integration gap, tracked here so it is not lost:** `compose.yaml` declares `gateway`,
> `mock-api`, `worker`, `scheduler`, `local-guardrail` and `web`, but **has no entry for the seven
> specialist agents**. `services/agents/` therefore runs only under pytest, not in the stack, and
> `make demo` cannot show them. §5 permits either shape — "the ADK A2A app can be mounted to the same
> ASGI deployment or served on a separate port" — and §22's repo layout implies separate services, so
> the resolution is agent services in compose (one image, role chosen by `AGENT_ROLE`, which 3.1
> already supports). Also note compose's `worker` (singular) has no `services/worker` directory;
> `services/workers` is evidently what it meant, and the two should be reconciled rather than left
> looking like two different things.
For each agent: Agent Card, versioned input/output schemas, tools, and a test that each forbidden capability is refused.
- [x] **Care Coordinator**: A2A clients, policy table enforcement, ≤ `MAX_DELEGATIONS_PER_TURN`; cannot write bookings or change patient choices
- [x] **Referral Intake**: `validate_referral`, `resolve_location`, `patient_preferences`; no diagnosis, no invented specialty
- [x] **Coverage & Network**: `check_plan`, `check_provider_network`, `get_coverage_rules`; no guarantees, no invented rules
- [x] **Provider Matching**: provider API, coverage evidence, directory RAG (stubbed until Phase 6)
  - [x] Sub-agent **Candidate Researcher**: candidate pool with provenance and filters
  - [x] Sub-agent **Constraint Analyst**: per-candidate constraint fit and alternatives to ask about
  - [x] Distance, filtering, uniqueness and simple ranking done in code; sub-agents share the parent budget
- [x] **Referral Readiness**: `get_referral_requirements`, `inspect_document`, `validate_referral_package`
- [x] **Scheduling**: `list_slots`, `create_booking`, `lookup_request`, cancel/reschedule; sole holder of `booking:write`
- [x] **Recovery & Handoff**: error evidence, directory, `create_handoff`; bounded actions only

**Done** — `services/agents`, **525 tests**, mypy clean, ADR-0033.

Every §3 row is data plus a test: `forbidden.py` transcribes the "forbidden authority" column and
`test_forbidden_capabilities.py` calls **every entry** and asserts the raised class is the mechanism
it claims (allowlist / policy / budget / schema / signature / code). No refusal is enforced by prompt
text, and a test asserts no `prompt` mechanism exists at all — which is the difference between a
guardrail and a request.

The **ADK `Runner` path is driven for real** (offline, `ScriptedLlm`): a turn goes through
`assembly.build_runner` → real `google.adk.runners.Runner` → real tool execution → schema → §8
projection, in four test files including whole turns over a real `TestClient`. No live model call was
made and none is claimed.

Four tool parameters — `workflow_id`, `command_id`, `patient_id`, `task_envelope` — were **real model
parameters until the tool-surface test showed what a model could do with them**. They are now read
from the task instead. That is the single most valuable thing this phase produced.

**Unverified, and it matters for §9:** `create_handoff` POSTs to a `/handoffs` route that **does not
exist** on the mock API — measured against the running service, giving 404 →
`UpstreamRejected(status_code=404)` → `terminal_error`. The tool is real; its backend is not. Also: no
failure envelope carries a reason (the A2A envelope has no message field, so a policy refusal reaches
the wire as `a2a_state: "rejected"` with no text and nothing logs it — pinned by a test asserting the
absence rather than pretended away); the ADK session service is in-memory; `psycopg` policy lookups
are synchronous inside the event loop; and the authored `distance_miles` fixture is not geometric.


### 3.3 Coordination (§4)
- [x] Parallelism only for independent work: plan-level coverage ∥ matching; provider network check after doctor is known
- [x] Readiness requirements not presented as final before a hospital is chosen
- [x] Context minimization: Matching never gets DOB/phone; Scheduling fetches via `patient_details_id` (§6)
- [x] Conversation summary, structured preferences, provenance-tagged tool results and workflow state stored separately; summaries cannot override sources

**Done for the standard journey** — ADR-0041 and
[completion evidence](docs/architecture/autonomous-coordination.md). Accepted answers have durable
continuations. Source-driven specialist results produce cards and transitions. Patient approval
binds the exact booking request. Timeouts reconcile the original key. A human queue entry requires
approval of the displayed summary. E2E tests supply patient commands and no specialist outputs.

### 3.4 Skills — `packages/skills` (§6)
- [x] Packages: `referral-intake`, `coverage-interpretation`, `constraint-negotiation`, `directory-grounding`, `referral-readiness`, `booking-confirmation`, `uncertain-booking-reconciliation`, `handoff-triage`
- [x] Each: `SKILL.md` + manifest + read-only references
- [x] Adding a skill grants no tool permission (test)
- [x] Agent Card skill entries mapped to runtime instruction packages as separate records

**Done** — 8 packages, 205 tests, ADR-0028. "A skill grants no tool permission" is enforced three
ways, not asserted in prose: the manifest schema has no field that could carry a grant (`authority`
and `grants_tool_permissions` are JSON-Schema `const`s and the loader rejects a recursive list of
key names), the loader raises on a mutated manifest, and a test requires every body to repoint
authority to the runtime allowlist and the policy table.

**Unverified:** `agent_card_skill_id` values are invented — no Agent Card registry exists yet, so
nothing cross-checks them. When the cards land, the IDs must match (§13, phase 3.5).

### 3.5 Registry and versioning — `registry/` (§13)
- [ ] Immutable `release_manifest` with every field listed in §13

> **Current state of the directory: empty.** `registry/` holds only `agents/` and `prompts/`, both
> with no files, and there is no `releases/` at all — while `make eval` already references
> `registry/releases/latest.json`, and the `release_registry` table has no rows, no fixture source
> and no writer. A `make seed` audit surfaced this. Two consequences worth stating: the eval target
> cannot run, and §13's per-task "effective release" has nothing to carry.
- [ ] Hashing: any description/instruction/tool/skill change changes the agent hash; sub-agent change bumps parent
- [ ] CI fails if an affected agent's version did not increase; any bump triggers evaluation
- [ ] Conversations pinned to a release; kill switch can stop pinned conversations; migration needs explicit checkpoint
- [ ] Canary cohort and rollback routing from a registry table

→ Scenario 1 passes with real agents replacing the stub; Agent Cards discoverable; every task carries the effective release.

---

## Phase 4 — Business scenarios and guardrails (§21 step 4)

### 4.1 Scenarios
- [ ] **Multi-constraint search** (§2.1, §2.4): Turkish-speaking + Friday + 15 miles, no slot → evidence-based alternatives (wider radius, other day, other hospital); hard distance never relaxed
- [ ] **Network conflict** (§2.2): "accepts insurance" but plan record unknown/out-of-network → shown as unknown with freshness
- [ ] **Missing document** (§2.3): report missing → user attaches synthetic doc → only that check reruns → workflow resumes
- [ ] Authoritative structured API wins over stale text in coverage/readiness (§10)
- [ ] Booking lifecycle across both adapters; reschedule cancels the old booking only after the new one is verified (§2.5)

### 4.2 Input guardrails (§9)
- [ ] Layer 1, regex: Unicode normalization, length limit, word boundaries, explicit patterns; state-aware acceptance of short replies; profanity in a genuine request ≠ attack
- [ ] Layer 2, OpenRouter decision model on a masked message → allow/block/clarify + category/reason
- [ ] Layer 3, local classifier with recorded artifact hash and thresholds; disagreement/uncertainty → clarify/HITL
- [ ] Emergency phrases → fixed approved safety response, never silent off-topic block
- [ ] Profiles: `three_stage` (all visible) vs faster conditional profile
- [ ] Measure FP/FN, added latency and cost per layer

### 4.3 Output guardrails (§9)
- [ ] Schema valid; entity IDs exist; times verified; phone/address sourced; booking state real
- [ ] No coverage/payment guarantee or clinical language

**Why the first item is still open, and where it goes.** `referraleasy_guardrails.output.OutputGuardrail`
is built and tested (all six checks, a labelled suite) and has **no production caller**. The obvious
place looks like the gateway's `SurfaceRegistry.issue()`, which is the single choke point every card
passes through — and that is the wrong place, for a reason that only shows up when you try it.
`SourceOracle`'s three questions are `entity_exists`, `slot_time` and `booking_state`, and the ids they
are asked about (`ENTITY_ID_FIELDS`: `provider_id`, `hospital_id`, `slot_id`, `plan_id`, …) live in the
mock API and the directory, not in the gateway's database. A gateway-side oracle would therefore have
to answer "yes" to every id it cannot resolve — which is every id the check exists for — and a vacuous
oracle is worse than no wiring: it makes the check look present.

It belongs with the **agents**, which hold the retrieval results and the directory client:
`services/agents/referraleasy_agents/evidence.py` already gathers the per-turn evidence, and the tool
results are the `sources` the guardrail takes. The wiring is a real piece of work in that service, not
a line in the gateway, and it is named here rather than forced in where it would not mean anything.

→ Scenarios 2, 3, 4 reproducible; guardrail test suite with labelled benign/attack/short-reply cases passes.

---

## Phase 5 — Deadlines, retries, leases, reconciliation, HITL (§21 step 5)

### 5.1 Budgets and retries (§8)
- [x] 3000 ms attempt, ≤ 3 attempts including first; shared remaining deadline down to sub-agent/tool/model
- [x] 30 s turn deadline; delegation/model/tool caps from env
- [x] Exactly one retry owner: SDK/HTTP/A2A internal retries disabled
- [x] Backoff from env; honor `Retry-After`; no retry on a valid empty result
- [x] Circuit breaker; after the 3rd transient failure → one-way move to recovery
- [x] Token/cost budgets per workflow and per day; rate limits; backpressure
- [x] Strict 3 s profile measured separately; per-agent overrides go into the manifest

### 5.2 Ownership and durability (§7)
- [x] Lease + generation enforced on every write
- [x] Another replica resumes from the last verified checkpoint, not from scratch
- [x] Two-replica test sending the same command
- [x] Cancel, user abandonment and transport timeout are distinct events; remote cancel is best effort

### 5.3 Booking reconciliation (§7)
- [x] `outcome_unknown` → `lookup_request` with the same key until resolved or escalated
- [x] Outbox worker (`SKIP LOCKED`); notification failure leaves booking intact
- [x] Failed reschedule → visible intermediate state, HITL if needed; never narrated as success

### 5.4 Recovery and HITL (§3, §11)
- [x] Recovery classifies error and chooses among state-machine-permitted actions; no LLM call just to reword a failure
- [x] Triggers: no hospital/specialist, conflicting sources, unresolved readiness, long uncertain booking, patient asks, no reliable recovery option
- [x] Handoff reason classes: technical outage / no-match / missing document / …
- [x] Patient sees the summary to be shared and consents
- [x] Admin Human Queue: claim (conflicting claims blocked), view sources, add alternatives/notes
- [x] Automation writes suspended while a human owns the workflow; resolution audited
- [x] Explicit return to a safe state; final booking still needs patient approval
- [x] Operator unavailable → pending ticket + directory contact, no pretend live agent

→ Scenarios 5 (timeout after commit) and 7 (no specialist → HITL) reproducible; stale-worker and double-booking tests green under fault injection.

---

**Done** — `services/workers` (`referraleasy_workers`), **264 tests (69 integration, 0 skipped)**,
mypy clean, ADR-0035. The two-replica and lease-expiry claims use **two real connections** with
distinct `pg_backend_pid()`, asserted by a test rather than assumed.

**The type checker found a real bug on its first run, in code that had already passed its own suite:**
`booking.cancel` compared the facility's state to `BookingState.cancelled` — a member that does not
exist (it is `CANCELLED`). `make typecheck` would have caught it on day one had the gate worked. That
is the argument for the gate, made by the gate.

Writing the regression pin for that bug then caught a **second** defect: `create_booking` read
`booking_request_id`/`booking_state` from `booking` and a top-level `detail`, but missed
`error.detail.*` — which is exactly where the **outcome-unknown envelope** carries them. So the view
reported `None` for a facility that had just named the booking and its state, in the one case
reconciliation exists for. Both are fixed and mutation-verified (reverting each fails tests).

**A caveat on the earlier report:** the agent concluded the `test_commands.py` collision I raised "was
probably stale bytecode". It was not — I observed it directly, and it was resolved by the rename in
this same session. The conclusion is right and the explanation is wrong, which is worth recording
because a wrong explanation is how a fixed bug gets reintroduced.

**Unchanged and still true:** the booking facility and the notification sink remain in-process
doubles by design; the real ones are the mock API's.


## Phase 6 — Hybrid RAG and dependency outages (§21 step 6, §10)

- [x] Directory index in OpenSearch, separate from telemetry indices
- [x] Entity-based chunks preserving name ↔ phone relations; metadata `entity_id`, `source_id`, `knowledge_version`, `updated_at`
- [x] Same model, dimension and preprocessing for document and query embeddings; new index on provider/dim change
- [x] Pipeline: same filters on both arms → BM25 top20 + vector top20 → dedup + RRF → rerank top10 → sourced top 3–5
- [x] Language/specialty/hospital filters; distance from coordinates, never from embedding score
- [x] Structured network records cannot be overridden by retrieved text
- [x] Degradation ladder with visible `degraded` flag: reranker down → RRF; embeddings down → BM25; OpenSearch down → versioned seed JSON, deterministic, no LLM; no source → HITL
- [x] Directory never asserts availability or booking confirmation
- [x] Index/embed init step for `make demo`
- [x] Retrieval eval set (recall@k, MRR) with rerank on/off

**Done** — `packages/rag`, 171 tests, of which **13 run against a live OpenSearch** (`make
test-integration-rag` — the only integration tests in the repo with no CI job; a service container
for OpenSearch is fiddly enough that an unverifiable job would be worse than a documented gap).
ADR-0029.

**Retrieval evaluation** (10 queries: 4 lexical, 6 paraphrase, 4 filtered; live embeddings):

| Configuration | MRR | recall@1 | recall@3 | recall@5 | hit_rate |
|---|---|---|---|---|---|
| hybrid, rerank on (reranker 404 → `hybrid_rrf`) | 0.950 | 0.733 | 1.000 | 1.000 | 1.000 |
| hybrid, rerank off (`hybrid_rrf`) | 0.950 | 0.733 | 1.000 | 1.000 | 1.000 |
| seed only, no index, no model call | 0.650 | 0.383 | 0.750 | 0.950 | 1.000 |

Rerank-on and rerank-off score identically **because the reranker is unavailable** — the
`cohere/rerank-v3.5` call returns **HTTP 404** from OpenRouter ("0 endpoints out of 1 requested are
available matching your guardrail restrictions and data policy"). So the live ordering effect of
rerank is **unmeasured**; what is proved is the degradation path. Two rows of that table are the
same measurement.

**Also unverified:** Vertex is never exercised (ADR-0006 pins `EMBEDDING_PROVIDER=vertex`, credentials
unresolved; OpenRouter serves the same model at 768 dims). 28 documents is not a corpus, so there is
no ablation isolating the vector arm, and the integration tests run with the offline hash embedder —
they prove the index, both arms and the filters against a real cluster, not the live embedding path.

Two real bugs the ladder tests caught, both of which would have silently defeated it: a refused
connection was classified as "a bug, not an outage" (because `opensearchpy`'s `ConnectionError`
subclasses `TransportError` and the guard ran first) and so escaped the ladder entirely; and the
seed rung matched all 28 documents on a stop word, so a term present in most of the corpus is now
refused as evidence for any document.

→ Scenario 6 (three transient failures → hybrid directory → static fallback) reproducible.

---

## Phase 7 — OTel/OpenSearch analytics, admin, alerts, Scenario Runner (§21 step 7)

### 7.1 Telemetry (§15)
- [ ] **OTel SDK in gateway, agents, mock API, workers** — the package exists and is live-verified,
  but **no service imports it yet**. This is the one bullet of 7.1 still open. The seam is
  `configure_telemetry(...)` plus `outage_guard`; see `packages/telemetry/README.md`.
- [x] ADK GenAI spans plus app spans; no chain-of-thought capture — the package has no API for
  reasoning text, though `extra_fields` would still accept one a service chose to ship.
- [x] W3C trace context over HTTP and A2A, proved by an integration test
- [x] A2A `task_id`/`context_id`/`message_id` as attributes; `conversation_id`, `workflow_id`, `command_id`, `booking_request_id` kept distinct
- [x] New trace + span link after async worker/HITL waits
- [x] PII/secret masking before export; trace ID never a metric label
- [x] Collector → Data Prepper → OpenSearch; bounded buffer + drop metric; retention 7 d telemetry / 30 d eval
- [x] Structured operation events with `event_id` dedup
- [x] Metrics from §15: triggers, attempts/retries, outcome classes (empty ≠ error), API failures, p50/p95/p99 incl. timeouts, tokens/cost, HITL wait/resolve, guardrail decisions, eval scores, feedback

**Done, except adoption** — `packages/telemetry`, 497 tests of which **36 run against the live
collector and OpenSearch**, ADR-0032. The live tests stamp every write with a unique
`deployment.annotation` and scope every read by it, so a zero is a finding rather than someone
else's traffic.

**101 mocked tests passed while every query the layer built was rejected with HTTP 400.** A helper
returned a *named* aggregation where two call sites needed a *body*, so OpenSearch saw an aggregation
type `distinct_events` that does not exist; the mock reproduced the builder's own nesting, so the
bug was invisible from inside. Separately, `a2a_carrier_from_metadata()` called `.get()` on a
protobuf `Struct`, which is *virtually* registered as a `Mapping` — `isinstance` passes, the
annotation reads as satisfied, and the method raises. Every test had passed a plain `dict`. Both were
found by running the live stack, and both have tests using the real types now.

**Unverified, and the honest list:** adoption (above); the exporter is **hand-written** because the
vendor package is not in the shared venv — it is accepted by the live collector but is the piece most
worth replacing; propagation is proved with both halves in one process, not two deployed services;
`feedback()`'s same-millisecond tie relies on document order; an unscoped `outcome_breakdown` counts
one attempt as three records (the caller must filter by `event_names`); pre-fix documents remain in
the older indices; the template's top-level `synthetic` field is unpopulated (the label travels as
`log.attributes.synthetic`).

### 7.2 Admin app (§12)
- [ ] Pages: Overview, Agents & Versions, Trace Explorer, Evaluations, Guardrails, Feedback, Fault Scenarios, Human Queue, Alerts
- [ ] Agents & Versions shows Agent Card, tools/skills/sub-agent versions, sample I/O
- [ ] Filters: day/month/custom range, timezone, agent/version, release/model, synthetic vs live-demo; deployment annotations; sample counts on every chart
- [ ] Feedback: last active thumbs per `response_id`; raw count, like rate and participation rate separate; optional end-of-conversation survey

### 7.3 Alerts, mock email, kill switch (§16)
- [ ] Scheduler rules: terminal failure > 20% with ≥ 20 invocations / 5 min; p95 attempt > 2.5 s over three windows; circuit open; quality drop with enough samples
- [ ] Dedup/cooldown, resolved events, mock email outbox with delivery state; notification failure never loops
- [ ] Kill switch stops booking writes; directory stays read-only available; rollback keeps reconciliation running

### 7.4 Demo Scenario Runner (§12, §17)
- [ ] Select scenario → reset seed → show expected behavior/fault step → link to the real trace and metrics

→ Every scenario run from the Runner links to a real trace and real metrics.

---

## Phase 8 — Evaluation, delivery, README (§21 step 8)

### 8.1 Evaluation (§14)
- [ ] LangSmith tracing, datasets, experiments; worker writes to LangSmith and OpenSearch idempotently under one `eval_run_id`
- [ ] Offline replay on a separate fixture DB; never recreate live bookings
- [ ] Offline dataset of ~40–60 cases covering all scenarios
- [ ] Quality metrics: correctness, groundedness, completeness; deterministic rules first, judge second; N/A and `eval_error` not scored as zero
- [ ] Outcome metrics: right patient/doctor/slot, constraints satisfied, tool/argument correctness, recovery success, appropriate escalation, unnecessary delegations, latency, cost per success; multiple valid tool paths accepted
- [ ] Unapproved or double booking = critical violation and release blocker
- [ ] Judge calibrated on a small human-labelled set
- [ ] Synthetic patient simulator (intent, wrong/missing answers, preference change)
- [ ] Online: 1% via `HMAC(seed, turn_id)` Bernoulli; batches at 00:00/12:00 America/Chicago; seeded shuffle; no reselection; zero samples shown as "no data"
- [ ] Critical events: 100% deterministic checks + separate risk-review queue; synthetic traffic labelled
- [ ] Every result stamped with all versions from §14 and `trace_id`; workflow scores not attributed to a single specialist
- [ ] Charts show sample/eligible/N/A/error counts; different judges/rubrics never merged into one series
- [ ] Comparison modes: single agent vs multi-agent, rerank on/off, guardrail layer combinations
- [ ] Eval CLI requires release manifest + dataset/rubric version and writes resolved versions into the report

### 8.2 Version regression scenario (§20 row 8)
- [ ] Change a description/skill → CI fails without version bump → bump → eval old vs new → chart → mock alert

### 8.3 Delivery (§18)
- [x] `make demo`: env validation → compose up → apply index templates → migrate → seed → index/embed init → health checks → print URLs. `scripts/demo.py` is an orchestrator, not a reimplementation: it calls the five scripts that already existed and `infra/opensearch/apply-index-templates.sh`, and it prints a URL only for a service the health probe reported answering. Two defects the orchestration surfaced are fixed with it — `migrate.py` took `$DATABASE_URL` from an environment nothing populated (so `make migrate` failed from a checkout), and `seed.py` compared the mock API's `source_directory` as an absolute path across the container boundary, which failed against the shipped compose configuration.
- [ ] Profiles `core`, `showcase`, optional `dashboards`; clearly labelled offline-fixture profile with canned model/embedding
- [ ] Pinned images, volumes, healthchecks, resource limits; measure real RAM and document it
- [x] Cloud Run design ADR: stateless handlers, shared session/task, graceful shutdown, concurrency/min/max, worker as separate service — `docs/adr/ADR-0040-cloud-run-design.md`. Design only, and it says so. Four things do not hold and are named there: the stream hold (25 s) can outlast Cloud Run's grace period, so `timeout_graceful_shutdown` or the hold has to be tuned; the §16 kill switch is process-local and stops bookings on one replica; §9's session identity is not implemented for that profile, so a deployed gateway has no way to obtain a session; and the concurrency/min/max numbers are undesigned because they are measurable only after a deployment.
- [ ] Runbooks in `docs/runbooks/`; scenario docs in `docs/demo-scenarios/`
- [ ] README maps each technology → scenario → code module → trace/eval evidence (not a plain list)
- [ ] Demo recordings of all eight scenarios

---

## Definition of Done (§21)

| # | Scenario (§20)          | Ready after | Evidence to link in README                   |
|---|-------------------------|-------------|----------------------------------------------|
| 1 | Standard booking        | Phase 3     | Trace, A2UI SSE events, receipt              |
| 2 | Multi-constraint search | Phase 4     | Sub-agent spans, effective skills, alternatives offered |
| 3 | Network conflict        | Phase 4     | Coverage evidence IDs, unknown shown, guardrail log |
| 4 | Missing document        | Phase 4     | Suspend/resume trace, single check rerun     |
| 5 | Timeout after commit    | Phase 5     | One booking row, reconciliation span         |
| 6 | Three transient errors  | Phase 6     | Retry counts, circuit open, fallback level   |
| 7 | No specialist found     | Phase 5     | Handoff ticket, claim, audit, safe return    |
| 8 | Version regression      | Phase 8     | CI failure, eval comparison chart, mock alert |

- [ ] All eight scenarios reproducible from the Scenario Runner with real task/trace/result
- [ ] Every `[!]` invariant above has a passing automated test
- [ ] Model/schema provider contracts tested; version changes visible in CI; eval reports the exact versions evaluated

---

## Open questions to settle early

- [ ] Language of the patient UI and agent responses (spec is Turkish, fixtures use miles/ZIP/America/Chicago — Turkish-speaking patients in a US setting?)
- [ ] Web framework for `apps/web` (not named in the spec); choose it to fit the A2UI renderer pinned in S3
- [ ] Whether Cloud Run is deployed or design-only for the portfolio
- [ ] Budget ceiling for daily demo token spend (§8)
