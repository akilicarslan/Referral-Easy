import { z } from 'zod';
import {
  ACCESSIBILITY_FEATURES,
  APPOINTMENT_VERIFICATION_METHODS,
  AVAILABILITY_RESULT_KINDS,
  BOOKING_ATTEMPT_RESULTS,
  BOOKING_STATES,
  COVERAGE_STATUSES,
  DOCUMENT_KINDS,
  FAULT_TYPES,
  FRESHNESS_STATES,
  HOSPITAL_ADAPTER_TYPES,
  LANGUAGE_FLUENCIES,
  NOTIFICATION_CHANNELS,
  OUTBOX_STATUSES,
  PLAN_TYPES,
  RECONCILIATION_STATUSES,
  SLOT_STATUSES,
  SLOT_TIMES_OF_DAY,
  VISIT_TYPES,
} from './enums.js';

/**
 * Every schema here is a transcription of `packages/contracts/schemas/*.json`
 * (v1). Field names, required sets and enums are copied verbatim; the objects
 * are `.strict()` because the frozen schemas all set
 * `additionalProperties: false`. Request/response envelopes that exist only at
 * the HTTP surface of this mock are marked `MOCK-LEVEL` and are the only
 * additions.
 */

/* ------------------------------------------------------------------ scalars */

/** RFC 3339 with an explicit offset; stored UTC, rendered in the hospital zone. */
export const isoTimestamp = z
  .string()
  .regex(
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/,
    'expected an RFC 3339 timestamp with an explicit offset',
  );

export const uuidValue = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/, 'expected a UUID');

export const sha256Hex = z.string().regex(/^[a-f0-9]{64}$/, 'expected a lowercase sha256 hex digest');

export const ianaTimezone = z.string().min(1);

export const languageTag = z.string().regex(/^[a-z]{2}(-[A-Za-z0-9]{2,8})*$/, 'expected a BCP-47 tag');

export const dateOnly = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'expected an ISO date');

export const geoPoint = z
  .object({
    latitude: z.number().min(-90).max(90),
    longitude: z.number().min(-180).max(180),
  })
  .strict();

/* -------------------------------------------------------------------- enums */

export const bookingState = z.enum(BOOKING_STATES);
export const slotStatus = z.enum(SLOT_STATUSES);
export const availabilityResultKind = z.enum(AVAILABILITY_RESULT_KINDS);
export const hospitalAdapterType = z.enum(HOSPITAL_ADAPTER_TYPES);
export const outboxStatus = z.enum(OUTBOX_STATUSES);
export const bookingAttemptResult = z.enum(BOOKING_ATTEMPT_RESULTS);
export const reconciliationStatus = z.enum(RECONCILIATION_STATUSES);
export const visitType = z.enum(VISIT_TYPES);
export const slotTimeOfDay = z.enum(SLOT_TIMES_OF_DAY);
export const coverageStatus = z.enum(COVERAGE_STATUSES);
export const freshnessState = z.enum(FRESHNESS_STATES);
export const documentKind = z.enum(DOCUMENT_KINDS);
export const notificationChannel = z.enum(NOTIFICATION_CHANNELS);
export const appointmentVerificationMethod = z.enum(APPOINTMENT_VERIFICATION_METHODS);
export const accessibilityFeature = z.enum(ACCESSIBILITY_FEATURES);
export const languageFluency = z.enum(LANGUAGE_FLUENCIES);
export const planType = z.enum(PLAN_TYPES);
export const faultType = z.enum(FAULT_TYPES);

/* ----------------------------------------------------------------- the time */

export const money = z
  .object({ amount_minor: z.number().int(), currency: z.string().min(3).max(3) })
  .strict();

export const languageSkill = z
  .object({ language: languageTag, fluency: languageFluency })
  .strict();

export const sourceBinding = z
  .object({ source_id: z.string().min(1), field_path: z.string().min(1) })
  .strict();

/* --------------------------------------------------------------- directory */

export const location = z
  .object({
    location_id: uuidValue,
    address_line1: z.string().min(1),
    address_line2: z.string().nullish(),
    city: z.string().min(1),
    state: z.string().regex(/^[A-Z]{2}$/),
    postal_code: z.string().regex(/^[0-9]{5}$/),
    geo: geoPoint,
    timezone: ianaTimezone,
  })
  .strict();

export const hospital = z
  .object({
    hospital_id: uuidValue,
    name: z.string().min(1),
    location_id: uuidValue,
    timezone: ianaTimezone,
    adapter_type: hospitalAdapterType,
    accepts_new_patients: z.boolean(),
    accessibility_features: z.array(accessibilityFeature),
    languages_supported: z.array(languageTag).optional(),
    accepted_plan_ids: z.array(uuidValue).optional(),
    directory_blurb: z.string().max(4000).nullish(),
    knowledge_version: z.string().optional(),
    updated_at: isoTimestamp.optional(),
    synthetic: z.literal(true),
  })
  .strict();

export const provider = z
  .object({
    provider_id: uuidValue,
    display_name: z.string().min(1),
    credentials: z.string().nullish(),
    specialty: z.string().min(1),
    subspecialties: z.array(z.string()).optional(),
    languages: z.array(languageSkill),
    accepts_new_patients: z.boolean(),
    accessibility_features: z.array(z.string()),
    first_visit_duration_minutes: z.number().int().min(5),
    return_visit_duration_minutes: z.number().int().min(5),
    hospital_ids: z.array(uuidValue).min(1),
    accepts_insurance: z.array(z.string()),
    profile_text: z.string().max(8000).nullish(),
    contact_phone: z.string().nullish(),
    contact_source: sourceBinding.nullish(),
    knowledge_version: z.string().optional(),
    updated_at: isoTimestamp.optional(),
    synthetic: z.literal(true),
  })
  .strict();

export const slot = z
  .object({
    slot_id: uuidValue,
    provider_id: uuidValue,
    hospital_id: uuidValue,
    start_at: isoTimestamp,
    end_at: isoTimestamp,
    display_timezone: ianaTimezone,
    visit_type: visitType,
    status: slotStatus,
    version: z.number().int().min(0).optional(),
  })
  .strict();

export const availabilityQuery = z
  .object({
    hospital_id: uuidValue,
    provider_id: uuidValue,
    visit_type: visitType,
    earliest: isoTimestamp.nullish(),
    latest: isoTimestamp.nullish(),
    preferred_days: z.array(z.string()).optional(),
    preferred_time_of_day: slotTimeOfDay.optional(),
  })
  .strict();

export const availabilityResult = z
  .object({
    query: availabilityQuery,
    slots: z.array(slot),
    result_kind: availabilityResultKind,
    partial: z.boolean().optional(),
    evidence_id: uuidValue,
    synthetic: z.boolean(),
  })
  .strict();

/* ----------------------------------------------------------------- coverage */

export const insurancePlan = z
  .object({
    plan_id: uuidValue,
    payer_name: z.string().min(1),
    plan_name: z.string().min(1),
    plan_type: planType,
    network_name: z.string().nullish(),
    requires_referral: z.boolean().optional(),
    requires_prior_auth_for_specialist: z.boolean().optional(),
    synthetic: z.literal(true),
  })
  .strict();

export const networkRecord = z
  .object({
    record_id: uuidValue,
    plan_id: uuidValue,
    provider_id: uuidValue,
    hospital_id: uuidValue.nullish(),
    status: coverageStatus,
    effective_from: dateOnly,
    effective_to: dateOnly.nullish(),
    evidence_id: uuidValue,
  })
  .strict();

export const coverageResult = z
  .object({
    coverage_id: uuidValue,
    plan_id: uuidValue,
    provider_id: uuidValue.nullish(),
    hospital_id: uuidValue.nullish(),
    accepts_insurance: z.boolean().nullish(),
    in_network_for_plan: z.boolean().nullish(),
    status: coverageStatus,
    referral_required: z.boolean().nullish(),
    prior_auth_required: z.boolean().nullish(),
    conflict: z.boolean().optional(),
    conflicting_evidence_ids: z.array(uuidValue).optional(),
    winning_evidence_id: uuidValue.nullish(),
    freshness: z
      .object({ state: freshnessState, age_seconds: z.number().int().min(0) })
      .strict(),
    evidence_ids: z.array(uuidValue),
    disclaimer_key: z.literal('coverage_not_a_guarantee').optional(),
    synthetic: z.literal(true),
  })
  .strict();

export const referralRequirement = z
  .object({
    requirement_id: uuidValue,
    hospital_id: uuidValue,
    plan_id: uuidValue.nullish(),
    visit_type: visitType,
    required_documents: z.array(documentKind),
    max_document_age_days: z.number().int().min(0).nullish(),
    requires_prior_auth_reference: z.boolean().optional(),
    evidence_id: uuidValue,
  })
  .strict();

/* ---------------------------------------------------------------- documents */

export const documentFixture = z
  .object({
    document_id: uuidValue,
    patient_id: uuidValue,
    kind: documentKind,
    filename: z.string().min(1),
    mime_type: z.enum(['text/plain', 'application/pdf', 'text/markdown']),
    text: z.string().max(50000),
    issued_at: isoTimestamp,
    source_organization: z.string().min(1),
    synthetic: z.literal(true),
    contains_injection_attempt: z.boolean(),
  })
  .strict();

export const documentCheck = z
  .object({
    kind: documentKind,
    status: z.enum(['satisfied', 'missing', 'expired', 'unreadable', 'not_required']),
    document_id: uuidValue.nullish(),
    reason_code: z.string().optional(),
    checked_at: isoTimestamp,
    evidence_ids: z.array(uuidValue).optional(),
    run_id: uuidValue.optional(),
  })
  .strict();

export const readinessResult = z
  .object({
    readiness_id: uuidValue,
    workflow_id: uuidValue,
    hospital_id: uuidValue,
    plan_id: uuidValue.nullish(),
    visit_type: visitType,
    checks: z.array(documentCheck),
    satisfied: z.boolean(),
    missing_kinds: z.array(documentKind).optional(),
    can_proceed: z.boolean().optional(),
    evidence_ids: z.array(uuidValue),
    checked_at: isoTimestamp,
  })
  .strict();

/* ------------------------------------------------------------------ booking */

export const bookingPayload = z
  .object({
    patient_id: uuidValue,
    provider_id: uuidValue,
    hospital_id: uuidValue,
    slot_id: uuidValue,
    visit_type: visitType,
    document_ids: z.array(uuidValue).optional(),
    contact_phone: z.string().nullish(),
  })
  .strict();

export const reconciliationState = z
  .object({
    status: reconciliationStatus,
    started_at: isoTimestamp,
    lookup_attempts: z.number().int().min(0),
    last_lookup_at: isoTimestamp.nullish(),
    escalation_reason: z.string().nullish(),
  })
  .strict();

export const bookingRequest = z
  .object({
    booking_request_id: uuidValue,
    workflow_id: uuidValue,
    command_id: uuidValue,
    idempotency_key: z.string().min(8).max(200),
    payload_hash: sha256Hex,
    payload: bookingPayload,
    approval_id: uuidValue.nullish(),
    state: bookingState,
    adapter_type: hospitalAdapterType,
    external_reference: z.string().nullish(),
    attempt_count: z.number().int().min(0),
    reconciliation: reconciliationState.nullish(),
    created_at: isoTimestamp,
    updated_at: isoTimestamp,
    reschedule_of: uuidValue.nullish(),
  })
  .strict();

export const bookingAttempt = z
  .object({
    attempt: z.number().int().min(1),
    idempotency_key: z.string().min(8),
    started_at: isoTimestamp,
    finished_at: isoTimestamp.nullish(),
    result: bookingAttemptResult,
    http_status: z.number().int().nullish(),
    raw_response_ref: z.string().nullish(),
  })
  .strict();

export const appointmentRecord = z
  .object({
    appointment_id: uuidValue,
    booking_request_id: uuidValue,
    hospital_id: uuidValue,
    provider_id: uuidValue,
    patient_id: uuidValue,
    slot_id: uuidValue.optional(),
    start_at: isoTimestamp,
    end_at: isoTimestamp.nullish(),
    display_timezone: ianaTimezone,
    state: bookingState,
    verified: z.boolean(),
    verification_method: appointmentVerificationMethod.optional(),
    synthetic: z.literal(true),
  })
  .strict();

export const notificationOutboxEntry = z
  .object({
    outbox_id: uuidValue,
    booking_request_id: uuidValue,
    channel: notificationChannel,
    recipient: z.string().min(1).optional(),
    template_key: z.string().min(1).optional(),
    status: outboxStatus,
    attempts: z.number().int().min(0),
    next_attempt_at: isoTimestamp.nullish(),
    last_error: z.string().nullish(),
    created_at: isoTimestamp,
  })
  .strict();

export const lookupResult = z
  .object({
    outcome: z.enum(['created', 'not_booked', 'unknown']),
    state: bookingState.nullish(),
    external_reference: z.string().nullish(),
    reconciled_at: isoTimestamp,
    looked_up_vendor_payload: z.string().nullish(),
  })
  .strict();

/* ------------------------------------------------- MOCK-LEVEL HTTP surface */

export const createBookingRequest = z
  .object({
    patient_id: uuidValue,
    provider_id: uuidValue,
    hospital_id: uuidValue,
    slot_id: uuidValue,
    visit_type: visitType,
    document_ids: z.array(uuidValue).optional(),
    contact_phone: z.string().nullish(),
    workflow_id: uuidValue.optional(),
    command_id: uuidValue.optional(),
    approval_id: uuidValue.nullish(),
    idempotency_key: z.string().min(8).max(200).optional(),
    expected_slot_version: z.number().int().min(0).optional(),
  })
  .strict();

export const bookingResponse = z
  .object({
    outcome: bookingAttemptResult,
    booking: bookingRequest,
    appointment: appointmentRecord.nullish(),
    attempts: z.array(bookingAttempt),
    /** Transport fact: was this request served from the stored key? Never a confirmation. */
    idempotent_replay: z.boolean(),
  })
  .strict();

export const lookupResponse = z
  .object({
    outcome: bookingAttemptResult,
    booking: bookingRequest,
    appointment: appointmentRecord.nullish(),
    attempts: z.array(bookingAttempt),
    reconciliation: reconciliationState.nullish(),
    lookup: lookupResult.nullish(),
  })
  .strict();

export const cancelRequest = z
  .object({
    reason: z.string().max(500).optional(),
    idempotency_key: z.string().min(8).max(200).optional(),
  })
  .strict();

export const cancelResponse = z
  .object({
    outcome: bookingAttemptResult,
    booking: bookingRequest,
    appointment: appointmentRecord.nullish(),
    idempotent_replay: z.boolean(),
  })
  .strict();

export const rescheduleRequest = z
  .object({
    new_slot_id: uuidValue,
    idempotency_key: z.string().min(8).max(200),
    expected_slot_version: z.number().int().min(0).optional(),
    reason: z.string().max(500).optional(),
  })
  .strict();

export const rescheduleResponse = z
  .object({
    outcome: bookingAttemptResult,
    booking: bookingRequest,
    appointment: appointmentRecord.nullish(),
    superseded_booking: bookingRequest.nullish(),
    superseded_appointment: appointmentRecord.nullish(),
    idempotent_replay: z.boolean(),
    /** True only when the replacement was verified first. */
    old_booking_cancelled: z.boolean(),
    cancellation_deferred_reason: z.string().nullish(),
  })
  .strict();

export const coverageLookupRequest = z
  .object({
    plan_id: uuidValue,
    // `provider_id` is nullable because the frozen CoverageResult allows a
    // hospital-level answer (`provider_id: null`): the fixture set contains one
    // (coverage result ...0009, a conflict between a stale public description
    // and the hospital's structured record) and the network_conflict journey
    // asserts on it. A lookup must name a provider OR a hospital.
    provider_id: uuidValue.nullish(),
    hospital_id: uuidValue.nullish(),
  })
  .strict();

export const readinessRequest = z
  .object({
    workflow_id: uuidValue,
    patient_id: uuidValue,
    hospital_id: uuidValue,
    plan_id: uuidValue.nullish(),
    visit_type: visitType,
    document_ids: z.array(uuidValue),
  })
  .strict();

export const errorEnvelope = z
  .object({
    error: z
      .object({
        code: z.string().min(1),
        message: z.string().min(1),
        request_id: z.string().min(1),
        fault_type: faultType.nullish(),
        scenario_id: z.string().nullish(),
        retry_after_seconds: z.number().int().min(0).nullish(),
        detail: z.unknown().optional(),
      })
      .strict(),
  })
  .strict();

/* ---------------------------------------------------------- named registry */

/**
 * The named schemas that make up the served OpenAPI document. Each entry is
 * emitted into `components.schemas`, so the document and the runtime validation
 * can never drift: both come from these objects.
 */
export const SCHEMA_REGISTRY = {
  Location: location,
  Hospital: hospital,
  Provider: provider,
  Slot: slot,
  AvailabilityQuery: availabilityQuery,
  AvailabilityResult: availabilityResult,
  InsurancePlan: insurancePlan,
  NetworkRecord: networkRecord,
  CoverageResult: coverageResult,
  ReferralRequirement: referralRequirement,
  DocumentFixture: documentFixture,
  DocumentCheck: documentCheck,
  ReadinessResult: readinessResult,
  BookingPayload: bookingPayload,
  BookingRequest: bookingRequest,
  ReconciliationState: reconciliationState,
  BookingAttempt: bookingAttempt,
  AppointmentRecord: appointmentRecord,
  NotificationOutboxEntry: notificationOutboxEntry,
  CreateBookingRequest: createBookingRequest,
  BookingResponse: bookingResponse,
  LookupResponse: lookupResponse,
  CancelRequest: cancelRequest,
  CancelResponse: cancelResponse,
  RescheduleRequest: rescheduleRequest,
  RescheduleResponse: rescheduleResponse,
  CoverageLookupRequest: coverageLookupRequest,
  ReadinessRequest: readinessRequest,
  ErrorEnvelope: errorEnvelope,
} as const;

export type SchemaName = keyof typeof SCHEMA_REGISTRY;

export type CreateBookingRequest = z.infer<typeof createBookingRequest>;
export type BookingResponse = z.infer<typeof bookingResponse>;
export type LookupResponse = z.infer<typeof lookupResponse>;
export type CancelResponse = z.infer<typeof cancelResponse>;
export type RescheduleRequest = z.infer<typeof rescheduleRequest>;
export type RescheduleResponse = z.infer<typeof rescheduleResponse>;
export type CoverageLookupRequest = z.infer<typeof coverageLookupRequest>;
export type ReadinessRequest = z.infer<typeof readinessRequest>;
export type BookingPayloadShape = z.infer<typeof bookingPayload>;
export type AvailabilityQueryShape = z.infer<typeof availabilityQuery>;
export type BookingRequestShape = z.infer<typeof bookingRequest>;
export type AppointmentRecordShape = z.infer<typeof appointmentRecord>;
export type SlotShape = z.infer<typeof slot>;
export type CoverageResultShape = z.infer<typeof coverageResult>;
export type ErrorEnvelopeShape = z.infer<typeof errorEnvelope>;
