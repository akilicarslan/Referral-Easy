# Production readiness programme

ReferralEasy stays a demo: synthetic data, local `docker compose`, Cloud Run design-only (ADR-0040),
roughly $10/day of model spend, English-only UI. This programme makes it **production-safe by default**
and puts the demo conveniences behind an explicit, loud opt-in profile. Every control comes with a test
that can fail.

It answers the agent release checklist audit of 2026-11-03. Under the checklist's own blocker rule
(sections 1–3, cross-tenant access, or an unapproved irreversible action), that audit was a **no-go**.

| Audit result (40 items) | Covered | Partial | Gap | N/A |
|---|---|---|---|---|
| 2026-11-03 | 5 | 26 | 7 | 2 |

## Release blockers

| ID | Blocker | Closed by |
|---|---|---|
| B1 | `APP_ENV` defaults to `demo`, which registers admin/patient token-minting routes that need no credential | WS1 S1 |
| B2 | The hospital POST happens before the authoritative approval check | WS4 S2–S5 |
| B3 | The mock hospital API stores an `approval_id` without verifying it | WS4 S6 |
| B4 | IDOR: `inspect_document` / `/documents/*` read any patient's document | WS2 S3, S6 |
| B5 | IDOR: cancel/reschedule accept any `booking_request_id` | WS2 S4, WS4 S8 |
| B6 | One static A2A token and an unsigned envelope; no user identity on any hop | WS1 S3–S7 |
| B7 | A delegated child gets the parent's full budget in ADK mode (~108 model calls per turn) | WS3 S3 |
| B8 | Prompt and patient text reach OpenSearch; span events are exported unmasked | WS5 S1–S3 |

## Workstreams

| WS | Plan | ADR | Closes | Slices |
|---|---|---|---|---|
| WS1 | [Identity, authorization & A2A trust](WS1-identity-authorization-a2a-trust.md) | 0043 | 1.1, 1.5, 1.6, 1.7, 2.1 · B1, B6 | 9 |
| WS2 | [Resource ownership & tool contracts](WS2-resource-ownership-tool-contracts.md) | 0044 | 1.2, 1.3, 4.3 · B4, B5 | 7 |
| WS3 | [ADK plugin stack, budget & circuit breaker](WS3-plugin-stack-budget-circuit-breaker.md) | 0045 | 1.4, 3.1, 3.3–3.5, 3.7, 3.8, 6.3 · B7 | 10 |
| WS4 | [Booking side-effect safety](WS4-booking-side-effect-safety.md) | 0046 | 5.1 · B2, B3 | 9 |
| WS5 | [Secrets & telemetry redaction](WS5-secrets-and-telemetry-redaction.md) | 0047 | 2.2–2.5, 6.4 · B8 | 10 |
| WS6 | [Untrusted content, egress & tenancy](WS6-untrusted-content-egress-tenancy.md) | 0048 | 4.1, 4.2, 4.4, 4.5 | 9 |
| WS7 | [Durable state & context](WS7-durable-state-and-context.md) | 0049 | 5.4, 5.5 (5.2, 5.3 regression-locked) | 8 |
| WS8 | [Evaluation, red team & alerting](WS8-evaluation-redteam-alerting.md) | 0050 | 6.1, 6.2, 6.3, 6.5, 6.6 | 9 |
| WS9 | [Release operations & data lifecycle](WS9-release-operations-data-lifecycle.md) | 0051 | 7.1–7.4 | 11 |

After the programme, 3.2 stays N/A (there are no `LoopAgent`s) and 3.6 was already covered by
deterministic `HUMAN_HANDOFF`.

## D0: production execution path. **Decided 2026-11-03: Option A, deterministic spine with agentic leaves**

**The production journey never calls a model.** `dispatch.py:195` always sends `execution_mode=workflow`, and
autonomous mode skips the ADK path (`commands.py:1724`). The ADK agents, and therefore WS3/WS5/WS6's plugins, only run on
direct tasks and in eval. A reviewer will find this.

| Option | What it shows |
|---|---|
| **A. Deterministic spine, agentic leaves (recommended)** | The gateway state machine stays the authority. Selected specialist roles (matching, readiness) run the ADK path in production under a release flag, canaried through WS9, with workflow mode as the automatic fallback. Plugins, budget, eval and red team then exercise real model traffic. |
| B. Workflow-only production | Honest and cheap. The ADK stack becomes a showcase that is never on the production path, which undercuts most of this programme. |
| C. ADK everywhere | Maximum agentic surface, but cost and latency risk against $10/day and the 30 s turn, and booking must stay deterministic anyway. |

### What Option A commits to

- **Agentic leaves.** `provider_matching` (with its `candidate_researcher`/`constraint_analyst` sub-agents) and
  `readiness` run the ADK path in production. `coverage_network` follows once it has eval coverage.
- **Deterministic spine.** `intake`, `scheduling` and the gateway state machine stay deterministic.
  `create_booking` is never reached from a model-chosen path; it remains a workflow step behind WS4's
  dispatch claim.
- **Fallback is automatic and deterministic.** Any ADK refusal, budget or spend exhaustion, open breaker,
  schema-invalid output, or taint that removes required tools makes the gateway re-run that step in
  workflow mode. The decision trace records the fallback. A fallback never starts another model loop
  (checklist 3.6).

### D0 slices

These are new and owned by this index. They land in Wave 2, after the prerequisites listed below.

| Slice | Change | Prerequisites |
|---|---|---|
| **D0-S1 Per-role execution flag** | The release manifest gains `execution: {role: {mode: adk\|workflow, canary_percent, shadow}}`. `effective.py` resolves it per workflow and pins it in `release_pin`, so a workflow never switches mode mid-flight. | WS9 S6 (runtime registry) |
| **D0-S2 Dispatch honours the flag** | `dispatch.py:195` stops hard-coding `execution_mode=workflow`. ADK mode receives the per-role budget profile and an ActorContext. The deterministic fallback above is implemented in `coordination.py` `_recover` routing, keyed on WS3's `failure_class`. | WS1 S4, WS3 S2/S5/S8 |
| **D0-S3 Shadow mode** | With `shadow: true`, the workflow result is authoritative. The ADK result runs within budget, is never shown or acted on, and is diffed against the workflow result: candidate overlap, readiness verdict agreement, citation validity. Diffs go to WS8's online-eval index. | WS8 S7, WS3 S4 (ledger) |
| **D0-S4 Canary and promote** | Shadow, then canary at 10%, then 50%, then 100% per role, using WS9's admin canary API. WS8's `quality_drop`/`terminal_failure_rate` per release drive WS9 S7 auto-rollback to `mode: workflow`. | WS9 S7, WS8 S8 |
| **D0-S5 Demo visibility** | The admin workflow view shows each step's execution mode (adk, workflow, or fallback with its reason), with a link to the Langfuse trace. `make demo` seeds one workflow of each kind. | D0-S2 |

**Consequences for the other plans.**
- WS3's per-turn caps and the $0.10/turn ceiling now apply to production traffic for these roles.
- WS8's T1 live eval and red team target `provider_matching` and `readiness` as the production surface.
- WS6's envelope and taint rules are on the production path, because readiness reads patient documents.
- WS2's ownership resolver protects production traffic as well as eval.

## Binding cross-plan resolutions

The plans were written in parallel. Where they disagree, this section wins, and each plan should be
amended when its slice is picked up.

1. **Migration numbering.** Several plans claimed `020`. Use this global order (it follows the waves):

   | # | File | Owner |
   |---|---|---|
   | 020 | `control_switch.sql` | WS9 S3 |
   | 021 | `booking_dispatch.sql` | WS4 S2 |
   | 022 | `actor_jti.sql` | WS1 S3 |
   | 023 | `model_spend_ledger.sql` | WS3 S4 |
   | 024 | `circuit_breaker.sql` | WS3 S6/S10 |
   | 025 | `adk_session_store.sql` | WS7 S1 |
   | 026 | `agent_suspension.sql` | WS7 S4 |
   | 027 | `workflow_payload_revision.sql` | WS7 S5 |
   | 028 | `tenant.sql` | WS6 S8 |
   | 029 | `online_eval.sql` | WS8 S7 |
   | 030 | `alert_rules.sql` | WS8 S8 |
   | 031 | `release_rollout.sql` | WS9 S6 |
   | 032 | `retention.sql` | WS9 S10 |
   | 033 | `patient_erasure.sql` | WS9 S11 |

2. **One kill-switch store.** WS4's single-row `booking_halt` is dropped. WS4's dispatch-claim function
   reads WS9's `control_switch` (scope `global` or `tool:create_booking`). WS9 S3 therefore lands before
   WS4 S2.
3. **Plugin order and the short-circuit rule.** ADK 2.12's `PluginManager` stops at the first non-`None`
   return and re-raises plugin exceptions as `RuntimeError` (verified in `plugin_manager.py:301-322`).
   The canonical order is fixed in `plugins.PLUGIN_ORDER`:
   `Identity → ToolPolicy → Budget → CircuitBreaker → UntrustedContent → Screening → Redaction → Provenance`.
   - **Refusing plugins** (the first four, plus Screening on block) raise typed errors. The handler unwraps
     them so a refusal is never reported as `terminal_error` (WS3).
   - **Transforming plugins** (UntrustedContent, Redaction) mutate in place and return `None`.
   - **Provenance** stamps through a span processor (WS9), so a refused call still carries provenance.
4. **Authoritative enforcement lives in `ToolSet.invoke`, not only in plugins.** The workflow path never
   runs an ADK runner. `ToolGate.check(tool, *, actor)` (WS1) plus the ownership resolver (WS2) and the
   control-switch snapshot (WS9) run in `invoke`. ToolPolicyPlugin is the ADK pre-flight that turns a
   refusal into model-visible feedback.
5. **ActorContext v1 claims** (WS1) gain a `tid` (tenant) claim. It is optional until WS6 S8 lands and
   required after. Session `user_id = t:{tid}/w:{workflow_id}` (WS6/WS7). Nothing is written to `app:` state.
6. **Redaction must not break tools** (WS5 S4/S5). Only free-text personal data (names, DOB, phone,
   email, address) is tokenised before the model. System ids (UUIDs, evidence ids) are never tokenised,
   because tools and citation checks need them. Patient-facing text is re-identified in `after_model` only
   on the patient channel.
7. **One spend ledger.** WS3's Postgres ledger enforces $10/day, $0.50/workflow and $0.10/turn. WS8's
   live eval ($2/day) and online sampler charge the same ledger under their own `purpose` key, so the
   daily cap is global. WS3's BudgetPlugin is the first caller of the unused `model_usage` emitter
   (`events.py:655`), which WS8's spend alert needs.
8. **Cancel/reschedule.** The model never supplies booking ids (WS2 S4). Both actions need their own
   approval and dispatch capability (WS4 S8) and stay deny-by-default until a patient cancel flow exists.

## Delivery waves

Each slice is PR-sized. Inside a wave, slices from different workstreams touch different files and can
run in parallel.

| Wave | Goal | Slices |
|---|---|---|
| **0: Foundations** | Remove the default-open config; set up the seams | WS1 S1 (B1) · WS3 S1, S7, S9 · WS4 S1 (xfail tests that pin B2/B3) · WS2 S1–S2 · WS5 S7 · WS9 S3 |
| **1: Release blockers** | Close B2–B8 | WS2 S3–S4 (B4, B5) · WS4 S2–S6 (B2, B3) · WS1 S3, S4, S7 (B6) · WS3 S3 (B7) · WS5 S1–S3 (B8) |
| **2: Plugin stack & controls** | Make every checklist control real; D0-S1/S2 at the end of the wave, D0-S3–S5 in Wave 3 | WS1 S2, S5, S6, S8 · WS2 S5–S7 · WS3 S2, S4–S6, S8, S10 · WS5 S4–S6, S8–S10 · WS6 S1–S6 · WS7 S1–S6 · WS9 S1, S2, S4, S5 |
| **3: Assurance & operations** | Prove it and run it | WS8 S1–S9 · WS6 S7–S9 · WS7 S7–S8 · WS9 S6–S11 · WS4 S7–S9 · WS1 S9 |

**Exit criteria:**
- **Wave 1:** WS4's "refusal ⇒ zero POSTs" tests and WS2's cross-patient tests turn from `xfail` to pass, and `APP_ENV` unset fails startup.
- **Programme:** WS8's red-team suite passes in both its scripted-attacker and live modes, the canary-token CI job finds nothing, and re-running the 40-item audit gives no Partial or Gap in sections 1–3.

## Wave 0 status (2026-11-03)

Wave 0 is implemented and the full suite, `ruff` and `mypy` pass. Each control has a test that would
fail without it; the pinned blockers are strict `xfail` tests, so the Wave 1 fixes cannot land
without the markers being removed.

| Slice | Landed | Proving tests |
|---|---|---|
| WS1 S1 (B1) | `APP_ENV` is required, with no default. The profiles are `production`, `demo` and `test` (`ci` is an alias for `demo`). The fixture profiles refuse to start on Cloud Run, with `SECRET_PROVIDER=gcp`, behind a public https `GATEWAY_URL`, or with `SYNTHETIC` not true. The profile is reported in `/healthz` and in the `X-ReferralEasy-Profile` header. The kill switch takes its actor from the session, not from the body. | `tests/gateway/test_profile.py`, `test_kill_switch.py::test_the_switch_is_attributed_to_the_session_not_the_body` |
| WS3 S1 | `plugins/` provides `PLUGIN_ORDER`, `build_plugin_stack()` (validates order, uniqueness and scope), `TurnScope` and `unwrap_plugin_error()`. The handler runs every turn through `App(plugins=…)` with a `RunConfig.max_llm_calls` backstop and an `asyncio.timeout` set from the turn budget. | `tests/agent_runtime/test_plugin_stack.py`, `tests/agents/test_plugin_stack_turn.py` |
| WS3 S7 | `run_with_retries` is the only retry loop (the workflow's private backoff is gone). Delegation and booking writes are single-attempt. LiteLLM `num_retries`/`max_retries` are pinned to 0. | `test_workflow_execution.py` (three new tests), `test_model_factory.py::test_litellm_retries_pinned_to_zero` |
| WS3 S9 | Agent cards set `streaming=False`, which matches the routes actually served. | `test_agent_cards.py::test_card_streaming_matches_served_routes` |
| WS4 S1 | B2/B3 are pinned: a refused dispatch must send zero hospital POSTs, and `create_booking` must not write without a capability. Halt already passes; five dispatch cases and two capability cases are strict `xfail`. | `tests/gateway/test_dispatch_refusal.py`, `tests/agents/test_booking_capability.py` |
| WS2 S1–S2 | `tools/argtypes.py` holds constrained, classified id/vocabulary/text types. All 50 parameters are retyped, `ToolSpec.args_model()` uses `extra="forbid"`, and `ToolSet.invoke` refuses a bad argument with `ArgumentRejected` before any transport call. | `tests/agents/test_tool_arg_contracts.py`, `test_tool_surface.py` (strict `xfail` for WS2 S4's removals) |
| WS5 S7 | New `packages/secret-provider` (import `referraleasy_secrets`): the `SecretName` inventory, plus `File`/`Env`/`GcpSecretManager` providers behind one interface. `from_profile()` fails closed without `APP_ENV`, and env secrets are refused outside the fixture profiles. File reads use a 60 s TTL, and a `.previous` version gives rotation an overlap. | `tests/secret_provider/test_provider.py` |
| WS9 S3 | `020_control_switch.sql`: `control_switch` plus the append-only `control_switch_event`. Both are written only through `control_switch_set()`/`control_switch_release()`; a direct write raises `RE009`. The actor comes from the transaction. Each change writes an audit row and NOTIFYs `control_switch` with an epoch. | `migrations/tests/test_control_switch.py` |

**Deviations from the plans** (amend each plan when its next slice is picked up):

- **WS1 S1:** the `test` profile keeps the fixture auth routes, because the suites need them. It is subject to the same deployment guards as `demo`. The admin `scp` scopes and the `admin_access` audit table are deferred. They need a migration, and the table above has no slot for one yet, so a number and a slice must be assigned before Wave 2.
- **WS2 S1–S2:** argument validation is already enforced in `ToolSet.invoke`, rather than waiting for S3. S3 still owns ownership resolution: the `OWNED` resolvers are named and gated by test, but not yet run.
- **WS4 S1:** the "approval expires between send and record" orphan case moves to S3, because its precondition needs S3's dispatch claim.
- **WS9 S3:** this slice is the store only. The `SwitchBoard` protocol, `PostgresSwitchBoard` (LISTEN plus TTL) and the gateway wiring land with S4. Releasing a switch is its own function (`control_switch_release`), which deletes the current row and appends a `released` event, rather than being a fourth mode.
- **WS5 S7:** no service reads secrets through the provider yet. Migrating the readers is WS5 S8. The package directory is `packages/secret-provider`, not the plan's `packages/secrets`, and its tests are in `tests/secret_provider/`. The repository's `.gitignore` (`secrets/`), `.dockerignore` (`**/secrets`) and `check_public_release.py` all treat a directory named `secrets` as private, and those guards stay as they are.

## New findings from planning (not in the audit)

- The caller chooses the patient: `require_patient_id` reads `payload["patient_id"]` first (`tools/call.py:233`). (WS1)
- The mock API checks no credential at all, so sending it the A2A token is pure leakage. (WS1)
- Three more mock routes have no patient scoping (`GET /documents?patient_id`, `GET /appointments/:id`, `GET /appointments?patient_id`), and CI never runs the mock API's vitest suite. (WS2)
- An approval that expires during a slow hospital call leaves an unrecorded appointment. `book()` and the 008 trigger re-check expiry after the POST. (WS4)
- ADK records prompt content by default, so OpenSearch gets prompt text even with Langfuse off. The HTTP middleware records each exception twice, unredacted. (WS5)
- Services connect to Postgres as a superuser, which makes any RLS meaningless. ADK 2.12 ships a `ModelArmorPlugin` (`google/adk/integrations/model_armor/`). (WS6)
- The replay eval runs only after a version bump, not on every PR. `model_usage` has no emitter caller. (WS8)
- The admin release filter queries `agent.release`, which nothing writes, and the agents image has no `registry/` folder. (WS9)
- The agent-side suspension store is only ever written; `resume()` is never called. A redelivered command that suspends again crashes with `SuspensionError`. `DatabaseSessionService` needs `sqlalchemy[asyncio]`, which is not installed. (WS7)
- A 429 from the agent's model is caught (`app.py:245-254`) but reported as `terminal_error`. LiteLLM retries are 0 by accident, with nothing pinning that. (WS3)

## Portfolio narrative

> *Deterministic spine, agentic leaves.*
>
> - A state machine owns every irreversible transition.
> - Agents act only through tools that re-derive identity from a gateway-signed ActorContext, resolve ownership before they run, and spend from one turn budget.
> - Irreversible writes need a reserved, single-use capability that the downstream system verifies itself.
> - Untrusted text reaches the model tagged as data, and a turn that read it loses its write tools.
> - Every control has a test that can fail, an alert when it trips, and a provenance stamp on the trace that shows which release made the decision.
