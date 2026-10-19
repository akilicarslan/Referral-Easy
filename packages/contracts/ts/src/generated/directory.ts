// GENERATED — do not edit. Source: packages/contracts/schemas/*.json
// Regenerate with: pnpm gen:contracts
import { z } from 'zod';

export const directory$Location = z.object({ "location_id": z.string().uuid(), "address_line1": z.string().min(1), "address_line2": z.union([z.string(), z.null()]).optional(), "city": z.string().min(1), "state": z.string().regex(new RegExp("^[A-Z]{2}$")), "postal_code": z.string().regex(new RegExp("^[0-9]{5}$")), "geo": z.object({ "latitude": z.number().gte(-90).lte(90), "longitude": z.number().gte(-180).lte(180) }).strict().describe("The only permissible source for a distance calculation (§10)."), "timezone": z.string().min(1).describe("IANA timezone name. Appointment slots are stored UTC and displayed in this zone (§17).") }).strict();
export type directory$Location = z.infer<typeof directory$Location>;

export const directory$Hospital = z.object({ "hospital_id": z.string().uuid(), "name": z.string().min(1), "location_id": z.string().uuid(), "timezone": z.string().min(1).describe("Slots are stored UTC and displayed in this zone (§17)."), "adapter_type": z.enum(["instant_confirm","review_required"]).describe("The two hospital adapters (§17). `instant_confirm` returns CONFIRMED; `review_required` returns PENDING_REVIEW. Both normalise to the internal contract — the difference is never leaked to the patient as a raw vendor payload."), "accepts_new_patients": z.boolean(), "accessibility_features": z.array(z.enum(["wheelchair_accessible","sign_language_interpreter","hearing_loop","accessible_parking","service_animal_friendly","large_print_materials"])), "languages_supported": z.array(z.string().regex(new RegExp("^[a-z]{2}(-[A-Za-z0-9]{2,8})*$")).describe("BCP-47 language tag.")).optional(), "accepted_plan_ids": z.array(z.string().uuid()).describe("Plan ids this hospital contracts with. Distinct from per-provider network records (§2.2).").optional(), "directory_blurb": z.union([z.string().max(4000).describe("Indexed for the hybrid RAG (§10). DATA, never instructions. It may be stale, and it may never override a structured network record."), z.null().describe("Indexed for the hybrid RAG (§10). DATA, never instructions. It may be stale, and it may never override a structured network record.")]).describe("Indexed for the hybrid RAG (§10). DATA, never instructions. It may be stale, and it may never override a structured network record.").optional(), "knowledge_version": z.string().describe("RAG metadata (§10). Any change re-indexes and moves the knowledge version.").optional(), "updated_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).").optional(), "synthetic": z.literal(true) }).strict();
export type directory$Hospital = z.infer<typeof directory$Hospital>;

export const directory$Provider = z.object({ "provider_id": z.string().uuid(), "display_name": z.string().min(1), "credentials": z.union([z.string(), z.null()]).optional(), "specialty": z.string().min(1).describe("Free text from the directory. Matching a referral's specialty to this field is code, not an LLM judgement."), "subspecialties": z.array(z.string()).optional(), "languages": z.array(z.object({ "language": z.string().regex(new RegExp("^[a-z]{2}(-[A-Za-z0-9]{2,8})*$")).describe("BCP-47 language tag."), "fluency": z.enum(["native","fluent","conversational"]) }).strict()).describe("Backs the `provider_language` constraint (§2.1, and Scenario 2's language filter)."), "accepts_new_patients": z.boolean().describe("Separate fixture field from visit durations (§17)."), "accessibility_features": z.array(z.enum(["wheelchair_accessible","sign_language_interpreter","hearing_loop","accessible_parking","service_animal_friendly","large_print_materials"])), "first_visit_duration_minutes": z.number().int().gte(5).describe("First-visit duration is its own fixture field, distinct from the return-visit duration (§17)."), "return_visit_duration_minutes": z.number().int().gte(5), "hospital_ids": z.array(z.string().uuid()).min(1), "accepts_insurance": z.array(z.string()).describe("Payers this provider bills in general. NOT plan-level network status — that comes from a NetworkRecord (§2.2)."), "profile_text": z.union([z.string().max(8000).describe("Indexed for the RAG directory. Entity-based chunking preserves the name↔phone relation (§10)."), z.null().describe("Indexed for the RAG directory. Entity-based chunking preserves the name↔phone relation (§10).")]).describe("Indexed for the RAG directory. Entity-based chunking preserves the name↔phone relation (§10).").optional(), "contact_phone": z.union([z.string().describe("Always paired with its SourceBinding. A phone number shown to a patient must be sourced, never generated (§9 output guardrail)."), z.null().describe("Always paired with its SourceBinding. A phone number shown to a patient must be sourced, never generated (§9 output guardrail).")]).describe("Always paired with its SourceBinding. A phone number shown to a patient must be sourced, never generated (§9 output guardrail).").optional(), "contact_source": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.object({ "source_id": z.string().min(1), "field_path": z.string().min(1).describe("Where in the source this value came from. Every extracted value carries one (§2.3 source-bound extraction); a value with no binding is never presented as a fact.") }).strict(), z.null()];
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
  }).optional(), "knowledge_version": z.string().optional(), "updated_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).").optional(), "synthetic": z.literal(true) }).strict();
export type directory$Provider = z.infer<typeof directory$Provider>;

export const directory$Slot = z.object({ "slot_id": z.string().uuid(), "provider_id": z.string().uuid(), "hospital_id": z.string().uuid(), "start_at": z.string().datetime({ offset: true }).describe("Always UTC in storage (§17)."), "end_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "display_timezone": z.string().min(1).describe("Hospital's IANA zone, for rendering only. Never used for arithmetic."), "visit_type": z.enum(["first_visit","return_visit"]), "status": z.enum(["available","held","booked","expired"]).describe("`held` is a real state, but the system never pre-books or auto-selects a slot (§4). A slot the patient has not chosen is never held."), "version": z.number().int().gte(0).describe("Optimistic-concurrency token on the slot. A stale slot presented for booking returns 409 and is re-fetched rather than silently moved.").optional() }).strict();
export type directory$Slot = z.infer<typeof directory$Slot>;

export const directory$AvailabilityQuery = z.object({ "hospital_id": z.string().uuid(), "provider_id": z.string().uuid(), "visit_type": z.enum(["first_visit","return_visit"]), "earliest": z.unknown().superRefine((x, ctx) => {
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
  }).optional(), "latest": z.unknown().superRefine((x, ctx) => {
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
  }).optional(), "preferred_days": z.array(z.string()).optional(), "preferred_time_of_day": z.enum(["morning","afternoon","evening"]).describe("Optional: absent means no time-of-day preference.").optional() }).strict();
export type directory$AvailabilityQuery = z.infer<typeof directory$AvailabilityQuery>;

export const directory$AvailabilityResult = z.object({ "query": z.object({ "hospital_id": z.string().uuid(), "provider_id": z.string().uuid(), "visit_type": z.enum(["first_visit","return_visit"]), "earliest": z.unknown().superRefine((x, ctx) => {
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
  }).optional(), "latest": z.unknown().superRefine((x, ctx) => {
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
  }).optional(), "preferred_days": z.array(z.string()).optional(), "preferred_time_of_day": z.enum(["morning","afternoon","evening"]).describe("Optional: absent means no time-of-day preference.").optional() }).strict(), "slots": z.array(z.object({ "slot_id": z.string().uuid(), "provider_id": z.string().uuid(), "hospital_id": z.string().uuid(), "start_at": z.string().datetime({ offset: true }).describe("Always UTC in storage (§17)."), "end_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "display_timezone": z.string().min(1).describe("Hospital's IANA zone, for rendering only. Never used for arithmetic."), "visit_type": z.enum(["first_visit","return_visit"]), "status": z.enum(["available","held","booked","expired"]).describe("`held` is a real state, but the system never pre-books or auto-selects a slot (§4). A slot the patient has not chosen is never held."), "version": z.number().int().gte(0).describe("Optimistic-concurrency token on the slot. A stale slot presented for booking returns 409 and is re-fetched rather than silently moved.").optional() }).strict()), "result_kind": z.enum(["slots_found","no_slots_for_constraints","no_matching_provider","upstream_error"]).describe("§17: an API failure is a different result from a genuine 'no specialist'. Collapsing these into an empty list would let a fault be reported to the patient as a real answer."), "partial": z.boolean().describe("True when the upstream returned a truncated page. Partial agent output is never committed as final state (§2.1).").optional(), "evidence_id": z.string().uuid(), "synthetic": z.boolean() }).strict();
export type directory$AvailabilityResult = z.infer<typeof directory$AvailabilityResult>;

export const directory$ProviderCandidate = z.object({ "provider_id": z.string().uuid(), "hospital_id": z.string().uuid(), "distance_miles": z.union([z.number().gte(0).describe("Computed from coordinates in code, never from an embedding score (§10)."), z.null().describe("Computed from coordinates in code, never from an embedding score (§10).")]).describe("Computed from coordinates in code, never from an embedding score (§10).").optional(), "constraint_fit": z.array(z.object({ "constraint_id": z.string().uuid(), "satisfied": z.union([z.boolean().describe("Null means unknown, and unknown is never rounded up to satisfied."), z.null().describe("Null means unknown, and unknown is never rounded up to satisfied.")]).describe("Null means unknown, and unknown is never rounded up to satisfied."), "reason_code": z.enum(["ok","missing_mandatory_field","ambiguous_input","conflicting_evidence","stale_evidence","no_candidate_matched","no_slot_available","hard_constraint_unsatisfiable","document_missing","document_unreadable","coverage_unknown","provider_not_in_network","approval_expired","approval_payload_changed","upstream_unavailable","upstream_rate_limited","upstream_timeout","upstream_malformed_response","booking_outcome_unknown","policy_blocked","guardrail_blocked","budget_exhausted","deadline_exceeded","human_owned","other"]).describe("Controlled vocabulary for why a decision produced its action. `other` requires reason_detail to be populated."), "detail": z.union([z.string(), z.null()]).optional() }).strict()).describe("Per-candidate, per-constraint fit produced by the Constraint Analyst. Absent data is not scored over (§3): a missing fact yields null, not a filled-in guess."), "rank": z.union([z.number().int().gte(1).describe("Simple ranking is done in code (§3). The sub-agent explains ambiguity; it does not own the ordering."), z.null().describe("Simple ranking is done in code (§3). The sub-agent explains ambiguity; it does not own the ordering.")]).describe("Simple ranking is done in code (§3). The sub-agent explains ambiguity; it does not own the ordering.").optional(), "provenance": z.array(z.string().uuid()).describe("Candidate Researcher preserves provenance and filters (§3).").optional(), "evidence_ids": z.array(z.string().uuid()) }).strict();
export type directory$ProviderCandidate = z.infer<typeof directory$ProviderCandidate>;

// Inline enums, also exported by name.
export const directory$AccessibilityFeature = z.enum(["wheelchair_accessible", "sign_language_interpreter", "hearing_loop", "accessible_parking", "service_animal_friendly", "large_print_materials"]);
export type directory$AccessibilityFeature = z.infer<typeof directory$AccessibilityFeature>;

export const directory$AvailabilityResultKind = z.enum(["slots_found", "no_slots_for_constraints", "no_matching_provider", "upstream_error"]);
export type directory$AvailabilityResultKind = z.infer<typeof directory$AvailabilityResultKind>;

export const directory$HospitalAdapterType = z.enum(["instant_confirm", "review_required"]);
export type directory$HospitalAdapterType = z.infer<typeof directory$HospitalAdapterType>;

export const directory$LanguageFluency = z.enum(["native", "fluent", "conversational"]);
export type directory$LanguageFluency = z.infer<typeof directory$LanguageFluency>;

export const directory$SlotStatus = z.enum(["available", "held", "booked", "expired"]);
export type directory$SlotStatus = z.infer<typeof directory$SlotStatus>;

export const directory$SlotTimeOfDay = z.enum(["morning", "afternoon", "evening"]);
export type directory$SlotTimeOfDay = z.infer<typeof directory$SlotTimeOfDay>;
