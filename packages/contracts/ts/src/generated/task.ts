// GENERATED — do not edit. Source: packages/contracts/schemas/*.json
// Regenerate with: pnpm gen:contracts
import { z } from 'zod';

export const task$CallBudget = z.object({ "deadline_at": z.string().datetime({ offset: true }).describe("Absolute shared deadline. Parent, sub-agent, tool and model calls all draw from the same remaining budget (§3, §8) — a sub-agent cannot mint its own."), "turn_started_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).").optional(), "remaining_model_calls": z.number().int().gte(0), "remaining_tool_calls": z.number().int().gte(0), "remaining_delegations": z.number().int().gte(0).describe("Bounded by MAX_DELEGATIONS_PER_TURN. At zero the coordinator must stop delegating and answer or escalate."), "attempt": z.number().int().gte(1).lte(3).describe("1-based, first call included. Exactly one layer owns retries (§8) — SDK/HTTP/A2A internal retries are disabled so the budget cannot be multiplied.").optional() }).strict();
export type task$CallBudget = z.infer<typeof task$CallBudget>;

export const task$TaskEnvelope = z.object({ "task_id": z.string().uuid(), "workflow_id": z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15)."), "command_id": z.string().uuid().describe("Deduplicated on replay (§7). The same command_id must never produce a second side effect."), "parent_task_id": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().uuid(), z.null()];
    const { errors, failed } = schemas.reduce<{
      errors: z.core.$ZodIssue[];
      failed: number;
    }>(
      ({ errors, failed }, schema) =>
        ((result) =>
          result.error
            ? {
                errors: [...errors, ...result.error.issues],
                failed: failed + 1,
              }
            : { errors, failed })(
          schema.safeParse(x),
        ),
      { errors: [], failed: 0 },
    );
    const passed = schemas.length - failed;
    if (passed !== 1) {
      ctx.addIssue(errors.length ? {
        path: [],
        code: "invalid_union",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      } : {
        path: [],
        code: "custom",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      });
    }
  }).describe("Set when this task was delegated by another agent. Null for a root task.").optional(), "goal": z.string().min(1).max(2000).describe("What the caller wants, in one instruction. Not a conversation transcript."), "workflow_state": z.enum(["INTAKE","COVERAGE_CHECK","PROVIDER_MATCHING","AWAITING_HOSPITAL_CHOICE","AWAITING_DOCTOR_CHOICE","READINESS_CHECK","AWAITING_SLOT_CHOICE","AWAITING_FINAL_APPROVAL","BOOKING_IN_FLIGHT","BOOKED","BOOKING_UNVERIFIED","RECOVERY","HUMAN_HANDOFF","ABANDONED","CANCELLED","FAILED"]).describe("Workflow state machine (§7). The policy table (§4) defines which agent/tool may run in each state; the coordinator LLM cannot override it.").optional(), "current_state_version": z.number().int().gte(0).describe("The version the caller read. A reply computed against a superseded version is rejected by the write path (§7)."), "constraints": z.array(z.object({ "constraint_id": z.string().uuid(), "field": z.enum(["specialty","max_distance_miles","provider_language","preferred_day","preferred_time_of_day","accepts_new_patients","accessibility","insurance_plan","visit_type"]), "kind": z.enum(["hard","soft"]).describe("`hard` is never relaxed silently. Changing it requires an explicit patient decision recorded on this object (§2.1)."), "value": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.number(), z.string(), z.boolean(), z.null()];
    const { errors, failed } = schemas.reduce<{
      errors: z.core.$ZodIssue[];
      failed: number;
    }>(
      ({ errors, failed }, schema) =>
        ((result) =>
          result.error
            ? {
                errors: [...errors, ...result.error.issues],
                failed: failed + 1,
              }
            : { errors, failed })(
          schema.safeParse(x),
        ),
      { errors: [], failed: 0 },
    );
    const passed = schemas.length - failed;
    if (passed !== 1) {
      ctx.addIssue(errors.length ? {
        path: [],
        code: "invalid_union",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      } : {
        path: [],
        code: "custom",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      });
    }
  }).describe("Typed by `field`: number for max_distance_miles, LanguageTag for provider_language, VisitType, weekday name, time-of-day bucket, boolean, or PlanId.").optional(), "source": z.enum(["patient_stated","referral_document","system_mandatory","inferred"]).describe("Only `patient_stated` may be relaxed by a patient decision; `system_mandatory` (specialty, approval, valid slot) can never be relaxed by anyone (§2.1)."), "relaxable": z.boolean().describe("False for mandatory fields regardless of `kind`.").optional(), "recorded_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "superseded_by": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().uuid(), z.null()];
    const { errors, failed } = schemas.reduce<{
      errors: z.core.$ZodIssue[];
      failed: number;
    }>(
      ({ errors, failed }, schema) =>
        ((result) =>
          result.error
            ? {
                errors: [...errors, ...result.error.issues],
                failed: failed + 1,
              }
            : { errors, failed })(
          schema.safeParse(x),
        ),
      { errors: [], failed: 0 },
    );
    const passed = schemas.length - failed;
    if (passed !== 1) {
      ctx.addIssue(errors.length ? {
        path: [],
        code: "invalid_union",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      } : {
        path: [],
        code: "custom",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      });
    }
  }).describe("Set when the patient explicitly changes a preference (§2.4). The old row is kept so the change is auditable rather than overwritten.").optional() }).strict()).describe("Only the constraints this agent needs. Context minimisation (§6) is enforced at the envelope, not by asking the model to behave.").optional(), "evidence_refs": z.array(z.object({ "evidence_id": z.string().uuid() }).strict().describe("Pointer to a stored tool/source result. The envelope carries references, not payloads (§5).")).describe("References, not payloads. A receiving agent must not be able to treat retrieved text as instructions (§9).").optional(), "allowed_actions": z.array(z.string().regex(new RegExp("^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")).describe("Namespaced action identifier (owner:action). The policy table (§4) maps state → allowed actions; the coordinator cannot exceed it.")).describe("The intersection of the caller's intent and the policy table for this state (§4). An action outside this list is refused by the tool layer even if the model requests it."), "deadline": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "call_budget": z.object({ "deadline_at": z.string().datetime({ offset: true }).describe("Absolute shared deadline. Parent, sub-agent, tool and model calls all draw from the same remaining budget (§3, §8) — a sub-agent cannot mint its own."), "turn_started_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).").optional(), "remaining_model_calls": z.number().int().gte(0), "remaining_tool_calls": z.number().int().gte(0), "remaining_delegations": z.number().int().gte(0).describe("Bounded by MAX_DELEGATIONS_PER_TURN. At zero the coordinator must stop delegating and answer or escalate."), "attempt": z.number().int().gte(1).lte(3).describe("1-based, first call included. Exactly one layer owns retries (§8) — SDK/HTTP/A2A internal retries are disabled so the budget cannot be multiplied.").optional() }).strict(), "release_manifest_id": z.string().regex(new RegExp("^rel-[0-9]{8}-[a-z0-9]{7}$")).describe("Pins the task to an immutable release (§13). A conversation never mixes versions mid-flight, and `latest` is never sufficient provenance for a report."), "patient_details_id": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().uuid(), z.null()];
    const { errors, failed } = schemas.reduce<{
      errors: z.core.$ZodIssue[];
      failed: number;
    }>(
      ({ errors, failed }, schema) =>
        ((result) =>
          result.error
            ? {
                errors: [...errors, ...result.error.issues],
                failed: failed + 1,
              }
            : { errors, failed })(
          schema.safeParse(x),
        ),
      { errors: [], failed: 0 },
    );
    const passed = schemas.length - failed;
    if (passed !== 1) {
      ctx.addIssue(errors.length ? {
        path: [],
        code: "invalid_union",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      } : {
        path: [],
        code: "custom",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      });
    }
  }).describe("Indirection so that agents which do not need identity (e.g. Provider Matching) never receive DOB or phone (§6). Scheduling resolves it at the tool layer.").optional(), "synthetic": z.boolean().describe("True for fixture data and the offline profile. Propagated into traces so demo traffic is never counted as live evidence (§14).") }).strict();
export type task$TaskEnvelope = z.infer<typeof task$TaskEnvelope>;

export const task$DecisionOutput = z.object({ "action": z.string().min(1).describe("What this agent decided to do. Must be a member of the envelope's allowed_actions; anything else is refused rather than executed."), "reason_code": z.enum(["ok","missing_mandatory_field","ambiguous_input","conflicting_evidence","stale_evidence","no_candidate_matched","no_slot_available","hard_constraint_unsatisfiable","document_missing","document_unreadable","coverage_unknown","provider_not_in_network","approval_expired","approval_payload_changed","upstream_unavailable","upstream_rate_limited","upstream_timeout","upstream_malformed_response","booking_outcome_unknown","policy_blocked","guardrail_blocked","budget_exhausted","deadline_exceeded","human_owned","other"]).describe("Controlled vocabulary for why a decision produced its action. `other` requires reason_detail to be populated."), "reason_detail": z.union([z.string().max(2000).describe("Required when reason_code is `other`, or when the code alone would not let a reviewer reconstruct the decision."), z.null().describe("Required when reason_code is `other`, or when the code alone would not let a reviewer reconstruct the decision.")]).describe("Required when reason_code is `other`, or when the code alone would not let a reviewer reconstruct the decision.").optional(), "evidence_ids": z.array(z.string().uuid()).describe("Everything this decision rests on. A decision with no evidence is only legal for a pure clarification request."), "missing_fields": z.array(z.object({ "field": z.string().min(1), "reason_code": z.enum(["ok","missing_mandatory_field","ambiguous_input","conflicting_evidence","stale_evidence","no_candidate_matched","no_slot_available","hard_constraint_unsatisfiable","document_missing","document_unreadable","coverage_unknown","provider_not_in_network","approval_expired","approval_payload_changed","upstream_unavailable","upstream_rate_limited","upstream_timeout","upstream_malformed_response","booking_outcome_unknown","policy_blocked","guardrail_blocked","budget_exhausted","deadline_exceeded","human_owned","other"]).describe("Controlled vocabulary for why a decision produced its action. `other` requires reason_detail to be populated."), "question": z.union([z.string().describe("What to ask the patient for this field. Null when the gap is not patient-answerable (escalate instead of asking)."), z.null().describe("What to ask the patient for this field. Null when the gap is not patient-answerable (escalate instead of asking).")]).describe("What to ask the patient for this field. Null when the gap is not patient-answerable (escalate instead of asking).").optional(), "mandatory": z.boolean().describe("Mandatory fields (specialty, approval, valid slot — §2.1) block progress; the rest do not.").optional() }).strict()), "next_allowed_actions": z.array(z.string().regex(new RegExp("^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")).describe("Namespaced action identifier (owner:action). The policy table (§4) maps state → allowed actions; the coordinator cannot exceed it.")).describe("What the workflow may permit next. Computed from the policy table, not proposed by the model."), "degraded": z.enum(["full","rerank_down","embeddings_down","opensearch_down","static_seed","no_source"]).describe("Retrieval degradation ladder (§10). Every level below `full` must surface a visible `degraded` flag; `static_seed` is deterministic and makes no LLM call; `no_source` goes to HITL.").describe("Set when the decision was reached on a reduced pipeline (§10). Never null-and-assumed-fine: `full` is stated explicitly, absence means unrecorded.").optional() }).strict();
export type task$DecisionOutput = z.infer<typeof task$DecisionOutput>;

export const task$AgentError = z.object({ "class": z.enum(["transient_upstream","rate_limited","timeout","malformed_upstream_response","policy_refusal","budget_exhausted","deadline_exceeded","dependency_unavailable","internal"]), "retryable": z.boolean().describe("A valid empty result is NOT an error and is not retried (§8); it is a `completed` result with `no_match`."), "message": z.string().max(2000), "retry_after_seconds": z.union([z.number().int().gte(0).describe("Honoured from Retry-After when the upstream supplies one (§8)."), z.null().describe("Honoured from Retry-After when the upstream supplies one (§8).")]).describe("Honoured from Retry-After when the upstream supplies one (§8).").optional(), "upstream": z.union([z.object({ "service": z.string().optional(), "endpoint": z.string().optional(), "status": z.union([z.number().int(), z.null()]).optional() }).strict(), z.null()]).optional(), "occurred_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).") }).strict();
export type task$AgentError = z.infer<typeof task$AgentError>;

export const task$ResultEnvelope = z.object({ "task_id": z.string().uuid(), "workflow_id": z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15)."), "command_id": z.string().uuid().describe("Idempotency key for a client-replayed command (§7). Replay with the same value must be deduplicated."), "outcome": z.enum(["completed","needs_input","no_match","retryable_error","terminal_error","outcome_unknown"]).describe("The authoritative outcome. Callers branch on this, never on the A2A state."), "a2a_state": z.enum(["submitted","working","input-required","auth-required","completed","canceled","failed","rejected","unknown"]).describe("Protocol projection of `outcome`, produced only by the adapter. The custom enum is never declared as a protocol state (§5)."), "workflow_state": z.enum(["INTAKE","COVERAGE_CHECK","PROVIDER_MATCHING","AWAITING_HOSPITAL_CHOICE","AWAITING_DOCTOR_CHOICE","READINESS_CHECK","AWAITING_SLOT_CHOICE","AWAITING_FINAL_APPROVAL","BOOKING_IN_FLIGHT","BOOKED","BOOKING_UNVERIFIED","RECOVERY","HUMAN_HANDOFF","ABANDONED","CANCELLED","FAILED"]).describe("Workflow state machine (§7). The policy table (§4) defines which agent/tool may run in each state; the coordinator LLM cannot override it.").optional(), "state_version": z.number().int().gte(0).describe("Version produced by this result. The caller writes it back only if it is still current.").optional(), "payload": z.union([z.record(z.string(), z.unknown()).describe("Typed per agent. Validated against the agent's versioned output schema before it is ever committed (§2.1: partial agent output is not final state)."), z.null().describe("Typed per agent. Validated against the agent's versioned output schema before it is ever committed (§2.1: partial agent output is not final state).")]).describe("Typed per agent. Validated against the agent's versioned output schema before it is ever committed (§2.1: partial agent output is not final state).").optional(), "evidence_ids": z.array(z.string().uuid()).optional(), "decision": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.object({ "action": z.string().min(1).describe("What this agent decided to do. Must be a member of the envelope's allowed_actions; anything else is refused rather than executed."), "reason_code": z.enum(["ok","missing_mandatory_field","ambiguous_input","conflicting_evidence","stale_evidence","no_candidate_matched","no_slot_available","hard_constraint_unsatisfiable","document_missing","document_unreadable","coverage_unknown","provider_not_in_network","approval_expired","approval_payload_changed","upstream_unavailable","upstream_rate_limited","upstream_timeout","upstream_malformed_response","booking_outcome_unknown","policy_blocked","guardrail_blocked","budget_exhausted","deadline_exceeded","human_owned","other"]).describe("Controlled vocabulary for why a decision produced its action. `other` requires reason_detail to be populated."), "reason_detail": z.union([z.string().max(2000).describe("Required when reason_code is `other`, or when the code alone would not let a reviewer reconstruct the decision."), z.null().describe("Required when reason_code is `other`, or when the code alone would not let a reviewer reconstruct the decision.")]).describe("Required when reason_code is `other`, or when the code alone would not let a reviewer reconstruct the decision.").optional(), "evidence_ids": z.array(z.string().uuid()).describe("Everything this decision rests on. A decision with no evidence is only legal for a pure clarification request."), "missing_fields": z.array(z.object({ "field": z.string().min(1), "reason_code": z.enum(["ok","missing_mandatory_field","ambiguous_input","conflicting_evidence","stale_evidence","no_candidate_matched","no_slot_available","hard_constraint_unsatisfiable","document_missing","document_unreadable","coverage_unknown","provider_not_in_network","approval_expired","approval_payload_changed","upstream_unavailable","upstream_rate_limited","upstream_timeout","upstream_malformed_response","booking_outcome_unknown","policy_blocked","guardrail_blocked","budget_exhausted","deadline_exceeded","human_owned","other"]).describe("Controlled vocabulary for why a decision produced its action. `other` requires reason_detail to be populated."), "question": z.union([z.string().describe("What to ask the patient for this field. Null when the gap is not patient-answerable (escalate instead of asking)."), z.null().describe("What to ask the patient for this field. Null when the gap is not patient-answerable (escalate instead of asking).")]).describe("What to ask the patient for this field. Null when the gap is not patient-answerable (escalate instead of asking).").optional(), "mandatory": z.boolean().describe("Mandatory fields (specialty, approval, valid slot — §2.1) block progress; the rest do not.").optional() }).strict()), "next_allowed_actions": z.array(z.string().regex(new RegExp("^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")).describe("Namespaced action identifier (owner:action). The policy table (§4) maps state → allowed actions; the coordinator cannot exceed it.")).describe("What the workflow may permit next. Computed from the policy table, not proposed by the model."), "degraded": z.enum(["full","rerank_down","embeddings_down","opensearch_down","static_seed","no_source"]).describe("Retrieval degradation ladder (§10). Every level below `full` must surface a visible `degraded` flag; `static_seed` is deterministic and makes no LLM call; `no_source` goes to HITL.").describe("Set when the decision was reached on a reduced pipeline (§10). Never null-and-assumed-fine: `full` is stated explicitly, absence means unrecorded.").optional() }).strict(), z.null()];
    const { errors, failed } = schemas.reduce<{
      errors: z.core.$ZodIssue[];
      failed: number;
    }>(
      ({ errors, failed }, schema) =>
        ((result) =>
          result.error
            ? {
                errors: [...errors, ...result.error.issues],
                failed: failed + 1,
              }
            : { errors, failed })(
          schema.safeParse(x),
        ),
      { errors: [], failed: 0 },
    );
    const passed = schemas.length - failed;
    if (passed !== 1) {
      ctx.addIssue(errors.length ? {
        path: [],
        code: "invalid_union",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      } : {
        path: [],
        code: "custom",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      });
    }
  }).optional(), "error": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.object({ "class": z.enum(["transient_upstream","rate_limited","timeout","malformed_upstream_response","policy_refusal","budget_exhausted","deadline_exceeded","dependency_unavailable","internal"]), "retryable": z.boolean().describe("A valid empty result is NOT an error and is not retried (§8); it is a `completed` result with `no_match`."), "message": z.string().max(2000), "retry_after_seconds": z.union([z.number().int().gte(0).describe("Honoured from Retry-After when the upstream supplies one (§8)."), z.null().describe("Honoured from Retry-After when the upstream supplies one (§8).")]).describe("Honoured from Retry-After when the upstream supplies one (§8).").optional(), "upstream": z.union([z.object({ "service": z.string().optional(), "endpoint": z.string().optional(), "status": z.union([z.number().int(), z.null()]).optional() }).strict(), z.null()]).optional(), "occurred_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).") }).strict(), z.null()];
    const { errors, failed } = schemas.reduce<{
      errors: z.core.$ZodIssue[];
      failed: number;
    }>(
      ({ errors, failed }, schema) =>
        ((result) =>
          result.error
            ? {
                errors: [...errors, ...result.error.issues],
                failed: failed + 1,
              }
            : { errors, failed })(
          schema.safeParse(x),
        ),
      { errors: [], failed: 0 },
    );
    const passed = schemas.length - failed;
    if (passed !== 1) {
      ctx.addIssue(errors.length ? {
        path: [],
        code: "invalid_union",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      } : {
        path: [],
        code: "custom",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      });
    }
  }).optional(), "call_budget_remaining": z.object({ "deadline_at": z.string().datetime({ offset: true }).describe("Absolute shared deadline. Parent, sub-agent, tool and model calls all draw from the same remaining budget (§3, §8) — a sub-agent cannot mint its own."), "turn_started_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).").optional(), "remaining_model_calls": z.number().int().gte(0), "remaining_tool_calls": z.number().int().gte(0), "remaining_delegations": z.number().int().gte(0).describe("Bounded by MAX_DELEGATIONS_PER_TURN. At zero the coordinator must stop delegating and answer or escalate."), "attempt": z.number().int().gte(1).lte(3).describe("1-based, first call included. Exactly one layer owns retries (§8) — SDK/HTTP/A2A internal retries are disabled so the budget cannot be multiplied.").optional() }).strict().optional(), "release_manifest_id": z.string().regex(new RegExp("^rel-[0-9]{8}-[a-z0-9]{7}$")).describe("Immutable release identifier (§13). Conversations are pinned to one."), "synthetic": z.boolean(), "completed_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).").optional() }).strict().and(z.unknown());
export type task$ResultEnvelope = z.infer<typeof task$ResultEnvelope>;

export const task$A2ATaskRecord = z.object({ "task_id": z.string().min(1), "context_id": z.string().min(1), "message_id": z.union([z.string(), z.null()]).optional(), "state": z.enum(["submitted","working","input-required","auth-required","completed","canceled","failed","rejected","unknown"]).describe("Standard A2A task lifecycle states. Protocol-level only."), "history_length": z.number().int().gte(0) }).strict();
export type task$A2ATaskRecord = z.infer<typeof task$A2ATaskRecord>;

export const task$AgentCard = z.object({ "name": z.string().regex(new RegExp("^[a-z][a-z0-9-]*$")), "description": z.string().min(1), "version": z.string().regex(new RegExp("^\\d+\\.\\d+\\.\\d+$")), "agent_hash": z.string().regex(new RegExp("^[a-f0-9]{64}$")).describe("Any change to a description, instruction, tool or skill changes this hash and forces a version bump (§13). CI fails when an affected agent's version did not increase."), "capabilities": z.object({ "streaming": z.boolean().optional(), "pushNotifications": z.boolean().optional(), "stateTransitionHistory": z.boolean().optional() }).strict(), "skills": z.array(z.object({ "id": z.string(), "name": z.string(), "description": z.string(), "skill_version": z.string() }).strict()), "input_schema_id": z.string().min(1), "output_schema_id": z.string().min(1), "auth": z.object({ "scheme": z.enum(["bearer","none"]), "verified_at_transport": z.literal(true).describe("Identity is verified on the HTTP transport (§9). A user_id inside the payload is never trusted, and a trace id never grants authority."), "required_scopes": z.array(z.string()).optional() }).strict(), "tools": z.array(z.string()).describe("Effective allowlist. Adding a skill grants no tool permission (§6) — this list is the only source of tool authority.").optional(), "sub_agents": z.array(z.string()).optional() }).strict();
export type task$AgentCard = z.infer<typeof task$AgentCard>;

// Inline enums, also exported by name.
export const task$AgentErrorClass = z.enum(["transient_upstream", "rate_limited", "timeout", "malformed_upstream_response", "policy_refusal", "budget_exhausted", "deadline_exceeded", "dependency_unavailable", "internal"]);
export type task$AgentErrorClass = z.infer<typeof task$AgentErrorClass>;

export const task$AuthScheme = z.enum(["bearer", "none"]);
export type task$AuthScheme = z.infer<typeof task$AuthScheme>;

export const task$ConstraintSource = z.enum(["patient_stated", "referral_document", "system_mandatory", "inferred"]);
export type task$ConstraintSource = z.infer<typeof task$ConstraintSource>;
