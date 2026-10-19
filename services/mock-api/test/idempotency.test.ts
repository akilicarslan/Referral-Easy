import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { bookingBody, call, makeHarness, type Harness } from './helpers.js';

/**
 * Invariant 1: no double booking.
 *
 * Both branches of the idempotency contract, plus the durability that makes
 * them worth anything: the key survives a restart of the service.
 */
describe('idempotency', () => {
  let harness: Harness | null = null;
  afterEach(async () => {
    await harness?.cleanup();
    harness = null;
  });

  it('returns the SAME booking for the same key and the same payload', async () => {
    harness = await makeHarness();
    const key = 'demo-key-same-payload-0001';
    const body = bookingBody(harness);

    const first = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: body,
      headers: { 'idempotency-key': key },
    });
    expect(first.status).toBe(200);
    expect(first.body.booking.state).toBe('CONFIRMED');
    expect(first.body.idempotent_replay).toBe(false);

    const second = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: body,
      headers: { 'idempotency-key': key },
    });
    expect(second.status).toBe(200);
    expect(second.body.booking.booking_request_id).toBe(first.body.booking.booking_request_id);
    expect(second.body.appointment.appointment_id).toBe(first.body.appointment.appointment_id);
    expect(second.body.idempotent_replay).toBe(true);
    expect(second.body.outcome).toBe('duplicate_replayed');

    // No second row anywhere: one booking, one appointment, one vendor call.
    const store = harness.built.context.store;
    expect(store.listBookings({}).length).toBe(1);
    expect(store.countAppointmentsForSlot(body.slot_id as string)).toBe(1);
    expect(store.countVendorCalls({ idempotencyKey: key, operation: 'create' })).toBe(1);
    expect(store.listAttempts(first.body.booking.booking_request_id).length).toBe(1);
  });

  it('rejects the same key with a DIFFERENT payload', async () => {
    harness = await makeHarness();
    const key = 'demo-key-different-payload-01';
    const first = bookingBody(harness);
    const created = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: first,
      headers: { 'idempotency-key': key },
    });
    expect(created.status).toBe(200);

    // Same key, one field changed: the hashed payload no longer matches.
    const conflicting = bookingBody(harness, { visit_type: 'return_visit' });
    const rejected = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: { ...conflicting, slot_id: first.slot_id },
      headers: { 'idempotency-key': key },
    });

    expect(rejected.status).toBe(409);
    expect(rejected.body.error.code).toBe('idempotency_key_reuse');
    expect(rejected.body.error.detail.stored_payload_hash).not.toBe(rejected.body.error.detail.presented_payload_hash);

    // The original booking is untouched and no second booking exists.
    const store = harness.built.context.store;
    expect(store.listBookings({}).length).toBe(1);
    const original = store.findBookingByKey(key)!;
    expect(original.booking_request_id).toBe(created.body.booking.booking_request_id);
  });

  it('keeps the key durable across a service restart', async () => {
    // The store lives outside the harness's own scratch directory, because a
    // harness cleanup deletes what it created and this file must outlive it.
    const storeDir = mkdtempSync(join(tmpdir(), 'mock-api-restart-'));
    const dbPath = join(storeDir, 'mock-api.sqlite');

    const first = await makeHarness({ dbPath });
    const key = 'demo-key-durable-000000001';
    const body = bookingBody(first);
    const created = await call(first.app, {
      method: 'POST',
      url: '/appointments',
      payload: body,
      headers: { 'idempotency-key': key },
    });
    expect(created.status).toBe(200);
    await first.cleanup();

    // A brand new process, pointed at the same store.
    harness = await makeHarness({ dbPath });
    const replayed = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: body,
      headers: { 'idempotency-key': key },
    });
    expect(replayed.status).toBe(200);
    expect(replayed.body.idempotent_replay).toBe(true);
    expect(replayed.body.booking.booking_request_id).toBe(created.body.booking.booking_request_id);
    expect(harness.built.context.store.listBookings({}).length).toBe(1);

    // Reconciliation still works by the same key after the restart.
    const lookedUp = await call(harness.app, {
      method: 'GET',
      url: `/appointments/lookup_request?idempotency_key=${key}`,
    });
    expect(lookedUp.status).toBe(200);
    expect(lookedUp.body.booking.booking_request_id).toBe(created.body.booking.booking_request_id);

    rmSync(storeDir, { recursive: true, force: true });
  });

  it('requires a key and says so instead of minting one', async () => {
    harness = await makeHarness();
    const response = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness),
    });
    expect(response.status).toBe(400);
    expect(response.body.error.code).toBe('missing_idempotency_key');
  });
});
