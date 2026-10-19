/**
 * The mock API's transcribed schemas must agree with `@referraleasy/contracts`.
 *
 * `src/contracts/zod.ts` restates `packages/contracts/schemas/*.json` by hand. That is a second
 * source of truth for shapes §1.1 says must have one, and it was only tolerable while the generated
 * package did not exist. It exists now, and nothing else compares the two — a schema edited on one
 * side would be discovered by the gateway at runtime, as a validation mismatch on a live request.
 *
 * This test is the guard for that gap until the transcription is replaced by re-exports. It is
 * deliberately an *equivalence* check rather than a diff: two schemas can be written differently
 * and still accept and reject the same values, which is the property that actually matters.
 *
 * When `zod.ts` is reduced to re-exports, this test becomes trivially true and can be deleted.
 */

import { describe, expect, it } from 'vitest';
import type { ZodType } from 'zod';

import {
  booking$AppointmentRecord,
  booking$BookingPayload,
  booking$BookingRequest,
  coverage$CoverageResult,
  directory$AvailabilityResult,
  directory$Provider,
  directory$Slot,
} from '@referraleasy/contracts';

import * as local from '../src/contracts/zod.js';

const U1 = '11111111-1111-4111-8111-111111111111';
const U2 = '22222222-2222-4222-8222-222222222222';
const U3 = '33333333-3333-4333-8333-333333333333';
const U4 = '44444444-4444-4444-8444-444444444444';

/** Valid instances drawn from the same fixture shapes the service actually serves. */
const VALID_COVERAGE = {
  coverage_id: U1,
  plan_id: U2,
  accepts_insurance: true,
  in_network_for_plan: null,
  status: 'unknown',
  referral_required: null,
  prior_auth_required: null,
  freshness: { state: 'stale', age_seconds: 64800 },
  evidence_ids: [],
  synthetic: true,
};

const VALID_SLOT = {
  slot_id: U1,
  provider_id: U2,
  hospital_id: U3,
  start_at: '2026-10-16T14:00:00+00:00',
  end_at: '2026-10-16T14:30:00+00:00',
  display_timezone: 'America/Chicago',
  visit_type: 'first_visit',
  status: 'available',
};

const VALID_APPOINTMENT = {
  appointment_id: U1,
  booking_request_id: U2,
  hospital_id: U3,
  provider_id: U4,
  patient_id: U1,
  slot_id: U2,
  start_at: '2026-10-16T14:00:00+00:00',
  display_timezone: 'America/Chicago',
  state: 'PENDING_REVIEW',
  verified: false,
  synthetic: true,
};

const VALID_PAYLOAD = {
  patient_id: U1,
  provider_id: U2,
  hospital_id: U3,
  slot_id: U4,
  visit_type: 'first_visit',
};

const VALID_BOOKING_REQUEST = {
  booking_request_id: U1,
  workflow_id: U2,
  command_id: U3,
  idempotency_key: 'idem-key-0001',
  payload_hash: 'a'.repeat(64),
  payload: VALID_PAYLOAD,
  state: 'REQUESTED',
  adapter_type: 'instant_confirm',
  attempt_count: 0,
  created_at: '2026-10-14T12:00:00+00:00',
  updated_at: '2026-10-14T12:00:00+00:00',
};

const VALID_AVAILABILITY = {
  query: {
    hospital_id: U1,
    provider_id: U2,
    visit_type: 'first_visit',
  },
  slots: [],
  result_kind: 'no_slots_for_constraints',
  evidence_id: U3,
  synthetic: true,
};

type Case = {
  /** Name in `src/contracts/zod.ts`. */
  local: keyof typeof local;
  /** The contract binding it transcribes. */
  shared: ZodType;
  label: string;
  valid: Record<string, unknown>;
  invalid: Record<string, unknown>[];
};

const CASES: Case[] = [
  {
    local: 'coverageResult',
    shared: coverage$CoverageResult,
    label: 'CoverageResult',
    valid: VALID_COVERAGE,
    invalid: [
      // Omitted required field.
      Object.fromEntries(Object.entries(VALID_COVERAGE).filter(([k]) => k !== 'status')),
      // `synthetic` is const true (§1).
      { ...VALID_COVERAGE, synthetic: false },
      // additionalProperties: false.
      { ...VALID_COVERAGE, surprise: 1 },
      // Not a uuid.
      { ...VALID_COVERAGE, coverage_id: 'nope' },
      // status is a closed enum.
      { ...VALID_COVERAGE, status: 'maybe' },
    ],
  },
  {
    local: 'slot',
    shared: directory$Slot,
    label: 'Slot',
    valid: VALID_SLOT,
    invalid: [
      Object.fromEntries(Object.entries(VALID_SLOT).filter(([k]) => k !== 'status')),
      { ...VALID_SLOT, status: 'maybe' },
      { ...VALID_SLOT, visit_type: 'third_visit' },
      { ...VALID_SLOT, surprise: 1 },
    ],
  },
  {
    local: 'appointmentRecord',
    shared: booking$AppointmentRecord,
    label: 'AppointmentRecord',
    valid: VALID_APPOINTMENT,
    invalid: [
      Object.fromEntries(Object.entries(VALID_APPOINTMENT).filter(([k]) => k !== 'verified')),
      // PENDING_REVIEW is legal; MAYBE_CONFIRMED is not.
      { ...VALID_APPOINTMENT, state: 'MAYBE_CONFIRMED' },
      { ...VALID_APPOINTMENT, surprise: 1 },
    ],
  },
  {
    local: 'bookingPayload',
    shared: booking$BookingPayload,
    label: 'BookingPayload',
    valid: VALID_PAYLOAD,
    invalid: [
      Object.fromEntries(Object.entries(VALID_PAYLOAD).filter(([k]) => k !== 'slot_id')),
      { ...VALID_PAYLOAD, visit_type: 'third_visit' },
      { ...VALID_PAYLOAD, surprise: 1 },
    ],
  },
  {
    local: 'bookingRequest',
    shared: booking$BookingRequest,
    label: 'BookingRequest',
    valid: VALID_BOOKING_REQUEST,
    invalid: [
      Object.fromEntries(Object.entries(VALID_BOOKING_REQUEST).filter(([k]) => k !== 'payload_hash')),
      // A 63-character hash: the pattern is part of the contract.
      { ...VALID_BOOKING_REQUEST, payload_hash: 'a'.repeat(63) },
      { ...VALID_BOOKING_REQUEST, state: 'MAYBE_CONFIRMED' },
      { ...VALID_BOOKING_REQUEST, surprise: 1 },
    ],
  },
  {
    local: 'availabilityResult',
    shared: directory$AvailabilityResult,
    label: 'AvailabilityResult',
    valid: VALID_AVAILABILITY,
    invalid: [
      Object.fromEntries(Object.entries(VALID_AVAILABILITY).filter(([k]) => k !== 'result_kind')),
      // §17: an upstream failure must be distinguishable from a genuine no-match.
      { ...VALID_AVAILABILITY, result_kind: 'something_else' },
      { ...VALID_AVAILABILITY, surprise: 1 },
    ],
  },
  {
    local: 'provider',
    shared: directory$Provider,
    label: 'Provider',
    valid: {
      provider_id: U1,
      display_name: 'Dr. Example',
      specialty: 'cardiology',
      languages: [{ language: 'en', fluency: 'native' }],
      accepts_new_patients: true,
      accessibility_features: ['wheelchair_accessible'],
      first_visit_duration_minutes: 45,
      return_visit_duration_minutes: 20,
      hospital_ids: [U2],
      accepts_insurance: ['Synthetic Payer'],
      synthetic: true,
    },
    invalid: [
      // Duration must be >= 5.
      {
        provider_id: U1,
        display_name: 'Dr. Example',
        specialty: 'cardiology',
        languages: [],
        accepts_new_patients: true,
        accessibility_features: [],
        first_visit_duration_minutes: 1,
        return_visit_duration_minutes: 20,
        hospital_ids: [U2],
        accepts_insurance: [],
        synthetic: true,
      },
      // hospital_ids requires at least one entry.
      {
        provider_id: U1,
        display_name: 'Dr. Example',
        specialty: 'cardiology',
        languages: [],
        accepts_new_patients: true,
        accessibility_features: [],
        first_visit_duration_minutes: 45,
        return_visit_duration_minutes: 20,
        hospital_ids: [],
        accepts_insurance: [],
        synthetic: true,
      },
    ],
  },
];

const accepts = (schema: ZodType, value: unknown): boolean => schema.safeParse(value).success;

describe('mock-api schemas agree with @referraleasy/contracts', () => {
  it('covers a non-trivial set of shared types', () => {
    // Guards against the file being emptied of cases and passing vacuously.
    expect(CASES.length).toBeGreaterThanOrEqual(7);
  });

  for (const testCase of CASES) {
    describe(testCase.label, () => {
      const localSchema = local[testCase.local] as unknown as ZodType;

      it('both accept the valid instance', () => {
        expect(accepts(localSchema, testCase.valid)).toBe(true);
        expect(accepts(testCase.shared, testCase.valid)).toBe(true);
      });

      testCase.invalid.forEach((instance, index) => {
        it(`both reject invalid instance #${index + 1}`, () => {
          const localVerdict = accepts(localSchema, instance);
          const sharedVerdict = accepts(testCase.shared, instance);
          expect(
            localVerdict,
            `mock-api and @referraleasy/contracts disagree on ${testCase.label} #${index + 1}: ` +
              `local=${localVerdict}, shared=${sharedVerdict}. ` +
              `The transcription in src/contracts/zod.ts has drifted from the frozen schemas.`,
          ).toBe(sharedVerdict);
          expect(localVerdict).toBe(false);
        });
      });
    });
  }
});
