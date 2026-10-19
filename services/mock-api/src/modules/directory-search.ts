import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { deterministicUuid } from '../domain/ids.js';
import { payloadHash } from '../domain/ids.js';
import { badRequest } from '../http/errors.js';
import type { FaultSpec } from '../faults/types.js';

/**
 * Directory search.
 *
 * This is the *directory* answering "which providers match these filters", and
 * nothing more. It deliberately does not rank, score or explain: the
 * constraint-by-constraint fit (`constraint_fit`, `reason_code`, `rank`) is the
 * constraint analyst's output, computed against one patient's constraint set,
 * and inventing it here would be this service pretending to be that layer.
 *
 * The route exists because the fault scenarios target it: `malformed_schema`
 * names `GET /directory/search` and asserts that a syntactically valid body
 * which fails its own contract must end in a terminal error rather than in a
 * silently empty candidate list.
 */

export interface DirectorySearchQuery {
  specialty?: string;
  hospital_id?: string;
  plan_id?: string;
  language?: string;
  accepts_new_patients?: boolean;
  limit?: number;
}

interface ProviderRecord {
  provider_id: string;
  display_name: string;
  credentials?: string;
  specialty: string;
  subspecialties?: string[];
  languages?: Array<{ language: string; fluency: string }>;
  accepts_new_patients?: boolean;
  accessibility_features?: string[];
  hospital_ids?: string[];
  accepts_insurance?: string[];
  first_visit_duration_minutes?: number;
  return_visit_duration_minutes?: number;
  profile_text?: string;
  contact_phone?: string;
  knowledge_version?: string;
}

const DEFAULT_LIMIT = 20;
const MAX_LIMIT = 50;

export function registerDirectorySearch(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'get',
    path: '/directory/search',
    summary: 'Filter the provider directory',
    description:
      'Unranked candidate list. `result_kind` distinguishes a genuine empty result (`no_matching_provider`) from a failed upstream (`upstream_error`); an empty list is never returned for a failure.',
    tags: ['directory'],
    query: [
      { name: 'specialty' },
      { name: 'hospital_id' },
      { name: 'plan_id' },
      { name: 'language' },
      { name: 'accepts_new_patients' },
      { name: 'limit' },
    ],
  });

  app.get<{ Querystring: Record<string, unknown> }>('/directory/search', async (request, reply) => {
    const query = searchQueryFromParams(request.query ?? {});
    const decision = request.faultDecision;
    if (decision) {
      // A fault this deep in the stack still has to be visible on the wire: a
      // caller debugging "why is this body wrong" gets the answer in the
      // headers rather than in the log of another process.
      reply.header('x-mock-fault', decision.fault.type);
      if (decision.scenario_id) reply.header('x-mock-fault-scenario', decision.scenario_id);
      reply.header('x-mock-fault-seed', String(decision.seed));
    }
    return searchDirectory(context, query, decision?.fault ?? null);
  });
}

export function searchQueryFromParams(params: Record<string, unknown>): DirectorySearchQuery {
  const first = (value: unknown): string | undefined => {
    if (typeof value === 'string' && value.trim() !== '') return value.trim();
    if (Array.isArray(value) && typeof value[0] === 'string' && value[0].trim() !== '') return String(value[0]).trim();
    return undefined;
  };
  const query: DirectorySearchQuery = {};
  const specialty = first(params.specialty);
  if (specialty) query.specialty = specialty;
  const hospitalId = first(params.hospital_id);
  if (hospitalId) query.hospital_id = hospitalId;
  const planId = first(params.plan_id);
  if (planId) query.plan_id = planId;
  const language = first(params.language);
  if (language) query.language = language;
  const acceptsNew = first(params.accepts_new_patients);
  if (acceptsNew !== undefined) query.accepts_new_patients = ['1', 'true', 'yes'].includes(acceptsNew.toLowerCase());
  const limit = first(params.limit);
  if (limit !== undefined) {
    const parsed = Number.parseInt(limit, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) throw badRequest('invalid_request', '`limit` must be a positive integer');
    query.limit = Math.min(parsed, MAX_LIMIT);
  }
  return query;
}

/** The one implementation the route calls. Exported for the fixture tests. */
export function searchDirectory(
  context: AppContext,
  query: DirectorySearchQuery,
  fault: FaultSpec | null,
): Record<string, unknown> {
  const evidenceId = deterministicUuid(`evidence:directory:${payloadHash(query as Record<string, unknown>)}`);

  if (fault?.type === 'malformed_schema') {
    // The feed answered 200 with a body that does not satisfy its own contract.
    // We learned nothing from it, and the response says so instead of pretending
    // that "nothing matched".
    const body: Record<string, unknown> = {
      query,
      result_kind: 'upstream_error',
      candidates: [],
      evidence_id: null,
      count: 0,
      synthetic: true,
    };
    return applyMalformedHints(body, fault);
  }

  const planRow = query.plan_id ? context.store.getPlan(query.plan_id) : undefined;
  if (query.plan_id && !planRow) {
    throw badRequest('unknown_plan', `No plan ${query.plan_id}`, { plan_id: query.plan_id });
  }
  const payerName = planRow ? (JSON.parse(String(planRow.payload)) as { payer_name?: string }).payer_name : undefined;

  const hospitalIds = query.hospital_id
    ? context.store.listProvidersForHospital(query.hospital_id).map((row) => String((JSON.parse(String(row.payload)) as ProviderRecord).provider_id))
    : null;

  const candidates = (context.store.listProviders().map((row) => JSON.parse(String(row.payload)) as ProviderRecord))
    .filter((provider) => {
      if (query.specialty && provider.specialty.toLowerCase() !== query.specialty.toLowerCase()) return false;
      if (hospitalIds && !hospitalIds.includes(provider.provider_id)) return false;
      if (query.accepts_new_patients !== undefined && Boolean(provider.accepts_new_patients) !== query.accepts_new_patients) return false;
      if (query.language && !(provider.languages ?? []).some((entry) => entry.language === query.language)) return false;
      if (payerName && !(provider.accepts_insurance ?? []).includes(payerName)) return false;
      return true;
    })
    .map((provider) => ({
      provider_id: provider.provider_id,
      display_name: provider.display_name,
      credentials: provider.credentials ?? null,
      specialty: provider.specialty,
      subspecialties: provider.subspecialties ?? [],
      languages: provider.languages ?? [],
      accepts_new_patients: provider.accepts_new_patients ?? null,
      accessibility_features: provider.accessibility_features ?? [],
      hospital_ids: provider.hospital_ids ?? [],
      contact_phone: provider.contact_phone ?? null,
      profile_text: provider.profile_text ?? null,
      knowledge_version: provider.knowledge_version ?? null,
    }))
    .slice(0, query.limit ?? DEFAULT_LIMIT);

  return {
    query,
    result_kind: candidates.length > 0 ? 'candidates_found' : 'no_matching_provider',
    candidates,
    evidence_id: evidenceId,
    count: candidates.length,
    synthetic: true,
  };
}

/**
 * Applies the fixture-authored malformed shape.
 *
 * The fixture says which fields are missing and which enum value is off, so the
 * broken body is the authored one rather than a generic failure.
 */
function applyMalformedHints(body: Record<string, unknown>, fault: FaultSpec): Record<string, unknown> {
  const broken = { ...body };
  const missing = Array.isArray(fault.missing_fields) ? (fault.missing_fields as unknown[]) : [];
  for (const field of missing) {
    if (typeof field === 'string') delete broken[field];
  }
  const enumField = typeof fault.invalid_enum_field === 'string' ? fault.invalid_enum_field : null;
  if (enumField) broken[enumField] = typeof fault.invalid_enum_value === 'string' ? fault.invalid_enum_value : 'invalid';
  return { ...broken, contract_violation: { missing_fields: missing, invalid_enum_field: enumField } };
}
