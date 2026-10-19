import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { notFound } from '../http/errors.js';
import { toUtcIso } from '../domain/time.js';

/**
 * Evidence.
 *
 * Every citation in this API — a network record, a provider blurb, a referral
 * requirement — carries an `evidence_id`. Serving the evidence makes those
 * citations resolvable, and means "why does the API believe this?" has an
 * answer that is data rather than a promise.
 *
 * Freshness is computed against the clock rather than echoed from the fixture,
 * so a run months later honestly reports stale evidence instead of repeating a
 * number captured on the day the fixtures were written.
 */
const STALE_AFTER_SECONDS = 30 * 24 * 60 * 60;

interface EvidenceRecord {
  evidence_id: string;
  source: string;
  source_id: string;
  authoritative?: boolean;
  retrieved_at?: string;
  freshness?: { state?: string; age_seconds?: number | null; [key: string]: unknown };
  invalidated_by?: string | null;
  [key: string]: unknown;
}

function withLiveFreshness(record: EvidenceRecord, now: Date): EvidenceRecord {
  const retrieved = record.retrieved_at ? Date.parse(record.retrieved_at) : Number.NaN;
  const ageSeconds = Number.isFinite(retrieved) ? Math.max(0, Math.round((now.getTime() - retrieved) / 1000)) : null;
  const state =
    ageSeconds === null
      ? (record.freshness?.state ?? 'unknown')
      : ageSeconds > STALE_AFTER_SECONDS
        ? 'stale'
        : 'fresh';
  return {
    ...record,
    freshness: {
      state,
      age_seconds: ageSeconds ?? record.freshness?.age_seconds ?? null,
      captured_at: record.retrieved_at ?? null,
      reference_age_seconds: record.freshness?.age_seconds ?? null,
    },
  };
}

export function registerEvidence(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'get',
    path: '/evidence',
    summary: 'Evidence records, optionally filtered by the record they cite',
    tags: ['evidence'],
    query: [{ name: 'source_id' }],
  });

  app.get<{ Querystring: { source_id?: string } }>('/evidence', async (request) => {
    const sourceId = request.query.source_id;
    const rows = sourceId ? context.store.listEvidenceForSource(sourceId) : context.store.listEvidence();
    const records = rows.map((row) => withLiveFreshness(JSON.parse(String(row.payload)) as EvidenceRecord, context.clock()));
    return { records, count: records.length };
  });

  context.docs.describe({
    method: 'get',
    path: '/evidence/:evidence_id',
    summary: 'One evidence record with freshness computed against the clock',
    tags: ['evidence'],
  });

  app.get<{ Params: { evidence_id: string } }>('/evidence/:evidence_id', async (request) => {
    const row = context.store.getEvidence(request.params.evidence_id);
    if (!row) throw notFound('unknown_evidence', `No evidence ${request.params.evidence_id}`);
    return withLiveFreshness(JSON.parse(String(row.payload)) as EvidenceRecord, context.clock());
  });

  context.docs.describe({
    method: 'get',
    path: '/evidence-snapshots',
    summary: 'Coverage evidence snapshots, newest first',
    tags: ['evidence'],
  });

  app.get('/evidence-snapshots', async () => {
    const snapshots = context.store.listCoverageSnapshots().map((row) => JSON.parse(String(row.payload)));
    return { snapshots, count: snapshots.length, as_of: toUtcIso(context.clock()) };
  });
}
