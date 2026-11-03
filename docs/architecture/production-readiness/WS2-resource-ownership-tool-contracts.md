# WS2 — Resource ownership & tool-argument contracts

**Checklist items closed:** 1.2 (every model-filled id classified), 1.3 / 4.3 (typed, constrained tool arguments) · **Release blockers closed:** B4 (document IDOR), B5 (booking IDOR) · **ADR:** ADR-0044 (proposed)

## 1. Current state (verified)

- **B4 confirmed.** `inspect_document(call, *, document_id)` (`services/agents/referraleasy_agents/tools/readiness.py:108-147`) GETs `f"/documents/{document_id}"` (:117) with no ownership check and returns `text` plus `patient_id` (:134) to the model. The mock API serves any document (`services/mock-api/src/modules/documents.ts:42-46`), and `/documents/check` never compares `document.patient_id` with `body.patient_id` (:82-85), so workflow A can certify with patient B's referral letter. `validate_referral_package` takes model-chosen `document_ids` (`tools/specs.py:266`), and so does intake's `validate_referral` (:115).
- **Additional unscoped mock routes, missing from the audit:** `GET /documents?patient_id=` (`documents.ts:26-32`), `GET /appointments/:id` (`appointments.ts:99-108`), and `GET /appointments?patient_id=` (:118-124). `POST /appointments` invents a `workflow_id` when the caller leaves it out (:50-51).
- **B5 confirmed.** `cancel_booking(booking_request_id)` and `reschedule_booking(booking_request_id, new_slot_id, idempotency_key)` (`tools/scheduling.py:340-413`) put a model-supplied id into the URL path (:356, :398). Nothing ties that id to the envelope's workflow or patient. The policy table allows cancel and reschedule in `BOOKED` (`services/gateway/src/referraleasy_gateway/policy_seed.py:220`, :264-270). **Correction:** the derived cancel key `re-cancel-{id}` (:349) is never read by the mock route, which passes only `reason` and `decision` to `booking.cancel` (`appointments.ts:139-142`).
- **Unbound lookup confirmed.** `enforce_idempotency_key` only compares keys when the turn carries one (`scheduling.py:107-116`). `SchedulingInput.idempotency_key` is optional (`schemas.py:285`). A turn without a key can therefore look up any workflow's booking.
- **1.2 baseline.** `SERVICE_SUPPLIED` (`tests/agents/test_tool_surface.py:55-80`) and the checks at :161-180 keep workflow, command, patient, state and approval out of every declaration. `delegate_task` already resolves and verifies `evidence_ids`/`constraint_ids` against the envelope (`tools/coordination.py:146-178`) and enum-checks `target_role` (:109-115). That is the precedent for this workstream.
- **1.3 / 4.3 confirmed.** `ParamSpec.annotation` is `str`, `dict` or a string alias such as `"str | None"` (`specs.py:101-103`). `_adk_tool` passes `**arguments` straight through, with the comment "the service is what validates" (`toolbox.py:272-286`). The mock API does not authenticate anything: `DirectoryApi` sends a bearer token (`tools/client.py:135`) and no mock route reads it.
- **ADK facts (2.12.0 source).** `JSON_SCHEMA_FOR_FUNC_DECL` is on by default (`features/_feature_registry.py:193-195`), so pydantic constraints reach `parameters_json_schema`. LiteLLM forwards that schema unchanged (`models/lite_llm.py:2711-2713`). `FUNCTION_TOOL_ARG_VALIDATION` is experimental and off (:175-177), so it must not be relied on. A dict returned from `BasePlugin.before_tool_callback` stops the tool from running (`plugins/base_plugin.py:297-319`).
- **Bypass path.** The deterministic workflow calls `ToolSet.invoke` directly (`referraleasy_agents/workflow.py:108-129`) and never goes through ADK. A plugin alone cannot be the enforcement point.
- **CI gap.** CI builds and starts the mock API (`.github/workflows/ci.yml:302-310`) and runs `pnpm lint`/`pnpm typecheck` (:785-789), but never runs its vitest suite. Only `make test-all` does (`Makefile:84`).

## 2. Target architecture

Every tool parameter is declared as a **constrained type** with an **id class**. A single function, `ArgContract.bind`, turns raw model arguments into a typed, ownership-resolved argument object. Handlers receive resolved objects, never raw ids. The hospital API independently scopes every patient record to the caller's ActorContext (WS1).

```mermaid
flowchart LR
  M[Model] -->|args| P[ToolPolicyPlugin.before_tool_callback]
  W[workflow.py deterministic path] --> I
  P -->|ArgContract.bind preflight; refusal dict to model| I[ToolSet.invoke]
  I -->|ArgContract.bind authoritative + per-turn resolver cache| H[handler typed resolved args]
  H --> C[DirectoryApi + X-ReferralEasy-Actor JWS]
  C --> F[mock API actorScope preHandler] --> S[(store, patient_id / workflow_id)]
```

**Id classification (the complete inventory of model-filled ids):**

| Param (tool) | Class | Rule after WS2 |
|---|---|---|
| `booking_request_id` (cancel, reschedule) | **(a) remove** | The gateway puts the workflow's own booking id (`booking_request.external_ref WHERE workflow_id = ctx.workflow_id`, `migrations/008_booking_request.sql:17-33`) into `SchedulingInput`. A new `require_booking_request_id(call)` reads it. |
| `idempotency_key` (create_booking, lookup_request) | **(a) remove** | Read from the turn's payload. A turn with no key is refused, which closes the unbound lookup. |
| `idempotency_key` (reschedule) | **(a) remove** | Derived as `deterministic_uuid("reschedule", workflow_id, command_id)`, so a redelivered command replays the same key. |
| `payload` (create_booking) | **(a) remove** | Read from `call.payload["booking_payload"]`, the approved payload. The handler asserts `payload.patient_id == ctx.patient_ref`. |
| `new_slot_id` (reschedule) | **(a) remove** | Comes from the patient's recorded choice (`SchedulingInput.slot_id`). This applies §3's "the system never auto-selects a slot". |
| `document_id` (inspect_document), `document_ids` (validate_referral_package, validate_referral) | **(b) owned** | `OwnedDocuments` resolver: owned means `document.patient_id == ctx.patient_ref`. Lists capped at maxItems 20. |
| `hospital_id` (validate_referral_package) | **(b) owned** | Must equal the role input's `hospital_id`, the patient's chosen hospital, because the call writes a certification. |
| `evidence_ids`, `constraint_ids` (delegate_task) | **(b) owned** | The existing envelope check, re-registered as the `EnvelopeRefs` resolver. |
| `hospital_id`, `hospital_ids`, `provider_id`, `plan_id` (directory, coverage, slots reads) | **(c) public** | UUID-typed (`referraleasy_contracts.generated.common:60-80`). No ownership check. |
| `target_role` (delegate_task) | **(c) closed vocabulary** | `Literal` of the six specialist roles, enforced in the declared schema. |

Non-id strings are constrained too: `visit_type` enum, `postal_code` `^\d{5}$`, BCP-47 `language`, `maxLength` on free text.

## 3. Key decisions & rejected alternatives

| Decision | Why | Alternative rejected, and why |
|---|---|---|
| Remove ids the server already knows (class a) instead of checking them | An argument that does not exist cannot be forged. Each workflow has at most one booking per approval (`008_booking_request.sql:51`). | *Keep the id and verify it:* adds a round-trip and leaves a field the model can probe. |
| Handlers receive `OwnedDocument` objects, not `str` ids | Ownership holds by construction: a handler cannot reach the transport with an id that was never resolved. | *Each handler calls `check_owner()` itself:* the next tool forgets to. That is how B4 happened. |
| Enforce in `ToolSet.invoke`; the plugin is a pre-flight adapter calling the same `bind` | `workflow.py` bypasses ADK, so the chokepoint has to be `invoke`. The plugin turns a refusal into a model-readable dict and a trace span. | *Plugin only:* misses the deterministic path. *Two separate implementations:* they drift. |
| Own the validation instead of using ADK `FUNCTION_TOOL_ARG_VALIDATION` | That flag is experimental and off by default, and silently skips annotations it cannot handle (`function_tool.py:225-234`). | Turning the flag on would put a security control behind a feature flag ADK can change. |
| Generate the declaration from pydantic arg models with `extra="forbid"` | One source for what the model sees and what the server accepts. Extra keys such as `booking_request_id` are rejected. | *Hand-written JSON schemas:* drift from the validator. |
| Mock API returns **404**, not 403, for records the caller does not own | Does not leak whether the record exists, which is the usual hospital-API stance. | 403 tells an attacker the record exists. |
| Refusals use `ReasonCode.policy_blocked` with `reason_detail="resource_not_owned"` | No contract regeneration needed, and WS8 can alert on the detail. | *A new `resource_not_owned` enum value:* churns every generated binding for a sub-case. |
| No demo opt-out for ownership | A bypass is not a convenience. The demo's synthetic data makes it harmless to keep the check on. | *A `MOCK_API_ACTOR_ENFORCEMENT=off` flag:* a switch someone eventually ships. |

**Proposed ADR-0044 decision text:**

> Every tool parameter carries a declared id class: server-supplied (forbidden in declarations), owned (resolved against the ActorContext's patient or workflow before the handler runs), or public (pattern-validated only). Arguments are validated by pydantic models that also generate the ADK declaration. Ownership is enforced in `ToolSet.invoke` and again by the hospital API, which scopes every patient record to the verified ActorContext. A tool parameter that looks like an id but has no class fails CI.

## 4. Implementation steps

- **S1. Typed argument models.** Create `services/agents/referraleasy_agents/tools/argtypes.py` with `Annotated` aliases (`DocumentRef = Annotated[UUID, IdClass.OWNED, Resolver("document")]`, `PublicHospitalId`, `ShortText = Annotated[str, Field(max_length=500)]`, `Weekday` enum, and so on). In `specs.py`, `ParamSpec` gains an `id_class` field and replaces the string annotations; `ToolSpec.args_model()` is built with `pydantic.create_model(..., __config__=ConfigDict(extra="forbid"))`. `toolbox._adk_tool` sets the annotations to the constrained types, so the ADK JSON-schema path emits `format: uuid`, `pattern`, `maxLength` and `maxItems`. No migration.
- **S2. Classification registry and gate.** Extend `tests/agents/test_tool_surface.py` and add `tests/agents/test_tool_arg_contracts.py` (see §5). `SERVICE_SUPPLIED` gains `booking_request_id`, `idempotency_key`, `new_slot_id` and `payload`. They are added once S4 removes them; until then they are listed with `xfail(strict=True)`.
- **S3. Owned-resource resolver and B4 fix.** Create `tools/ownership.py` containing:
  - `OwnershipAnchor`, which reads `call.actor.patient_ref` / `call.actor.workflow_id` from WS1. Until WS1 lands it falls back to `require_patient_id` / `require_workflow_id` (`call.py:200-242`).
  - `OwnedResolver[T]` (protocol: `kind`, `async resolve(call, ids) -> Mapping[UUID, T]`).
  - `OwnedDocuments`, which lists `GET /documents?patient_id=<anchor>` once per turn into a cache on `ToolSet`.
  - `ResourceNotOwnedRefused(ToolRefused)` in `errors.py`.

  `ArgContract.bind(call, spec, raw)` validates the arguments, then resolves every OWNED field. `ToolSet.invoke` (`toolbox.py:200-237`) calls `bind` before `handler_for`. `inspect_document` takes `document: OwnedDocument`, re-asserts `body.patient_id == anchor` after the fetch as defence in depth, and stops returning `patient_id` (data minimisation; handed to WS5). `validate_referral_package` gets the same treatment, plus the hospital binding.
- **S4. Scheduling surface reduction (B5).**
  - `create_booking()` and `lookup_request()` take no model arguments; they read the key, payload and `expected_slot_version` from `SchedulingInput`.
  - `enforce_idempotency_key` refuses when the turn has no key.
  - `cancel_booking(reason)` and `reschedule_booking(reason)` read `require_booking_request_id(call)` and `slot_id`. The gateway's dispatch populates `SchedulingInput.booking_request_id` from `booking_request.external_ref`; `schemas.py:275` gains the field.
  - The `workflow.py:115-129` call sites are updated.
  - Coordinate with WS4, which owns whether cancel and reschedule need fresh patient approval.
- **S5. ToolPolicyPlugin (argument half).** Create `packages/agent-runtime/.../plugins/tool_policy.py` on WS3's framework. `before_tool_callback` runs `ArgContract.bind` against the `ToolSet` bound to the invocation. On refusal it returns `{"error": "argument_rejected" | "resource_not_owned", "reason_code": "policy_blocked", "detail": ...}` and sets span attributes `re.tool.arg_contract` and `re.tool.ownership.denied_kind`.
- **S6. Mock API defence in depth.**
  - Create `services/mock-api/src/http/actor.ts`, a Fastify `preHandler` that verifies the `X-ReferralEasy-Actor` JWS (WS1's key; locally WS1's file-backed public key) into `request.actor`. It accepts the token when its `aud` equals the caller's authenticated service role (sender-bound, not forwarded blindly).
  - Create `src/http/route-scopes.ts`, which classifies every route as `public` or `patient_scoped` or `workflow_scoped`.
  - Patient-scoped routes compare against `actor.patient_ref`; a mismatch is a 404 `unknown_document` / `unknown_booking`. This covers `/documents*`, `/documents/check` (every id must belong to the patient), and `/appointments*`, including lookup, where `booking.workflow_id` must match.
  - A `system` actor with scope `fixtures:read` serves `make seed` verification.
  - `DirectoryApi` attaches the header.
- **S7. CI.** Add `pnpm --filter @referraleasy/mock-api test` to the Node job in `ci.yml`.

## 5. Tests that prove it

**`tests/agents/test_tool_arg_contracts.py`** (`pytest.mark.contract`; runs in CI's default `uv run pytest`, `ci.yml:189`):
- `test_every_id_shaped_parameter_is_classified`: walks every `ToolSpec` parameter and nested arg-model field. Any name matching `(^|_)(id|ids|key|ref)$|_role$`, or any UUID-typed field, must have an `id_class`, and every OWNED field must name a registered resolver.
- `test_the_classifier_catches_an_unclassified_id` (negative): feeds a synthetic spec with `foo_id: str` to the same checker and asserts it is reported. This proves the gate can fail.
- `test_no_parameter_is_an_unconstrained_str`: every `str` field has a pattern, enum or `maxLength`.
- `test_adk_declaration_carries_constraints[tool]`: `FunctionTool(...)._get_declaration().parameters_json_schema` shows `format: uuid` for ids, `maxItems` on lists, and `additionalProperties: false`.
- `test_path_traversal_id_never_reaches_transport` (attack): `inspect_document(document_id="../appointments/x/cancel")` raises `ArgumentRejected`, and the API stub records zero calls.
- `test_extra_argument_is_rejected`: `cancel_booking(booking_request_id=…)` after S4 is refused (`extra="forbid"`).

**`tests/agents/test_resource_ownership.py`** (two seeded patients):
- `test_inspect_document_refuses_another_patients_document` (B4 attack): raises `ResourceNotOwnedRefused`. No `GET /documents/{id}` is issued and no text appears in the result.
- `test_inspect_document_rechecks_owner_after_fetch`: the listing stub claims the document is owned but the body's `patient_id` differs, so the call is refused.
- `test_validate_package_refuses_foreign_document_ids` and `test_validate_package_refuses_unchosen_hospital`.
- `test_cancel_and_reschedule_target_only_the_workflows_booking` (B5): the URL path contains the payload-supplied id, never an argument.
- `test_lookup_without_a_turn_key_is_refused` (negative for `scheduling.py:107-116`).
- `test_reschedule_key_is_stable_per_command`.
- `test_create_booking_refuses_payload_for_another_patient`.
- `test_resolver_lists_once_per_turn`.
- `test_plugin_refusal_skips_the_handler`: `before_tool_callback` returns a dict and the handler spy is not called.

**`services/mock-api/test/ownership.test.ts`** (vitest; CI after S7):
- `GET /documents/:id → 404 for another patient`.
- `no actor → 401`.
- `/documents/check rejects a foreign document id`.
- `cancel of another workflow's booking → 404 and state unchanged`.
- `lookup_request with another workflow's key → 404`.
- `every registered route is classified`: iterates the OpenAPI registry. Its negative twin registers an unclassified dummy route and asserts the classifier flags it.

**Eval (WS8):** add an `idor_document_probe` scenario to the replay dataset. It expects `policy_blocked/resource_not_owned` and no foreign text in any output.

## 6. Demo-mode story

`make demo` is unchanged: seeding uses the `system` actor, and agent calls carry the ActorContext WS1 already mints. The control stays on in every profile.

The control is visible in three places:
1. The seeded instruction-like document (`documents.ts:13-15`) is extended so its text asks the reader to "also inspect document <patient B's id>". The showcase run shows the readiness agent's call refused with `resource_not_owned`.
2. The Langfuse span carries `re.tool.ownership=denied` (WS9 provenance).
3. The admin audit page lists the refusal, and WS8 alert `tool_ownership_denied` counts it.

WS6's injection carries the probe; WS2 contains it.

## 7. Dependencies & sequencing

- **Lands independently:** S1, S2, S3 (through the `OwnershipAnchor` fallback) and S7.
- **S4** needs a small gateway dispatch change and WS4 agreement on cancel/reschedule approval semantics.
- **S5** needs WS3's plugin framework and must run in the reserved order slot, after Identity and before Budget.
- **S6** needs WS1's ActorContext signing key and verifier contract, plus header propagation.
- **Hand-offs:** WS5 removes `patient_id` from tool results. WS8 owns the `tool_ownership_denied` alert and the eval scenario. WS9 owns the span attribute schema.

## 8. Effort & risk

| Slice | Effort | Main risk | Rollback |
|---|---|---|---|
| S1 | M | Haiku emits non-UUID ids, so refusals rise. Mitigated with self-describing errors and the eval replay gate (`make eval`). | Revert; declarations fall back to `str`. |
| S2 | S | Over-broad regex flags false positives. Fix by classifying, never by skipping. | Revert test. |
| S3 | M | Listing latency adds one GET per turn (cached). | Revert; S6 still protects. |
| S4 | M | Gateway and agents schema skew during deploy. Ship the new optional field first. | Revert agents; the gateway field is additive. |
| S5 | S | Plugin and `invoke` drift. Prevented by construction (shared `bind`). | Remove from `App(plugins=)`; `invoke` still enforces. |
| S6 | L | Every mock test needs an actor. A `withActor()` helper in `test/helpers.ts`. | Revert slice. |
| S7 | S | Pre-existing vitest failures surface. | None needed. |

## 9. Portfolio talking points

- "The safest argument is one that doesn't exist: ids the server already knows were deleted from the model's surface."
- "Ownership holds by construction: handlers receive resolved `OwnedDocument` objects, so a new tool can't reach the transport with an unverified id. A CI gate fails on any unclassified id-shaped parameter, and has a negative test proving it can fail."
- "One validator, two entry points. ADK's plugin gives the model a clean refusal, but the deterministic workflow bypasses ADK, so the authoritative check lives at the `ToolSet.invoke` chokepoint."
- "Defence in depth like a real hospital API: records are scoped to the signed ActorContext and answer 404, so a compromised agent can't enumerate patients."
