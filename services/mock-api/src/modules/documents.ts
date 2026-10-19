import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { notFound } from '../http/errors.js';
import { parseOrThrow } from '../http/validate.js';
import { readinessRequest } from '../contracts/zod.js';
import type { DocumentCheck, DocumentFixture, ReadinessResult, ReferralRequirement } from '../contracts/types.js';
import { deterministicUuid, newUuid } from '../domain/ids.js';
import { toUtcIso } from '../domain/time.js';

/**
 * Documents and readiness.
 *
 * Document text is DATA. The fixtures deliberately include one document whose
 * body looks like an instruction to an automated reader; nothing here ever
 * interprets it, and the check that uses it only looks at its metadata.
 */
export function registerDocuments(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'get',
    path: '/documents',
    summary: 'List a patient’s synthetic documents',
    tags: ['documents'],
    query: [{ name: 'patient_id' }, { name: 'kind' }],
  });

  app.get<{ Querystring: { patient_id?: string; kind?: string } }>('/documents', async (request) => {
    const patientId = request.query.patient_id;
    if (!patientId) return { documents: [], count: 0, reason: 'patient_id is required' };
    let documents = context.store.listDocuments(patientId).map((row) => JSON.parse(String(row.payload)) as DocumentFixture);
    if (request.query.kind) documents = documents.filter((document) => document.kind === request.query.kind);
    return { documents, count: documents.length };
  });

  context.docs.describe({
    method: 'get',
    path: '/documents/{document_id}',
    summary: 'Fetch one document fixture',
    tags: ['documents'],
    params: [{ name: 'document_id' }],
  });

  app.get<{ Params: { document_id: string } }>('/documents/:document_id', async (request) => {
    const row = context.store.getDocument(request.params.document_id);
    if (!row) throw notFound('unknown_document', `No document ${request.params.document_id}`);
    return JSON.parse(String(row.payload)) as DocumentFixture;
  });

  context.docs.describe({
    method: 'post',
    path: '/documents/check',
    summary: 'Readiness check for a hospital’s requirements',
    description:
      'One check per required kind, plus `not_required` entries for anything supplied that was not asked for. Satisfied is only true when every required check passed.',
    tags: ['documents'],
    requestSchema: 'ReadinessRequest',
    responseSchema: 'ReadinessResult',
  });

  app.post('/documents/check', async (request) => {
    const body = parseOrThrow(readinessRequest, request.body ?? {}, 'Readiness request');
    const hospital = context.store.getHospital(body.hospital_id);
    if (!hospital) throw notFound('unknown_hospital', `No hospital ${body.hospital_id}`);

    const requirements = context.store
      .listReferralRequirements(body.hospital_id, body.visit_type, body.plan_id ?? null)
      .map((row) => JSON.parse(String(row.payload)) as ReferralRequirement);

    const required = new Map<string, number | null>();
    for (const requirement of requirements) {
      for (const kind of requirement.required_documents) {
        const current = required.get(kind);
        const maxAge = requirement.max_document_age_days ?? null;
        // The strictest applicable rule wins.
        if (current === undefined || (maxAge !== null && (current === null || maxAge < current))) {
          required.set(kind, maxAge);
        }
      }
    }

    const checkedAt = toUtcIso(context.clock());
    const runId = newUuid();
    const provided = body.document_ids
      .map((documentId) => context.store.getDocument(documentId))
      .filter((row): row is Record<string, unknown> => row !== undefined)
      .map((row) => JSON.parse(String(row.payload)) as DocumentFixture);

    const checks: DocumentCheck[] = [];
    const missingKinds: string[] = [];

    for (const [kind, maxAgeDays] of required) {
      const document = provided.find((candidate) => candidate.kind === kind);
      if (!document) {
        checks.push({
          kind: kind as DocumentCheck['kind'],
          status: 'missing',
          document_id: null,
          reason_code: 'document_missing',
          checked_at: checkedAt,
          evidence_ids: [deterministicUuid(`evidence:readiness:${body.workflow_id}:${kind}:missing`)],
          run_id: runId,
        });
        missingKinds.push(kind);
        continue;
      }
      const ageDays = Math.floor(
        (Date.parse(checkedAt) - Date.parse(document.issued_at)) / (24 * 60 * 60 * 1000),
      );
      if (maxAgeDays !== null && ageDays > maxAgeDays) {
        checks.push({
          kind: kind as DocumentCheck['kind'],
          status: 'expired',
          document_id: document.document_id,
          reason_code: 'stale_evidence',
          checked_at: checkedAt,
          evidence_ids: [deterministicUuid(`evidence:readiness:${body.workflow_id}:${kind}:expired`)],
          run_id: runId,
        });
        missingKinds.push(kind);
        continue;
      }
      checks.push({
        kind: kind as DocumentCheck['kind'],
        status: 'satisfied',
        document_id: document.document_id,
        reason_code: 'ok',
        checked_at: checkedAt,
        evidence_ids: [deterministicUuid(`evidence:document:${document.document_id}`)],
        run_id: runId,
      });
    }

    for (const document of provided) {
      if (required.has(document.kind)) continue;
      checks.push({
        kind: document.kind,
        status: 'not_required',
        document_id: document.document_id,
        reason_code: 'ok',
        checked_at: checkedAt,
        evidence_ids: [deterministicUuid(`evidence:document:${document.document_id}`)],
        run_id: runId,
      });
    }

    const satisfied = missingKinds.length === 0;
    const result: ReadinessResult = {
      readiness_id: newUuid(),
      workflow_id: body.workflow_id,
      hospital_id: body.hospital_id,
      plan_id: body.plan_id ?? null,
      visit_type: body.visit_type,
      checks,
      satisfied,
      missing_kinds: missingKinds as ReadinessResult['missing_kinds'],
      can_proceed: satisfied,
      evidence_ids: [...new Set(checks.flatMap((check) => check.evidence_ids ?? []))],
      checked_at: checkedAt,
    };
    return result;
  });
}
