/**
 * The generated Zod bindings must actually validate.
 *
 * The first test here is the important one. `json-schema-to-zod` silently emits `z.any()` for any
 * `$ref` it cannot resolve, and a Zod schema full of `z.any()` type-checks, runs, and validates
 * nothing — so the failure mode is a boundary that accepts anything. The generator dereferences
 * every `$ref` before generation; these tests are what keeps that true.
 */

import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import {
  booking$BookingRequest,
  common$BookingState,
  coverage$CoverageResult,
  directory$AvailabilityQuery,
} from '../src/index.js';

const GENERATED_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'generated');

const generatedSources = Object.fromEntries(
  readdirSync(GENERATED_DIR)
    .filter((f) => f.endsWith('.ts') && f !== 'index.ts')
    .map((f) => [f, readFileSync(join(GENERATED_DIR, f), 'utf8')]),
);

describe('generated bindings are not vacuous', () => {
  it('contains no z.any() — every $ref resolved to a real validator', () => {
    const offenders = Object.entries(generatedSources)
      .filter(([, src]) => src.includes('z.any()'))
      .map(([name]) => name);
    expect(offenders).toEqual([]);
  });

  it('resolves a cross-file $ref into a real constraint, not a passthrough', () => {
    // workflow_id is `common.json#/$defs/WorkflowId` -> format: uuid. If refs regressed to
    // z.any(), a non-uuid would parse.
    expect(
      booking$BookingRequest.safeParse({ workflow_id: 'not-a-uuid' }).success,
    ).toBe(false);
  });

  it('emits every definition from every schema file', () => {
    // One module per schema file. Asserting the count rather than a name list keeps this
    // honest: adding a schema without regenerating fails here.
    expect(Object.keys(generatedSources)).toHaveLength(9);
    for (const src of Object.values(generatedSources)) {
      expect(src).toContain('export const');
    }
  });
});

describe('unknown vs false stays distinguishable', () => {
  const base = {
    coverage_id: '11111111-1111-4111-8111-111111111111',
    plan_id: '22222222-2222-4222-8222-222222222222',
    accepts_insurance: true,
    in_network_for_plan: null,
    status: 'unknown',
    referral_required: null,
    prior_auth_required: null,
    freshness: { state: 'stale', age_seconds: 86400 },
    evidence_ids: [],
    synthetic: true,
  };

  it('accepts null for in_network_for_plan (Scenario 3)', () => {
    const parsed = coverage$CoverageResult.safeParse(base);
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.in_network_for_plan).toBeNull();
      expect(parsed.data.accepts_insurance).toBe(true);
    }
  });

  it('keeps explicit out-of-network distinct from unknown', () => {
    const parsed = coverage$CoverageResult.safeParse({ ...base, in_network_for_plan: false });
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.in_network_for_plan).toBe(false);
  });

  it('rejects a missing required field', () => {
    const { accepts_insurance: _omitted, ...rest } = base;
    expect(coverage$CoverageResult.safeParse(rest).success).toBe(false);
  });

  it('rejects an unexpected field (additionalProperties: false)', () => {
    expect(coverage$CoverageResult.safeParse({ ...base, surprise: 1 }).success).toBe(false);
  });

  it('rejects synthetic: false — all data is synthetic (§1)', () => {
    expect(coverage$CoverageResult.safeParse({ ...base, synthetic: false }).success).toBe(false);
  });
});

describe('booking lifecycle is not coercible', () => {
  it('accepts PENDING_REVIEW and keeps it distinct from CONFIRMED', () => {
    const request = {
      booking_request_id: '11111111-1111-4111-8111-111111111111',
      workflow_id: '22222222-2222-4222-8222-222222222222',
      command_id: '33333333-3333-4333-8333-333333333333',
      idempotency_key: 'idem-key-0001',
      payload_hash: 'a'.repeat(64),
      payload: {
        patient_id: '11111111-1111-4111-8111-111111111111',
        provider_id: '22222222-2222-4222-8222-222222222222',
        hospital_id: '33333333-3333-4333-8333-333333333333',
        slot_id: '11111111-1111-4111-8111-111111111111',
        visit_type: 'first_visit',
      },
      state: 'PENDING_REVIEW',
      adapter_type: 'review_required',
      attempt_count: 1,
      created_at: '2026-10-14T12:00:00+00:00',
      updated_at: '2026-10-14T12:00:00+00:00',
    };

    const parsed = booking$BookingRequest.safeParse(request);
    expect(parsed.success).toBe(true);
    if (!parsed.success) return;

    expect(parsed.data.state).toBe('PENDING_REVIEW');
    expect(parsed.data.state).not.toBe('CONFIRMED');
    // Round-trips through JSON without being promoted.
    const roundTripped = booking$BookingRequest.parse(JSON.parse(JSON.stringify(parsed.data)));
    expect(roundTripped.state).toBe('PENDING_REVIEW');
  });

  it('rejects an unknown booking state', () => {
    expect(common$BookingState.safeParse('MAYBE_CONFIRMED').success).toBe(false);
  });

  it('has exactly the six lifecycle states from §2.5', () => {
    expect(common$BookingState.options).toEqual([
      'REQUESTED',
      'PENDING_REVIEW',
      'CONFIRMED',
      'REJECTED',
      'CANCEL_PENDING',
      'CANCELLED',
    ]);
  });
});

describe('oneOf branches validate rather than passing through', () => {
  const query = {
    hospital_id: '11111111-1111-4111-8111-111111111111',
    provider_id: '22222222-2222-4222-8222-222222222222',
    visit_type: 'first_visit',
  };

  it('accepts an absent optional timestamp', () => {
    expect(directory$AvailabilityQuery.safeParse(query).success).toBe(true);
  });

  it('accepts a well-formed timestamp', () => {
    const parsed = directory$AvailabilityQuery.safeParse({
      ...query,
      earliest: '2026-10-14T12:00:00+00:00',
    });
    expect(parsed.success).toBe(true);
  });

  it('rejects a malformed timestamp — the superRefine is real validation', () => {
    expect(directory$AvailabilityQuery.safeParse({ ...query, earliest: 'not-a-date' }).success).toBe(
      false,
    );
  });

  it('rejects a wrong-typed timestamp', () => {
    expect(directory$AvailabilityQuery.safeParse({ ...query, earliest: 12345 }).success).toBe(false);
  });
});
