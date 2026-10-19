import { afterEach, describe, expect, it } from 'vitest';
import { HOSPITAL, bookingBody, call, fastTimeoutScenarios, makeHarness, slotFor, type Harness } from './helpers.js';

/**
 * Invariants 2, 4 and 5, stated as the observable behaviour a caller depends on:
 *
 *  - an idempotency key survives an unknown outcome, and reconciliation uses the
 *    SAME key (invariant 2);
 *  - PENDING_REVIEW and REQUESTED are never presented as confirmed, and a 200
 *    does not mean an appointment exists (invariant 4);
 *  - `outcome_unknown` is not a rejection, and the two timeouts are not the same
 *    event (invariant 5).
 */
function hasKey(value: unknown, name: string): boolean {
  if (Array.isArray(value)) return value.some((entry) => hasKey(entry, name));
  if (value && typeof value === 'object') {
    return Object.entries(value).some(([key, child]) => key === name || hasKey(child, name));
  }
  return false;
}

describe('booking lifecycle', () => {
  let harness: Harness | null = null;
  afterEach(async () => {
    await harness?.cleanup();
    harness = null;
  });

  it('never presents a review queue as a confirmed appointment', async () => {
    harness = await makeHarness();
    const created = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { hospital_id: HOSPITAL.reviewRequired }),
      headers: { 'idempotency-key': 'lifecycle-review-000001' },
    });

    expect(created.status).toBe(200);
    expect(created.body.booking.state).toBe('PENDING_REVIEW');
    expect(created.body.appointment.state).toBe('PENDING_REVIEW');
    expect(created.body.appointment.verified).toBe(false);
    expect(created.body.appointment.verification_method).toBeUndefined();
    expect(created.body.attempts[0].result).not.toBe('created'.replace('created', 'confirmed'));

    // There is deliberately no boolean to misread, at any depth of the payload.
    expect(hasKey(created.body, 'confirmed')).toBe(false);
    expect(hasKey(created.body, 'is_confirmed')).toBe(false);

    // The same answer on the read path, and the key still resolves it.
    const read = await call(harness.app, { method: 'GET', url: `/appointments/${created.body.booking.booking_request_id}` });
    expect(read.body.booking.state).toBe('PENDING_REVIEW');

    // Reconciling against the hospital confirms the CASE exists; it does not
    // upgrade the state, and no path turns a review queue into a confirmation.
    const reconciled = await call(harness.app, {
      method: 'GET',
      url: '/appointments/lookup_request?idempotency_key=lifecycle-review-000001',
    });
    expect(reconciled.body.lookup.outcome).toBe('created');
    expect(reconciled.body.appointment.state).toBe('PENDING_REVIEW');
    expect(reconciled.body.appointment.verified).toBe(true);
    expect(reconciled.body.booking.state).toBe('PENDING_REVIEW');
  });

  it('answers a timeout before commit with a clean no, and a timeout after commit with a maybe', async () => {
    harness = await makeHarness({ scenarios: fastTimeoutScenarios() });
    const store = harness.built.context.store;

    // --- the hospital never committed anything -------------------------------
    const slotBefore = slotFor(harness, { index: 0 });
    const before = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { slot_id: slotBefore.slot_id }),
      headers: { 'idempotency-key': 'lifecycle-timeout-before-01', 'x-fault-scenario': 'timeout_before_commit' },
    });
    expect(before.status).toBe(504);
    expect(before.body.error.code).toBe('booking_outcome_unknown');
    expect(before.body.error.detail.attempt_result).toBe('timeout_before_commit');
    expect(before.body.error.detail.booking_exists).toBe(false);
    expect(before.body.error.detail.idempotency_key).toBe('lifecycle-timeout-before-01');
    expect(before.body.error.detail.reconcile_with).toContain('lifecycle-timeout-before-01');

    // Nothing was claimed, so the slot is still there for anyone else.
    expect(store.getSlot(slotBefore.slot_id)!.status).toBe('available');
    expect(store.countAppointmentsForSlot(slotBefore.slot_id)).toBe(0);

    const resolvedBefore = await call(harness.app, {
      method: 'GET',
      url: '/appointments/lookup_request?idempotency_key=lifecycle-timeout-before-01',
    });
    expect(resolvedBefore.body.outcome).toBe('timeout_before_commit');
    expect(resolvedBefore.body.lookup.outcome).toBe('not_booked');
    expect(resolvedBefore.body.appointment).toBeNull();
    expect(resolvedBefore.body.reconciliation.status).toBe('resolved_not_booked');

    // --- the hospital committed, the caller never heard ----------------------
    const slotAfter = slotFor(harness, { index: 1 });
    const after = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { slot_id: slotAfter.slot_id }),
      headers: { 'idempotency-key': 'lifecycle-timeout-after-01', 'x-fault-scenario': 'timeout_after_commit' },
    });
    expect(after.status).toBe(504);
    expect(after.body.error.code).toBe('booking_outcome_unknown');
    expect(after.body.error.detail.attempt_result).toBe('timeout_after_commit');
    expect(after.body.error.detail.booking_exists).toBe(true);

    // The write stuck. The appointment carries the hospital's OWN confirmation
    // (`create_response`), while what the caller knows is still unresolved —
    // and it is that second fact, not the first, that the 504 reports.
    expect(store.getSlot(slotAfter.slot_id)!.status).toBe('booked');
    const pending = store.findBookingByKey('lifecycle-timeout-after-01')!;
    expect(pending.state).toBe('CONFIRMED');
    expect(pending.reconciliation?.status).toBe('pending');
    const orphan = store.getAppointmentByBooking(pending.booking_request_id)!;
    expect(orphan.verified).toBe(true);
    expect(orphan.verification_method).toBe('create_response');

    // The SAME key resolves it. A different key is not a substitute for it.
    const other = await call(harness.app, { method: 'GET', url: '/appointments/lookup_request?idempotency_key=lifecycle-some-other-01' });
    expect(other.status).toBe(404);
    expect(other.body.error.code).toBe('unknown_idempotency_key');

    const resolvedAfter = await call(harness.app, {
      method: 'GET',
      url: '/appointments/lookup_request?idempotency_key=lifecycle-timeout-after-01',
    });
    expect(resolvedAfter.body.lookup.outcome).toBe('created');
    expect(resolvedAfter.body.reconciliation.status).toBe('resolved_booked');
    expect(resolvedAfter.body.appointment.verified).toBe(true);
    expect(resolvedAfter.body.appointment.verification_method).toBe('lookup_request');
    expect(resolvedAfter.body.booking.external_reference).toBeTruthy();

    // Fifth invariant: neither timeout is a rejection, and they are not the
    // same event wearing one name.
    expect(before.body.error.detail.booking_exists).not.toBe(after.body.error.detail.booking_exists);
    expect(before.body.error.detail.booking_request_id).not.toBe(after.body.error.detail.booking_request_id);
    for (const body of [before.body, after.body, resolvedAfter.body]) {
      expect(JSON.stringify(body)).not.toContain('"REJECTED"');
      expect(hasKey(body, 'rejected')).toBe(false);
    }
  });

  it('rejects a booking the hospital refuses without calling it unknown', async () => {
    harness = await makeHarness();
    const store = harness.built.context.store;
    const slot = slotFor(harness);

    const refused = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { slot_id: slot.slot_id }),
      headers: { 'idempotency-key': 'lifecycle-refused-000001', 'x-vendor-decision': 'decline' },
    });

    expect(refused.status).toBe(200);
    expect(refused.body.outcome).toBe('rejected_by_hospital');
    expect(refused.body.booking.state).toBe('REJECTED');
    expect(refused.body.appointment.state).toBe('REJECTED');
    expect(refused.body.appointment.verified).toBe(false);
    expect(refused.body.attempts[0].result).toBe('rejected_by_hospital');
    // The refusal is a definite answer, and the slot goes back to the pool.
    // The record of the refusal is kept; it is not a live appointment, so it
    // does not occupy the slot.
    expect(store.getSlot(slot.slot_id)!.status).toBe('available');
    expect(store.getAppointmentBySlot(slot.slot_id)).toBeUndefined();
    expect(store.countAppointmentsForSlot(slot.slot_id)).toBe(1);

    // A refusal cannot be cancelled, and asking again is refused the same way.
    const cancelled = await call(harness.app, {
      method: 'POST',
      url: `/appointments/${refused.body.booking.booking_request_id}/cancel`,
      payload: {},
    });
    expect(cancelled.status).toBe(409);
    expect(cancelled.body.error.code).toBe('not_cancellable');
  });

  it('cancels an appointment and puts its slot back into circulation', async () => {
    harness = await makeHarness();
    const store = harness.built.context.store;
    const slot = slotFor(harness);
    const created = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { slot_id: slot.slot_id }),
      headers: { 'idempotency-key': 'lifecycle-cancel-000001' },
    });
    const bookingId = created.body.booking.booking_request_id;

    const cancelled = await call(harness.app, {
      method: 'POST',
      url: `/appointments/${bookingId}/cancel`,
      payload: { reason: 'patient asked to cancel' },
    });
    expect(cancelled.status).toBe(200);
    expect(cancelled.body.booking.state).toBe('CANCELLED');
    expect(cancelled.body.appointment.state).toBe('CANCELLED');
    expect(cancelled.body.idempotent_replay).toBe(false);
    expect(store.getSlot(slot.slot_id)!.status).toBe('available');

    // Cancelling twice is a replay, not a second cancellation.
    const again = await call(harness.app, { method: 'POST', url: `/appointments/${bookingId}/cancel`, payload: {} });
    expect(again.status).toBe(200);
    expect(again.body.idempotent_replay).toBe(true);
    expect(again.body.booking.state).toBe('CANCELLED');

    // ...and the freed slot can genuinely be booked by someone else.
    const rebooked = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { slot_id: slot.slot_id, patient_id: created.body.booking.payload.patient_id }),
      headers: { 'idempotency-key': 'lifecycle-rebook-000001' },
    });
    expect(rebooked.status).toBe(200);
    expect(rebooked.body.booking.state).toBe('CONFIRMED');
    expect(store.countAppointmentsForSlot(slot.slot_id)).toBe(2);
  });

  it('secures and verifies the replacement before cancelling the original', async () => {
    harness = await makeHarness();
    const store = harness.built.context.store;
    const originalSlot = slotFor(harness, { index: 0 });
    const replacementSlot = slotFor(harness, { index: 1 });
    expect(replacementSlot.slot_id).not.toBe(originalSlot.slot_id);

    const original = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { slot_id: originalSlot.slot_id }),
      headers: { 'idempotency-key': 'lifecycle-resched-orig-01' },
    });
    const originalId = original.body.booking.booking_request_id;

    const moved = await call(harness.app, {
      method: 'POST',
      url: `/appointments/${originalId}/reschedule`,
      payload: { new_slot_id: replacementSlot.slot_id, idempotency_key: 'lifecycle-resched-new-01' },
    });

    expect(moved.status).toBe(200);
    expect(moved.body.old_booking_cancelled).toBe(true);
    expect(moved.body.cancellation_deferred_reason).toBeNull();
    expect(moved.body.booking.state).toBe('CONFIRMED');
    expect(moved.body.booking.reschedule_of).toBe(originalId);
    expect(moved.body.appointment.verified).toBe(true);
    expect(moved.body.superseded_booking.state).toBe('CANCELLED');
    expect(moved.body.superseded_appointment.state).toBe('CANCELLED');

    // The patient has exactly one live appointment, on exactly one slot.
    expect(store.getSlot(replacementSlot.slot_id)!.status).toBe('booked');
    expect(store.getSlot(originalSlot.slot_id)!.status).toBe('available');
    expect(store.listBookings({ state: 'CONFIRMED' })).toHaveLength(1);
    expect(store.getAppointmentBySlot(replacementSlot.slot_id)!.verified).toBe(true);
  });

  it('keeps the original appointment when the replacement is not secured', async () => {
    harness = await makeHarness();
    const store = harness.built.context.store;
    const originalSlot = slotFor(harness, { index: 0 });
    const takenSlot = slotFor(harness, { index: 1 });

    const original = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { slot_id: originalSlot.slot_id }),
      headers: { 'idempotency-key': 'lifecycle-keep-orig-0001' },
    });
    const originalId = original.body.booking.booking_request_id;

    // Someone else takes the slot the patient wanted to move to.
    const otherPatient = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { slot_id: takenSlot.slot_id }),
      headers: { 'idempotency-key': 'lifecycle-other-patient-01' },
    });
    expect(otherPatient.status).toBe(200);

    const blocked = await call(harness.app, {
      method: 'POST',
      url: `/appointments/${originalId}/reschedule`,
      payload: { new_slot_id: takenSlot.slot_id, idempotency_key: 'lifecycle-resched-blocked-01' },
    });
    expect(blocked.status).toBe(409);
    expect(blocked.body.error.detail.attempt_result).toBe('slot_conflict');

    // The original booking is untouched, still verified, still holding its slot.
    const stillThere = store.findBookingById(originalId)!;
    expect(stillThere.state).toBe('CONFIRMED');
    expect(store.getAppointmentByBooking(originalId)!.state).toBe('CONFIRMED');
    expect(store.getAppointmentByBooking(originalId)!.verified).toBe(true);
    expect(store.getSlot(originalSlot.slot_id)!.status).toBe('booked');
  });

  it('does not cancel the original when the hospital refuses the replacement', async () => {
    harness = await makeHarness();
    const store = harness.built.context.store;
    const originalSlot = slotFor(harness, { index: 0 });
    const replacementSlot = slotFor(harness, { index: 1 });

    const original = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { slot_id: originalSlot.slot_id }),
      headers: { 'idempotency-key': 'lifecycle-decline-orig-01' },
    });
    const originalId = original.body.booking.booking_request_id;

    const refused = await call(harness.app, {
      method: 'POST',
      url: `/appointments/${originalId}/reschedule`,
      payload: { new_slot_id: replacementSlot.slot_id, idempotency_key: 'lifecycle-decline-new-01' },
      headers: { 'x-vendor-decision': 'decline' },
    });

    expect(refused.status).toBe(200);
    expect(refused.body.booking.state).toBe('REJECTED');
    expect(refused.body.old_booking_cancelled).toBe(false);
    expect(refused.body.superseded_booking).toBeNull();
    expect(refused.body.cancellation_deferred_reason).toContain('REJECTED');

    expect(store.findBookingById(originalId)!.state).toBe('CONFIRMED');
    expect(store.getAppointmentByBooking(originalId)!.verified).toBe(true);
    expect(store.getSlot(originalSlot.slot_id)!.status).toBe('booked');
  });
});
