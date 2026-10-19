import type { z } from 'zod';
import type {
  appointmentRecord,
  availabilityQuery,
  availabilityResult,
  bookingAttempt,
  bookingPayload,
  bookingRequest,
  coverageResult,
  documentCheck,
  documentFixture,
  hospital,
  insurancePlan,
  location,
  networkRecord,
  notificationOutboxEntry,
  provider,
  readinessResult,
  reconciliationState,
  referralRequirement,
  slot,
} from './zod.js';

/**
 * The single seam to the frozen contract types.
 *
 * Today these are inferred from `zod.ts`, which is a transcription of
 * `packages/contracts/schemas/*.json`. When the generated
 * `@referraleasy/contracts` package lands, this is the only file that has to
 * change: every other module imports its types from here.
 */
export type Location = z.infer<typeof location>;
export type Hospital = z.infer<typeof hospital>;
export type Provider = z.infer<typeof provider>;
export type Slot = z.infer<typeof slot>;
export type AvailabilityQuery = z.infer<typeof availabilityQuery>;
export type AvailabilityResult = z.infer<typeof availabilityResult>;
export type InsurancePlan = z.infer<typeof insurancePlan>;
export type NetworkRecord = z.infer<typeof networkRecord>;
export type CoverageResult = z.infer<typeof coverageResult>;
export type ReferralRequirement = z.infer<typeof referralRequirement>;
export type DocumentFixture = z.infer<typeof documentFixture>;
export type DocumentCheck = z.infer<typeof documentCheck>;
export type ReadinessResult = z.infer<typeof readinessResult>;
export type BookingPayload = z.infer<typeof bookingPayload>;
export type BookingRequest = z.infer<typeof bookingRequest>;
export type BookingAttempt = z.infer<typeof bookingAttempt>;
export type ReconciliationState = z.infer<typeof reconciliationState>;
export type AppointmentRecord = z.infer<typeof appointmentRecord>;
export type NotificationOutboxEntry = z.infer<typeof notificationOutboxEntry>;
