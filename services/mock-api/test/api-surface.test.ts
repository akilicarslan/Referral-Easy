import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { call, makeHarness, HOSPITAL_IDS, PLAN_IDS, slotFor, providerFor, type Harness } from './helpers.js';

describe('service surface', () => {
  let harness: Harness;
  beforeAll(async () => {
    harness = await makeHarness();
  });
  afterAll(async () => {
    await harness.cleanup();
  });

  it('boots, seeds a directory and reports the fault source', async () => {
    const health = await call(harness.app, { method: 'GET', url: '/health' });
    expect(health.status).toBe(200);
    expect(health.body.status).toBe('ok');
    expect(health.body.fault_seed).toBe(42);
    expect(health.body.fault_scenarios_loaded).toBe(11);
    expect(harness.built.seed?.slots).toBeGreaterThan(100);
  });

  it('serves an OpenAPI document generated from the same Zod contracts', async () => {
    const response = await call(harness.app, { method: 'GET', url: '/openapi.json' });
    expect(response.status).toBe(200);
    expect(response.body.openapi).toBe('3.1.0');
    expect(Object.keys(response.body.paths).length).toBeGreaterThan(15);
    expect(response.body.components.schemas.BookingRequest.additionalProperties).toBe(false);
    expect(response.body.components.schemas.AvailabilityResult.properties.result_kind.enum).toEqual([
      'slots_found',
      'no_slots_for_constraints',
      'no_matching_provider',
      'upstream_error',
    ]);
  });

  it('lists locations, hospitals and providers', async () => {
    const locations = await call(harness.app, { method: 'GET', url: '/locations' });
    expect(locations.body.count).toBe(4);

    const hospitals = await call(harness.app, { method: 'GET', url: '/hospitals' });
    expect(hospitals.body.count).toBe(4);
    const adapters = hospitals.body.hospitals.map((hospital: any) => hospital.adapter_type).sort();
    expect(adapters).toEqual(['instant_confirm', 'instant_confirm', 'review_required', 'review_required']);

    const providers = await call(harness.app, { method: 'GET', url: '/providers?specialty=neurology' });
    expect(providers.body.count).toBeGreaterThan(0);
  });

  it('returns coverage with unknown kept as unknown', async () => {
    const providerId = providerFor(harness, HOSPITAL_IDS.northlake);
    const covered = await call(harness.app, {
      method: 'POST',
      url: '/coverage/lookup',
      payload: { plan_id: PLAN_IDS.blueMesaHmo, provider_id: providerId, hospital_id: HOSPITAL_IDS.northlake },
    });
    expect(covered.status).toBe(200);
    expect(covered.body.disclaimer_key).toBe('coverage_not_a_guarantee');
    expect(typeof covered.body.accepts_insurance).toBe('boolean');
  });

  it('returns availability with result_kind and UTC slot instants', async () => {
    const slot = slotFor(harness, { hospitalId: HOSPITAL_IDS.northlake });
    const response = await call(harness.app, {
      method: 'POST',
      url: '/availability',
      payload: {
        hospital_id: slot.hospital_id,
        provider_id: slot.provider_id,
        visit_type: slot.visit_type,
      },
    });
    expect(response.status).toBe(200);
    expect(response.body.result_kind).toBe('slots_found');
    expect(response.body.slots.length).toBeGreaterThan(0);
    expect(response.body.slots[0].start_at).toMatch(/Z$/);
    expect(response.body.slots[0].display_timezone).toBe('America/Chicago');
  });
});
