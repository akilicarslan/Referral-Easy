import { setTimeout as delay } from 'node:timers/promises';
import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { EDGE_FAULTS, type FaultSpec } from './types.js';

/**
 * The fixture-authored hints that this service does not act on.
 *
 * They are not errors and not noise: a fixture that says the directory should
 * `degrade_to: "static_seed"` is telling the caller what to fall back to, and a
 * reviewer reproducing a run needs to see it. So it travels in the envelope.
 */
export function fixtureHints(spec: FaultSpec): Record<string, unknown> {
  const { type, delay_ms, retry_after_seconds, retry_after_s, retry_after_header, detail, ...hints } = spec;
  void type;
  void delay_ms;
  void retry_after_seconds;
  void retry_after_s;
  void retry_after_header;
  void detail;
  return hints;
}

function retryAfterFor(spec: FaultSpec, fallback: number): number {
  return spec.retry_after_seconds ?? (spec.retry_after_header ? Number.parseInt(spec.retry_after_header, 10) || fallback : fallback);
}

/**
 * Applies the edge faults before any route logic runs.
 *
 * Only `latency`, `rate_limit_429` and `service_unavailable_503` live here:
 * they describe a network that is slow, throttled or down, and the caller must
 * see a transport failure rather than a well-formed body. The remaining faults
 * are produced deeper in the stack on purpose — see `EDGE_FAULTS` and
 * `PIPELINE_FAULTS` in `types.ts`.
 */
export function registerFaultPlugin(app: FastifyInstance, context: AppContext): void {
  app.decorateRequest('faultDecision', null);

  app.addHook('onRequest', async (request, reply) => {
    const decision = context.engine.beginRequest({
      method: request.method,
      path: request.url.split('?')[0] ?? request.url,
      headers: request.headers as Record<string, string | string[] | undefined>,
      query: (request.query ?? {}) as Record<string, unknown>,
    });
    request.faultDecision = decision;
    if (!decision) return;
    if (!EDGE_FAULTS.includes(decision.fault.type)) return;

    reply.header('x-mock-fault', decision.fault.type);
    if (decision.scenario_id) reply.header('x-mock-fault-scenario', decision.scenario_id);
    // The seed that produced the decision travels with it, so a reproducer does
    // not have to guess which run they are looking at.
    reply.header('x-mock-fault-seed', String(decision.seed));

    const delayMs = decision.fault.delay_ms ?? 0;
    if (delayMs > 0) {
      await delay(Math.min(delayMs, 120_000));
    }

    if (decision.fault.type === 'latency') return;

    const hints = fixtureHints(decision.fault);

    if (decision.fault.type === 'rate_limit_429') {
      const retryAfter = retryAfterFor(decision.fault, 5);
      reply.header('retry-after', String(retryAfter));
      await reply.code(429).send({
        error: {
          code: 'upstream_rate_limited',
          message: 'The hospital scheduling system is rate limiting this caller',
          request_id: String(request.id),
          fault_type: decision.fault.type,
          scenario_id: decision.scenario_id,
          retry_after_seconds: retryAfter,
          detail: { attempt_result: 'rate_limited', ...(Object.keys(hints).length ? { fault_params: hints } : {}) },
        },
      });
      return reply;
    }

    if (decision.fault.type === 'service_unavailable_503') {
      const retryAfter = retryAfterFor(decision.fault, 0) || null;
      if (retryAfter !== null) reply.header('retry-after', String(retryAfter));
      await reply.code(503).send({
        error: {
          code: 'upstream_unavailable',
          message: 'The hospital scheduling system is unavailable',
          request_id: String(request.id),
          fault_type: decision.fault.type,
          scenario_id: decision.scenario_id,
          retry_after_seconds: retryAfter,
          detail: { attempt_result: 'upstream_unavailable', ...(Object.keys(hints).length ? { fault_params: hints } : {}) },
        },
      });
      return reply;
    }
  });
}
