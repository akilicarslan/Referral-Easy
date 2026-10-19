// GENERATED — do not edit. Source: packages/contracts/schemas/*.json
// Regenerate with: pnpm gen:contracts
import { z } from 'zod';

export const common$ContractVersion = z.literal("v1").describe("Bumped when any schema in this directory makes a breaking change.");
export type common$ContractVersion = z.infer<typeof common$ContractVersion>;

export const common$Uuid = z.string().uuid();
export type common$Uuid = z.infer<typeof common$Uuid>;

export const common$WorkflowId = z.string().uuid().describe("One end-to-end patient referral journey. Distinct from conversation_id and command_id (§15).");
export type common$WorkflowId = z.infer<typeof common$WorkflowId>;

export const common$ConversationId = z.string().uuid().describe("Transport-level chat session. Never used as a booking key.");
export type common$ConversationId = z.infer<typeof common$ConversationId>;

export const common$CommandId = z.string().uuid().describe("Idempotency key for a client-replayed command (§7). Replay with the same value must be deduplicated.");
export type common$CommandId = z.infer<typeof common$CommandId>;

export const common$TaskId = z.string().uuid();
export type common$TaskId = z.infer<typeof common$TaskId>;

export const common$PatientId = z.string().uuid();
export type common$PatientId = z.infer<typeof common$PatientId>;

export const common$ProviderId = z.string().uuid();
export type common$ProviderId = z.infer<typeof common$ProviderId>;

export const common$HospitalId = z.string().uuid();
export type common$HospitalId = z.infer<typeof common$HospitalId>;

export const common$LocationId = z.string().uuid();
export type common$LocationId = z.infer<typeof common$LocationId>;

export const common$PlanId = z.string().uuid();
export type common$PlanId = z.infer<typeof common$PlanId>;

export const common$SlotId = z.string().uuid();
export type common$SlotId = z.infer<typeof common$SlotId>;

export const common$DocumentId = z.string().uuid();
export type common$DocumentId = z.infer<typeof common$DocumentId>;

export const common$BookingRequestId = z.string().uuid();
export type common$BookingRequestId = z.infer<typeof common$BookingRequestId>;

export const common$HandoffId = z.string().uuid();
export type common$HandoffId = z.infer<typeof common$HandoffId>;

export const common$EvidenceId = z.string().uuid();
export type common$EvidenceId = z.infer<typeof common$EvidenceId>;

export const common$ReleaseManifestId = z.string().regex(new RegExp("^rel-[0-9]{8}-[a-z0-9]{7}$")).describe("Immutable release identifier (§13). Conversations are pinned to one.");
export type common$ReleaseManifestId = z.infer<typeof common$ReleaseManifestId>;

export const common$StateVersion = z.number().int().gte(0).describe("Monotonic optimistic-concurrency token on a workflow (§7). Every write must present the version it read.");
export type common$StateVersion = z.infer<typeof common$StateVersion>;

export const common$Generation = z.number().int().gte(0).describe("Monotonic ownership epoch. A worker whose generation is behind the row's cannot write (§7).");
export type common$Generation = z.infer<typeof common$Generation>;

export const common$Lease = z.object({ "owner_id": z.string().min(1).describe("Replica/worker that currently owns the work."), "generation": z.number().int().gte(0).describe("Monotonic ownership epoch. A worker whose generation is behind the row's cannot write (§7)."), "expires_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).") }).strict().describe("Ownership lease on active work. A stale worker (expired lease or old generation) cannot write workflow state (§7).");
export type common$Lease = z.infer<typeof common$Lease>;

export const common$IsoTimestamp = z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17).");
export type common$IsoTimestamp = z.infer<typeof common$IsoTimestamp>;

export const common$IanaTimezone = z.string().min(1).describe("IANA timezone name. Appointment slots are stored UTC and displayed in this zone (§17).");
export type common$IanaTimezone = z.infer<typeof common$IanaTimezone>;

export const common$LanguageTag = z.string().regex(new RegExp("^[a-z]{2}(-[A-Za-z0-9]{2,8})*$")).describe("BCP-47 language tag.");
export type common$LanguageTag = z.infer<typeof common$LanguageTag>;

export const common$Money = z.object({ "amount_minor": z.number().int().describe("Minor units (cents). Never a float."), "currency": z.string().regex(new RegExp("^[A-Z]{3}$")) }).strict();
export type common$Money = z.infer<typeof common$Money>;

export const common$DurationSeconds = z.number().int().gte(0).describe("Duration in seconds.");
export type common$DurationSeconds = z.infer<typeof common$DurationSeconds>;

export const common$Miles = z.number().gte(0).describe("Distance. Always computed from coordinates — never from an embedding score (§10).");
export type common$Miles = z.infer<typeof common$Miles>;

export const common$GeoPoint = z.object({ "latitude": z.number().gte(-90).lte(90), "longitude": z.number().gte(-180).lte(180) }).strict();
export type common$GeoPoint = z.infer<typeof common$GeoPoint>;

export const common$ResultOutcome = z.enum(["completed","needs_input","no_match","retryable_error","terminal_error","outcome_unknown"]).describe("Application-level task result (§5). This enum is NEVER exposed as an A2A protocol state — the adapter maps it (see A2ATaskState and the mapping table below).");
export type common$ResultOutcome = z.infer<typeof common$ResultOutcome>;

export const common$A2ATaskState = z.enum(["submitted","working","input-required","auth-required","completed","canceled","failed","rejected","unknown"]).describe("Standard A2A task lifecycle states. Protocol-level only.");
export type common$A2ATaskState = z.infer<typeof common$A2ATaskState>;

export const common$BookingState = z.enum(["REQUESTED","PENDING_REVIEW","CONFIRMED","REJECTED","CANCEL_PENDING","CANCELLED"]).describe("Booking lifecycle (§2.5). REQUESTED and PENDING_REVIEW are NEVER presented to the patient as confirmed, and HTTP 200 never implies CONFIRMED.");
export type common$BookingState = z.infer<typeof common$BookingState>;

export const common$WorkflowState = z.enum(["INTAKE","COVERAGE_CHECK","PROVIDER_MATCHING","AWAITING_HOSPITAL_CHOICE","AWAITING_DOCTOR_CHOICE","READINESS_CHECK","AWAITING_SLOT_CHOICE","AWAITING_FINAL_APPROVAL","BOOKING_IN_FLIGHT","BOOKED","BOOKING_UNVERIFIED","RECOVERY","HUMAN_HANDOFF","ABANDONED","CANCELLED","FAILED"]).describe("Workflow state machine (§7). The policy table (§4) defines which agent/tool may run in each state; the coordinator LLM cannot override it.");
export type common$WorkflowState = z.infer<typeof common$WorkflowState>;

export const common$ConstraintKind = z.enum(["hard","soft"]).describe("Hard constraints are never relaxed without an explicit patient decision (§2.1). Soft preferences change only on explicit patient choice (§2.4).");
export type common$ConstraintKind = z.infer<typeof common$ConstraintKind>;

export const common$ConstraintField = z.enum(["specialty","max_distance_miles","provider_language","preferred_day","preferred_time_of_day","accepts_new_patients","accessibility","insurance_plan","visit_type"]);
export type common$ConstraintField = z.infer<typeof common$ConstraintField>;

export const common$CoverageStatus = z.enum(["in_network","out_of_network","unknown"]).describe("'accepts this insurance' and 'in-network for this plan' are separate facts (§2.2). A contradiction or a gap resolves to unknown — never to a guess, and never to a payment guarantee.");
export type common$CoverageStatus = z.infer<typeof common$CoverageStatus>;

export const common$DocumentKind = z.enum(["referral_letter","prior_report","authorization_reference"]).describe("Documents required by some hospitals before booking (§2.3). Document text is DATA, never instructions (§9).");
export type common$DocumentKind = z.infer<typeof common$DocumentKind>;

export const common$VisitType = z.enum(["first_visit","return_visit"]);
export type common$VisitType = z.infer<typeof common$VisitType>;

export const common$HandoffReasonClass = z.enum(["technical_outage","no_match","missing_document","conflicting_sources","prolonged_unknown_booking","patient_requested","no_reliable_recovery"]).describe("Handoff reasons are classed, not free text (§11).");
export type common$HandoffReasonClass = z.infer<typeof common$HandoffReasonClass>;

export const common$GuardrailDecision = z.enum(["allow","block","clarify"]).describe("Input-guardrail verdict (§9). Uncertainty and layer disagreement resolve to clarify or HITL — never to a silent allow.");
export type common$GuardrailDecision = z.infer<typeof common$GuardrailDecision>;

export const common$GuardrailLayer = z.enum(["regex","decision_model","local_classifier"]).describe("One of the three input-guardrail layers (§9).");
export type common$GuardrailLayer = z.infer<typeof common$GuardrailLayer>;

export const common$GuardrailCategory = z.enum(["benign","off_topic","prompt_injection","abuse","pii_leak_attempt","emergency","medical_advice_request","oversized_input","malformed_input"]);
export type common$GuardrailCategory = z.infer<typeof common$GuardrailCategory>;

export const common$DegradationLevel = z.enum(["full","rerank_down","embeddings_down","opensearch_down","static_seed","no_source"]).describe("Retrieval degradation ladder (§10). Every level below `full` must surface a visible `degraded` flag; `static_seed` is deterministic and makes no LLM call; `no_source` goes to HITL.");
export type common$DegradationLevel = z.infer<typeof common$DegradationLevel>;

export const common$FreshnessState = z.enum(["fresh","stale","unknown"]);
export type common$FreshnessState = z.infer<typeof common$FreshnessState>;

export const common$ReasonCode = z.enum(["ok","missing_mandatory_field","ambiguous_input","conflicting_evidence","stale_evidence","no_candidate_matched","no_slot_available","hard_constraint_unsatisfiable","document_missing","document_unreadable","coverage_unknown","provider_not_in_network","approval_expired","approval_payload_changed","upstream_unavailable","upstream_rate_limited","upstream_timeout","upstream_malformed_response","booking_outcome_unknown","policy_blocked","guardrail_blocked","budget_exhausted","deadline_exceeded","human_owned","other"]).describe("Controlled vocabulary for why a decision produced its action. `other` requires reason_detail to be populated.");
export type common$ReasonCode = z.infer<typeof common$ReasonCode>;

export const common$EvidenceRef = z.object({ "evidence_id": z.string().uuid() }).strict().describe("Pointer to a stored tool/source result. The envelope carries references, not payloads (§5).");
export type common$EvidenceRef = z.infer<typeof common$EvidenceRef>;

export const common$Evidence = z.object({ "evidence_id": z.string().uuid(), "source": z.string().min(1).describe("Which subsystem produced this."), "source_id": z.string().min(1).describe("Entity id at the source. Kept alongside the evidence id so freshness can be re-checked."), "authoritative": z.boolean().describe("True for structured API results. §10: on a conflict the authoritative structured result wins — a stale description or document text may never hide a missing result."), "retrieved_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "freshness": z.object({ "state": z.enum(["fresh","stale","unknown"]), "age_seconds": z.number().int().gte(0) }).strict(), "synthetic": z.boolean().describe("True for every fixture and every offline-profile response. Admin charts must label or exclude synthetic rows so canned output is never presented as a measurement (§12, §18)."), "invalidated_by": z.union([z.array(z.string().uuid()).describe("Evidence this record supersedes or contradicts. Populated when contradiction is detected rather than silently picking a winner."), z.null().describe("Evidence this record supersedes or contradicts. Populated when contradiction is detected rather than silently picking a winner.")]).describe("Evidence this record supersedes or contradicts. Populated when contradiction is detected rather than silently picking a winner.").optional() }).strict();
export type common$Evidence = z.infer<typeof common$Evidence>;

export const common$SourceBinding = z.object({ "source_id": z.string().min(1), "field_path": z.string().min(1).describe("Where in the source this value came from. Every extracted value carries one (§2.3 source-bound extraction); a value with no binding is never presented as a fact.") }).strict();
export type common$SourceBinding = z.infer<typeof common$SourceBinding>;

export const common$MissingField = z.object({ "field": z.string().min(1), "reason_code": z.enum(["ok","missing_mandatory_field","ambiguous_input","conflicting_evidence","stale_evidence","no_candidate_matched","no_slot_available","hard_constraint_unsatisfiable","document_missing","document_unreadable","coverage_unknown","provider_not_in_network","approval_expired","approval_payload_changed","upstream_unavailable","upstream_rate_limited","upstream_timeout","upstream_malformed_response","booking_outcome_unknown","policy_blocked","guardrail_blocked","budget_exhausted","deadline_exceeded","human_owned","other"]).describe("Controlled vocabulary for why a decision produced its action. `other` requires reason_detail to be populated."), "question": z.union([z.string().describe("What to ask the patient for this field. Null when the gap is not patient-answerable (escalate instead of asking)."), z.null().describe("What to ask the patient for this field. Null when the gap is not patient-answerable (escalate instead of asking).")]).describe("What to ask the patient for this field. Null when the gap is not patient-answerable (escalate instead of asking).").optional(), "mandatory": z.boolean().describe("Mandatory fields (specialty, approval, valid slot — §2.1) block progress; the rest do not.").optional() }).strict();
export type common$MissingField = z.infer<typeof common$MissingField>;

export const common$LanguageSkill = z.object({ "language": z.string().regex(new RegExp("^[a-z]{2}(-[A-Za-z0-9]{2,8})*$")).describe("BCP-47 language tag."), "fluency": z.enum(["native","fluent","conversational"]) }).strict();
export type common$LanguageSkill = z.infer<typeof common$LanguageSkill>;

export const common$ActionRef = z.string().regex(new RegExp("^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")).describe("Namespaced action identifier (owner:action). The policy table (§4) maps state → allowed actions; the coordinator cannot exceed it.");
export type common$ActionRef = z.infer<typeof common$ActionRef>;

// Inline enums, also exported by name.
export const common$LanguageFluency = z.enum(["native", "fluent", "conversational"]);
export type common$LanguageFluency = z.infer<typeof common$LanguageFluency>;
