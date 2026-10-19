// GENERATED — do not edit. Source: packages/contracts/schemas/*.json
// Regenerate with: pnpm gen:contracts
import { z } from 'zod';

export const workflow$WorkflowRecord = z.object({ "workflow_id": z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15)."), "conversation_id": z.string().uuid().describe("Transport-level chat session. Never used as a booking key."), "patient_id": z.string().uuid(), "state": z.enum(["INTAKE","COVERAGE_CHECK","PROVIDER_MATCHING","AWAITING_HOSPITAL_CHOICE","AWAITING_DOCTOR_CHOICE","READINESS_CHECK","AWAITING_SLOT_CHOICE","AWAITING_FINAL_APPROVAL","BOOKING_IN_FLIGHT","BOOKED","BOOKING_UNVERIFIED","RECOVERY","HUMAN_HANDOFF","ABANDONED","CANCELLED","FAILED"]).describe("Workflow state machine (§7). The policy table (§4) defines which agent/tool may run in each state; the coordinator LLM cannot override it."), "state_version": z.number().int().gte(0).describe("Monotonic optimistic-concurrency token on a workflow (§7). Every write must present the version it read."), "lease": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.object({ "owner_id": z.string().min(1).describe("Replica/worker that currently owns the work."), "generation": z.number().int().gte(0).describe("Monotonic ownership epoch. A worker whose generation is behind the row's cannot write (§7)."), "expires_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).") }).strict().describe("Ownership lease on active work. A stale worker (expired lease or old generation) cannot write workflow state (§7)."), z.null()];
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
  }).describe("Present while a worker owns active work. Every write is checked against BOTH the lease expiry and the generation.").optional(), "release_manifest_id": z.string().regex(new RegExp("^rel-[0-9]{8}-[a-z0-9]{7}$")).describe("The conversation is pinned to this release for its whole lifetime (§13). Mid-conversation version mixing is not permitted."), "automation_suspended": z.boolean().describe("True while a human owns the workflow (§11). While set, no automation may write — a human and the automation can never write concurrently."), "human_owner_id": z.union([z.string(), z.null()]).optional(), "active_handoff_id": z.unknown().superRefine((x, ctx) => {
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
  }).optional(), "active_booking_request_id": z.unknown().superRefine((x, ctx) => {
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
  }).optional(), "turn_deadline_at": z.union([z.string().datetime({ offset: true }), z.null()]).optional(), "budget": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.object({ "deadline_at": z.string().datetime({ offset: true }).describe("Absolute shared deadline. Parent, sub-agent, tool and model calls all draw from the same remaining budget (§3, §8) — a sub-agent cannot mint its own."), "turn_started_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).").optional(), "remaining_model_calls": z.number().int().gte(0), "remaining_tool_calls": z.number().int().gte(0), "remaining_delegations": z.number().int().gte(0).describe("Bounded by MAX_DELEGATIONS_PER_TURN. At zero the coordinator must stop delegating and answer or escalate."), "attempt": z.number().int().gte(1).lte(3).describe("1-based, first call included. Exactly one layer owns retries (§8) — SDK/HTTP/A2A internal retries are disabled so the budget cannot be multiplied.").optional() }).strict(), z.null()];
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
  }).optional(), "created_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "updated_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).") }).strict();
export type workflow$WorkflowRecord = z.infer<typeof workflow$WorkflowRecord>;

export const workflow$StateTransition = z.object({ "transition_id": z.string().uuid(), "workflow_id": z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15)."), "from_state": z.enum(["INTAKE","COVERAGE_CHECK","PROVIDER_MATCHING","AWAITING_HOSPITAL_CHOICE","AWAITING_DOCTOR_CHOICE","READINESS_CHECK","AWAITING_SLOT_CHOICE","AWAITING_FINAL_APPROVAL","BOOKING_IN_FLIGHT","BOOKED","BOOKING_UNVERIFIED","RECOVERY","HUMAN_HANDOFF","ABANDONED","CANCELLED","FAILED"]).describe("Workflow state machine (§7). The policy table (§4) defines which agent/tool may run in each state; the coordinator LLM cannot override it."), "to_state": z.enum(["INTAKE","COVERAGE_CHECK","PROVIDER_MATCHING","AWAITING_HOSPITAL_CHOICE","AWAITING_DOCTOR_CHOICE","READINESS_CHECK","AWAITING_SLOT_CHOICE","AWAITING_FINAL_APPROVAL","BOOKING_IN_FLIGHT","BOOKED","BOOKING_UNVERIFIED","RECOVERY","HUMAN_HANDOFF","ABANDONED","CANCELLED","FAILED"]).describe("Workflow state machine (§7). The policy table (§4) defines which agent/tool may run in each state; the coordinator LLM cannot override it."), "from_version": z.number().int().gte(0).describe("Monotonic optimistic-concurrency token on a workflow (§7). Every write must present the version it read."), "to_version": z.number().int().gte(0).describe("Monotonic optimistic-concurrency token on a workflow (§7). Every write must present the version it read."), "actor": z.string().min(1), "command_id": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().uuid().describe("Idempotency key for a client-replayed command (§7). Replay with the same value must be deduplicated."), z.null()];
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
  }).optional(), "reason_code": z.enum(["ok","missing_mandatory_field","ambiguous_input","conflicting_evidence","stale_evidence","no_candidate_matched","no_slot_available","hard_constraint_unsatisfiable","document_missing","document_unreadable","coverage_unknown","provider_not_in_network","approval_expired","approval_payload_changed","upstream_unavailable","upstream_rate_limited","upstream_timeout","upstream_malformed_response","booking_outcome_unknown","policy_blocked","guardrail_blocked","budget_exhausted","deadline_exceeded","human_owned","other"]).describe("Controlled vocabulary for why a decision produced its action. `other` requires reason_detail to be populated.").optional(), "occurred_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).") }).strict();
export type workflow$StateTransition = z.infer<typeof workflow$StateTransition>;

export const workflow$CommandDedup = z.object({ "command_id": z.string().uuid().describe("Idempotency key for a client-replayed command (§7). Replay with the same value must be deduplicated."), "workflow_id": z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15)."), "request_hash": z.string().regex(new RegExp("^[a-f0-9]{64}$")).describe("Same command_id + same hash ⇒ replay the stored response. Same command_id + different hash ⇒ reject. Silently accepting the second would let a replayed command mutate state (§7)."), "response_ref": z.union([z.string(), z.null()]).optional(), "received_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).") }).strict();
export type workflow$CommandDedup = z.infer<typeof workflow$CommandDedup>;

export const workflow$PolicyRule = z.object({ "state": z.enum(["INTAKE","COVERAGE_CHECK","PROVIDER_MATCHING","AWAITING_HOSPITAL_CHOICE","AWAITING_DOCTOR_CHOICE","READINESS_CHECK","AWAITING_SLOT_CHOICE","AWAITING_FINAL_APPROVAL","BOOKING_IN_FLIGHT","BOOKED","BOOKING_UNVERIFIED","RECOVERY","HUMAN_HANDOFF","ABANDONED","CANCELLED","FAILED"]).describe("Workflow state machine (§7). The policy table (§4) defines which agent/tool may run in each state; the coordinator LLM cannot override it."), "allowed_agents": z.array(z.string()).describe("Which specialist may run in this state. Enforced in code — the coordinator LLM cannot override this table (§4)."), "allowed_actions": z.array(z.string().regex(new RegExp("^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")).describe("Namespaced action identifier (owner:action). The policy table (§4) maps state → allowed actions; the coordinator cannot exceed it.")), "forbidden_actions": z.array(z.string().regex(new RegExp("^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")).describe("Namespaced action identifier (owner:action). The policy table (§4) maps state → allowed actions; the coordinator cannot exceed it.")).describe("Stated explicitly so a refusal is a policy decision with a reason, not a missing branch."), "requires_human_approval": z.boolean().optional(), "notes": z.union([z.string(), z.null()]).optional() }).strict();
export type workflow$PolicyRule = z.infer<typeof workflow$PolicyRule>;

export const workflow$Handoff = z.object({ "handoff_id": z.string().uuid(), "workflow_id": z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15)."), "reason_class": z.enum(["technical_outage","no_match","missing_document","conflicting_sources","prolonged_unknown_booking","patient_requested","no_reliable_recovery"]).describe("Handoff reasons are classed, not free text (§11)."), "reason_detail": z.union([z.string().max(2000), z.null()]).optional(), "patient_visible_summary": z.string().min(1).max(4000).describe("The exact text the patient reviewed. The patient sees what will be shared and consents before it leaves the workflow (§11)."), "consent_given_at": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), z.null()];
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
  }).describe("Null means consent has not been given. An un-consented handoff is not queued to a human."), "response_deadline_at": z.union([z.string().datetime({ offset: true }).describe("When no operator is available the patient gets a pending ticket plus directory contact details — never a pretence of a live agent (§11)."), z.null().describe("When no operator is available the patient gets a pending ticket plus directory contact details — never a pretence of a live agent (§11).")]).describe("When no operator is available the patient gets a pending ticket plus directory contact details — never a pretence of a live agent (§11).").optional(), "status": z.enum(["created","queued","claimed","resolved","expired","withdrawn"]), "created_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).") }).strict();
export type workflow$Handoff = z.infer<typeof workflow$Handoff>;

export const workflow$HumanQueueTicket = z.object({ "ticket_id": z.string().uuid(), "handoff_id": z.string().uuid(), "workflow_id": z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15)."), "status": z.enum(["pending","claimed","resolved","expired"]), "claimed_by": z.union([z.string(), z.null()]).optional(), "claimed_at": z.union([z.string().datetime({ offset: true }), z.null()]).optional(), "claim_generation": z.number().int().gte(0).describe("Conflicting claims are blocked by an atomic compare-and-set on this generation (§11). Two operators cannot both own a ticket."), "added_alternatives": z.array(z.object({ "option_id": z.string().uuid(), "kind": z.enum(["wider_radius","different_day","different_time_of_day","different_hospital","different_provider","human_handoff"]).describe("The bounded set of alternatives the system may offer. Never an invented option."), "description": z.string().min(1), "relaxes_constraint_id": z.unknown().superRefine((x, ctx) => {
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
  }).describe("Which constraint this option would relax. Must be null for `hard` constraints — a hard constraint is never presented as relaxable.").optional(), "candidate_ids": z.array(z.string()).describe("Concrete hospital/provider/slot ids this option refers to. An option with no candidates is not shown.").optional(), "evidence_ids": z.array(z.string().uuid()).describe("§2.4 requires alternatives to be evidence-based. An option with no evidence must not be offered."), "estimated_delay_days": z.union([z.number().int().gte(0), z.null()]).optional(), "additional_distance_miles": z.union([z.number().gte(0), z.null()]).optional() }).strict()).optional(), "operator_notes": z.union([z.string().max(4000), z.null()]).optional(), "return_state": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.enum(["INTAKE","COVERAGE_CHECK","PROVIDER_MATCHING","AWAITING_HOSPITAL_CHOICE","AWAITING_DOCTOR_CHOICE","READINESS_CHECK","AWAITING_SLOT_CHOICE","AWAITING_FINAL_APPROVAL","BOOKING_IN_FLIGHT","BOOKED","BOOKING_UNVERIFIED","RECOVERY","HUMAN_HANDOFF","ABANDONED","CANCELLED","FAILED"]).describe("Workflow state machine (§7). The policy table (§4) defines which agent/tool may run in each state; the coordinator LLM cannot override it."), z.null()];
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
  }).describe("The explicit safe state the workflow returns to. Automation stays suspended until this is set (§11).").optional(), "final_approval_still_required": z.literal(true).describe("§7/§11: resolving a ticket never grants booking consent. The patient still approves the final booking.").optional(), "created_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).") }).strict();
export type workflow$HumanQueueTicket = z.infer<typeof workflow$HumanQueueTicket>;

export const workflow$AuditEntry = z.object({ "audit_id": z.string().uuid(), "workflow_id": z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15)."), "actor": z.string().min(1), "actor_kind": z.enum(["patient","human_operator","agent","system"]), "action": z.string().min(1), "before": z.union([z.record(z.string(), z.unknown()), z.null()]).optional(), "after": z.union([z.record(z.string(), z.unknown()), z.null()]).optional(), "occurred_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).") }).strict();
export type workflow$AuditEntry = z.infer<typeof workflow$AuditEntry>;

export const workflow$ReleaseManifest = z.object({ "release_manifest_id": z.string().regex(new RegExp("^rel-[0-9]{8}-[a-z0-9]{7}$")).describe("Immutable release identifier (§13). Conversations are pinned to one."), "created_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "parent_release_manifest_id": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().regex(new RegExp("^rel-[0-9]{8}-[a-z0-9]{7}$")).describe("Immutable release identifier (§13). Conversations are pinned to one."), z.null()];
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
  }).optional(), "agents": z.array(z.object({ "agent_name": z.string(), "version": z.string(), "agent_hash": z.string().regex(new RegExp("^[a-f0-9]{64}$")), "instruction_hash": z.string().regex(new RegExp("^[a-f0-9]{64}$")).optional(), "tool_description_hashes": z.record(z.string(), z.string()).optional(), "skill_hashes": z.record(z.string(), z.string()).optional(), "sub_agent_manifest_hash": z.union([z.string(), z.null()]).optional(), "a2a_card_hash": z.string().regex(new RegExp("^[a-f0-9]{64}$")) }).strict()), "prompts": z.record(z.string(), z.string()), "tools": z.record(z.string(), z.object({ "schema_hash": z.string().optional(), "implementation_digest": z.string().optional() }).strict()).describe("Schema AND implementation digest (§13) — a behaviour change in code must move the hash even when the schema is unchanged."), "skills": z.record(z.string(), z.string()), "provider": z.object({ "agent_model_provider": z.string(), "agent_model_id": z.string(), "params": z.record(z.string(), z.unknown()), "per_agent_overrides": z.record(z.string(), z.record(z.string(), z.unknown())).describe("Per-agent overrides are recorded in the manifest, not hidden in env (§8).").optional() }).strict(), "guardrail_policy": z.object({ "mode": z.enum(["three_stage","conditional"]), "classifier_hash": z.string(), "thresholds": z.record(z.string(), z.number()) }).strict(), "rag": z.object({ "embedding_model_id": z.string(), "embedding_dimensions": z.number().int(), "index_name": z.string(), "reranker_model_id": z.string(), "knowledge_version": z.string() }).strict(), "source": z.object({ "git_sha": z.string(), "image_digest": z.string(), "lockfile_hash": z.string() }).strict(), "rollout": z.object({ "cohort": z.enum(["canary","stable","rolled_back"]).optional(), "cohort_percentage": z.number().gte(0).lte(1).optional(), "kill_switch_engaged": z.boolean().optional() }).strict().optional() }).strict();
export type workflow$ReleaseManifest = z.infer<typeof workflow$ReleaseManifest>;

// Inline enums, also exported by name.
export const workflow$AlternativeOptionKind = z.enum(["wider_radius", "different_day", "different_time_of_day", "different_hospital", "different_provider", "human_handoff"]);
export type workflow$AlternativeOptionKind = z.infer<typeof workflow$AlternativeOptionKind>;

export const workflow$GuardrailMode = z.enum(["three_stage", "conditional"]);
export type workflow$GuardrailMode = z.infer<typeof workflow$GuardrailMode>;

export const workflow$HandoffStatus = z.enum(["created", "queued", "claimed", "resolved", "expired", "withdrawn"]);
export type workflow$HandoffStatus = z.infer<typeof workflow$HandoffStatus>;

export const workflow$RolloutCohort = z.enum(["canary", "stable", "rolled_back"]);
export type workflow$RolloutCohort = z.infer<typeof workflow$RolloutCohort>;

export const workflow$TicketStatus = z.enum(["pending", "claimed", "resolved", "expired"]);
export type workflow$TicketStatus = z.infer<typeof workflow$TicketStatus>;
