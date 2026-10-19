import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { badRequest, notFound } from '../http/errors.js';
import { parseOrThrow } from '../http/validate.js';
import { coverageLookupRequest } from '../contracts/zod.js';
import type { CoverageResult, InsurancePlan, Provider } from '../contracts/types.js';
import { deterministicUuid } from '../domain/ids.js';

/**
 * Coverage.
 *
 * Two separate facts are kept separate all the way out: whether the provider
 * bills this payer at all, and whether they are in network for THIS plan. A
 * contradiction between them is surfaced as a conflict, never smoothed into a
 * comfortable answer, and `null` (unknown) never becomes `false` or `true`.
 *
 * The conflict flag is not a heuristic about the two booleans. It is a reading
 * of the *recorded standing* of the sources: a source that was explicitly
 * invalidated (`invalidated_by`) or is recorded stale does not stand on its
 * own, and when such a source sits next to one that does, the answer is
 * disputed and both sides are cited. That rule reproduces the fixture set's
 * authored conflicts (`coverage_results.json`) exactly, including the two the
 * naive "accepts insurance but out of network" reading gets wrong — a clean
 * out-of-network answer is not a conflict, and a stale public description
 * against a live structured record is one.
 *
 * A lookup may name a provider, a hospital, or both. Without a provider there
 * is no plan-level record to read, so the answer is the hospital's own
 * evidence: `status` unknown, `accepts_insurance` null (nobody was named who
 * could accept it), and the hospital's directory blurb — the thing a naive
 * answer would rest on — decides the reported freshness.
 */

/* ------------------------------------------------------------------ evidence */

/** A stored row, as far as this module cares (`store` rows carry their payload). */
type EvidenceRow = Record<string, unknown>;

/** The parts of an `evidence` row this module reasons about. */
interface CitedEvidence {
  evidence_id: string;
  source: string;
  source_id: string;
  authoritative: boolean;
  invalidated_by: string[];
  state: string;
  age_seconds: number;
}

function stringList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((entry) => String(entry));
  return value === null || value === undefined ? [] : [String(value)];
}

function parseEvidence(row: EvidenceRow | undefined): CitedEvidence | null {
  if (!row) return null;
  const raw = JSON.parse(String(row.payload)) as Record<string, unknown>;
  const freshness = (raw.freshness ?? {}) as Record<string, unknown>;
  return {
    evidence_id: String(raw.evidence_id),
    source: String(raw.source ?? ''),
    source_id: String(raw.source_id ?? ''),
    authoritative: raw.authoritative === true,
    invalidated_by: stringList(raw.invalidated_by),
    state: typeof freshness.state === 'string' ? freshness.state : 'unknown',
    age_seconds: typeof freshness.age_seconds === 'number' ? freshness.age_seconds : 0,
  };
}

/**
 * Does this source still stand on its own? A stale source, or one another
 * source explicitly invalidated, does not. A *fresh* source that merely lacks
 * authority (a directory blurb describing the same provider) does — that is a
 * second vantage point, not a contradiction.
 */
function outranked(evidence: CitedEvidence): boolean {
  return evidence.state !== 'fresh' || evidence.invalidated_by.length > 0;
}

/* --------------------------------------------------------------- resolution */

interface NetworkRecordShape {
  record_id: string;
  plan_id: string;
  provider_id: string;
  hospital_id?: string | null;
  status: string;
  effective_from?: string;
  evidence_id: string;
}

export function registerCoverage(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'get',
    path: '/plans',
    summary: 'List synthetic insurance plans',
    tags: ['coverage'],
  });

  app.get('/plans', async () => {
    const plans = context.store.listPlans().map((row) => JSON.parse(String(row.payload)) as InsurancePlan);
    return { plans, count: plans.length };
  });

  context.docs.describe({
    method: 'get',
    path: '/network-records',
    summary: 'Raw plan-level network records for a provider',
    tags: ['coverage'],
    query: [{ name: 'plan_id' }, { name: 'provider_id' }, { name: 'hospital_id' }],
  });

  app.get<{ Querystring: { plan_id?: string; provider_id?: string; hospital_id?: string } }>(
    '/network-records',
    async (request) => {
      const { plan_id: planId, provider_id: providerId, hospital_id: hospitalId } = request.query;
      if (!planId || !providerId) {
        return { records: [], reason: 'plan_id and provider_id are required' };
      }
      const records = context.store
        .listNetworkRecords(planId, providerId, hospitalId ?? null)
        .map((row) => JSON.parse(String(row.payload)));
      return { records, count: records.length };
    },
  );

  context.docs.describe({
    method: 'post',
    path: '/coverage/lookup',
    summary: 'Resolve coverage for a provider and plan, or for a hospital alone',
    description:
      'Returns the frozen CoverageResult. `accepts_insurance` and `in_network_for_plan` are independent, and `null` means unknown — a caller must never treat unknown as in-network. With no provider_id the lookup answers at hospital level: status unknown, accepts_insurance null, and the conflict between the hospital\'s public description and its structured record reported as such.',
    tags: ['coverage'],
    requestSchema: 'CoverageLookupRequest',
    responseSchema: 'CoverageResult',
  });

  /* The same lookup, addressable with query parameters: the scenario fixtures
     target `GET /coverage`, and the stale-evidence fault has to be triggerable
     on the route a caller actually uses. */
  context.docs.describe({
    method: 'get',
    path: '/coverage',
    summary: 'Resolve coverage for a provider and plan (query-parameter form)',
    description: 'Identical behaviour to `POST /coverage/lookup`; the frozen CoverageLookupRequest is parsed in both cases.',
    tags: ['coverage'],
    query: [{ name: 'plan_id' }, { name: 'provider_id' }, { name: 'hospital_id' }],
    responseSchema: 'CoverageResult',
  });

  app.get<{ Querystring: { plan_id?: string; provider_id?: string; hospital_id?: string } }>('/coverage', async (request) => {
    const body = parseOrThrow(
      coverageLookupRequest,
      {
        plan_id: request.query.plan_id,
        provider_id: request.query.provider_id ?? null,
        hospital_id: request.query.hospital_id ?? null,
      },
      'Coverage lookup request',
    );
    return resolveCoverage(context, body, request.faultDecision?.fault.type ?? null);
  });

  app.post('/coverage/lookup', async (request) => {
    const body = parseOrThrow(coverageLookupRequest, request.body ?? {}, 'Coverage lookup request');
    return resolveCoverage(context, body, request.faultDecision?.fault.type ?? null);
  });
}

/** The one implementation both spellings of the lookup call. */
function resolveCoverage(
  context: AppContext,
  body: { plan_id: string; provider_id?: string | null; hospital_id?: string | null },
  fault: string | null,
): CoverageResult {
  const planRow = context.store.getPlan(body.plan_id);
  if (!planRow) throw notFound('unknown_plan', `No plan ${body.plan_id}`);
  const plan = JSON.parse(String(planRow.payload)) as InsurancePlan;

  const providerRow = body.provider_id ? context.store.getProvider(body.provider_id) : undefined;
  if (body.provider_id && !providerRow) throw notFound('unknown_provider', `No provider ${body.provider_id}`);
  const provider = providerRow ? (JSON.parse(String(providerRow.payload)) as Provider) : null;

  /* A lookup that names neither a provider nor a hospital has no subject to
     answer about — that is a caller error, not an empty result. */
  if (!provider && !body.hospital_id) {
    throw badRequest('missing_lookup_subject', 'A coverage lookup needs a provider_id or a hospital_id', {
      plan_id: body.plan_id,
    });
  }

  const records = provider
    ? context.store
        .listNetworkRecords(plan.plan_id, provider.provider_id, body.hospital_id ?? null)
        .map((row) => JSON.parse(String(row.payload)) as NetworkRecordShape)
    : [];
  /* Newest first (`ORDER BY effective_from DESC` in the store). */
  const latest = records[0];

  const recordEvidence = records
    .map((record) => parseEvidence(context.store.getEvidence(record.evidence_id)))
    .filter((entry): entry is CitedEvidence => entry !== null);

  const subjectId = provider ? `provider:${provider.provider_id}` : `hospital:${String(body.hospital_id)}`;
  const subjectEvidence = context.store
    .listEvidenceForSubject(subjectId)
    .map((row) => parseEvidence(row))
    .filter((entry): entry is CitedEvidence => entry !== null);

  const cited = citeWithInvalidations(context, [...recordEvidence, ...subjectEvidence]);

  /* The dispute is only nameable when an outranked source faces an authoritative
     one, and only rows that carry the lookup's own answer can be named: the
     evidence behind this pair's records, or the subject's own rows. An
     invalidated snapshot that happens to cite the same arbiter is still part of
     the trail (`evidence_ids`) without being part of the dispute. */
  const outrankedCited = cited.filter(outranked);
  const carrierOfAnswer = (entry: CitedEvidence): boolean =>
    subjectEvidence.some((subject) => subject.evidence_id === entry.evidence_id) ||
    recordEvidence.some((recordEvidenceEntry) => recordEvidenceEntry.evidence_id === entry.evidence_id);
  const disputedRows = outrankedCited.filter(carrierOfAnswer);
  const arbiters = unique(disputedRows.map((entry) => arbiterFor(entry, cited, latest)).filter((id): id is string => id !== null));
  const conflict = disputedRows.length > 0 && arbiters.length > 0 && cited.some((entry) => entry.authoritative);

  const conflicting_evidence_ids = conflict
    ? unique([...disputedRows.map((entry) => entry.evidence_id), ...arbiters])
    : null;

  const evidence_ids = unique([
    ...(conflicting_evidence_ids ?? []),
    ...recordEvidence.map((entry) => entry.evidence_id),
    ...subjectEvidence.map((entry) => entry.evidence_id),
    ...cited.map((entry) => entry.evidence_id),
  ]);

  const winner =
    (arbiters.length > 0 ? cited.find((entry) => entry.evidence_id === arbiters[0]) : undefined) ??
    (latest ? recordEvidence.find((entry) => entry.evidence_id === latest.evidence_id) : undefined);

  /* The freshness the patient is shown belongs to the source the answer rests
     on. With no provider there is no record, so it is the hospital's public
     description that was read — and its staleness is the point of the answer. */
  const freshnessSource = provider
    ? (latest ? recordEvidence.find((entry) => entry.evidence_id === latest.evidence_id) : undefined)
    : (subjectEvidence.find((entry) => entry.source === 'rag_directory') ?? subjectEvidence[0]);
  const staleFault = fault === 'stale_coverage_evidence';

  /* A response that reports a conflict has to name the record the answer rests
     on, whichever way the conflict arose: the armed stale-evidence fault claims
     one too, and claiming a conflict while pointing at nothing would be exactly
     the unsupported assertion this field exists to prevent. */
  const reportedConflict = conflict || staleFault;
  const winningEvidenceId = reportedConflict
    ? (winner?.evidence_id ?? arbiters[0] ?? latest?.evidence_id ?? null)
    : null;

  const status = latest ? (latest.status as CoverageResult['status']) : 'unknown';

  return {
    /* Deterministic for the same question: this is a frozen lookup, not a
       record, and a client may cache or replay it by id. */
    coverage_id: deterministicUuid(`coverage:${plan.plan_id}:${provider?.provider_id ?? 'none'}:${body.hospital_id ?? 'none'}`),
    plan_id: plan.plan_id,
    provider_id: provider?.provider_id ?? null,
    hospital_id: body.hospital_id ?? null,
    /* "Does this provider bill this payer" is a fact about the provider's own
       directory listing, and it is answered as soon as that listing is read —
       including when the plan-level record says `unknown`. An unknown *network
       status* must not swallow a known payer relationship: the fixture set's
       own network_conflict journey asserts exactly this combination
       (accepts_insurance true, in_network_for_plan null, status unknown). */
    accepts_insurance: provider ? provider.accepts_insurance.includes(plan.payer_name) : null,
    in_network_for_plan:
      latest === undefined ? null : latest.status === 'in_network' ? true : latest.status === 'out_of_network' ? false : null,
    status,
    referral_required: plan.requires_referral ?? null,
    prior_auth_required: plan.requires_prior_auth_for_specialist ?? null,
    conflict: reportedConflict,
    ...(conflicting_evidence_ids && conflicting_evidence_ids.length > 0
      ? { conflicting_evidence_ids }
      : {}),
    winning_evidence_id: winningEvidenceId,
    freshness: {
      state: staleFault ? 'stale' : freshnessState(freshnessSource),
      age_seconds: freshnessSource?.age_seconds ?? 0,
    },
    evidence_ids,
    disclaimer_key: 'coverage_not_a_guarantee',
    synthetic: true,
  };
}

/** `fresh` | `stale` | `unknown`, as the fixture records it for the source. */
function freshnessState(evidence: CitedEvidence | undefined): CoverageResult['freshness']['state'] {
  if (!evidence) return 'unknown';
  return evidence.state === 'fresh' ? 'fresh' : evidence.state === 'stale' ? 'stale' : 'unknown';
}

/**
 * The sources that answer a coverage question, as opposed to merely being
 * retrieved alongside it: the plan-level record API and the structured
 * directory. A referral-requirement row or a prose directory blurb is cited,
 * but neither can be the record that wins a dispute.
 */
const ANSWER_SOURCES = new Set(['coverage_api', 'provider_directory']);

/**
 * The source that beats a defeated one: the row that was recorded as
 * invalidating it, else the newest record's evidence (the record is the
 * authoritative structured answer), else the newest authoritative
 * answer-bearing subject row.
 */
function arbiterFor(
  defeated: CitedEvidence,
  cited: CitedEvidence[],
  latest: NetworkRecordShape | undefined,
): string | null {
  const declared = defeated.invalidated_by.find((id) => cited.some((entry) => entry.evidence_id === id));
  if (declared) return declared;
  if (latest && cited.some((entry) => entry.evidence_id === latest.evidence_id)) return latest.evidence_id;
  const structured = cited.find((entry) => entry.authoritative && ANSWER_SOURCES.has(entry.source));
  return structured?.evidence_id ?? null;
}

/**
 * Close the citation set over the invalidation relation, in both directions: a
 * source that was defeated is part of the trail, and so is any other source the
 * surviving one went out of its way to defeat.
 */
function citeWithInvalidations(context: AppContext, seed: CitedEvidence[]): CitedEvidence[] {
  const byId = new Map<string, CitedEvidence>(seed.map((entry) => [entry.evidence_id, entry]));
  /* 50 rows in the fixture set; a real service would index this relation. */
  const all = context.store
    .listEvidence()
    .map((row) => parseEvidence(row))
    .filter((entry): entry is CitedEvidence => entry !== null);

  let changed = true;
  while (changed) {
    changed = false;
    const known = new Set(byId.keys());
    for (const entry of all) {
      if (byId.has(entry.evidence_id)) continue;
      const defeatsKnown = entry.invalidated_by.some((id) => known.has(id));
      const defeatedByKnown = [...known].some((id) => byId.get(id)?.invalidated_by.includes(entry.evidence_id));
      if (defeatsKnown || defeatedByKnown) {
        byId.set(entry.evidence_id, entry);
        changed = true;
      }
    }
  }
  return [...byId.values()];
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}
