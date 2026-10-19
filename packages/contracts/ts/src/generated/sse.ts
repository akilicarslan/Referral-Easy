// GENERATED — do not edit. Source: packages/contracts/schemas/*.json
// Regenerate with: pnpm gen:contracts
import { z } from 'zod';

export const sse$RenderableComponent = z.enum(["HospitalPicker","DoctorPicker","SlotPicker","PatientDetailsForm","BookingReview","AppointmentReceipt","DirectoryFallbackCard","HumanHandoffCard"]).describe("The eight components from §12. Coverage, preference and readiness information are section/field variants of these — never a new component, and never a new UI system per feature.");
export type sse$RenderableComponent = z.infer<typeof sse$RenderableComponent>;

export const sse$ComponentInstance = z.object({ "instance_id": z.string().uuid().describe("Stable for the life of the surface slot. Re-rendering the same instance id replaces it rather than adding a second one — which is what makes an SSE replay idempotent on the client (§12: re-rendering never creates a second booking)."), "component": z.enum(["HospitalPicker","DoctorPicker","SlotPicker","PatientDetailsForm","BookingReview","AppointmentReceipt","DirectoryFallbackCard","HumanHandoffCard"]).describe("One of the eight §12 components."), "props": z.record(z.string(), z.unknown()).describe("Component-specific. Validated against the component's own schema before it reaches the client; the gateway never forwards an unvalidated agent payload (§2.1)."), "workflow_state_version": z.number().int().gte(0).describe("The version this instance was built from. A client action carrying a stale version is refused rather than applied (§9)."), "action": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().regex(new RegExp("^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")).describe("Namespaced action identifier (owner:action). The policy table (§4) maps state → allowed actions; the coordinator cannot exceed it."), z.null()];
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
  }).describe("The server-validated action this component submits. Null for display-only components. The client never invents an action: it echoes one the server issued.").optional(), "submitted": z.boolean().describe("Set once the patient has answered this instance, so a replayed surface cannot solicit the same choice twice.").optional(), "disabled": z.boolean().optional(), "notice": z.union([z.string().describe("Inline explanation, e.g. why an option is unavailable or that a value is unknown rather than absent (§2.2)."), z.null().describe("Inline explanation, e.g. why an option is unavailable or that a value is unknown rather than absent (§2.2).")]).describe("Inline explanation, e.g. why an option is unavailable or that a value is unknown rather than absent (§2.2).").optional() }).strict();
export type sse$ComponentInstance = z.infer<typeof sse$ComponentInstance>;

export const sse$StreamEvent = z.object({ "event_id": z.string().regex(new RegExp("^[0-9a-f]{32}$")).describe("Content-addressed id used for SSE replay (§12) and for deduplicating authoritative events (§15). Replaying from a given id must reproduce the same sequence and must not create a second booking."), "seq": z.number().int().gte(0).describe("Monotonic within a workflow. A client that sees a gap knows it missed an event and should replay."), "workflow_id": z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15)."), "type": z.enum(["workflow_state","surface_update","agent_progress","partial_text","notice","terminal"]).describe("`partial_text` is explicitly NOT final state (§2.1: partial agent output is never committed). `terminal` closes the stream and carries the outcome; the client must not infer completion from the stream simply ending."), "payload": z.union([z.record(z.string(), z.unknown()).describe("Typed by `type`: a SurfaceUpdate for surface_update, a WorkflowStateChange for workflow_state, and so on."), z.null().describe("Typed by `type`: a SurfaceUpdate for surface_update, a WorkflowStateChange for workflow_state, and so on.")]).describe("Typed by `type`: a SurfaceUpdate for surface_update, a WorkflowStateChange for workflow_state, and so on.").optional(), "progress_elapsed_ms": z.union([z.number().int().gte(0).describe("Progress latency, measured separately from final-response latency (§12). Null on a terminal event, which carries the final timing instead."), z.null().describe("Progress latency, measured separately from final-response latency (§12). Null on a terminal event, which carries the final timing instead.")]).describe("Progress latency, measured separately from final-response latency (§12). Null on a terminal event, which carries the final timing instead.").optional(), "created_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "synthetic": z.boolean().describe("True for fixture and offline-profile output, so the UI can label it and no admin chart can present canned data as a measurement (§12, §18).") }).strict();
export type sse$StreamEvent = z.infer<typeof sse$StreamEvent>;

export const sse$SurfaceUpdate = z.object({ "surface_id": z.string().uuid(), "components": z.array(z.object({ "instance_id": z.string().uuid().describe("Stable for the life of the surface slot. Re-rendering the same instance id replaces it rather than adding a second one — which is what makes an SSE replay idempotent on the client (§12: re-rendering never creates a second booking)."), "component": z.enum(["HospitalPicker","DoctorPicker","SlotPicker","PatientDetailsForm","BookingReview","AppointmentReceipt","DirectoryFallbackCard","HumanHandoffCard"]).describe("One of the eight §12 components."), "props": z.record(z.string(), z.unknown()).describe("Component-specific. Validated against the component's own schema before it reaches the client; the gateway never forwards an unvalidated agent payload (§2.1)."), "workflow_state_version": z.number().int().gte(0).describe("The version this instance was built from. A client action carrying a stale version is refused rather than applied (§9)."), "action": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().regex(new RegExp("^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")).describe("Namespaced action identifier (owner:action). The policy table (§4) maps state → allowed actions; the coordinator cannot exceed it."), z.null()];
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
  }).describe("The server-validated action this component submits. Null for display-only components. The client never invents an action: it echoes one the server issued.").optional(), "submitted": z.boolean().describe("Set once the patient has answered this instance, so a replayed surface cannot solicit the same choice twice.").optional(), "disabled": z.boolean().optional(), "notice": z.union([z.string().describe("Inline explanation, e.g. why an option is unavailable or that a value is unknown rather than absent (§2.2)."), z.null().describe("Inline explanation, e.g. why an option is unavailable or that a value is unknown rather than absent (§2.2).")]).describe("Inline explanation, e.g. why an option is unavailable or that a value is unknown rather than absent (§2.2).").optional() }).strict()).describe("Ordered. A later update replaces instances by `instance_id`; it never appends a duplicate."), "remove_instance_ids": z.array(z.string().uuid()).describe("Instances the client must drop, so a superseded choice cannot be re-submitted.").optional() }).strict();
export type sse$SurfaceUpdate = z.infer<typeof sse$SurfaceUpdate>;

export const sse$WorkflowStateChange = z.object({ "from_state": z.enum(["INTAKE","COVERAGE_CHECK","PROVIDER_MATCHING","AWAITING_HOSPITAL_CHOICE","AWAITING_DOCTOR_CHOICE","READINESS_CHECK","AWAITING_SLOT_CHOICE","AWAITING_FINAL_APPROVAL","BOOKING_IN_FLIGHT","BOOKED","BOOKING_UNVERIFIED","RECOVERY","HUMAN_HANDOFF","ABANDONED","CANCELLED","FAILED"]).describe("Workflow state machine (§7). The policy table (§4) defines which agent/tool may run in each state; the coordinator LLM cannot override it."), "to_state": z.enum(["INTAKE","COVERAGE_CHECK","PROVIDER_MATCHING","AWAITING_HOSPITAL_CHOICE","AWAITING_DOCTOR_CHOICE","READINESS_CHECK","AWAITING_SLOT_CHOICE","AWAITING_FINAL_APPROVAL","BOOKING_IN_FLIGHT","BOOKED","BOOKING_UNVERIFIED","RECOVERY","HUMAN_HANDOFF","ABANDONED","CANCELLED","FAILED"]).describe("Workflow state machine (§7). The policy table (§4) defines which agent/tool may run in each state; the coordinator LLM cannot override it."), "state_version": z.number().int().gte(0).describe("Monotonic optimistic-concurrency token on a workflow (§7). Every write must present the version it read."), "reason_code": z.enum(["ok","missing_mandatory_field","ambiguous_input","conflicting_evidence","stale_evidence","no_candidate_matched","no_slot_available","hard_constraint_unsatisfiable","document_missing","document_unreadable","coverage_unknown","provider_not_in_network","approval_expired","approval_payload_changed","upstream_unavailable","upstream_rate_limited","upstream_timeout","upstream_malformed_response","booking_outcome_unknown","policy_blocked","guardrail_blocked","budget_exhausted","deadline_exceeded","human_owned","other"]).describe("Controlled vocabulary for why a decision produced its action. `other` requires reason_detail to be populated."), "degraded": z.union([z.enum(["full","rerank_down","embeddings_down","opensearch_down","static_seed","no_source"]).describe("Retrieval degradation ladder (§10). Every level below `full` must surface a visible `degraded` flag; `static_seed` is deterministic and makes no LLM call; `no_source` goes to HITL."), z.null()]).describe("Set when this step ran on a reduced pipeline (§10). Surfaced to the patient, because an unknown answer produced without the directory is a different answer.").optional() }).strict();
export type sse$WorkflowStateChange = z.infer<typeof sse$WorkflowStateChange>;

export const sse$ClientAction = z.object({ "instance_id": z.string().uuid(), "action": z.string().regex(new RegExp("^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")).describe("Namespaced action identifier (owner:action). The policy table (§4) maps state → allowed actions; the coordinator cannot exceed it."), "workflow_state_version": z.number().int().gte(0).describe("Monotonic optimistic-concurrency token on a workflow (§7). Every write must present the version it read."), "command_id": z.string().uuid().describe("The idempotency key for this action (§7). A resubmitted action reuses it, so a double-click or a retried request cannot become a second booking."), "value": z.union([z.record(z.string(), z.unknown()).describe("The patient's answer. Validated server-side against the component's schema; form data is never trusted because it came from the client (§6, §12)."), z.null().describe("The patient's answer. Validated server-side against the component's schema; form data is never trusted because it came from the client (§6, §12).")]).describe("The patient's answer. Validated server-side against the component's schema; form data is never trusted because it came from the client (§6, §12).").optional() }).strict();
export type sse$ClientAction = z.infer<typeof sse$ClientAction>;

// Inline enums, also exported by name.
export const sse$StreamEventType = z.enum(["workflow_state", "surface_update", "agent_progress", "partial_text", "notice", "terminal"]);
export type sse$StreamEventType = z.infer<typeof sse$StreamEventType>;
