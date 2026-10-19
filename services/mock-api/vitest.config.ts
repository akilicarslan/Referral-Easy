import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['test/**/*.test.ts'],
    environment: 'node',
    // Fault-injection and lifecycle tests own real SQLite files and simulated
    // upstream latency; running them in one worker keeps the seeded request
    // sequences deterministic.
    pool: 'forks',
    poolOptions: { forks: { singleFork: true } },
    testTimeout: 20_000,
    hookTimeout: 20_000,
  },
});
