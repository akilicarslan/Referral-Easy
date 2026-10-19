// GENERATED — do not edit. Source: packages/contracts/schemas/*.json
// Regenerate with: pnpm gen:contracts
import { z } from 'zod';

export const booking$Approval = z.object({ "approval_id": z.string().uuid(), "patient_id": z.string().uuid(), "workflow_id": z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15)."), "state_version": z.number().int().gte(0).describe("The workflow version the patient approved against."), "payload_hash": z.string().regex(new RegExp("^[a-f0-9]{64}$")).describe("Hash of the exact booking payload the patient saw. Any data change — slot, provider, visit type, preferences — invalidates the approval, because the hash no longer matches (§9)."), "granted_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "expires_at": z.string().datetime({ offset: true }).describe("Approval is time-bounded. An expired approval is never extended implicitly — the patient is asked again."), "granted_by": z.enum(["patient","human_operator"]).describe("The final booking always requires patient approval, even after human intervention (§7, §11): an operator's resolution returns to a safe state, it does not grant booking consent."), "consumed": z.boolean().describe("Set when a booking write takes it. A consumed approval cannot authorise a second write — this is a structural guard against double booking, not just a validation rule."), "consumed_by_booking_request_id": z.unknown().superRefine((x, ctx) => {
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
  }).optional(), "invalidated_reason": z.enum(["payload_changed","state_version_superseded","expired","patient_withdrew"]).describe("Optional: absent means the approval is still valid.").optional() }).strict();
export type booking$Approval = z.infer<typeof booking$Approval>;

export const booking$BookingPayload = z.object({ "patient_id": z.string().uuid(), "provider_id": z.string().uuid(), "hospital_id": z.string().uuid(), "slot_id": z.string().uuid(), "visit_type": z.enum(["first_visit","return_visit"]), "document_ids": z.array(z.string().uuid()).optional(), "contact_phone": z.union([z.string(), z.null()]).optional() }).strict();
export type booking$BookingPayload = z.infer<typeof booking$BookingPayload>;

export const booking$BookingRequest = z.object({ "booking_request_id": z.string().uuid(), "workflow_id": z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15)."), "command_id": z.string().uuid().describe("Idempotency key for a client-replayed command (§7). Replay with the same value must be deduplicated."), "idempotency_key": z.string().min(8).max(200).describe("Sent to the hospital adapter. On outcome_unknown the SAME key is re-used for lookup and reconcile — a second, independent key is never minted (§7)."), "payload_hash": z.string().regex(new RegExp("^[a-f0-9]{64}$")), "payload": z.object({ "patient_id": z.string().uuid(), "provider_id": z.string().uuid(), "hospital_id": z.string().uuid(), "slot_id": z.string().uuid(), "visit_type": z.enum(["first_visit","return_visit"]), "document_ids": z.array(z.string().uuid()).optional(), "contact_phone": z.union([z.string(), z.null()]).optional() }).strict(), "approval_id": z.unknown().superRefine((x, ctx) => {
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
  }).optional(), "state": z.enum(["REQUESTED","PENDING_REVIEW","CONFIRMED","REJECTED","CANCEL_PENDING","CANCELLED"]).describe("Booking lifecycle (§2.5). REQUESTED and PENDING_REVIEW are NEVER presented to the patient as confirmed, and HTTP 200 never implies CONFIRMED."), "adapter_type": z.enum(["instant_confirm","review_required"]), "external_reference": z.union([z.string().describe("The hospital-side identifier once it exists. Null while REQUESTED or unresolved."), z.null().describe("The hospital-side identifier once it exists. Null while REQUESTED or unresolved.")]).describe("The hospital-side identifier once it exists. Null while REQUESTED or unresolved.").optional(), "attempt_count": z.number().int().gte(0), "reconciliation": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.object({ "status": z.enum(["pending","resolved_booked","resolved_not_booked","escalated"]).describe("Driven by lookup_request against the same idempotency key until it resolves or escalates (§7)."), "started_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "lookup_attempts": z.number().int().gte(0), "last_lookup_at": z.union([z.string().datetime({ offset: true }), z.null()]).optional(), "escalation_reason": z.union([z.string(), z.null()]).optional() }).strict(), z.null()];
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
  }).optional(), "created_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "updated_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "reschedule_of": z.unknown().superRefine((x, ctx) => {
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
  }).describe("Set when this request replaces an earlier booking. §2.5: the old booking is cancelled only AFTER the new one is verified — never before.").optional() }).strict();
export type booking$BookingRequest = z.infer<typeof booking$BookingRequest>;

export const booking$ReconciliationState = z.object({ "status": z.enum(["pending","resolved_booked","resolved_not_booked","escalated"]).describe("Driven by lookup_request against the same idempotency key until it resolves or escalates (§7)."), "started_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "lookup_attempts": z.number().int().gte(0), "last_lookup_at": z.union([z.string().datetime({ offset: true }), z.null()]).optional(), "escalation_reason": z.union([z.string(), z.null()]).optional() }).strict();
export type booking$ReconciliationState = z.infer<typeof booking$ReconciliationState>;

export const booking$BookingAttempt = z.object({ "attempt": z.number().int().gte(1), "idempotency_key": z.string().min(8).describe("Every attempt for one logical booking carries the SAME key. A differing key on a retry is the double-booking bug this field exists to catch."), "started_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "finished_at": z.union([z.string().datetime({ offset: true }), z.null()]).optional(), "result": z.enum(["created","duplicate_replayed","slot_conflict","rejected_by_hospital","timeout_before_commit","timeout_after_commit","malformed_response","rate_limited","upstream_unavailable","outcome_unknown"]).describe("`timeout_before_commit` and `timeout_after_commit` are distinct outcomes (§17) and lead to different next steps: only the latter creates a booking that must be reconciled."), "http_status": z.union([z.number().int(), z.null()]).optional(), "raw_response_ref": z.union([z.string().describe("Pointer to the stored vendor payload. Kept for the adapter-normalisation evidence, never shown to the patient."), z.null().describe("Pointer to the stored vendor payload. Kept for the adapter-normalisation evidence, never shown to the patient.")]).describe("Pointer to the stored vendor payload. Kept for the adapter-normalisation evidence, never shown to the patient.").optional() }).strict();
export type booking$BookingAttempt = z.infer<typeof booking$BookingAttempt>;

export const booking$AppointmentRecord = z.object({ "appointment_id": z.string().uuid(), "booking_request_id": z.string().uuid(), "hospital_id": z.string().uuid(), "provider_id": z.string().uuid(), "patient_id": z.string().uuid(), "slot_id": z.string().uuid().optional(), "start_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "end_at": z.union([z.string().datetime({ offset: true }), z.null()]).optional(), "display_timezone": z.string().min(1).describe("IANA timezone name. Appointment slots are stored UTC and displayed in this zone (§17)."), "state": z.enum(["REQUESTED","PENDING_REVIEW","CONFIRMED","REJECTED","CANCEL_PENDING","CANCELLED"]).describe("Booking lifecycle (§2.5). REQUESTED and PENDING_REVIEW are NEVER presented to the patient as confirmed, and HTTP 200 never implies CONFIRMED."), "verified": z.boolean().describe("True only after the appointment was read back from the hospital. A receipt is never rendered from a local optimistic write."), "verification_method": z.enum(["create_response","lookup_request"]).describe("Optional: absent means the appointment has not been verified yet.").optional(), "synthetic": z.literal(true) }).strict();
export type booking$AppointmentRecord = z.infer<typeof booking$AppointmentRecord>;

export const booking$NotificationOutboxEntry = z.object({ "outbox_id": z.string().uuid(), "booking_request_id": z.string().uuid(), "channel": z.enum(["email","sms"]), "recipient": z.string().min(1).optional(), "template_key": z.string().min(1).optional(), "status": z.enum(["pending","sent","failed","abandoned"]).describe("Written in the SAME transaction as the booking (§7). A notification failure never rolls the booking back, and a permanently failed notification stops rather than looping forever (§16)."), "attempts": z.number().int().gte(0), "next_attempt_at": z.union([z.string().datetime({ offset: true }), z.null()]).optional(), "last_error": z.union([z.string(), z.null()]).optional(), "created_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).") }).strict();
export type booking$NotificationOutboxEntry = z.infer<typeof booking$NotificationOutboxEntry>;

// Inline enums, also exported by name.
export const booking$AppointmentVerificationMethod = z.enum(["create_response", "lookup_request"]);
export type booking$AppointmentVerificationMethod = z.infer<typeof booking$AppointmentVerificationMethod>;

export const booking$ApprovalGrantedBy = z.enum(["patient", "human_operator"]);
export type booking$ApprovalGrantedBy = z.infer<typeof booking$ApprovalGrantedBy>;

export const booking$ApprovalInvalidationReason = z.enum(["payload_changed", "state_version_superseded", "expired", "patient_withdrew"]);
export type booking$ApprovalInvalidationReason = z.infer<typeof booking$ApprovalInvalidationReason>;

export const booking$BookingAttemptResult = z.enum(["created", "duplicate_replayed", "slot_conflict", "rejected_by_hospital", "timeout_before_commit", "timeout_after_commit", "malformed_response", "rate_limited", "upstream_unavailable", "outcome_unknown"]);
export type booking$BookingAttemptResult = z.infer<typeof booking$BookingAttemptResult>;

export const booking$NotificationChannel = z.enum(["email", "sms"]);
export type booking$NotificationChannel = z.infer<typeof booking$NotificationChannel>;

export const booking$OutboxStatus = z.enum(["pending", "sent", "failed", "abandoned"]);
export type booking$OutboxStatus = z.infer<typeof booking$OutboxStatus>;

export const booking$ReconciliationStatus = z.enum(["pending", "resolved_booked", "resolved_not_booked", "escalated"]);
export type booking$ReconciliationStatus = z.infer<typeof booking$ReconciliationStatus>;
