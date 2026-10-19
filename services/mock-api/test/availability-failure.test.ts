import { afterEach, describe, expect, it } from 'vitest';
import { hourInZone } from '../src/domain/time.js';
import { HOSPITAL, call, makeHarness, providerFor, type Harness } from './helpers.js';

/**
 * "Upstream error is not the same answer as no specialist."
 *
 * Four ways this endpoint can come back, and every pair of them has to be
 * distinguishable without guessing: we asked and found slots, we asked and the
 * constraints matched nothing, we asked and this provider does not work here,
 * and we could not ask at all. The last one carries an empty slot list too —
 * which is exactly why it may never be reported as a fact about the world.
 */
interface Observation {
  label: string;
  status: number;
  result_kind: string | null;
  error_code: string | null;
  slot_count: number | null;
}

function observe(label: string, response: { status: number; body: any }): Observation {
  return {
    label,
    status: response.status,
    result_kind: response.body?.result_kind ?? null,
    error_code: response.body?.error?.code ?? null,
    slot_count: Array.isArray(response.body?.slots) ? response.body.slots.length : null,
  };
}

function signature(observation: Observation): string {
  return [observation.status, observation.result_kind, observation.error_code, observation.slot_count].join('|');
}

describe('availability failures are distinguished from empty answers', () => {
  let harness: Harness | null = null;
  afterEach(async () => {
    await harness?.cleanup();
    harness = null;
  });

  it('never collapses "we could not ask" into "there is nothing"', async () => {
    harness = await makeHarness();
    const store = harness.built.context.store;
    const hospitalId = HOSPITAL.instantConfirm;
    const providerId = providerFor(harness, hospitalId);
    const query = { hospital_id: hospitalId, provider_id: providerId, visit_type: 'first_visit' };

    const found = observe('slots_found', await call(harness.app, { method: 'POST', url: '/availability', payload: query }));

    const empty = observe(
      'empty_valid_result',
      await call(harness.app, {
        method: 'POST',
        url: '/availability',
        payload: query,
        headers: { 'x-fault-scenario': 'empty_valid_result' },
      }),
    );

    const malformed = observe(
      'malformed_schema',
      await call(harness.app, {
        method: 'POST',
        url: '/availability',
        payload: query,
        headers: { 'x-fault-scenario': 'malformed_schema' },
      }),
    );

    // A provider who genuinely does not work at this hospital: a real answer.
    const instantProviders = new Set(
      store
        .listProvidersForHospital(hospitalId)
        .map((row) => (JSON.parse(String(row.payload)) as { provider_id: string }).provider_id),
    );
    const foreignProvider = store
      .listProvidersForHospital(HOSPITAL.reviewRequired)
      .map((row) => (JSON.parse(String(row.payload)) as { provider_id: string }).provider_id)
      .find((id) => !instantProviders.has(id));
    expect(foreignProvider).toBeDefined();
    const noProvider = observe(
      'no_matching_provider',
      await call(harness.app, {
        method: 'POST',
        url: '/availability',
        payload: { ...query, provider_id: foreignProvider },
      }),
    );

    // The hospital edge itself is down: a transport failure, not a body.
    const unavailable = observe(
      'service_unavailable_503',
      await call(harness.app, {
        method: 'POST',
        url: '/availability',
        payload: query,
        headers: { 'x-fault-scenario': 'service_unavailable_503' },
      }),
    );

    expect(found).toMatchObject({ status: 200, result_kind: 'slots_found' });
    expect(found.slot_count).toBeGreaterThan(0);
    expect(empty).toMatchObject({ status: 200, result_kind: 'no_slots_for_constraints', slot_count: 0 });
    expect(malformed).toMatchObject({ status: 200, result_kind: 'upstream_error', slot_count: 0 });
    expect(noProvider).toMatchObject({ status: 200, result_kind: 'no_matching_provider', slot_count: 0 });
    expect(unavailable).toMatchObject({ status: 503, error_code: 'upstream_unavailable' });
    expect(unavailable.result_kind).toBeNull();

    const observations = [found, empty, malformed, noProvider, unavailable];
    const signatures = new Set(observations.map(signature));
    expect(signatures.size).toBe(observations.length);

    // The two empty answers are the dangerous pair: one is a fact, one is a
    // failure. They differ in `result_kind` and in nothing else, which is
    // precisely why every consumer is told to branch on it.
    expect(empty.result_kind).not.toBe(malformed.result_kind);
    expect(JSON.stringify(empty)).not.toBe(JSON.stringify(malformed));

    // An empty-but-valid answer is still not a failed lookup: the evidence id
    // is present, and no error envelope is fabricated.
    const malformedBody = (
      await call(harness.app, {
        method: 'POST',
        url: '/availability',
        payload: query,
        headers: { 'x-fault-scenario': 'malformed_schema' },
      })
    ).body;
    expect(malformedBody.evidence_id).toBeTruthy();
    expect(malformedBody.error).toBeUndefined();
    expect(malformedBody.synthetic).toBe(true);
  });

  it('does not report an empty list when the hospital is simply unavailable', async () => {
    harness = await makeHarness();
    const response = await call(harness.app, {
      method: 'POST',
      url: '/availability',
      payload: { hospital_id: HOSPITAL.instantConfirm, provider_id: providerFor(harness, HOSPITAL.instantConfirm), visit_type: 'first_visit' },
      headers: { 'x-fault-scenario': 'service_unavailable_503' },
    });
    expect(response.status).toBe(503);
    expect(response.body.slots).toBeUndefined();
    expect(response.body.result_kind).toBeUndefined();
    expect(response.body.error.fault_type).toBe('service_unavailable_503');
    expect(response.headers['x-mock-fault']).toBe('service_unavailable_503');
  });

  it('echoes a preference instead of filtering on it, and reads the hospital wall clock rather than the UTC instant', async () => {
    harness = await makeHarness();
    const hospitalId = HOSPITAL.instantConfirm;
    const providerId = providerFor(harness, hospitalId);
    const base = { hospital_id: hospitalId, provider_id: providerId, visit_type: 'first_visit' };

    const preferred = await call(harness.app, {
      method: 'POST',
      url: '/availability',
      payload: { ...base, preferred_time_of_day: 'morning' },
    });
    expect(preferred.status).toBe(200);
    expect(preferred.body.result_kind).toBe('slots_found');
    expect(preferred.body.slots.length).toBeGreaterThan(0);

    // A preference is a wish, not a constraint. The same query without it
    // returns the same slots, and the wish travels back inside `query` for the
    // layer that ranks — filtering here would turn "no Friday session exists"
    // into a claim about the network, and would leave the caller no alternatives
    // to offer.
    const plain = await call(harness.app, { method: 'POST', url: '/availability', payload: base });
    expect(preferred.body.slots.map((slot: { slot_id: string }) => slot.slot_id)).toEqual(
      plain.body.slots.map((slot: { slot_id: string }) => slot.slot_id),
    );
    expect(preferred.body.query.preferred_time_of_day).toBe('morning');

    let utcAndWallClockDisagree = false;
    for (const slot of preferred.body.slots) {
      // Stored in UTC, rendered in the hospital's zone: both are true at once.
      expect(slot.start_at).toMatch(/Z$/);
      expect(slot.display_timezone).toBe('America/Chicago');

      // The trap this pins: 14:00Z is 09:00 in Chicago, so a caller that
      // classified slots on the stored instant would call a morning
      // appointment an afternoon one.
      const wallClockHour = hourInZone(slot.start_at, slot.display_timezone);
      const utcHour = Number.parseInt(slot.start_at.slice(11, 13), 10);
      if ((wallClockHour < 12) !== (utcHour < 12)) utcAndWallClockDisagree = true;
    }
    expect(utcAndWallClockDisagree).toBe(true);
  });
});
