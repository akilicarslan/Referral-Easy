import Fastify, { type FastifyInstance } from 'fastify';
import { ZodError } from 'zod';
import { loadConfig, type MockApiConfig } from './config.js';
import { Database } from './db/database.js';
import { Store } from './db/store.js';
import { seedStore } from './domain/seeding.js';
import type { SeedSummary } from './domain/seed.js';
import { AdapterRegistry } from './adapters/registry.js';
import { BookingService } from './booking/service.js';
import { FaultEngine } from './faults/engine.js';
import { registerFaultPlugin } from './faults/plugin.js';
import { OutboxWorker } from './notifications/worker.js';
import { OpenApiRegistry } from './contracts/openapi.js';
import { AppError } from './http/errors.js';
import { UpstreamError } from './adapters/types.js';
import type { AppContext } from './context.js';
import { registerLocations } from './modules/locations.js';
import { registerHospitals } from './modules/hospitals.js';
import { registerProviders } from './modules/providers.js';
import { registerCoverage } from './modules/coverage.js';
import { registerReferralRequirements } from './modules/referral-requirements.js';
import { registerDocuments } from './modules/documents.js';
import { registerAvailability } from './modules/availability.js';
import { registerDirectorySearch } from './modules/directory-search.js';
import { registerEvidence } from './modules/evidence.js';
import { registerAppointments } from './modules/appointments.js';
import { registerNotifications } from './modules/notifications.js';
import { registerMeta } from './modules/meta.js';

export interface BuildOptions {
  config?: Partial<MockApiConfig>;
  logger?: boolean;
  seed?: boolean;
}

export interface BuiltApp {
  app: FastifyInstance;
  context: AppContext;
  seed: SeedSummary | null;
  close: () => Promise<void>;
}

/**
 * Builds the whole service in-process.
 *
 * Tests use this directly with an ephemeral SQLite file, which is why every
 * invariant can be asserted without a network or a container.
 */
export async function buildApp(options: BuildOptions = {}): Promise<BuiltApp> {
  const config = loadConfig(options.config ?? {});
  const db = new Database(config.dbPath);
  const store = new Store(db);
  const seed = options.seed === false ? null : seedStore(config, store, new Date());

  const engine = new FaultEngine({ config, store });
  const adapters = new AdapterRegistry(store);
  const outbox = new OutboxWorker(config, store, engine);
  const booking = new BookingService(config, store, adapters, engine, outbox);
  const docs = new OpenApiRegistry();
  const context: AppContext = {
    config,
    db,
    store,
    engine,
    adapters,
    booking,
    outbox,
    docs,
    clock: () => new Date(),
    seed: () => seed,
  };

  const app = Fastify({
    logger: options.logger === false ? false : { level: process.env.LOG_LEVEL ?? 'info' },
  });

  registerFaultPlugin(app, context);
  registerMeta(app, context);
  registerLocations(app, context);
  registerHospitals(app, context);
  registerProviders(app, context);
  registerCoverage(app, context);
  registerReferralRequirements(app, context);
  registerDocuments(app, context);
  registerAvailability(app, context);
  registerDirectorySearch(app, context);
  registerEvidence(app, context);
  registerAppointments(app, context);
  registerNotifications(app, context);

  app.setNotFoundHandler(async (request, reply) => {
    return reply.code(404).send({
      error: {
        code: 'not_found',
        message: `No route for ${request.method} ${request.url.split('?')[0]}`,
        request_id: String(request.id),
        fault_type: null,
        scenario_id: null,
        retry_after_seconds: null,
      },
    });
  });

  app.setErrorHandler(async (error, request, reply) => {
    const requestId = String(request.id);
    const faultType = request.faultDecision?.fault.type ?? null;
    const scenarioId = request.faultDecision?.scenario_id ?? null;

    if (error instanceof AppError) {
      reply.header('x-mock-fault', error.faultType ?? '');
      return reply.code(error.status).send({
        error: {
          code: error.code,
          message: error.message,
          request_id: requestId,
          fault_type: error.faultType ?? faultType,
          scenario_id: error.scenarioId ?? scenarioId,
          retry_after_seconds: error.retryAfterSeconds,
          detail: error.detail,
        },
      });
    }

    if (error instanceof UpstreamError) {
      return reply.code(error.httpStatus >= 400 ? error.httpStatus : 502).send({
        error: {
          code: error.code,
          message: error.message,
          request_id: requestId,
          fault_type: error.faultType ?? faultType,
          scenario_id: scenarioId,
          retry_after_seconds: error.retryAfterSeconds,
        },
      });
    }

    if (error instanceof ZodError) {
      // A response that does not satisfy the published contract is a bug, and
      // it fails loudly rather than shipping a body the document does not describe.
      return reply.code(500).send({
        error: {
          code: 'contract_violation',
          message: 'The generated response did not match its own published schema',
          request_id: requestId,
          fault_type: faultType,
          scenario_id: scenarioId,
          retry_after_seconds: null,
          detail: error.issues.map((issue) => ({ path: issue.path.join('.'), message: issue.message })),
        },
      });
    }

    const failure = error as Error & { statusCode?: number };
    const status = failure.statusCode ?? 500;
    if (status >= 500) request.log.error({ err: error }, 'unhandled error');
    return reply.code(status).send({
      error: {
        code: status === 400 ? 'invalid_request' : 'internal_error',
        message: status >= 500 ? 'The mock API failed to handle this request' : failure.message,
        request_id: requestId,
        fault_type: faultType,
        scenario_id: scenarioId,
        retry_after_seconds: null,
      },
    });
  });

  await app.ready();

  return {
    app,
    context,
    seed,
    close: async () => {
      await app.close();
      db.close();
    },
  };
}
