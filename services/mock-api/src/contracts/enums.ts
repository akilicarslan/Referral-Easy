/**
 * The mock API's vocabulary, sourced from `@referraleasy/contracts`.
 *
 * This module used to restate every enum as a local literal union, transcribed by hand from
 * `packages/contracts/schemas/*.json`. That was a second source of truth for the same vocabulary
 * — exactly what §1.1 forbids — and it was only tolerable because the generated package did not
 * exist yet. It does now, so these are derivations rather than copies.
 *
 * Export names are unchanged, so no call site moved. Each type is declared via `z.infer` and each
 * array is annotated from that type, so the literal unions survive regardless of how Zod types
 * `.options`.
 *
 * The one thing that stays local is `FAULT_TYPES`: fault injection is the mock hospital's own
 * behaviour, not a cross-service contract, so it has no business in the shared schemas.
 */

import { z } from 'zod';

import {
  booking$AppointmentVerificationMethod,
  booking$BookingAttemptResult,
  booking$NotificationChannel,
  booking$OutboxStatus,
  booking$ReconciliationStatus,
  common$BookingState,
  common$CoverageStatus,
  common$DocumentKind,
  common$FreshnessState,
  common$LanguageFluency,
  common$VisitType,
  coverage$PlanType,
  directory$AccessibilityFeature,
  directory$AvailabilityResultKind,
  directory$HospitalAdapterType,
  directory$SlotStatus,
  directory$SlotTimeOfDay,
} from '@referraleasy/contracts';

// ── Booking lifecycle (§2.5) ────────────────────────────────────────────────
export type BookingState = z.infer<typeof common$BookingState>;
export const BOOKING_STATES: readonly BookingState[] = common$BookingState.options;

/**
 * States in which a slot is claimed by a live booking request.
 *
 * Derived here rather than in the schema: it is the mock hospital's own rule about which requests
 * hold a slot, not a fact about the lifecycle itself. `REJECTED` and `CANCELLED` release the slot.
 */
export const ACTIVE_BOOKING_STATES: readonly BookingState[] = [
  'REQUESTED',
  'PENDING_REVIEW',
  'CONFIRMED',
  'CANCEL_PENDING',
];

export type SlotStatus = z.infer<typeof directory$SlotStatus>;
export const SLOT_STATUSES: readonly SlotStatus[] = directory$SlotStatus.options;

export type AvailabilityResultKind = z.infer<typeof directory$AvailabilityResultKind>;
export const AVAILABILITY_RESULT_KINDS: readonly AvailabilityResultKind[] =
  directory$AvailabilityResultKind.options;

export type HospitalAdapterType = z.infer<typeof directory$HospitalAdapterType>;
export const HOSPITAL_ADAPTER_TYPES: readonly HospitalAdapterType[] =
  directory$HospitalAdapterType.options;

export type OutboxStatus = z.infer<typeof booking$OutboxStatus>;
export const OUTBOX_STATUSES: readonly OutboxStatus[] = booking$OutboxStatus.options;

export type BookingAttemptResult = z.infer<typeof booking$BookingAttemptResult>;
export const BOOKING_ATTEMPT_RESULTS: readonly BookingAttemptResult[] =
  booking$BookingAttemptResult.options;

export type ReconciliationStatus = z.infer<typeof booking$ReconciliationStatus>;
export const RECONCILIATION_STATUSES: readonly ReconciliationStatus[] =
  booking$ReconciliationStatus.options;

// ── Clinical / directory vocabulary ─────────────────────────────────────────
export type VisitType = z.infer<typeof common$VisitType>;
export const VISIT_TYPES: readonly VisitType[] = common$VisitType.options;

export type SlotTimeOfDay = z.infer<typeof directory$SlotTimeOfDay>;
export const SLOT_TIMES_OF_DAY: readonly SlotTimeOfDay[] = directory$SlotTimeOfDay.options;

export type CoverageStatus = z.infer<typeof common$CoverageStatus>;
export const COVERAGE_STATUSES: readonly CoverageStatus[] = common$CoverageStatus.options;

export type FreshnessState = z.infer<typeof common$FreshnessState>;
export const FRESHNESS_STATES: readonly FreshnessState[] = common$FreshnessState.options;

export type DocumentKind = z.infer<typeof common$DocumentKind>;
export const DOCUMENT_KINDS: readonly DocumentKind[] = common$DocumentKind.options;

export type NotificationChannel = z.infer<typeof booking$NotificationChannel>;
export const NOTIFICATION_CHANNELS: readonly NotificationChannel[] =
  booking$NotificationChannel.options;

export type AppointmentVerificationMethod = z.infer<typeof booking$AppointmentVerificationMethod>;
export const APPOINTMENT_VERIFICATION_METHODS: readonly AppointmentVerificationMethod[] =
  booking$AppointmentVerificationMethod.options;

export type AccessibilityFeature = z.infer<typeof directory$AccessibilityFeature>;
export const ACCESSIBILITY_FEATURES: readonly AccessibilityFeature[] =
  directory$AccessibilityFeature.options;

export type LanguageFluency = z.infer<typeof common$LanguageFluency>;
export const LANGUAGE_FLUENCIES: readonly LanguageFluency[] = common$LanguageFluency.options;

export type PlanType = z.infer<typeof coverage$PlanType>;
export const PLAN_TYPES: readonly PlanType[] = coverage$PlanType.options;

// ── Local to the mock hospital ──────────────────────────────────────────────
/**
 * The eleven injectable faults. Ordering is the canonical order used by the
 * seeded random mode, so it must not change without bumping the fault version.
 *
 * Deliberately NOT in the shared schemas: fault injection is this service's own
 * behaviour, not a contract any other service is bound by.
 */
export const FAULT_TYPES = [
  'latency',
  'rate_limit_429',
  'service_unavailable_503',
  'malformed_schema',
  'empty_valid_result',
  'stale_slot_409',
  'timeout_before_commit',
  'timeout_after_commit',
  'duplicate_event',
  'stale_coverage_evidence',
  'notification_failure',
] as const;
export type FaultType = (typeof FAULT_TYPES)[number];
