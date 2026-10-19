import { afterEach, describe, expect, it } from 'vitest';
import { HOSPITAL, bookingBody, call, makeHarness, type Harness } from './helpers.js';

/** Field names that belong to a vendor dialect and must never cross the boundary. */
const VENDOR_ONLY_FIELDS = [
  'queue_state',
  'case_number',
  'ticket_status',
  'service_category',
  'appointment_ref',
  'client_key',
  'practitioner_code',
  'facility_ref',
  'referral_ticket',
  'slot_status',
  'decline_reason',
  'refusal_code',
];

function collectKeys(value: unknown, keys: Set<string>): void {
  if (Array.isArray(value)) {
    for (const entry of value) collectKeys(entry, keys);
    return;
  }
  if (value && typeof value === 'object') {
    for (const [key, child] of Object.entries(value)) {
      keys.add(key);
      collectKeys(child, keys);
    }
  }
}

/**
 * Two hospitals, two dialects, ONE internal shape.
 *
 * The evidence is the raw vendor payloads kept in `vendor_calls`: they must
 * differ from each other, while the normalised booking they produce is
 * expressed in exactly the same vocabulary.
 */
describe('adapter normalisation', () => {
  let harness: Harness | null = null;
  afterEach(async () => {
    await harness?.cleanup();
    harness = null;
  });

  it('normalises both dialects to the same field names', async () => {
    harness = await makeHarness();

    const instant = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { hospital_id: HOSPITAL.instantConfirm }),
      headers: { 'idempotency-key': 'adapter-instant-confirm-01' },
    });
    const review = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { hospital_id: HOSPITAL.reviewRequired }),
      headers: { 'idempotency-key': 'adapter-review-required-01' },
    });

    expect(instant.status).toBe(200);
    expect(review.status).toBe(200);

    // Same keys, different values: that is what "one internal shape" means.
    const withoutMethod = (value: Record<string, unknown>): string[] =>
      Object.keys(value).filter((key) => key !== 'verification_method').sort();
    expect(Object.keys(instant.body.booking).sort()).toEqual(Object.keys(review.body.booking).sort());
    expect(withoutMethod(instant.body.appointment)).toEqual(withoutMethod(review.body.appointment));
    expect(instant.body.booking.adapter_type).toBe('instant_confirm');
    expect(review.body.booking.adapter_type).toBe('review_required');

    // The two hospitals genuinely disagree about whether a booking exists yet.
    expect(instant.body.booking.state).toBe('CONFIRMED');
    expect(instant.body.appointment.verified).toBe(true);
    expect(instant.body.appointment.verification_method).toBe('create_response');

    expect(review.body.booking.state).toBe('PENDING_REVIEW');
    expect(review.body.appointment.verified).toBe(false);
    expect(review.body.appointment.verification_method).toBeUndefined();

    for (const response of [instant.body, review.body]) {
      const keys = new Set<string>();
      collectKeys(response, keys);
      for (const vendorField of VENDOR_ONLY_FIELDS) {
        expect(keys.has(vendorField)).toBe(false);
      }
    }
  });

  it('keeps two vendor payload shapes as evidence, neither of them normalised', async () => {
    harness = await makeHarness();
    const instantKey = 'adapter-evidence-instant-01';
    const reviewKey = 'adapter-evidence-review-01';

    await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { hospital_id: HOSPITAL.instantConfirm }),
      headers: { 'idempotency-key': instantKey },
    });
    await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { hospital_id: HOSPITAL.reviewRequired }),
      headers: { 'idempotency-key': reviewKey },
    });

    const instantCalls = await call(harness.app, {
      method: 'GET',
      url: `/_diagnostics/vendor-calls?idempotency_key=${instantKey}&operation=create`,
    });
    const reviewCalls = await call(harness.app, {
      method: 'GET',
      url: `/_diagnostics/vendor-calls?idempotency_key=${reviewKey}&operation=create`,
    });

    expect(instantCalls.body.count).toBe(1);
    expect(reviewCalls.body.count).toBe(1);
    const instantRaw = instantCalls.body.calls[0];
    const reviewRaw = reviewCalls.body.calls[0];

    // Dialect 1: flat, `api_version` + `client_key`, appointment nested with an
    // UPPERCASE one-shot status, answered 201.
    expect(instantRaw.adapter_type).toBe('instant_confirm');
    expect(instantRaw.http_status).toBe(201);
    expect(Object.keys(instantRaw.request).sort()).toEqual([
      'api_version',
      'client_key',
      'patient_ref',
      'practitioner',
      'service_line',
      'site_code',
      'slot',
    ]);
    expect(instantRaw.response.appointment.status).toBe('SCHEDULED');
    expect(instantRaw.response.appointment.appointment_ref).toMatch(/^NL-\d{8}-/);

    // Dialect 2: nested `case`, a window instead of a slot, a ticket instead of
    // a client key, answered 202 with a QUEUE STATE.
    expect(reviewRaw.adapter_type).toBe('review_required');
    expect(reviewRaw.http_status).toBe(202);
    expect(Object.keys(reviewRaw.request)).toEqual(['case']);
    expect(Object.keys(reviewRaw.request.case).sort()).toEqual([
      'attachments',
      'facility_ref',
      'patient_ref',
      'provider_ref',
      'referral_ticket',
      'requested_window',
      'service_category',
    ]);
    expect(reviewRaw.request.case.requested_window).toHaveProperty('from');
    expect(reviewRaw.response.decision.queue_state).toBe('AWAITING_REVIEW');
    expect(reviewRaw.response.case_number).toMatch(/^SA-\d{4}-/);

    // The two raw requests are different documents, not one shape spelled twice.
    expect(JSON.stringify(instantRaw.request)).not.toBe(JSON.stringify(reviewRaw.request));
    // And the normalised booking they produced carries neither dialect's names.
    const booking = harness.built.context.store.findBookingByKey(instantKey)!;
    expect(booking.external_reference).not.toContain('appointment_ref');
    expect(booking.external_reference).not.toContain('queue');
  });

  it('refuses a booking whose slot belongs to another provider', async () => {
    harness = await makeHarness();
    const instant = bookingBody(harness, { hospital_id: HOSPITAL.instantConfirm });
    const reviewer = bookingBody(harness, { hospital_id: HOSPITAL.reviewRequired });

    const mismatch = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: { ...instant, slot_id: reviewer.slot_id },
      headers: { 'idempotency-key': 'adapter-slot-mismatch-01' },
    });

    expect(mismatch.status).toBe(400);
    expect(mismatch.body.error.code).toBe('slot_mismatch');
    expect(harness.built.context.store.listBookings({}).length).toBe(0);
  });
});
