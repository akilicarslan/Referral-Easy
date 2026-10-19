import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { FAULT_TYPES, type FaultType } from '../contracts/enums.js';
import { badRequest } from '../http/errors.js';
import { loadScenarios } from '../faults/scenarios.js';

/**
 * Service metadata, the served OpenAPI document, and the fault control plane.
 *
 * None of these paths can be faulted (see `NEVER_FAULTED` in the engine): being
 * able to disarm a scenario and read the resulting state is what makes the
 * faults usable rather than merely present.
 */
export function registerMeta(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'get',
    path: '/openapi.json',
    summary: 'The OpenAPI 3.1 document generated from the Zod contracts',
    tags: ['meta'],
  });

  app.get('/openapi.json', async () => context.docs.document(context.config));

  context.docs.describe({ method: 'get', path: '/health', summary: 'Liveness', tags: ['meta'] });

  /* `/healthz` is the same answer: the compose topology probes the mock API at
     /healthz (docs/adr/ADR-0023-compose-topology.md), and a probe that 404s
     reports a healthy container as unhealthy. */
  context.docs.describe({ method: 'get', path: '/healthz', summary: 'Liveness (compose probe spelling)', tags: ['meta'] });

  const health = async (): Promise<Record<string, unknown>> => {
    const load = loadScenarios(context.config.faultScenarioDir);
    return {
      status: 'ok',
      service: 'mock-api',
      version: '0.1.0',
      offline: true,
      llm_calls: 0,
      fault_seed: context.config.faultSeed,
      fault_scenario_source: load.source,
      fault_scenario_directory: load.directory,
      fixture_scenarios: load.fixtureIds,
      fault_scenarios_loaded: load.scenarios.length,
      fault_variants_loaded: load.variants.length,
      fixture_load_errors: load.errors,
      scenario_manifest: load.manifest,
      shadowed_builtin_ids: load.shadowed,
      database: context.config.dbPath,
      seed: context.seed(),
      up: true,
    };
  };

  app.get('/health', health);
  app.get('/healthz', health);

  context.docs.describe({
    method: 'get',
    path: '/',
    summary: 'Endpoint index',
    tags: ['meta'],
  });

  app.get('/', async () => ({
    service: 'ReferralEasy mock hospital network API',
    version: '0.1.0',
    openapi: '/openapi.json',
    modules: {
      locations: ['GET /locations', 'GET /locations/:location_id'],
      hospitals: ['GET /hospitals', 'GET /hospitals/:hospital_id'],
      providers: ['GET /providers', 'GET /providers/:provider_id'],
      coverage: ['POST /coverage/lookup', 'GET /plans', 'GET /network-records'],
      'referral-requirements': ['GET /referral-requirements'],
      documents: ['GET /documents', 'GET /documents/:document_id', 'POST /documents/check'],
      availability: ['POST /availability'],
      appointments: [
        'POST /appointments',
        'GET /appointments',
        'GET /appointments/:booking_request_id',
        'GET /appointments/lookup_request',
        'POST /appointments/:booking_request_id/cancel',
        'POST /appointments/:booking_request_id/reschedule',
      ],
      notifications: [
        'GET /notifications/outbox',
        'GET /notifications/outbox/:outbox_id',
        'GET /notifications/outbox/:outbox_id/preview',
        'POST /notifications/outbox/:outbox_id/retry',
        'POST /notifications/drain',
      ],
    },
    fault_control: [
      'GET /_faults/scenarios',
      'GET /_faults/state',
      'POST /_faults/arm',
      'POST /_faults/reset',
      'GET /_diagnostics/vendor-calls',
      'GET /_diagnostics/attempts',
    ],
    notes: [
      'All data is synthetic. No provider credentials exist and no model is ever called.',
      'The response `state` field is the only authority on whether a booking exists.',
    ],
  }));

  /* ------------------------------------------------------ fault control plane */

  context.docs.describe({
    method: 'get',
    path: '/_faults/scenarios',
    summary: 'List the loaded fault scenarios',
    tags: ['faults'],
  });

  app.get('/_faults/scenarios', async () => {
    const load = loadScenarios(context.config.faultScenarioDir);
    return {
      source: load.source,
      directory: load.directory,
      fixture_ids: load.fixtureIds,
      errors: load.errors,
      manifest: load.manifest,
      journeys: load.journeys,
      variants: load.variants,
      shadowed_builtin_ids: load.shadowed,
      scenarios: context.engine.scenarios.map((scenario) => ({
        scenario_id: scenario.scenario_id,
        description: scenario.description ?? null,
        fault_seed: scenario.fault_seed ?? context.config.faultSeed,
        default_fault: scenario.default_fault ?? null,
        journey: scenario.journey ?? null,
        injection: scenario.injection ?? null,
        source_file: scenario.source_file ?? null,
        variant_ids: scenario.variant_ids ?? [],
        steps: scenario.steps ?? [],
      })),
      fault_types: FAULT_TYPES,
    };
  });

  context.docs.describe({
    method: 'get',
    path: '/_faults/state',
    summary: 'Current fault state, seed and recorded events',
    tags: ['faults'],
  });

  app.get('/_faults/state', async () => context.engine.describeState());

  context.docs.describe({
    method: 'post',
    path: '/_faults/arm',
    summary: 'Arm one scenario, a random mode, or disarm entirely',
    tags: ['faults'],
  });

  app.post<{ Body: { scenario_id?: string | null; mode?: string } }>('/_faults/arm', async (request) => {
    const body = (request.body ?? {}) as { scenario_id?: string | null; mode?: string };
    const mode = body.mode === 'random' || body.mode === 'off' || body.mode === 'scenario' ? body.mode : 'scenario';
    // A scenario is armed by its id, and a fixture id need not be a fault name:
    // `three_transient_errors` is a legitimate scenario that faults with a 503.
    const known = new Set<string>([...FAULT_TYPES, ...context.engine.scenarios.map((scenario) => scenario.scenario_id)]);
    if (body.scenario_id && !known.has(body.scenario_id)) {
      throw badRequest('unknown_scenario', `Unknown scenario ${body.scenario_id}`, { known: [...known].sort() });
    }
    context.engine.arm(body.scenario_id ?? null, mode);
    return { armed: context.engine.armed, seed: context.engine.seed };
  });

  context.docs.describe({
    method: 'post',
    path: '/_faults/reset',
    summary: 'Clear occurrence counters and recorded fault events',
    tags: ['faults'],
  });

  app.post('/_faults/reset', async () => {
    context.engine.reset();
    return { reset: true, armed: context.engine.armed, seed: context.engine.seed };
  });

  /* ------------------------------------------------------------- diagnostics */

  context.docs.describe({
    method: 'get',
    path: '/_diagnostics/attempts',
    summary: 'Every recorded booking attempt for one idempotency key',
    tags: ['diagnostics'],
    query: [{ name: 'idempotency_key' }],
  });

  app.get<{ Querystring: { idempotency_key?: string } }>('/_diagnostics/attempts', async (request) => {
    const key = request.query.idempotency_key;
    if (!key) return { booking: null, attempts: [], note: 'idempotency_key is required' };
    const booking = context.store.findBookingByKey(key);
    if (!booking) return { booking: null, attempts: [], note: 'no booking with that key' };
    return {
      booking,
      attempts: context.store.listAttempts(booking.booking_request_id),
      appointments: context.store.getAppointmentByBooking(booking.booking_request_id) ?? null,
      appointments_for_slot: booking.payload.slot_id ? context.store.countAppointmentsForSlot(booking.payload.slot_id) : 0,
      outbox: context.store.listOutbox({ bookingRequestId: booking.booking_request_id }),
      vendor_calls: context.store.listVendorCalls({ idempotencyKey: key }),
    };
  });

  context.docs.describe({
    method: 'get',
    path: '/_diagnostics/vendor-calls',
    summary: 'Raw vendor payloads kept as adapter-normalisation evidence',
    description: 'This is the proof that the two hospitals really do speak different dialects, and that neither leaks past normalisation.',
    tags: ['diagnostics'],
    query: [{ name: 'idempotency_key' }, { name: 'operation' }],
  });

  app.get<{ Querystring: { idempotency_key?: string; operation?: string } }>(
    '/_diagnostics/vendor-calls',
    async (request) => {
      const rows = context.store.listVendorCalls({
        ...(request.query.idempotency_key ? { idempotencyKey: request.query.idempotency_key } : {}),
        ...(request.query.operation ? { operation: request.query.operation } : {}),
      });
      return {
        calls: rows.map((row) => ({
          vendor_call_id: row.vendor_call_id,
          hospital_id: row.hospital_id,
          adapter_type: row.adapter_type,
          operation: row.operation,
          idempotency_key: row.idempotency_key,
          http_status: row.http_status,
          fault_type: row.fault_type,
          request: row.request ? JSON.parse(String(row.request)) : null,
          response: row.response ? JSON.parse(String(row.response)) : null,
          created_at: row.created_at,
        })),
        count: rows.length,
      };
    },
  );
}
