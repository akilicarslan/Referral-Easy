import { afterEach, describe, expect, it } from 'vitest';
import { bookingBody, call, makeHarness, slotFor, type Harness } from './helpers.js';
import type { FaultScenario } from '../src/faults/types.js';

/**
 * Invariant 3: a notification or telemetry failure NEVER rolls a booking back.
 *
 * The two halves of that guarantee pull in opposite directions, so both are
 * asserted here: the outbox rows are written INSIDE the booking transaction
 * (nothing partial can survive), and the delivery happens OUTSIDE it (nothing
 * late can undo the booking).
 */
const everySendFails: FaultScenario = {
  scenario_id: 'notification_failure',
  description: 'test override: every internal send fails, so both channels are exercised',
  fault_seed: 42,
  default_fault: 'notification_failure',
  steps: [{ match: { method: 'POST', path: '/internal/notifications/send' }, fault: 'notification_failure' }],
};

/**
 * Arms the scenario through the control plane rather than a request header.
 *
 * A scenario that only targets an INTERNAL hop matches no inbound request, so
 * the header would be attached to a decision that never happens — the armed
 * scenario is what gives the internal send its fault.
 */
async function armNotificationFailure(harness: Harness): Promise<void> {
  const armed = await call(harness.app, {
    method: 'POST',
    url: '/_faults/arm',
    payload: { scenario_id: 'notification_failure', mode: 'scenario' },
  });
  expect(armed.status).toBe(200);
  expect(armed.body.armed.scenario_id).toBe('notification_failure');
}

describe('transactions and the outbox', () => {
  let harness: Harness | null = null;
  afterEach(async () => {
    await harness?.cleanup();
    harness = null;
  });

  it('writes the appointment, the outbox and the attempt in one transaction', async () => {
    harness = await makeHarness();
    const store = harness.built.context.store;
    const slot = slotFor(harness);
    const body = bookingBody(harness, { slot_id: slot.slot_id });

    // A crash at the very end of the commit: the attempt row cannot be written.
    const originalInsertAttempt = store.insertAttempt.bind(store);
    store.insertAttempt = () => {
      throw new Error('simulated crash while writing the attempt row');
    };

    const crashed = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: body,
      headers: { 'idempotency-key': 'tx-atomicity-000001' },
    });
    expect(crashed.status).toBe(500);
    expect(crashed.body.error.code).toBe('internal_error');

    // Nothing partial survived: no appointment, no outbox row, no attempt.
    const booking = store.findBookingByKey('tx-atomicity-000001')!;
    expect(store.countAppointmentsForSlot(slot.slot_id)).toBe(0);
    expect(store.listOutbox({ bookingRequestId: booking.booking_request_id })).toHaveLength(0);
    expect(store.listAttempts(booking.booking_request_id)).toHaveLength(0);
    // The booking itself was written by the earlier claim transaction, and the
    // slot is still only held — the window reconciliation exists to close.
    expect(booking.state).toBe('REQUESTED');
    expect(store.getSlot(slot.slot_id)!.status).toBe('held');
    expect(store.listBookings({ state: 'CONFIRMED' })).toHaveLength(0);

    // Telemetry comes back, and the SAME key resolves the unknown outcome.
    store.insertAttempt = originalInsertAttempt;
    const reconciled = await call(harness.app, {
      method: 'GET',
      url: '/appointments/lookup_request?idempotency_key=tx-atomicity-000001',
    });
    expect(reconciled.status).toBe(200);
    expect(reconciled.body.lookup.outcome).toBe('not_booked');
    expect(reconciled.body.appointment).toBeNull();
    expect(store.getSlot(slot.slot_id)!.status).toBe('available');
  });

  it('leaves the booking intact when every notification fails', async () => {
    harness = await makeHarness({ scenarios: [everySendFails] });
    const store = harness.built.context.store;
    const slot = slotFor(harness);
    await armNotificationFailure(harness);

    const created = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness, { slot_id: slot.slot_id }),
      headers: { 'idempotency-key': 'tx-notify-fail-000001' },
    });

    expect(created.status).toBe(200);
    expect(created.body.booking.state).toBe('CONFIRMED');
    expect(created.body.appointment.verified).toBe(true);

    const bookingId = created.body.booking.booking_request_id;
    const entries = store.listOutbox({ bookingRequestId: bookingId });
    expect(entries).toHaveLength(2);
    expect(entries.map((entry) => entry.status)).toEqual(['failed', 'failed']);
    expect(entries.every((entry) => (entry.last_error ?? '').includes('notification provider unavailable'))).toBe(true);
    expect(entries.every((entry) => entry.attempts === 1)).toBe(true);

    // The booking is untouched by the failure, and still confirms.
    expect(store.findBookingById(bookingId)!.state).toBe('CONFIRMED');
    expect(store.getAppointmentByBooking(bookingId)!.state).toBe('CONFIRMED');
    expect(store.getSlot(slot.slot_id)!.status).toBe('booked');

    // Another delivery pass while the provider is still down changes nothing
    // about the booking, and the entries are still retryable, not abandoned.
    const drained = await call(harness.app, { method: 'POST', url: '/notifications/drain' });
    expect(drained.status).toBe(200);
    expect(store.findBookingById(bookingId)!.state).toBe('CONFIRMED');
    expect(store.listOutbox({ bookingRequestId: bookingId }).map((entry) => entry.status)).toEqual(['failed', 'failed']);
  });

  it('gives up on a notification that can never be delivered instead of retrying forever', async () => {
    harness = await makeHarness({ scenarios: [everySendFails], notificationMaxAttempts: 1 });
    const store = harness.built.context.store;
    await armNotificationFailure(harness);

    const created = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness),
      headers: { 'idempotency-key': 'tx-notify-abandon-000001' },
    });
    const bookingId = created.body.booking.booking_request_id;
    const [first] = store.listOutbox({ bookingRequestId: bookingId });
    expect(first!.status).toBe('abandoned');
    expect(first!.attempts).toBe(1);
    expect(first!.next_attempt_at).toBeNull();

    // A drain pass must not resurrect it.
    await call(harness.app, { method: 'POST', url: '/notifications/drain' });
    expect(store.getOutboxEntry(first!.outbox_id)!.attempts).toBe(1);
    expect(store.getOutboxEntry(first!.outbox_id)!.status).toBe('abandoned');

    // Reopening it is a deliberate operator action, and with the provider back
    // the delivery goes through — the terminal state is a stop, not a death.
    await call(harness.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: null, mode: 'off' } });
    const retried = await call(harness.app, { method: 'POST', url: `/notifications/outbox/${first!.outbox_id}/retry` });
    expect(retried.status).toBe(200);
    expect(retried.body.entry.status).toBe('sent');
    expect(store.findBookingById(bookingId)!.state).toBe('CONFIRMED');
  });

  it('renders the patient message in the hospital timezone, not in UTC', async () => {
    harness = await makeHarness();
    const created = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness),
      headers: { 'idempotency-key': 'tx-notify-preview-0001' },
    });
    const bookingId = created.body.booking.booking_request_id;
    const [entry] = harness.built.context.store.listOutbox({ bookingRequestId: bookingId });
    expect(entry!.status).toBe('sent');

    const preview = await call(harness.app, { method: 'GET', url: `/notifications/outbox/${entry!.outbox_id}/preview` });
    expect(preview.status).toBe(200);
    expect(preview.body.body).toContain(bookingId);
    expect(preview.body.subject).toContain('Appointment confirmed');

    // The stored instant is UTC; what the patient reads is the hospital's wall
    // clock. Same moment, two renderings, and the message carries the local one.
    const startAt = created.body.appointment.start_at as string;
    expect(startAt).toMatch(/Z$/);
    const format = (timeZone: string): string =>
      new Intl.DateTimeFormat('en-US', { timeZone, dateStyle: 'full', timeStyle: 'short' }).format(new Date(startAt));
    expect(created.body.appointment.display_timezone).toBe('America/Chicago');
    expect(preview.body.body).toContain(format('America/Chicago'));
    expect(format('America/Chicago')).not.toBe(format('UTC'));
    expect(preview.body.body).not.toContain(startAt.slice(0, 10).replace(/-/g, '') + 'T');
  });
});
