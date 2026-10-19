import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { notFound } from '../http/errors.js';
import { parseOrThrow } from '../http/validate.js';
import { availabilityQuery } from '../contracts/zod.js';
import type { AvailabilityQuery, AvailabilityResult } from '../contracts/types.js';
import { deterministicUuid, payloadHash } from '../domain/ids.js';

/**
 * Availability.
 *
 * The four result kinds exist because "we could not ask" and "we asked and
 * there is nothing" are different answers, and collapsing them into an empty
 * list would let a fault be reported to a patient as a fact about the world.
 *
 *  - slots_found              : real slots, listed
 *  - no_slots_for_constraints : we asked; the constraints matched nothing
 *  - no_matching_provider     : we asked; this provider does not work here
 *  - upstream_error           : we could NOT ask. Nothing was learned.
 */
/**
 * The query-parameter spelling of the same question.
 *
 * The frozen `AvailabilityQuery` is what is parsed either way; this only
 * reshapes `?preferred_days=monday,tuesday` into the array the schema expects,
 * so the two routes cannot drift apart.
 */
export function availabilityQueryFromParams(params: Record<string, unknown>): AvailabilityQuery {
  const days = params.preferred_days ?? params['preferred_days[]'];
  const raw: Record<string, unknown> = {
    hospital_id: params.hospital_id,
    provider_id: params.provider_id,
    visit_type: params.visit_type,
    earliest: params.earliest ?? null,
    latest: params.latest ?? null,
    ...(Array.isArray(days)
      ? { preferred_days: days }
      : typeof days === 'string' && days.trim() !== ''
        ? { preferred_days: days.split(',').map((day) => day.trim()).filter((day) => day !== '') }
        : {}),
    ...(params.preferred_time_of_day ? { preferred_time_of_day: params.preferred_time_of_day } : {}),
  };
  return parseOrThrow(availabilityQuery, raw, 'Availability query');
}

export function registerAvailability(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'post',
    path: '/availability',
    summary: 'Find available slots for a provider',
    description:
      'Consumers MUST branch on `result_kind`, never on `slots.length`. A failed upstream call returns `result_kind: "upstream_error"` with an empty slot list, which is not the same answer as `no_slots_for_constraints`.',
    tags: ['availability'],
    requestSchema: 'AvailabilityQuery',
    responseSchema: 'AvailabilityResult',
  });

  /* The same route, addressable with query parameters. The scenario fixtures
     target `GET /availability`, and a fault that cannot be triggered is not a
     fault — so both spellings run the identical code path below. */
  context.docs.describe({
    method: 'get',
    path: '/availability',
    summary: 'Find available slots for a provider (query-parameter form)',
    description: 'Identical behaviour to `POST /availability`; the frozen AvailabilityQuery is parsed in both cases.',
    tags: ['availability'],
    query: [
      { name: 'hospital_id' },
      { name: 'provider_id' },
      { name: 'visit_type' },
      { name: 'earliest' },
      { name: 'latest' },
      { name: 'preferred_days' },
      { name: 'preferred_time_of_day' },
    ],
    responseSchema: 'AvailabilityResult',
  });

  app.get<{ Querystring: Record<string, unknown> }>('/availability', async (request) => {
    return answerAvailability(context, availabilityQueryFromParams(request.query ?? {}), request.faultDecision?.fault.type ?? null);
  });

  app.post('/availability', async (request) => {
    const query = parseOrThrow(availabilityQuery, request.body ?? {}, 'Availability query');
    return answerAvailability(context, query, request.faultDecision?.fault.type ?? null);
  });
}

/** The one implementation both spellings of the route call. */
function answerAvailability(context: AppContext, query: AvailabilityQuery, fault: string | null): AvailabilityResult {
  const hospitalRow = context.store.getHospital(query.hospital_id);
  if (!hospitalRow) throw notFound('unknown_hospital', `No hospital ${query.hospital_id}`);
  const providerRow = context.store.getProvider(query.provider_id);
  if (!providerRow) throw notFound('unknown_provider', `No provider ${query.provider_id}`);

  const hospital = JSON.parse(String(hospitalRow.payload)) as { timezone: string };
  const provider = JSON.parse(String(providerRow.payload)) as { hospital_ids: string[] };
  const evidenceId = deterministicUuid(`evidence:availability:${payloadHash(query as unknown as Record<string, unknown>)}`);

  const base = {
    query,
    evidence_id: evidenceId,
    synthetic: true as const,
  };

  if (!provider.hospital_ids.includes(query.hospital_id)) {
    return { ...base, slots: [], result_kind: 'no_matching_provider' };
  }

  if (fault === 'malformed_schema') {
    // The directory feed answered, but with a payload that does not match its
    // own schema. We learned nothing from it, and say exactly that — the page
    // is marked partial so a caller cannot mistake it for a complete answer.
    return { ...base, slots: [], result_kind: 'upstream_error', partial: true };
  }

  if (fault === 'empty_valid_result') {
    return { ...base, slots: [], result_kind: 'no_slots_for_constraints' };
  }

  // Two different empties. "This provider does not offer this visit type here"
  // is a fact about the provider; "this provider offers it, but not inside your
  // window" is a fact about the caller's constraints. Collapsing them would
  // report a scheduling gap as a missing specialist.
  const offersVisitType = context.store.listSlots({
    hospitalId: query.hospital_id,
    providerId: query.provider_id,
    visitType: query.visit_type,
  });
  if (offersVisitType.length === 0 && fault !== 'empty_valid_result') {
    return { ...base, slots: [], result_kind: 'no_matching_provider' };
  }

  const slots = context.store.listSlots({
    hospitalId: query.hospital_id,
    providerId: query.provider_id,
    visitType: query.visit_type,
    earliest: query.earliest ?? null,
    latest: query.latest ?? null,
  });

  /* `preferred_days` and `preferred_time_of_day` are PREFERENCES, and this
     answers with the facts instead of quietly dropping whatever does not match
     one. A preference is not a constraint: filtering on
     `preferred_days: ['friday']` is how "no Friday session exists" turns into a
     claim about the network when it is only a claim about the filter. The
     window, the visit type and the provider's link to the hospital are the hard
     facts; the slots come back in chronological order; and the preferences
     travel back inside `query`, where the layer that ranks — and can explain
     itself — still has them. */
  return {
    ...base,
    slots,
    result_kind: slots.length > 0 ? 'slots_found' : 'no_slots_for_constraints',
  };
}
