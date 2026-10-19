import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { loadConfig } from '../src/config.js';
import { FAULT_TYPES } from '../src/contracts/enums.js';
import { call, makeFixtureHarness, type Harness } from './helpers.js';

/**
 * The service against the SHARED fixtures.
 *
 * Everything else in the suite pins the built-in dataset so a change under
 * `fixtures/` cannot silently move a contract test. This file is the opposite:
 * it is the integration proof that the API serves the network the demo's
 * scenarios actually name — the manifest's hospital/slot/record ids resolve,
 * every fault file loads, and each authored fault is reproducible from the seed
 * it declares.
 */

const config = loadConfig();
const fixturesRoot = config.fixturesRoot;
const faultDir = config.faultScenarioDir;

let harness: Harness | null = null;

afterEach(async () => {
  await harness?.cleanup();
  harness = null;
});

async function boot(): Promise<Harness> {
  harness = await makeFixtureHarness();
  return harness;
}

function readFixture<T>(...parts: string[]): T {
  return JSON.parse(readFileSync(resolve(fixturesRoot, ...parts), 'utf8')) as T;
}

function listFixture<T>(...parts: string[]): T[] {
  const parsed = readFixture<unknown>(...parts);
  if (Array.isArray(parsed)) return parsed as T[];
  const list = Object.values(parsed as Record<string, unknown>).find((value) => Array.isArray(value));
  return (list ?? []) as T[];
}

interface ScenarioManifest {
  scenarios: Array<{
    scenario_id: string;
    number: number;
    title: string;
    fault_scenarios: string[];
    references: Record<string, string[]>;
    expected_result_kinds: string[];
  }>;
}

describe('shared fixtures drive the served network', () => {
  it('serves the fixture dataset and reports it', async () => {
    const h = await boot();
    const seed = h.built.seed!;
    expect(seed.mode).toBe('fixtures');
    expect(seed.source_directory).toBe(fixturesRoot);
    expect(seed.hospitals).toBe(listFixture<unknown>('directory', 'hospitals.json').length);
    expect(seed.providers).toBe(listFixture<unknown>('directory', 'providers.json').length);
    expect(seed.slots).toBe(listFixture<unknown>('directory', 'slots.json').length);
    expect(seed.plans).toBe(listFixture<unknown>('coverage', 'plans.json').length);
    expect(seed.evidence).toBe(listFixture<unknown>('evidence.json').length);

    const health = await call(h.app, { method: 'GET', url: '/health' });
    expect(health.body.seed.mode).toBe('fixtures');
  });

  it('resolves every id the scenario manifest references', async () => {
    const h = await boot();
    const manifest = readFixture<ScenarioManifest>('scenarios.json');
    const checked = { hospitals: 0, providers: 0, slots: 0, plans: 0, patients: 0 };
    const missing: string[] = [];

    for (const scenario of manifest.scenarios) {
      for (const hospitalId of scenario.references.hospitals ?? []) {
        const response = await call(h.app, { method: 'GET', url: `/hospitals/${hospitalId}` });
        if (response.status !== 200) missing.push(`hospital ${hospitalId}`);
        checked.hospitals += 1;
      }
      for (const providerId of scenario.references.providers ?? []) {
        const response = await call(h.app, { method: 'GET', url: `/providers/${providerId}` });
        if (response.status !== 200) missing.push(`provider ${providerId}`);
        checked.providers += 1;
      }
      for (const planId of scenario.references.plans ?? []) {
        const response = await call(h.app, { method: 'GET', url: `/plans` });
        if (!response.body.plans.some((plan: { plan_id: string }) => plan.plan_id === planId)) missing.push(`plan ${planId}`);
        checked.plans += 1;
      }
      for (const documentId of scenario.references.documents ?? []) {
        const response = await call(h.app, { method: 'GET', url: `/documents/${documentId}` });
        if (response.status !== 200) missing.push(`document ${documentId}`);
      }
      for (const patientId of scenario.references.patients ?? []) {
        const response = await call(h.app, { method: 'GET', url: `/documents?patient_id=${patientId}` });
        if (response.status !== 200) missing.push(`patient ${patientId}`);
        checked.patients += 1;
      }
    }

    expect(missing).toEqual([]);
    expect(checked.hospitals).toBeGreaterThan(0);
    expect(checked.slots).toBe(0);
  });

  it('holds the slots the fault scenarios book against', async () => {
    const h = await boot();
    const staleSlot = readFixture<{ steps: Array<{ fault: { slot_id: string; presented_version: number; current_version: number } }> }>(
      'fault-scenarios',
      'stale_slot_409.json',
    ).steps[0]!.fault;
    const slot = h.built.context.store.getSlot(staleSlot.slot_id);
    expect(slot, `slot ${staleSlot.slot_id} must exist in the served network`).toBeDefined();
    expect(slot!.status).toBe('held');
    expect(staleSlot.presented_version).toBeLessThan(staleSlot.current_version);

    const record = readFixture<{ steps: Array<{ fault: { record_id: string; superseded_by_record_id: string } }> }>(
      'fault-scenarios',
      'stale_coverage_evidence.json',
    ).steps[0]!.fault;
    const planIds = listFixture<{ plan_id: string }>('coverage', 'plans.json').map((plan) => plan.plan_id);
    const providerId = listFixture<{ provider_id: string }>('directory', 'providers.json')[0]!.provider_id;
    const ids: string[] = [];
    for (const planId of planIds) {
      const records = await call(h.app, { method: 'GET', url: `/network-records?plan_id=${planId}&provider_id=${providerId}` });
      ids.push(...records.body.records.map((entry: { record_id: string }) => entry.record_id));
    }
    expect(ids).toContain(record.record_id);
    expect(ids).toContain(record.superseded_by_record_id);
  });

  it('answers the fixture-authored availability queries, and names the one it refuses to match', async () => {
    const h = await boot();
    interface AvailabilityCase {
      query: Record<string, unknown>;
      slots: Array<{ slot_id: string; start_at: string }>;
      result_kind?: string;
      partial?: boolean;
    }
    const cases = listFixture<AvailabilityCase>('directory', 'availability_results.json');
    expect(cases.length).toBeGreaterThan(0);

    const divergences: string[] = [];

    for (const testCase of cases) {
      const days = (testCase.query.preferred_days ?? []) as string[];
      const label = `${testCase.query.hospital_id} ${testCase.query.provider_id}${days.length > 0 ? ` days=${days.join(',')}` : ''}`;
      const params = new URLSearchParams();
      for (const [key, value] of Object.entries(testCase.query)) {
        if (value === null || value === undefined) continue;
        if (Array.isArray(value)) for (const entry of value) params.append(key, String(entry));
        else params.set(key, String(value));
      }

      // The upstream-failure case is only reachable with the fault armed.
      const upstreamFailure = testCase.result_kind === 'upstream_error';
      if (upstreamFailure) await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'malformed_schema' } });

      const response = await call(h.app, { method: 'GET', url: `/availability?${params.toString()}` });
      expect(response.status, label).toBe(200);

      // Chronological order is part of the answer, in every case.
      const returned = response.body.slots as Array<{ slot_id: string; start_at: string }>;
      expect(returned.map((slot) => slot.start_at), `ordering for ${label}`).toEqual([...returned.map((slot) => slot.start_at)].sort());

      if (!upstreamFailure) {
        // The oracle is computed from the fixture files directly, not from the
        // store the module reads: available slots of this provider at this
        // hospital for this visit type inside the DECLARED window.
        const query = testCase.query as { hospital_id: string; provider_id: string; visit_type: string; earliest?: string; latest?: string };
        const oracle = listFixture<{ slot_id: string; provider_id: string; hospital_id: string; visit_type: string; status: string; start_at: string }>('directory', 'slots.json')
          .filter(
            (slot) =>
              slot.provider_id === query.provider_id &&
              slot.hospital_id === query.hospital_id &&
              slot.visit_type === query.visit_type &&
              slot.status === 'available' &&
              (!query.earliest || slot.start_at >= query.earliest) &&
              (!query.latest || slot.start_at <= query.latest),
          )
          .sort((left, right) => left.start_at.localeCompare(right.start_at))
          .map((slot) => slot.slot_id);
        const offered = listFixture<{ slot_id: string; provider_id: string; hospital_id: string; visit_type: string }>('directory', 'slots.json').some(
          (slot) => slot.provider_id === query.provider_id && slot.hospital_id === query.hospital_id && slot.visit_type === query.visit_type,
        );
        const expectedKind = oracle.length > 0 ? 'slots_found' : offered ? 'no_slots_for_constraints' : 'no_matching_provider';

        expect(returned.map((slot) => slot.slot_id), `slots for ${label}`).toEqual(oracle);
        expect(response.body.result_kind, `result_kind for ${label}`).toBe(expectedKind);
      }

      if (testCase.result_kind) {
        const actual = returned.map((slot) => slot.slot_id).sort();
        const authored = testCase.slots.map((slot) => slot.slot_id).sort();
        if (JSON.stringify(actual) !== JSON.stringify(authored) || response.body.result_kind !== testCase.result_kind) {
          divergences.push(label);
          continue;
        }
        expect(response.body.result_kind, `authored result_kind for ${label}`).toBe(testCase.result_kind);
      }
      // `partial` is optional in the contract: absent means "not truncated".
      if (testCase.partial !== undefined) expect(response.body.partial ?? false, `partial for ${label}`).toBe(testCase.partial);

      if (upstreamFailure) {
        await call(h.app, { method: 'POST', url: '/_faults/reset' });
        await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { mode: 'off' } });
      }
    }

    // Four authored expectations disagree with the data the same fixture set
    // ships. Each is a fixture-side finding rather than a service behaviour to
    // copy, and pinning them here keeps the disagreement visible:
    //
    //   …0001/…0001        authored 3 slots; slot …0005 (2026-10-29) is available
    //                      and inside the DECLARED window (latest 2026-11-14).
    //   …0001/…0001 friday authored 0 slots and `no_slots_for_constraints`; the
    //                      window holds 4 available slots and none of them is on
    //                      a Friday, which is what emptied the list.
    //   …0001/…0004 friday authored 1 slot; slot …0012 (Mon 2026-10-19) is
    //                      available and inside the window, and was dropped for
    //                      the same reason.
    //   …0001/…0011        authored "no_matching_provider"; slot …0027 is an
    //                      available first visit at that hospital in the window,
    //                      so this provider does work there.
    //
    // The first and the last are data defects. The middle two are the file
    // reading `preferred_days` as a hard filter — and it cannot mean that
    // consistently: `preferred_days: ['friday']` keeps the WEDNESDAY slot …0021
    // in case 6, and `preferred_time_of_day` is contradicted in every case whose
    // data can tell (cases 1, 5, 6, 9 and 10 each list a slot that is not of the
    // preferred time of day). Since no single filter rule reproduces the file,
    // the service answers with the facts and echoes the preferences inside
    // `query`, where the layer that ranks — and can explain itself — still has
    // them. See `answerAvailability` in `src/modules/availability.ts`.
    expect(divergences).toEqual([
      '20000000-0000-4000-8000-000000000001 30000000-0000-4000-8000-000000000001',
      '20000000-0000-4000-8000-000000000001 30000000-0000-4000-8000-000000000001 days=friday',
      '20000000-0000-4000-8000-000000000001 30000000-0000-4000-8000-000000000004 days=friday',
      '20000000-0000-4000-8000-000000000001 30000000-0000-4000-8000-000000000011',
    ]);
  });
});

describe('fixture fault scenarios', () => {
  it('loads every file the manifest names, with no errors and no leftovers', async () => {
    const h = await boot();
    const load = h.built.context.engine.load;
    const manifest = readFixture<ScenarioManifest>('scenarios.json');
    const named = manifest.scenarios.flatMap((scenario) => scenario.fault_scenarios);

    expect(load.errors).toEqual([]);
    expect(load.manifest.found).toBe(true);
    expect(load.manifest.complete).toBe(true);
    expect(load.manifest.missing_files).toEqual([]);
    expect(load.manifest.unreferenced_files).toEqual([]);
    expect(named.length).toBeGreaterThan(0);
    expect(load.variants.map((variant) => variant.source_file).sort()).toEqual([...named].sort());

    // Grouping comes from the manifest, not from a guess.
    for (const scenario of manifest.scenarios.filter((entry) => entry.fault_scenarios.length > 0)) {
      const journey = load.journeys.find((entry) => entry.journey === scenario.scenario_id);
      expect(journey, `journey ${scenario.scenario_id}`).toBeDefined();
      expect(journey!.files.sort()).toEqual([...scenario.fault_scenarios].sort());
      expect(journey!.declared_by_manifest).toBe(true);
    }
  });

  it('makes every fault reachable by its own name, carrying the authored seed', async () => {
    const h = await boot();
    const load = h.built.context.engine.load;
    const byInjection = new Map(load.variants.map((variant) => [variant.injection, variant]));

    for (const fault of FAULT_TYPES) {
      const variant = byInjection.get(fault);
      expect(variant, `no fixture injects ${fault}`).toBeDefined();
      const scenario = h.built.context.engine.scenarios.find((entry) => entry.scenario_id === fault);
      expect(scenario, `arming ${fault} must resolve`).toBeDefined();
      expect(scenario!.fault_seed).toBe(variant!.fault_seed);

      const armed = await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: fault } });
      expect(armed.status, `arm ${fault}`).toBe(200);
      await call(h.app, { method: 'POST', url: '/_faults/reset' });
    }
  });

  it('reproduces the three-transient-error ladder in order, from the fixture steps', async () => {
    const h = await boot();
    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'three_transient_errors' } });
    const engine = h.built.context.engine;
    const decide = () =>
      engine.beginRequest({ method: 'GET', path: '/availability', headers: {}, query: {} });

    expect(decide()?.fault.type).toBe('latency');
    expect(decide()?.fault.type).toBe('rate_limit_429');
    const third = decide();
    expect(third?.fault.type).toBe('service_unavailable_503');
    expect(third?.fault.retry_after_seconds).toBe(2);
    expect(third?.seed).toBe(43);
    expect(decide()).toBeNull();

    // The fourth fault of the journey targets the directory search, which the
    // union had to keep rather than drop.
    const search = engine.beginRequest({ method: 'GET', path: '/directory/search', headers: {}, query: {} });
    expect(search?.fault.type).toBe('malformed_schema');
    expect(search?.seed).toBe(44);
  });

  it('keeps the sub-variants of a journey separately addressable', async () => {
    const h = await boot();
    const load = h.built.context.engine.load;
    const ids = load.variants.map((variant) => variant.id);
    expect(ids).toContain('timeout_after_commit/timeout_before_commit');
    expect(ids).toContain('timeout_after_commit/duplicate_event');
    expect(ids).toContain('standard_booking/notification_failure');
    expect(ids).toContain('standard_booking/stale_slot_409');

    // The bare journey id resolves to the canonical injection, not to whichever
    // file happened to be read last.
    const journey = load.journeys.find((entry) => entry.journey === 'timeout_after_commit')!;
    expect(journey.preferred_file).toBe('timeout_after_commit.json');
    const scenario = h.built.context.engine.scenarios.find((entry) => entry.scenario_id === 'timeout_after_commit')!;
    expect(scenario.steps?.length).toBe(1);
    expect(scenario.fault_seed).toBe(42);
  });

  it('answers the malformed directory search with a contract-violating 200, never an empty success', async () => {
    const h = await boot();
    const armed = await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'malformed_schema' } });
    expect(armed.status).toBe(200);

    const broken = await call(h.app, { method: 'GET', url: '/directory/search?specialty=cardiology' });
    expect(broken.status).toBe(200);
    expect(broken.body.candidates).toBeUndefined();
    expect(broken.body.result_kind).toBe('maybe');
    expect(broken.headers['x-mock-fault']).toBe('malformed_schema');

    await call(h.app, { method: 'POST', url: '/_faults/reset' });
    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { mode: 'off' } });

    const clean = await call(h.app, { method: 'GET', url: '/directory/search?specialty=cardiology' });
    expect(clean.status).toBe(200);
    expect(clean.body.result_kind).toBe('candidates_found');
    expect(clean.body.candidates.length).toBeGreaterThan(0);
    expect(clean.body.candidates[0]).not.toHaveProperty('constraint_fit');
    expect(clean.body.candidates[0]).not.toHaveProperty('rank');

    const none = await call(h.app, { method: 'GET', url: '/directory/search?specialty=astrology' });
    expect(none.body.result_kind).toBe('no_matching_provider');
    expect(none.body.candidates).toEqual([]);
  });

  it('reports the empty upstream page as no_slots_for_constraints, not as a failure', async () => {
    const h = await boot();
    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'empty_valid_result' } });
    const response = await call(h.app, { method: 'GET', url: '/availability?hospital_id=20000000-0000-4000-8000-000000000001&provider_id=30000000-0000-4000-8000-000000000001&visit_type=first_visit' });
    expect(response.status).toBe(200);
    expect(response.body.result_kind).toBe('no_slots_for_constraints');
    expect(response.body.slots).toEqual([]);
    expect(response.body.error).toBeUndefined();
    expect(response.headers['x-mock-fault']).toBeUndefined();
  });

  it('shows the stale coverage evidence as stale and conflicted, with the record ids intact', async () => {
    const h = await boot();
    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'stale_coverage_evidence' } });
    const response = await call(
      h.app,
      {
        method: 'GET',
        url: '/coverage?plan_id=60000000-0000-4000-8000-000000000001&provider_id=30000000-0000-4000-8000-000000000001&hospital_id=20000000-0000-4000-8000-000000000001',
      },
    );
    expect(response.status).toBe(200);
    expect(response.body.freshness.state).toBe('stale');
    expect(response.body.conflict).toBe(true);
    expect(response.body.winning_evidence_id).toBe('50000000-0000-4000-8000-000000000001');

    const evidence = await call(h.app, { method: 'GET', url: '/evidence/50000000-0000-4000-8000-000000000001' });
    expect(evidence.status).toBe(200);
    expect(evidence.body.source).toBe('coverage_api');
    expect(evidence.body.freshness.age_seconds).toBeGreaterThan(0);
    const snapshots = await call(h.app, { method: 'GET', url: '/evidence-snapshots' });
    expect(snapshots.body.count).toBe(2);
    expect(snapshots.body.snapshots.map((snapshot: { stale: boolean }) => snapshot.stale).sort()).toEqual([false, true]);
  });

  it('refuses a booking against a slot whose version moved on', async () => {
    const h = await boot();
    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'stale_slot_409' } });
    const slots = listFixture<{ slot_id: string; provider_id: string; hospital_id: string; visit_type: string }>('directory', 'slots.json');
    const target = slots.find((slot) => slot.slot_id === '40000000-0000-4000-8000-000000000013')!;
    const response = await call(h.app, {
      method: 'POST',
      url: '/appointments',
      payload: {
        patient_id: 'b0000000-0000-4000-8000-000000000001',
        provider_id: target.provider_id,
        hospital_id: target.hospital_id,
        slot_id: target.slot_id,
        visit_type: target.visit_type,
        contact_phone: '+13125550101',
      },
      headers: { 'idempotency-key': 'fixture-stale-slot-0001' },
    });
    expect(response.status).toBe(409);
    expect(response.body.error.code).toBe('slot_conflict');
    expect(response.body.error.detail.slot_status).toBe('held');
    expect(response.body.error.detail.stale_version_presented).toBe(true);
    // The fixture's own numbering travels with the refusal.
    expect(response.body.error.detail.fault_params.presented_version).toBe(2);
    expect(response.body.error.detail.fault_params.current_version).toBe(3);

    // The loser of a slot conflict writes NOTHING: no booking, no attempt, no
    // appointment. The refusal lives in the response, not in a half-written row.
    const diagnostics = await call(h.app, { method: 'GET', url: '/_diagnostics/attempts?idempotency_key=fixture-stale-slot-0001' });
    expect(diagnostics.body.booking).toBeNull();
    expect(diagnostics.body.attempts).toEqual([]);
    expect(diagnostics.body.note).toBe('no booking with that key');

    // The slot is untouched and still there for the next caller to re-fetch.
    const slotAfter = h.built.context.store.getSlot(target.slot_id)!;
    expect(slotAfter.status).toBe('held');
    expect(h.built.context.store.countAppointmentsForSlot(target.slot_id)).toBe(0);
  });

  it('applies a fault that names a subject only to the request about that subject', async () => {
    const h = await boot();
    const store = h.built.context.store;
    const slots = listFixture<{ slot_id: string; provider_id: string; hospital_id: string; visit_type: string }>('directory', 'slots.json');
    const target = slots.find((slot) => slot.slot_id === '40000000-0000-4000-8000-000000000013')!;

    /* Another slot for the SAME provider at the SAME hospital: the only thing
       that differs between the two bookings below is the subject the fault
       fixture names. */
    const other = store
      .listSlots({ hospitalId: target.hospital_id, providerId: target.provider_id, visitType: 'first_visit' })
      .find((slot) => slot.slot_id !== target.slot_id && slot.status === 'available')!;
    expect(other, 'the fixture set must offer a second bookable slot for this provider').toBeTruthy();

    const body = (slotId: string, extra: Record<string, unknown> = {}): Record<string, unknown> => ({
      patient_id: 'b0000000-0000-4000-8000-000000000001',
      provider_id: target.provider_id,
      hospital_id: target.hospital_id,
      slot_id: slotId,
      visit_type: target.visit_type,
      contact_phone: '+13125550101',
      ...extra,
    });

    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'stale_slot_409' } });

    /* The fixture says THIS slot moved on; it does not say "the next booking is
       refused". A booking about another slot is served normally. */
    const unrelated = await call(h.app, {
      method: 'POST',
      url: '/appointments',
      payload: body(other.slot_id),
      headers: { 'idempotency-key': 'fixture-stale-other-0001' },
    });
    expect(unrelated.status, JSON.stringify(unrelated.body)).toBeLessThan(400);
    expect(unrelated.body.booking.state).toBe('CONFIRMED');

    /* The decision was offered and handed straight back: the fault log records
       faults that were SERVED, so nothing is on record for that request, and the
       armed scenario has not spent the occurrence it was authored for. */
    const offered = await call(h.app, { method: 'GET', url: '/_faults/state' });
    const staleEvents = (offered.body.occurrences as Array<{ fault_type: string }>).filter(
      (event) => event.fault_type === 'stale_slot_409',
    );
    expect(staleEvents).toEqual([]);

    /* Proof that the occurrence survived: the very next booking of the named
       slot is refused by the scenario armed two requests ago. */
    const named = await call(h.app, {
      method: 'POST',
      url: '/appointments',
      payload: body(target.slot_id),
      headers: { 'idempotency-key': 'fixture-stale-named-0002' },
    });
    expect(named.status).toBe(409);
    const detail = named.body.error.detail as Record<string, unknown>;
    expect(detail.slot_id).toBe(target.slot_id);
    expect(detail.stale_version_presented).toBe(true);
    /* The caller presented no version, and the envelope says exactly that
       instead of naming a version nobody sent. */
    expect(detail.presented_version).toBeNull();
    /* What the hospital reports the slot stands at now (the fixture's own
       number), kept apart from the stored row's version this service holds. */
    expect(detail.current_version).toBe(3);
    expect(detail.slot_version).toBe(store.getSlot(target.slot_id)!.version);

    /* A caller that DOES present a version is quoted with its own number. */
    await call(h.app, { method: 'POST', url: '/_faults/reset' });
    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'stale_slot_409' } });
    const presented = await call(h.app, {
      method: 'POST',
      url: '/appointments',
      payload: body(target.slot_id, { expected_slot_version: 9 }),
      headers: { 'idempotency-key': 'fixture-stale-presented-0003' },
    });
    expect(presented.status).toBe(409);
    expect((presented.body.error.detail as Record<string, unknown>).presented_version).toBe(9);
    // …while the fixture's authored numbers still travel under `fault_params`.
    expect((presented.body.error.detail as Record<string, unknown>).fault_params).toMatchObject({
      presented_version: 2,
      current_version: 3,
      http_status: 409,
    });
  });

  it('honours the retry backoff on an automatic delivery pass, and not on an explicit one', async () => {
    /* The default backoff (5 minutes), which is what a demo run uses: with zero
       the two halves of this rule would be indistinguishable. */
    harness = await makeFixtureHarness({ notificationBackoffMinutes: 5 });
    const h = harness;
    const slot = listFixture<{ slot_id: string; provider_id: string; hospital_id: string; visit_type: string }>(
      'directory',
      'slots.json',
    ).find((entry) => entry.slot_id === '40000000-0000-4000-8000-000000000001')!;

    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'notification_failure' } });
    const booking = await call(h.app, {
      method: 'POST',
      url: '/appointments',
      payload: {
        patient_id: 'b0000000-0000-4000-8000-000000000001',
        provider_id: slot.provider_id,
        hospital_id: slot.hospital_id,
        slot_id: slot.slot_id,
        visit_type: slot.visit_type,
        contact_phone: '+13125550101',
      },
      headers: { 'idempotency-key': 'fixture-notification-backoff-0001' },
    });
    expect(booking.status).toBeLessThan(400);
    const bookingRequestId = booking.body.booking.booking_request_id as string;

    const outbox = await call(h.app, { method: 'GET', url: `/notifications/outbox?booking_request_id=${bookingRequestId}` });
    const entries = outbox.body.entries as Array<{ outbox_id: string; channel: string; status: string; attempts: number; next_attempt_at: string | null }>;
    const sms = entries.find((entry) => entry.channel === 'sms')!;
    expect(sms.status).toBe('failed');
    expect(sms.next_attempt_at).not.toBeNull();
    expect(new Date(sms.next_attempt_at!).getTime()).toBeGreaterThan(Date.now());

    /* The automatic pass leaves the failed leg alone: it is not due yet, and a
       provider that just failed is not hammered by the loop. */
    const automatic = await call(h.app, {
      method: 'POST',
      url: '/notifications/drain',
      payload: { booking_request_id: bookingRequestId },
    });
    expect(automatic.status).toBe(200);
    expect(automatic.body.examined).toBe(0);
    expect(automatic.body.sent).toEqual([]);
    expect(automatic.body.failed).toEqual([]);

    /* …and the entry is exactly as it was, backoff and all: skipped, not
       re-attempted, not silently marked delivered. */
    const after = await call(h.app, { method: 'GET', url: `/notifications/outbox/${sms.outbox_id}` });
    expect(after.body).toMatchObject({
      status: 'failed',
      attempts: 1,
      next_attempt_at: sms.next_attempt_at,
    });

    /* An explicit delivery request means now, so the same entry IS attempted —
       past the backoff — and the fault the fixture names answers for it. */
    await call(h.app, { method: 'POST', url: '/_faults/reset' });
    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'notification_failure' } });
    const notified = await call(h.app, { method: 'POST', url: '/notifications', payload: { booking_request_id: bookingRequestId } });
    expect(notified.status).toBe(502);
    expect(notified.body.error.code).toBe('notification_undeliverable');
    expect(notified.body.error.detail.channel).toBe('sms');
    expect(notified.body.error.detail.attempts).toBe(2);
    expect(notified.body.error.detail.booking_still_committed).toBe(true);

    /* The named-entry retry is explicit in the same way, and with the scenario
       spent it actually delivers the leg the patient was waiting for. */
    const retried = await call(h.app, { method: 'POST', url: `/notifications/outbox/${sms.outbox_id}/retry` });
    expect(retried.status).toBe(200);
    expect(retried.body.report.sent).toEqual([sms.outbox_id]);
    expect(retried.body.entry).toMatchObject({ status: 'sent', attempts: 3, last_error: null, next_attempt_at: null });
  });

  it('fails the delivery the fixture names, and only that leg, leaving the booking standing', async () => {
    const h = await boot();
    const slots = listFixture<{ slot_id: string; provider_id: string; hospital_id: string; visit_type: string }>('directory', 'slots.json');
    const slot = slots.find((entry) => entry.slot_id === '40000000-0000-4000-8000-000000000001')!;

    // The fixture addresses the delivery as `POST /notifications` with
    // `channel: "sms"`. The delivery the journey actually performs is the outbox
    // sender's hop, and the two are one folded route — so arming the fault by
    // its name reaches that hop rather than sitting on an address nobody calls.
    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'notification_failure' } });

    const booking = await call(h.app, {
      method: 'POST',
      url: '/appointments',
      payload: {
        patient_id: 'b0000000-0000-4000-8000-000000000001',
        provider_id: slot.provider_id,
        hospital_id: slot.hospital_id,
        slot_id: slot.slot_id,
        visit_type: slot.visit_type,
        document_ids: ['a0000000-0000-4000-8000-000000000001'],
        contact_phone: '+13125550101',
      },
      headers: { 'idempotency-key': 'fixture-notification-0001' },
    });
    expect(booking.status).toBeLessThan(400);
    expect(booking.body.booking.state).toBe('CONFIRMED');
    const bookingRequestId = booking.body.booking.booking_request_id as string;

    const outbox = await call(h.app, { method: 'GET', url: `/notifications/outbox?booking_request_id=${bookingRequestId}` });
    const byChannel = new Map(outbox.body.entries.map((entry: { channel: string; status: string }) => [entry.channel, entry.status]));
    // The leg the fixture names failed; the other one still reached the patient.
    expect(byChannel.get('sms')).toBe('failed');
    expect(byChannel.get('email')).toBe('sent');

    // The booking is not collateral damage. That is the whole invariant.
    const reread = await call(h.app, { method: 'GET', url: `/appointments/${bookingRequestId}` });
    expect(reread.body.booking.state).toBe('CONFIRMED');

    // Replaying the scenario resets the occurrence counters — the fault is one
    // per occurrence, and the inline delivery already spent it — so the route
    // the fixture names answers for the failure in its own words: 502, the SMS
    // leg, and the booking explicitly still standing.
    await call(h.app, { method: 'POST', url: '/_faults/reset' });
    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'notification_failure' } });
    const notified = await call(h.app, { method: 'POST', url: '/notifications', payload: { booking_request_id: bookingRequestId } });
    expect(notified.status).toBe(502);
    expect(notified.body.error.code).toBe('notification_undeliverable');
    expect(notified.body.error.fault_type).toBe('notification_failure');
    expect(notified.body.error.detail.channel).toBe('sms');
    expect(notified.body.error.detail.booking_still_committed).toBe(true);
    expect(notified.body.error.detail.booking_state).toBe('CONFIRMED');

    // The recorded event carries the seed the fixture declares (50), not the
    // process default: a log with the wrong seed cannot be replayed.
    const state = await call(h.app, { method: 'GET', url: '/_faults/state' });
    const seeds = (state.body.occurrences as Array<{ fault_type: string; seed: number }>)
      .filter((event) => event.fault_type === 'notification_failure')
      .map((event) => event.seed);
    expect(seeds).toContain(50);
    expect(seeds.every((seed) => seed === 50)).toBe(true);
  });
});
