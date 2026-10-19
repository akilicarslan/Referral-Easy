import type { MockApiConfig } from '../config.js';
import type { Store } from '../db/store.js';
import { fixturesAvailable, seedFromFixtures } from './fixture-seed.js';
import { seedDirectory, type SeedSummary } from './seed.js';

/**
 * Chooses the dataset the API serves.
 *
 * `auto` — the default — serves the shared demo fixtures whenever they are
 * present, because the scenario manifest and the fault files reference those
 * concrete ids, and falls back to the built-in dataset when they are not (a
 * container built without the fixtures, a unit test with an isolated store).
 * The choice is explicit in `MOCK_API_SEED` and reported by `/health`, so a run
 * never has to be guessed at.
 */
export function seedStore(config: MockApiConfig, store: Store, now: Date = new Date()): SeedSummary {
  if (config.seedMode === 'builtin') return seedDirectory(store, now);
  if (config.seedMode === 'fixtures') return seedFromFixtures(store, { fixturesRoot: config.fixturesRoot });
  if (fixturesAvailable(config.fixturesRoot)) return seedFromFixtures(store, { fixturesRoot: config.fixturesRoot });
  return seedDirectory(store, now);
}
