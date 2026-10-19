import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { FastifyInstance } from 'fastify';
import { buildApp, type BuiltApp } from '../src/app.js';
import { DEMO_PATIENT_ID, HOSPITAL_IDS, PLAN_IDS, DEMO_PHONE } from '../src/domain/seed.js';
import type { SlotRecord } from '../src/db/store.js';
import type { FaultScenario } from '../src/faults/types.js';

/**
 * The two adapter dialects, as wired by the seed: `instant_confirm` confirms in
 * the create response, `review_required` only opens a review queue.
 */
export const HOSPITAL = {
  instantConfirm: HOSPITAL_IDS.northlake,
  reviewRequired: HOSPITAL_IDS.stAuburn,
};

/**
 * The same two scenarios the service ships with, but with a 5 ms stall instead
 * of 30 s. The delay IS the fault, so a test that had to sit through the real
 * one would be testing the clock rather than the pipeline.
 */
export function fastTimeoutScenarios(): FaultScenario[] {
  return [
    {
      scenario_id: 'timeout_before_commit',
      description: 'test override: times out fast, before anything is committed',
      fault_seed: 42,
      default_fault: 'timeout_before_commit',
      steps: [{ match: { method: 'POST', path: '/appointments' }, occurrence: 1, fault: { type: 'timeout_before_commit', delay_ms: 5 } }],
    },
    {
      scenario_id: 'timeout_after_commit',
      description: 'test override: times out fast, after the commit stuck',
      fault_seed: 42,
      default_fault: 'timeout_after_commit',
      steps: [{ match: { method: 'POST', path: '/appointments' }, occurrence: 1, fault: { type: 'timeout_after_commit', delay_ms: 5 } }],
    },
  ];
}

export interface Harness {
  app: FastifyInstance;
  built: BuiltApp;
  dir: string;
  cleanup: () => Promise<void>;
}

/**
 * Every test gets its own SQLite file: the idempotency keys, the slot claims
 * and the outbox have to survive a restart, and that is only true for a real
 * store.
 */
export interface HarnessOptions {
  faultSeed?: number;
  notificationMaxAttempts?: number;
  seed?: boolean;
  dbPath?: string;
  /**
   * Scenarios written out as real fixture files, exactly as the fixtures agent
   * would drop them in `fixtures/fault-scenarios/`. A scenario id that matches
   * a built-in replaces it, which is how the tests shorten the 30 s timeouts.
   */
  scenarios?: FaultScenario[];
}

export async function makeHarness(options: HarnessOptions = {}): Promise<Harness> {
  const dir = mkdtempSync(join(tmpdir(), 'mock-api-test-'));
  const scenarioDir = join(dir, 'fault-scenarios');
  if (options.scenarios && options.scenarios.length > 0) {
    mkdirSync(scenarioDir, { recursive: true });
    for (const scenario of options.scenarios) {
      writeFileSync(join(scenarioDir, `${scenario.scenario_id}.json`), JSON.stringify(scenario, null, 2), 'utf8');
    }
  }
  const built = await buildApp({
    logger: false,
    seed: options.seed ?? true,
    config: {
      dbPath: options.dbPath ?? join(dir, 'mock-api.sqlite'),
      faultSeed: options.faultSeed ?? 42,
      faultScenarioDir: options.scenarios && options.scenarios.length > 0 ? scenarioDir : join(dir, 'no-such-fixture-dir'),
      // These tests pin the built-in dataset deliberately: the shared demo
      // fixtures are exercised by `fixtures-integration.test.ts`, so a change in
      // `fixtures/` can never make a contract test pass or fail by accident.
      seedMode: 'builtin',
      notificationMaxAttempts: options.notificationMaxAttempts ?? 3,
      notificationBackoffMinutes: 0,
    },
  });
  return {
    app: built.app,
    built,
    dir,
    cleanup: async () => {
      await built.close();
      rmSync(dir, { recursive: true, force: true });
    },
  };
}

/**
 * A harness on the SHARED fixtures: `fixtures/directory`, `fixtures/coverage`,
 * `fixtures/documents`, `fixtures/evidence.json` and the real
 * `fixtures/fault-scenarios`. Nothing is pinned or shortened, so what this
 * harness sees is what a demo run sees.
 */
export async function makeFixtureHarness(
  options: { faultSeed?: number; dbPath?: string; notificationBackoffMinutes?: number } = {},
): Promise<Harness> {
  const dir = mkdtempSync(join(tmpdir(), 'mock-api-fixtures-'));
  const built = await buildApp({
    logger: false,
    config: {
      dbPath: options.dbPath ?? join(dir, 'mock-api.sqlite'),
      seedMode: 'fixtures',
      faultSeed: options.faultSeed ?? 42,
      /* Zero by default so a failing delivery is retryable within the test's own
         lifetime. A test that is ABOUT the retry backoff sets the real default
         (5) and gets the schedule a demo run sees. */
      notificationBackoffMinutes: options.notificationBackoffMinutes ?? 0,
    },
  });
  return {
    app: built.app,
    built,
    dir,
    cleanup: async () => {
      await built.close();
      rmSync(dir, { recursive: true, force: true });
    },
  };
}

export interface InjectedResponse {
  status: number;
  body: any;
  headers: Record<string, unknown>;
}

export async function call(
  app: FastifyInstance,
  options: {
    method: 'GET' | 'POST' | 'PUT' | 'DELETE';
    url: string;
    payload?: unknown;
    headers?: Record<string, string>;
  },
): Promise<InjectedResponse> {
  const response = await app.inject({
    method: options.method,
    url: options.url,
    ...(options.payload === undefined ? {} : { payload: options.payload as object }),
    headers: options.headers ?? {},
  });
  let body: unknown = null;
  try {
    body = response.body ? JSON.parse(response.body) : null;
  } catch {
    body = response.body;
  }
  return { status: response.statusCode, body, headers: response.headers as Record<string, unknown> };
}

/**
 * A provider that genuinely offers `visitType` at `hospitalId`.
 *
 * The provider→hospital link table is not the same thing as a schedule: a
 * provider can be linked to a hospital without the seed giving them any slots
 * there, so the first linked row is not a safe default. Picking the first
 * provider who actually has availability keeps every test off that landmine.
 */
export function providerFor(harness: Harness, hospitalId: string, visitType: 'first_visit' | 'return_visit' = 'first_visit'): string {
  const providers = harness.built.context.store.listProvidersForHospital(hospitalId);
  for (const row of providers) {
    const providerId = (JSON.parse(String(row.payload)) as { provider_id: string }).provider_id;
    if (harness.built.context.store.listSlots({ hospitalId, providerId, visitType }).length > 0) return providerId;
  }
  throw new Error(`no provider offers ${visitType} at hospital ${hospitalId} — seeding is broken`);
}

export function slotFor(
  harness: Harness,
  options: { hospitalId?: string; providerId?: string; visitType?: 'first_visit' | 'return_visit'; index?: number } = {},
): SlotRecord {
  const hospitalId = options.hospitalId ?? HOSPITAL_IDS.northlake;
  const hospitalRow = harness.built.context.store.getHospital(hospitalId);
  if (!hospitalRow) throw new Error(`no hospital ${hospitalId}`);
  const visitType = options.visitType ?? 'first_visit';
  const providerId = options.providerId ?? providerFor(harness, hospitalId, visitType);
  const slots = harness.built.context.store.listSlots({ hospitalId, providerId, visitType });
  if (slots.length === 0) throw new Error('fixture produced no slots — seeding is broken');
  const index = options.index ?? 0;
  const slot = slots[index % slots.length]!;
  return slot;
}

export function bookingBody(
  harness: Harness,
  overrides: Partial<{
    patient_id: string;
    provider_id: string;
    hospital_id: string;
    slot_id: string;
    visit_type: 'first_visit' | 'return_visit';
    document_ids: string[];
    contact_phone: string | null;
  }> = {},
): Record<string, unknown> {
  const hospitalId = overrides.hospital_id ?? HOSPITAL_IDS.northlake;
  const visitType = overrides.visit_type ?? 'first_visit';
  const providerId = overrides.provider_id ?? providerFor(harness, hospitalId, visitType);
  const slot = slotFor(harness, { hospitalId, providerId, visitType });
  return {
    patient_id: overrides.patient_id ?? DEMO_PATIENT_ID,
    provider_id: providerId,
    hospital_id: hospitalId,
    slot_id: overrides.slot_id ?? slot.slot_id,
    visit_type: visitType,
    contact_phone: overrides.contact_phone ?? DEMO_PHONE,
    ...(overrides.document_ids ? { document_ids: overrides.document_ids } : {}),
  };
}

export { DEMO_PATIENT_ID, HOSPITAL_IDS, PLAN_IDS };
