# ADR-0030 — Agent runtime: one image, seven specialists, and one owner per budget

- **Status:** Accepted
- **Date:** 2026-10-14
- **Phase:** the shared runtime behind the seven ADK specialists (§3)
- **Depends on:** §3 (agent roles), §7 (workflow states and the policy table), ADR-0002 (Google ADK
  with LiteLLM over OpenRouter), ADR-0003 (A2A exposure), ADR-0028 (skill packages grant no
  authority), `referraleasy_contracts` (the result envelope and the A2A projection)

## Context

Seven specialist agents — `care_coordinator`, `referral_intake`, `coverage_network`,
`provider_matching`, `referral_readiness`, `scheduling`, `recovery_handoff` — are built on one
runtime. They differ in prompt, tools and skill packages; they must not differ in how they spend
time, how they retry, how they authenticate, how they project a result onto the A2A protocol, or how
they decide whether a tool may run. If any of those were per-agent, seven copies would drift and the
drift would only be visible in production, under load, on the copy nobody re-read.

Six hazards shaped the decisions below, and each is a plausible "small shortcut":

1. **The retry multiplier.** Three attempts is a contract. It becomes nine if httpx retries once, the
   A2A client retries once and the model SDK retries once, while every counter still reads three —
   the budget appears enforced and is not.
2. **The terminal state.** `outcome_unknown` — a booking write that may or may not have committed —
   maps to the protocol's `failed`, which is terminal. Retyping that mapping somewhere else is how a
   double booking becomes possible: `failed` says "do not retry", and a second, well-meaning table
   that maps it to a retryable state would resume an unresolved write.
3. **The new skill that grants a capability.** Tool authority lives in an allowlist and in
   `policy_rule` keyed by `(agent_role, tool_name, workflow_state)`, default deny. If a document
   shipped beside a prompt could name a tool, writing Markdown would widen authority.
4. **The held call.** A turn has a 30-second deadline; a patient choosing a hospital may take a day.
   Keeping an LLM stream open across `needs_input` is either a promise the runtime cannot keep or a
   bill for time in which nothing happens.
5. **The self-declared identity.** An A2A caller sends `user_id` in the payload. Honouring it means
   any caller who can reach the service can act as any patient.
6. **The trace id as a credential.** A correlation id is in every log line. Treating it as
   authentication is one comparison away and looks like a convenience.

## Decision

`packages/agent-runtime` is a single installable package, `referraleasy_agent_runtime`, with no
specialist-specific code. Each specialist supplies its own prompt and skill packages; everything
below is shared.

### One image, seven services

The container is role-less. `AGENT_ROLE` decides which service the process is: it selects the label,
the allowlist, the skill packages and the service name (`agent-scheduling`,
`agent-care_coordinator`). `python -m referraleasy_agent_runtime` serves it; `main()` is the only
entry point, and `--check` prints the resolved deployment — role, manifest entry, port — without
opening a socket or importing a model SDK, so a container's configuration is verifiable before it
takes traffic.

The HTTP surface is three routes: `/healthz` (open), `/manifest` (guarded — the release record: role,
model, caps), `/tasks` (guarded — one turn). There is no per-role route, and no per-role cap
override: a role that could raise its own ceiling would be a role with a different contract.

### The model factory returns identity beside the model

`build_model()` returns `(model, ModelRef)`. The model is constructed exactly one way — the
spike-tested path of ADR-0002, `LiteLlm(model="openrouter/<vendor>/<model>")` with
`AGENT_MODEL_ID=anthropic/claude-haiku-5.5` — and the `ModelRef` records provider, model id,
parameters, and whether the value came from the environment or from a per-agent override
(`AGENT_MODEL_OVERRIDES`). The reason to return both is auditability: an override that does not
appear in the release manifest is an override nobody can review. The API key is read from the
process environment and never appears in the ref, the manifest entry, or a `repr`.

### The budget is one object, and delegation does not mint time

`BudgetCaps` is the spec's numbers, resolved from the environment and validated at startup: 3000 ms
per attempt, at most 3 attempts **including the first**, a 30 000 ms turn deadline, 8 delegations,
12 model calls and 16 tool calls per turn, a 1500 ms HTTP tool timeout, a 3000 ms fallback path, and
a backoff curve of 150 ms base doubling to a 500 ms maximum.

`TurnBudget` is the only authority on any of them. Three properties are deliberate:

- **Attempts are counted per call site, not per turn.** `next_attempt("sql:list_slots")` returns 1, 2,
  3 and then raises; a different site has its own three. Otherwise a turn that retried two unrelated
  calls once each would refuse the third — the cap would measure the turn's bad luck rather than one
  call's persistence.
- **A delegation shares the parent's deadline and counters.** `with parent.delegation(role) as child`
  hands the child what is *left* of the turn, not a fresh 30 seconds, and the child's model and tool
  calls are the parent's counters. A grandchild cannot mint time the root never had.
- **Exhaustion is a typed outcome.** `BudgetExhausted` carries `CapKind` and projects to
  `retryable_error` — except when it happens at or after a booking write attempt, where it is
  `outcome_unknown` and requires reconciliation. That single flag is what stops a retry from
  double-booking, and it is why the budget object has to know which tool is in flight.

A valid empty result is **not** an error. "No slot matches" is an answer; only an exhausted budget,
a transport failure or a refusal is a failure. The runtime therefore never treats emptiness as
retryable, and never retries a call to obtain a non-empty answer.

### One layer owns retries

`run_with_retries` in `retries.py` is that layer, and it is the only one:

- httpx is built with `retries=0` (`build_http_client` returns the client *and* its `HttpProfile`, so
  the claim is inspectable at runtime rather than asserted in a comment);
- the A2A client passes no retry policy of its own;
- the model SDK is called once per attempt.

The curve is exponential from the base, clamped to the max, with no jitter — a call site is
single-flight, so the thundering-herd reason for jitter does not apply and a deterministic delay is
testable. A `Retry-After` from the upstream takes precedence and is **not** clamped to the local
maximum: the upstream knows its own recovery time, and shortening it would only produce another
refusal. `is_transient` is narrow on purpose — a timeout, a transport error, our own
`UpstreamTransient`, and HTTP 429/5xx. A 4xx that is not 429 is a well-formed refusal, and a policy
refusal or configuration error must never be retried, because retrying it spends the budget to
receive the same answer.

### Envelopes and the A2A projection belong to the contracts package

The runtime does not own the outcome vocabulary or the mapping onto the protocol. `outcome`,
`a2a_state` and `requires_reconciliation` come from `referraleasy_contracts`
(`A2ATaskState`, `ResultOutcome`, the projection table), and `results.py` writes an envelope by
calling into it. The runtime holds no local table, and a test asserts that identity — the runtime's
`A2A_TASK_STATE_BY_OUTCOME` *is* the contracts' object — plus three AST guards (no protocol-state
literal in the runtime, no local outcome table, no local projection definition), each with a planted
violation proving it can fail.

The mapping is worth restating because it is not the obvious one:

| application outcome | protocol state | terminal |
| --- | --- | --- |
| `completed` | `completed` | yes |
| `no_match` | `completed` | yes |
| `needs_input` | `input-required` | no |
| `retryable_error` | `failed` | yes |
| `terminal_error` | `failed` | yes |
| `outcome_unknown` | `failed` | yes, and requires reconciliation |

`no_match` is a successful turn. `retryable_error` is terminal *at the protocol level* — "retryable"
describes the application's intent, not the task's state. And `outcome_unknown` is `failed` while
the booking is unresolved, which is exactly why the envelope always carries the application outcome
beside the protocol state: the protocol state alone would say "finished" about a write that may have
committed. A refusal is not an outcome at all — it is a protocol event (`policy_refused` → `rejected`,
`patient_cancelled` → `canceled`, `auth_failed` → `auth-required`).

Failure of a turn is not an HTTP status. An unclassified crash is a 200 whose task state is `failed`,
and the exception text is never echoed to the caller (a `postgres://` connection string once appeared
in a test that proved it).

### Tool authority is two checks in a fixed order, in one place

`ToolGate.check(tool, workflow_state)` first consults the role's allowlist in `roles.py` — data, not
logic: `scheduling` is the sole holder of booking write authority (`create_booking`,
`cancel_booking`, `reschedule_booking`), `care_coordinator` holds nothing but `delegate_task`. Only
if the tool is allowed does the gate ask the database's own `policy_assert(role, tool, state)`. A
tool outside the allowlist costs no database round-trip.

The gate is default-deny: no authority, no workflow state, or no policy backend is a refusal, not a
permission. A refusal from any backend arrives as the same typed `PolicyRefused` — the gate
translates, and the SQL adapter translates too, precisely so that a backend raising the domain's own
`PolicyDeniedError` cannot escape untranslated while an unexpected error (a dropped connection) is
re-raised untouched, because a transient failure must stay transient.

A skill manifest may declare tools. `select_skills` records the claim, the trace entry writes it as
`declared_tools_not_granted`, and the gate never reads it. Adding a skill grants no tool permission —
ADR-0028's boundary, enforced here from the other side.

### Service auth: the token is the credential, and nothing else is

`A2A_SERVICE_TOKEN` is verified as a bearer token at the transport, in a FastAPI dependency that runs
before the handler. `BearerTokenVerifier.verify(authorization)` takes the `Authorization` header and
nothing else — no `trace_id`, no payload, no `**kwargs` — and a test reads the signature to keep it
that way, because any additional parameter would be a caller-controlled input on the authentication
path. A missing token is `401` with `WWW-Authenticate`; a blank configured token refuses to start,
since it would authenticate everyone. The principal carries a 12-character fingerprint, never the
token. Every identity claim in the payload is dropped and *reported*, because a payload that names
its own `user_id` is either a bug or an attack and both deserve to be visible.

### Skill selection reads manifests, and records what it chose

Selection is cheap by construction: it reads `manifest.json` for every candidate and **no**
`SKILL.md`. The body is read only after a package is selected, and only for the packages selected.
The effective list goes into the trace, and so do the rejections with their reasons — a skill that
was silently not selected is the hardest kind of missing behaviour to debug. The runtime does not own
`packages/skills`; it loads through a `SkillSource` seam (`SkillsPackageSource` reads the real
package, `EmptySkillSource` refuses, a double counts reads in tests).

### Suspend and resume never hold a call

`needs_input` ends the turn. `suspend_turn` persists a `Suspension` and returns a projection whose
state is `input-required` and whose `protocol_is_terminal` is false; the persisted record carries the
workflow state, the answer options, the skills and the idempotency key, and **no** turn-scoped value
(no deadline, no remaining time, no budget, no lease, no attempt). A test asserts that set of keys is
absent, and a second test asserts structurally that `suspend.py` contains no `async def` at all —
a coroutine there is a call someone could keep open across a human wait. `resume` builds a
`ResumePlan` with a **fresh** full 30 000 ms deadline, because charging the resumed turn for the
suspended one would punish a fast patient. An answer the agent never offered is refused rather than
handed to a model: a choice outside the offered list is a bug or an attempt to change the booking.

### The seven roles' tools are data

`ROLE_TOOLS` transcribes the spec's table, and the test suite states the same table independently and
compares. `booking_write_authority()` returns exactly `{scheduling}`, and the module asserts that
invariant on import: a second role with booking write authority is a double-booking bug, and it
should fail at import rather than in production.

## Consequences

- **The numbers are testable, and are tested.** `test_budget.py` asserts every cap against the spec
  and against the built-in defaults (so a drift in either place fails), that attempts stop at three
  including the first, that a lease shrinks to what is left of the turn, that a child cannot outlive
  or outspend its parent, and that a delegation made by a grandchild still consumes the root's cap.
- **The retry multiplier cannot return silently.** `test_retries.py` asserts the constructed httpx
  transport's `retries` is 0 and that no other layer retries, and asserts backoff values and the
  `Retry-After` precedence.
- **The mapping cannot be duplicated.** `test_a2a_projection.py` asserts object identity with the
  contracts package, pins every row of the table above, and guards the source text with AST checks
  that fail on a planted violation.
- **Authority is defended from both directions.** `test_roles_and_tools.py` states the spec's tool
  table independently, asserts the coordinator's refusal, asserts that a refusal costs no database
  round-trip and no budget, and asserts that a hostile manifest claiming `create_booking` grants
  nothing.
- **The live path is verified, not assumed.** Nine `@pytest.mark.spike` tests call the real model
  through the real factory: tool calling, structured output (`response_schema`, which returns the
  object shape — `response_json_schema` returns the bare value and was wrong here), streaming,
  cancellation, a real call inside `run_with_retries` charging the real counters, and a 1 ms attempt
  budget cancelling a real HTTP call into `BudgetExhausted`. The default run deselects them, so the
  offline suite stays instant and needs no network and no database.
- **The offline suite is deterministic by construction.** A virtual clock, an explicit environment
  dict, and a recording policy double. No test in the default run sleeps, opens a socket, or reaches
  a database.
- **A green run means the tests ran.** The doubles live in `tests/agent_runtime/agent_runtime_doubles.py`
  rather than in `conftest.py`, because a bare `from conftest import ...` resolves against
  `sys.modules["conftest"]` — whichever of this repository's four `conftest.py` files pytest imported
  first — and one test in this package passed when its directory ran alone and failed in the full-suite
  run. Shared state between test directories is not worth the import shortcut.
- **One workspace-wide packaging note.** `referraleasy_agent_runtime` is a workspace member with a
  `[tool.uv.sources]` entry; consumers (the composition root, the specialists' build) depend on it as
  an ordinary package, never through `sys.path` manipulation.

## Alternatives considered

**Letting ADK or LangGraph own the budget and the retries.** Rejected: the caps are a contract that
the trace must report, and a framework's retry policy is configured in its own vocabulary and counted
in its own counters. The runtime implements one retry loop it can assert on.

**A shared base class per specialist with per-role hook methods.** Rejected: seven subclasses of a
runtime base class is how the shared code becomes seven nearly-identical files. The seam here is data
(role → tools, role → skills, role → prompt) plus a handler the deployment supplies.

**Persisting a serialised ADK session across the human wait and re-entering the same call.** Rejected
in favour of suspend/resume: it would keep a model call (or a resumable stream) alive across a wait
measured in hours, and the persisted object would carry the dead turn's budget with it.

**Trusting `user_id` from the A2A payload.** Rejected: the token says which *service* may call; only
the workflow state and the database say which *patient* the turn is about.

**A second outcome→protocol table inside the runtime, "because the contracts one is awkward".**
Rejected explicitly, and now guarded by AST tests: the terminality of `outcome_unknown` is a
correctness property of the booking flow, not a formatting choice.

**Declaring tools in each skill manifest and letting the gate read them.** Rejected: it is a second,
unaudited policy table, and the shortest route to a Markdown file granting a capability the database
denies (ADR-0028).

**A generic `dict[str, Any]` result instead of typed envelopes.** Rejected: `outcome_unknown` and
`retryable_error` differ by one flag with opposite operational consequences, and a dict is where that
flag goes missing.
