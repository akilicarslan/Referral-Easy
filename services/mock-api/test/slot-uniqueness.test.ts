import { afterEach, describe, expect, it } from 'vitest';
import { bookingBody, call, makeHarness, slotFor, type Harness } from './helpers.js';
import { newUuid } from '../src/domain/ids.js';

/**
 * Invariant 1: no double booking.
 *
 * The service is single-threaded and the store is synchronous, so "concurrency"
 * here means what it means for the real thing: two callers who both read the
 * same slot version and both believe it is free. The arbiter is the conditional
 * UPDATE inside BEGIN IMMEDIATE, backed by a UNIQUE index that holds even if a
 * future refactor forgets to claim first.
 */
describe('slot uniqueness', () => {
  let harness: Harness | null = null;
  afterEach(async () => {
    await harness?.cleanup();
    harness = null;
  });

  it('gives the slot to exactly one of two racing booking requests', async () => {
    harness = await makeHarness();
    const slot = slotFor(harness);
    const body = bookingBody(harness, { slot_id: slot.slot_id });

    const [first, second] = await Promise.all([
      call(harness.app, { method: 'POST', url: '/appointments', payload: body, headers: { 'idempotency-key': 'race-key-aaaa-0001' } }),
      call(harness.app, { method: 'POST', url: '/appointments', payload: body, headers: { 'idempotency-key': 'race-key-bbbb-0002' } }),
    ]);

    const statuses = [first.status, second.status].sort((left, right) => left - right);
    expect(statuses).toEqual([200, 409]);

    const winner = first.status === 200 ? first : second;
    const loser = first.status === 200 ? second : first;

    expect(winner.body.booking.state).toBe('CONFIRMED');
    expect(winner.body.appointment.verified).toBe(true);
    expect(loser.body.error.code).toBe('slot_conflict');
    expect(loser.body.error.detail.attempt_result).toBe('slot_conflict');
    expect(loser.body.error.detail.slot_id).toBe(slot.slot_id);
    expect(['held', 'booked']).toContain(loser.body.error.detail.slot_status);

    const store = harness.built.context.store;
    // One appointment, one booking, and the loser wrote nothing at all.
    expect(store.countAppointmentsForSlot(slot.slot_id)).toBe(1);
    expect(store.listBookings({}).length).toBe(1);
    expect(store.getSlot(slot.slot_id)!.status).toBe('booked');
  });

  it('treats two racing requests with the SAME key as one booking, not two', async () => {
    harness = await makeHarness();
    const slot = slotFor(harness);
    const body = bookingBody(harness, { slot_id: slot.slot_id });
    const key = 'race-key-same-000003';

    const [first, second] = await Promise.all([
      call(harness.app, { method: 'POST', url: '/appointments', payload: body, headers: { 'idempotency-key': key } }),
      call(harness.app, { method: 'POST', url: '/appointments', payload: body, headers: { 'idempotency-key': key } }),
    ]);

    expect([first.status, second.status]).toEqual([200, 200]);
    const outcomes = [first.body.outcome, second.body.outcome].sort();
    expect(outcomes).toEqual(['created', 'duplicate_replayed']);
    expect(first.body.booking.booking_request_id).toBe(second.body.booking.booking_request_id);

    const store = harness.built.context.store;
    expect(store.listBookings({}).length).toBe(1);
    expect(store.listAttempts(first.body.booking.booking_request_id).length).toBe(1);
    expect(store.countVendorCalls({ idempotencyKey: key, operation: 'create' })).toBe(1);
  });

  it('refuses a claim built on a version that has already moved on', async () => {
    harness = await makeHarness();
    const store = harness.built.context.store;
    const slot = slotFor(harness);

    // Two readers who both saw the same free slot, which is the only way a
    // double booking can even be attempted.
    const seenByFirst = store.getSlot(slot.slot_id)!;
    const seenBySecond = store.getSlot(slot.slot_id)!;
    expect(seenByFirst.version).toBe(seenBySecond.version);

    const claimed = store.claimSlot(seenByFirst.slot_id, seenByFirst.version);
    const rejected = store.claimSlot(seenBySecond.slot_id, seenBySecond.version);

    expect(claimed.claimed).toBe(true);
    expect(rejected.claimed).toBe(false);
    expect(store.getSlot(slot.slot_id)!.status).toBe('held');
    // Even without a version, a held slot is not claimable.
    expect(store.claimSlot(slot.slot_id, null).claimed).toBe(false);
  });

  it('has a database constraint that refuses a second live appointment for one slot', async () => {
    harness = await makeHarness();
    const store = harness.built.context.store;
    const slot = slotFor(harness);
    const body = bookingBody(harness, { slot_id: slot.slot_id });
    const created = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: body,
      headers: { 'idempotency-key': 'constraint-key-000004' },
    });
    expect(created.status).toBe(200);

    const appointment = store.getAppointmentBySlot(slot.slot_id)!;
    expect(appointment.state).toBe('CONFIRMED');

    // Bypass the claim path entirely, as a regression would: the index still wins.
    expect(() => store.insertAppointment({ ...appointment, appointment_id: newUuid() })).toThrowError(/UNIQUE|constraint/i);
    expect(store.countAppointmentsForSlot(slot.slot_id)).toBe(1);

    // A cancelled appointment no longer occupies the slot, so the guard is a
    // guard against double booking rather than against all history.
    store.updateAppointmentState(appointment.appointment_id, { state: 'CANCELLED' });
    expect(() => store.insertAppointment({ ...appointment, appointment_id: newUuid() })).not.toThrow();
  });
});
