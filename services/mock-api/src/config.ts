import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const SERVICE_ROOT = resolve(HERE, '..');
const REPO_ROOT = resolve(SERVICE_ROOT, '..', '..');

export interface MockApiConfig {
  host: string;
  port: number;
  /** Absolute path of the SQLite file, or ':memory:' for an ephemeral store. */
  dbPath: string;
  /** Root seed for the fault engine. Every fault decision is a pure function of it. */
  faultSeed: number;
  /** Directory holding `*.json` fault scenarios produced by the fixtures agent. */
  faultScenarioDir: string;
  /** Directory holding the shared demo fixtures (directory, coverage, documents, evidence). */
  fixturesRoot: string;
  /**
   * Which dataset to serve. `auto` serves the shared fixtures when they are
   * present and the built-in dataset otherwise, so the service is coherent with
   * the rest of the demo without becoming unusable when the fixtures are absent.
   */
  seedMode: 'auto' | 'fixtures' | 'builtin';
  faultsEnabled: boolean;
  /** Attempts before an outbox entry reaches the terminal `abandoned` state. */
  notificationMaxAttempts: number;
  /** Backoff between delivery attempts, in minutes. 0 in tests. */
  notificationBackoffMinutes: number;
  /** Advertised base URL, used to render the OpenAPI `servers` entry. */
  publicUrl: string;
  /** Distance from the service root; used for documentation and the OpenAPI info block. */
  serviceRoot: string;
}

function intFromEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === '') return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function seedModeFromEnv(): 'auto' | 'fixtures' | 'builtin' {
  const raw = (process.env.MOCK_API_SEED ?? '').trim().toLowerCase();
  if (raw === 'fixtures' || raw === 'builtin' || raw === 'auto') return raw;
  return 'auto';
}

function boolFromEnv(name: string, fallback: boolean): boolean {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === '') return fallback;
  return !['0', 'false', 'no', 'off'].includes(raw.trim().toLowerCase());
}

export function loadConfig(overrides: Partial<MockApiConfig> = {}): MockApiConfig {
  const port = intFromEnv('PORT', 4010);
  // The fault scenarios live inside the fixture set, so one variable moves the
  // whole dataset: a container that mounts the fixtures elsewhere would
  // otherwise serve the directory it was told about and fault from the
  // repository root it was built with.
  const fixturesRoot = process.env.MOCK_API_FIXTURES_DIR
    ? resolve(process.env.MOCK_API_FIXTURES_DIR)
    : resolve(REPO_ROOT, 'fixtures');
  const config: MockApiConfig = {
    host: process.env.HOST ?? '0.0.0.0',
    port,
    dbPath: process.env.MOCK_API_DB ?? resolve(SERVICE_ROOT, '.data', 'mock-api.sqlite'),
    faultSeed: intFromEnv('MOCK_FAULT_SEED', 42),
    faultScenarioDir: process.env.MOCK_FAULT_SCENARIOS_DIR ?? resolve(fixturesRoot, 'fault-scenarios'),
    fixturesRoot,
    seedMode: seedModeFromEnv(),
    faultsEnabled: boolFromEnv('MOCK_API_FAULTS_ENABLED', true),
    notificationMaxAttempts: intFromEnv('MOCK_API_NOTIFICATION_MAX_ATTEMPTS', 3),
    notificationBackoffMinutes: intFromEnv('MOCK_API_NOTIFICATION_BACKOFF_MINUTES', 5),
    publicUrl: process.env.MOCK_API_URL ?? `http://localhost:${port}`,
    serviceRoot: SERVICE_ROOT,
  };
  return { ...config, ...overrides };
}
