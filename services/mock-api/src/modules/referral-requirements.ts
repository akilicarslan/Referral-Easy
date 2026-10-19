import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { badRequest, notFound } from '../http/errors.js';
import type { ReferralRequirement } from '../contracts/types.js';

/**
 * A hospital's document requirement set. §4: this is never presented to the
 * patient as final before that hospital has been chosen, which is why the
 * caller has to name the hospital explicitly.
 */
export function registerReferralRequirements(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'get',
    path: '/referral-requirements',
    summary: 'Document requirements for a hospital, visit type and optional plan',
    description: 'Plan-specific rules win over hospital-wide ones; the response lists what applies and which rule each came from.',
    tags: ['referral-requirements'],
    query: [{ name: 'hospital_id' }, { name: 'visit_type' }, { name: 'plan_id' }],
  });

  app.get<{ Querystring: { hospital_id?: string; visit_type?: string; plan_id?: string } }>(
    '/referral-requirements',
    async (request) => {
      const { hospital_id: hospitalId, visit_type: visitType, plan_id: planId } = request.query;
      if (!hospitalId) throw badRequest('missing_parameter', 'hospital_id is required');
      if (!visitType) throw badRequest('missing_parameter', 'visit_type is required');
      if (visitType !== 'first_visit' && visitType !== 'return_visit') {
        throw badRequest('invalid_visit_type', 'visit_type must be first_visit or return_visit');
      }
      if (!context.store.getHospital(hospitalId)) {
        throw notFound('unknown_hospital', `No hospital ${hospitalId}`);
      }

      const requirements = context.store
        .listReferralRequirements(hospitalId, visitType, planId ?? null)
        .map((row) => JSON.parse(String(row.payload)) as ReferralRequirement);

      return {
        hospital_id: hospitalId,
        visit_type: visitType,
        plan_id: planId ?? null,
        requirements,
        required_documents: [...new Set(requirements.flatMap((entry) => entry.required_documents))],
        count: requirements.length,
      };
    },
  );
}
