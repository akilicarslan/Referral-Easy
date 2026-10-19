import type { MockApiConfig } from './config.js';
import type { Database } from './db/database.js';
import type { Store } from './db/store.js';
import type { AdapterRegistry } from './adapters/registry.js';
import type { BookingService } from './booking/service.js';
import type { FaultEngine } from './faults/engine.js';
import type { OutboxWorker } from './notifications/worker.js';
import type { OpenApiRegistry } from './contracts/openapi.js';
import type { FaultDecision } from './faults/types.js';
import type { SeedSummary } from './domain/seed.js';

/** Everything a route module is allowed to touch. */
export interface AppContext {
  config: MockApiConfig;
  db: Database;
  store: Store;
  engine: FaultEngine;
  adapters: AdapterRegistry;
  booking: BookingService;
  outbox: OutboxWorker;
  docs: OpenApiRegistry;
  clock: () => Date;
  /** Which dataset is being served, and how many rows it holds. */
  seed: () => SeedSummary | null;
}

declare module 'fastify' {
  interface FastifyRequest {
    /** Set by the fault plugin before any route runs. */
    faultDecision: FaultDecision | null;
  }
}
