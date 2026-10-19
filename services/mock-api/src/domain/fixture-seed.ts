import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Store } from '../db/store.js';
import type { SeedSummary } from './seed.js';

/**
 * Seeds the service from the shared demo fixtures.
 *
 * The fixtures under `fixtures/` are the demo's ground truth: the scenario
 * manifest (`fixtures/scenarios.json`) names hospital `20000000-…-0001`, slot
 * `40000000-…-0013` and network record `70000000-…-0002`, and the fault
 * scenarios target concrete fixture ids. Serving a parallel synthetic dataset
 * would make every one of those references dangle, so when the fixtures are
 * present they are what the API serves — verbatim, record for record, with the
 * payload stored as the published contract record.
 *
 * Nothing here is invented: every value comes from a file, and a missing or
 * unreadable file is a loud failure rather than a partially seeded world.
 */

export interface FixtureLoadOptions {
  fixturesRoot: string;
}

/** Ids the demo scenarios depend on, read from the fixtures rather than guessed. */
export interface FixtureIdentity {
  patient_id: string;
  patient_name: string;
  instant_confirm_hospital_id: string;
  review_required_hospital_id: string;
  plan_id: string;
  plan_payer_name: string;
  provider_id: string;
  slot_id: string;
  network_record_id: string;
}

interface Loaded {
  locations: Record<string, unknown>[];
  hospitals: Record<string, unknown>[];
  providers: Record<string, unknown>[];
  slots: Record<string, unknown>[];
  plans: Record<string, unknown>[];
  networkRecords: Record<string, unknown>[];
  referralRequirements: Record<string, unknown>[];
  documents: Record<string, unknown>[];
  evidence: Record<string, unknown>[];
  coverageSnapshots: Record<string, unknown>[];
  identity: FixtureIdentity;
}

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, 'utf8')) as unknown;
}

/** Accepts either a bare array or an object wrapping the list under any key. */
function readList(path: string): Record<string, unknown>[] {
  if (!existsSync(path)) throw new Error(`missing fixture file ${path}`);
  const parsed = readJson(path);
  if (Array.isArray(parsed)) return parsed as Record<string, unknown>[];
  if (parsed && typeof parsed === 'object') {
    const list = Object.values(parsed as Record<string, unknown>).find((value) => Array.isArray(value));
    if (Array.isArray(list)) return list as Record<string, unknown>[];
  }
  throw new Error(`fixture file ${path} holds a list under no recognisable key`);
}

function str(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== 'string' || value === '') throw new Error(`fixture record is missing a string ${key}`);
  return value;
}

function loadFixtures(root: string): Loaded {
  if (!existsSync(root)) throw new Error(`fixtures directory ${root} does not exist`);

  const locations = readList(join(root, 'directory', 'locations.json'));
  const hospitals = readList(join(root, 'directory', 'hospitals.json'));
  const providers = readList(join(root, 'directory', 'providers.json'));
  const slots = readList(join(root, 'directory', 'slots.json'));
  const plans = readList(join(root, 'coverage', 'plans.json'));
  const networkRecords = readList(join(root, 'coverage', 'network_records.json'));
  const referralRequirements = readList(join(root, 'coverage', 'referral_requirements.json'));
  const documents = readList(join(root, 'documents', 'documents.json'));
  const evidence = readList(join(root, 'evidence.json'));
  const coverageSnapshots = readList(join(root, 'coverage', 'evidence_snapshots.json'));
  const patients = readList(join(root, 'patients', 'patient_details.json'));

  const instantConfirm = hospitals.find((hospital) => hospital.adapter_type === 'instant_confirm');
  const reviewRequired = hospitals.find((hospital) => hospital.adapter_type === 'review_required');
  const firstPatient = patients[0];
  const firstPlan = plans[0];
  const firstProvider = providers[0];
  const firstSlot = slots.find((slot) => slot.provider_id === firstProvider?.provider_id) ?? slots[0];
  const firstRecord = networkRecords[0];
  if (!instantConfirm || !reviewRequired || !firstPatient || !firstPlan || !firstProvider || !firstSlot || !firstRecord) {
    throw new Error(`fixtures under ${root} do not contain a usable directory`);
  }

  return {
    locations,
    hospitals,
    providers,
    slots,
    plans,
    networkRecords,
    referralRequirements,
    documents,
    evidence,
    coverageSnapshots,
    identity: {
      patient_id: str(firstPatient, 'patient_id'),
      patient_name: str(firstPatient, 'full_name'),
      instant_confirm_hospital_id: str(instantConfirm, 'hospital_id'),
      review_required_hospital_id: str(reviewRequired, 'hospital_id'),
      plan_id: str(firstPlan, 'plan_id'),
      plan_payer_name: str(firstPlan, 'payer_name'),
      provider_id: str(firstProvider, 'provider_id'),
      slot_id: str(firstSlot, 'slot_id'),
      network_record_id: str(firstRecord, 'record_id'),
    },
  };
}

/** Reads the fixtures without writing anything, for diagnostics and tests. */
export function readFixtureIdentity(root: string): FixtureIdentity {
  return loadFixtures(root).identity;
}

export function fixturesAvailable(root: string): boolean {
  return (
    existsSync(join(root, 'directory', 'hospitals.json')) &&
    existsSync(join(root, 'directory', 'slots.json')) &&
    existsSync(join(root, 'coverage', 'plans.json')) &&
    existsSync(join(root, 'documents', 'documents.json')) &&
    existsSync(join(root, 'evidence.json'))
  );
}

/**
 * Writes the fixture directory into the store. Idempotent: directory rows are
 * replaced, slots are `INSERT OR IGNORE`d so a restart can never resurrect a
 * slot a live booking already holds.
 */
export function seedFromFixtures(store: Store, options: FixtureLoadOptions): SeedSummary {
  const fixture = loadFixtures(options.fixturesRoot);

  for (const location of fixture.locations) {
    store.upsertDirectoryRow(
      'locations',
      str(location, 'location_id'),
      [
        ['city', str(location, 'city')],
        ['state', str(location, 'state')],
        ['timezone', str(location, 'timezone')],
      ],
      location,
    );
  }

  for (const hospital of fixture.hospitals) {
    store.upsertDirectoryRow(
      'hospitals',
      str(hospital, 'hospital_id'),
      [
        ['name', str(hospital, 'name')],
        ['location_id', str(hospital, 'location_id')],
        ['timezone', str(hospital, 'timezone')],
        ['adapter_type', str(hospital, 'adapter_type')],
      ],
      hospital,
    );
  }

  for (const provider of fixture.providers) {
    const providerId = str(provider, 'provider_id');
    store.upsertDirectoryRow(
      'providers',
      providerId,
      [
        ['specialty', str(provider, 'specialty')],
        ['display_name', str(provider, 'display_name')],
      ],
      provider,
    );
    const hospitalIds = Array.isArray(provider.hospital_ids) ? (provider.hospital_ids as unknown[]) : [];
    for (const hospitalId of hospitalIds) {
      if (typeof hospitalId === 'string') store.linkProviderHospital(providerId, hospitalId);
    }
  }

  for (const plan of fixture.plans) {
    store.upsertDirectoryRow('insurance_plans', str(plan, 'plan_id'), [], plan);
  }

  for (const record of fixture.networkRecords) {
    store.upsertNetworkRecord(record as Record<string, unknown> & { record_id: string });
  }

  for (const requirement of fixture.referralRequirements) {
    store.upsertReferralRequirement(requirement as Record<string, unknown> & { requirement_id: string });
  }

  for (const document of fixture.documents) {
    store.upsertDocument(document as Record<string, unknown> & { document_id: string });
  }

  for (const record of fixture.evidence) {
    store.upsertEvidence(record as Record<string, unknown> & { evidence_id: string });
  }

  for (const snapshot of fixture.coverageSnapshots) {
    store.upsertCoverageSnapshot(snapshot as Record<string, unknown> & { snapshot_id: string });
  }

  let slots = 0;
  for (const slot of fixture.slots) {
    store.insertSlot(slot);
    slots += 1;
  }

  return {
    mode: 'fixtures',
    source_directory: options.fixturesRoot,
    locations: fixture.locations.length,
    hospitals: fixture.hospitals.length,
    providers: fixture.providers.length,
    plans: fixture.plans.length,
    networkRecords: fixture.networkRecords.length,
    referralRequirements: fixture.referralRequirements.length,
    documents: fixture.documents.length,
    slots,
    evidence: fixture.evidence.length,
    coverageSnapshots: fixture.coverageSnapshots.length,
  };
}
