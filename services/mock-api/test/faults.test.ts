import { afterEach, describe, expect, it } from 'vitest';
import { FAULT_TYPES } from '../src/contracts/enums.js';
import type { FaultScenario } from '../src/faults/types.js';
import { HOSPITAL, PLAN_IDS, bookingBody, call, fastTimeoutScenarios, makeHarness, providerFor, slotFor, type Harness } from './helpers.js';

const ALL_FAULTS: readonly string[] = FAULT_TYPES;

/** A scenario with no steps is a whole-surface fault — the blast-radius reading in the ADR. */
const blanket: FaultScenario = {
  scenario_id: 'rate_limit_429',
  description: 'test override: no steps at all, so the default_fault covers the whole surface',
  fault_seed: 42,
  default_fault: 'rate_limit_429',
  steps: [],
};

/**
 * The shipped `malformed_schema` scenario faults the DIRECTORY feed; the same
 * fault type also has a meaning at the vendor boundary, where a create response
 * that does not match its own schema must not be read as an appointment.
 */
const malformedOnTheBookingPath: FaultScenario = {
  scenario_id: 'malformed_schema',
  description: 'test override: the vendor create response does not match the vendor schema',
  fault_seed: 42,
  default_fault: 'malformed_schema',
  steps: [{ match: { method: 'POST', path: '/appointments' }, occurrence: 1, fault: 'malformed_schema' }],
};

function eventLine(event: Record<string, unknown>): string {
  return [event.fault_type, event.method, event.path, event.occurrence, event.request_seq, event.seed, event.mode].join('|');
}

/** The same 24 requests, in the same order, from the same handle. */
async function drawInPlace(handle: Harness): Promise<string[]> {
  const drawn: string[] = [];
  for (let index = 0; index < 24; index += 1) {
    const response = await call(handle.app, { method: 'GET', url: '/locations', headers: { 'x-fault-mode': 'random' } });
    drawn.push(response.status === 200 ? 'ok' : `faulted:${String(response.headers['x-mock-fault'])}`);
  }
  return drawn;
}

describe('faults', () => {
  let harness: Harness | null = null;
  afterEach(async () => {
    await harness?.cleanup();
    harness = null;
  });

  async function bookWith(
    handle: Harness,
    options: { key: string; index: number; fault?: string; headers?: Record<string, string>, hospitalId?: string },
  ) {
    const hospitalId = options.hospitalId ?? HOSPITAL.instantConfirm;
    const slot = slotFor(handle, { index: options.index, hospitalId });
    const response = await call(handle.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(handle, { slot_id: slot.slot_id, hospital_id: hospitalId }),
      headers: {
        'idempotency-key': options.key,
        ...(options.fault ? { 'x-fault-scenario': options.fault } : {}),
        ...(options.headers ?? {}),
      },
    });
    return { response, slot };
  }

  it('reproduces all eleven faults from one seed, each with its own observable', async () => {
    harness = await makeHarness({ scenarios: [...fastTimeoutScenarios(), malformedOnTheBookingPath] });
    const store = harness.built.context.store;
    const observed = new Map<string, string>();
    const providerId = providerFor(harness, HOSPITAL.instantConfirm);

    // 1 — latency: the answer is correct, it just arrives late.
    const started = Date.now();
    const latency = await call(harness.app, {
      method: 'POST',
      url: '/availability',
      payload: { hospital_id: HOSPITAL.instantConfirm, provider_id: providerId, visit_type: 'first_visit' },
      headers: { 'x-fault-scenario': 'latency' },
    });
    const elapsed = Date.now() - started;
    expect(latency.status).toBe(200);
    expect(latency.body.result_kind).toBe('slots_found');
    expect(latency.headers['x-mock-fault']).toBe('latency');
    expect(elapsed).toBeGreaterThan(1000);
    observed.set('latency', `200|slots_found|delayed_ms>=1000`);

    // 2 — the hospital edge rate-limits the caller.
    const limited = await bookWith(harness, { key: 'fault-429-000001', index: 0, fault: 'rate_limit_429' });
    expect(limited.response.status).toBe(429);
    expect(limited.response.body.error.code).toBe('upstream_rate_limited');
    expect(limited.response.headers['retry-after']).toBe('5');
    observed.set('rate_limit_429', `429|upstream_rate_limited|retry_after=5`);

    // 3 — the hospital edge is down: a transport failure, never an empty answer.
    const down = await bookWith(harness, { key: 'fault-503-000001', index: 1, fault: 'service_unavailable_503' });
    expect(down.response.status).toBe(503);
    expect(down.response.body.error.code).toBe('upstream_unavailable');
    expect(down.response.body.slots).toBeUndefined();
    observed.set('service_unavailable_503', `503|upstream_unavailable`);

    // 4 — the vendor answered, in a shape that is not its own schema.
    const malformed = await bookWith(harness, { key: 'fault-malformed-0001', index: 2, fault: 'malformed_schema' });
    expect(malformed.response.status).toBe(502);
    expect(malformed.response.body.error.code).toBe('malformed_upstream_response');
    expect(malformed.response.body.error.detail.attempt_result).toBe('malformed_response');
    expect(store.getSlot(malformed.slot.slot_id)!.status).toBe('available');
    observed.set('malformed_schema', `502|malformed_upstream_response`);

    // 5 — a genuine, valid, empty answer.
    const empty = await call(harness.app, {
      method: 'POST',
      url: '/availability',
      payload: { hospital_id: HOSPITAL.instantConfirm, provider_id: providerId, visit_type: 'first_visit' },
      headers: { 'x-fault-scenario': 'empty_valid_result' },
    });
    expect(empty.status).toBe(200);
    expect(empty.body.result_kind).toBe('no_slots_for_constraints');
    observed.set('empty_valid_result', `200|no_slots_for_constraints`);

    // 6 — the caller booked against a slot version that has moved on.
    const stale = await bookWith(harness, { key: 'fault-stale-000001', index: 3, fault: 'stale_slot_409' });
    expect(stale.response.status).toBe(409);
    expect(stale.response.body.error.code).toBe('slot_conflict');
    expect(stale.response.body.error.detail.stale_version_presented).toBe(true);
    observed.set('stale_slot_409', `409|slot_conflict|stale_version_presented=true`);

    // 7 & 8 — the two timeouts, which are not the same event and are the whole
    // reason the distinction exists.
    const before = await bookWith(harness, { key: 'fault-timeout-before-01', index: 4, fault: 'timeout_before_commit' });
    expect(before.response.status).toBe(504);
    expect(before.response.body.error.detail.booking_exists).toBe(false);
    expect(store.getSlot(before.slot.slot_id)!.status).toBe('available');
    observed.set('timeout_before_commit', `504|booking_outcome_unknown|booking_exists=false`);

    const after = await bookWith(harness, { key: 'fault-timeout-after-01', index: 5, fault: 'timeout_after_commit' });
    expect(after.response.status).toBe(504);
    expect(after.response.body.error.detail.booking_exists).toBe(true);
    expect(store.getSlot(after.slot.slot_id)!.status).toBe('booked');
    observed.set('timeout_after_commit', `504|booking_outcome_unknown|booking_exists=true`);

    // 9 — the hospital delivers the same event twice for one key.
    const duplicate = await bookWith(harness, { key: 'fault-duplicate-0001', index: 6, fault: 'duplicate_event' });
    expect(duplicate.response.status).toBe(200);
    expect(duplicate.response.body.booking.state).toBe('CONFIRMED');
    expect(duplicate.response.body.attempts).toHaveLength(2);
    expect(duplicate.response.body.attempts[1].result).toBe('duplicate_replayed');
    expect(store.countAppointmentsForSlot(duplicate.slot.slot_id)).toBe(1);
    observed.set('duplicate_event', `200|created|attempts=2|appointments=1`);

    // 10 — the coverage evidence is stale and disagrees with the plan record.
    const coverage = await call(harness.app, {
      method: 'POST',
      url: '/coverage/lookup',
      payload: { plan_id: PLAN_IDS.blueMesaPpo, provider_id: providerId, hospital_id: HOSPITAL.instantConfirm },
      headers: { 'x-fault-scenario': 'stale_coverage_evidence' },
    });
    expect(coverage.status).toBe(200);
    expect(coverage.body.conflict).toBe(true);
    expect(coverage.body.freshness.state).toBe('stale');
    observed.set('stale_coverage_evidence', `200|conflict=true|freshness=stale`);

    // 11 — the confirmation cannot be delivered. The booking is not affected.
    const armed = await call(harness.app, {
      method: 'POST',
      url: '/_faults/arm',
      payload: { scenario_id: 'notification_failure', mode: 'scenario' },
    });
    expect(armed.status).toBe(200);
    const notified = await bookWith(harness, { key: 'fault-notify-000001', index: 7 });
    expect(notified.response.status).toBe(200);
    expect(notified.response.body.booking.state).toBe('CONFIRMED');
    const entries = store.listOutbox({ bookingRequestId: notified.response.body.booking.booking_request_id });
    expect(entries.filter((entry) => entry.status === 'failed')).toHaveLength(1);
    observed.set('notification_failure', `200|CONFIRMED|outbox_failed=1`);

    // Every fault in the canonical list fired...
    expect([...observed.keys()].sort()).toEqual([...ALL_FAULTS].sort());
    // ...and no two of them are the same observable.
    expect(new Set(observed.values()).size).toBe(ALL_FAULTS.length);

    // The record says which seed produced which fault, and where.
    const state = await call(harness.app, { method: 'GET', url: '/_faults/state' });
    expect(state.body.seed).toBe(42);
    const events = state.body.occurrences as Array<Record<string, unknown>>;
    expect([...new Set(events.map((event) => event.fault_type as string))].sort()).toEqual([...ALL_FAULTS].sort());
    expect(events.every((event) => event.seed === 42)).toBe(true);
    expect(events.every((event) => typeof event.request_seq === 'number')).toBe(true);
    const scenarios = await call(harness.app, { method: 'GET', url: '/_faults/scenarios' });
    expect(scenarios.body.scenarios).toHaveLength(ALL_FAULTS.length);
    expect(scenarios.body.fault_types).toEqual(ALL_FAULTS);
  });

  it('is a pure function of seed, scenario and request sequence', async () => {
    const runSequence = async (): Promise<string[]> => {
      const handle = await makeHarness({ scenarios: fastTimeoutScenarios() });
      await call(handle.app, { method: 'GET', url: '/health' });
      await call(handle.app, { method: 'GET', url: '/locations' });
      await call(handle.app, {
        method: 'POST',
        url: '/availability',
        payload: { hospital_id: HOSPITAL.instantConfirm, provider_id: providerFor(handle, HOSPITAL.instantConfirm), visit_type: 'first_visit' },
        headers: { 'x-fault-scenario': 'empty_valid_result' },
      });
      await call(handle.app, { method: 'GET', url: '/locations', headers: { 'x-fault-mode': 'off' } });
      await call(handle.app, {
        method: 'POST',
        url: '/availability',
        payload: { hospital_id: HOSPITAL.instantConfirm, provider_id: providerFor(handle, HOSPITAL.instantConfirm), visit_type: 'first_visit' },
        headers: { 'x-fault-scenario': 'empty_valid_result' },
      });
      const state = await call(handle.app, { method: 'GET', url: '/_faults/state' });
      await handle.cleanup();
      return (state.body.occurrences as Array<Record<string, unknown>>).map(eventLine);
    };

    const first = await runSequence();
    const second = await runSequence();
    expect(first.length).toBeGreaterThan(0);
    expect(second).toEqual(first);
    // The first `/availability` is faulted, the second is not: occurrence
    // counters are part of the contract, not an implementation detail.
    expect(first.some((line) => line.startsWith('empty_valid_result|POST|/availability|1|'))).toBe(true);
    expect(first.every((line) => line.split('|')[3] === '1')).toBe(true);
  });

  it('replays the same decisions after a reset, with nothing kept in memory', async () => {
    harness = await makeHarness({ scenarios: fastTimeoutScenarios() });
    const fire = async (key: string) => {
      await call(harness!.app, {
        method: 'POST',
        url: '/appointments',
        payload: bookingBody(harness!, { slot_id: slotFor(harness!, { index: 0 }).slot_id }),
        headers: { 'idempotency-key': key, 'x-fault-scenario': 'timeout_before_commit' },
      });
    };
    const events = async (): Promise<string[]> => {
      const state = await call(harness!.app, { method: 'GET', url: '/_faults/state' });
      return (state.body.occurrences as Array<Record<string, unknown>>).map(eventLine);
    };

    await fire('fault-replay-000001');
    const first = await events();
    expect(first).toHaveLength(1);

    const reset = await call(harness.app, { method: 'POST', url: '/_faults/reset' });
    expect(reset.status).toBe(200);
    expect(await events()).toHaveLength(0);

    await fire('fault-replay-000002');
    expect(await events()).toEqual(first);
  });

  it('draws the random mode from the seed, not from a stateful generator', async () => {
    const draw = async (seed: number): Promise<string[]> => {
      const handle = await makeHarness({ faultSeed: seed });
      const drawn: string[] = [];
      for (let index = 0; index < 24; index += 1) {
        const response = await call(handle.app, { method: 'GET', url: '/locations', headers: { 'x-fault-mode': 'random' } });
        drawn.push(response.status === 200 ? 'ok' : `faulted:${String(response.headers['x-mock-fault'])}`);
      }
      await handle.cleanup();
      return drawn;
    };

    const seeded42 = await draw(42);
    const again42 = await draw(42);
    const seeded7 = await draw(7);

    expect(seeded42.filter((entry) => entry !== 'ok').length).toBeGreaterThan(0);
    expect(again42).toEqual(seeded42);
    expect(seeded7).not.toEqual(seeded42);

    // In one process, the same 24 requests replay exactly after a reset —
    // the draw is (seed, sequence), never a generator's hidden state.
    const handle = await makeHarness({ faultSeed: 42 });
    harness = handle;
    const once = await drawInPlace(handle);
    expect((await call(handle.app, { method: 'POST', url: '/_faults/reset' })).status).toBe(200);
    const twice = await drawInPlace(handle);
    expect(twice).toEqual(once);
    expect(once.filter((entry) => entry !== 'ok').length).toBeGreaterThan(0);
  });

  it('keeps the control plane and the health check out of reach of a blanket fault', async () => {
    harness = await makeHarness({ scenarios: [blanket] });
    const armed = await call(harness.app, {
      method: 'POST',
      url: '/_faults/arm',
      payload: { scenario_id: 'rate_limit_429', mode: 'scenario' },
    });
    expect(armed.status).toBe(200);

    // The scenario has no steps, so its default_fault covers the whole surface —
    // and the surface excludes the endpoints you need in order to disarm it.
    const affected = await call(harness.app, { method: 'GET', url: '/locations' });
    expect(affected.status).toBe(429);
    expect(affected.headers['x-mock-fault']).toBe('rate_limit_429');

    // `/healthz` is in the list for the same reason as `/_faults/*`: the compose
    // topology probes it, so a faulted probe takes the container down mid-demo.
    for (const url of ['/', '/health', '/healthz', '/openapi.json', '/_faults/state', '/_faults/scenarios']) {
      const response = await call(harness.app, { method: 'GET', url });
      expect(response.status).toBe(200);
      expect(response.headers['x-mock-fault']).toBeUndefined();
    }

    const disarmed = await call(harness.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: null, mode: 'off' } });
    expect(disarmed.status).toBe(200);
    expect((await call(harness.app, { method: 'GET', url: '/locations' })).status).toBe(200);
  });

  it('refuses an unknown fault or scenario rather than quietly doing nothing', async () => {
    harness = await makeHarness();
    const unknownScenario = await call(harness.app, {
      method: 'POST',
      url: '/_faults/arm',
      payload: { scenario_id: 'not_a_fault', mode: 'scenario' },
    });
    expect(unknownScenario.status).toBe(400);
    expect(unknownScenario.body.error.code).toBe('unknown_scenario');

    // An unknown scenario in a header simply matches nothing: the request is
    // answered normally instead of failing in a way that looks like a fault.
    const response = await call(harness.app, {
      method: 'GET',
      url: '/locations',
      headers: { 'x-fault-scenario': 'no-such-scenario', 'x-fault': 'not_a_fault' },
    });
    expect(response.status).toBe(200);
    expect(response.headers['x-mock-fault']).toBeUndefined();
  });

  it('accepts a one-off fault through the x-fault header without arming anything', async () => {
    harness = await makeHarness();
    const response = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness),
      headers: { 'idempotency-key': 'fault-one-off-000001', 'x-fault': 'service_unavailable_503' },
    });
    expect(response.status).toBe(503);
    expect(response.headers['x-mock-fault']).toBe('service_unavailable_503');
    expect(harness.built.context.store.listBookings({})).toHaveLength(0);

    // ...and the very next request, without the header, goes through.
    const next = await call(harness.app, {
      method: 'POST',
      url: '/appointments',
      payload: bookingBody(harness),
      headers: { 'idempotency-key': 'fault-one-off-000002' },
    });
    expect(next.status).toBe(200);
  });
});
