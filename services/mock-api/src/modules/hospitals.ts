import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { notFound } from '../http/errors.js';
import type { Hospital, Provider } from '../contracts/types.js';

interface HospitalQuery {
  location_id?: string;
  adapter_type?: string;
  plan_id?: string;
  accepts_new_patients?: string;
}

export function registerHospitals(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'get',
    path: '/hospitals',
    summary: 'List hospitals, optionally filtered',
    description: '`adapter_type` is exposed because the demo needs to reach both vendors; patient-facing code must never branch on it.',
    tags: ['hospitals'],
    query: [
      { name: 'location_id' },
      { name: 'adapter_type' },
      { name: 'plan_id' },
      { name: 'accepts_new_patients' },
    ],
  });

  app.get<{ Querystring: HospitalQuery }>('/hospitals', async (request) => {
    const query = request.query;
    let hospitals = context.store
      .listHospitals()
      .map((row) => JSON.parse(String(row.payload)) as Hospital);

    if (query.location_id) hospitals = hospitals.filter((h) => h.location_id === query.location_id);
    if (query.adapter_type) hospitals = hospitals.filter((h) => h.adapter_type === query.adapter_type);
    if (query.plan_id) {
      hospitals = hospitals.filter((h) => (h.accepted_plan_ids ?? []).includes(query.plan_id as string));
    }
    if (query.accepts_new_patients !== undefined) {
      const wanted = query.accepts_new_patients !== 'false';
      hospitals = hospitals.filter((h) => h.accepts_new_patients === wanted);
    }
    return { hospitals, count: hospitals.length };
  });

  context.docs.describe({
    method: 'get',
    path: '/hospitals/{hospital_id}',
    summary: 'Fetch one hospital, with its providers',
    tags: ['hospitals'],
    params: [{ name: 'hospital_id' }],
  });

  app.get<{ Params: { hospital_id: string } }>('/hospitals/:hospital_id', async (request) => {
    const row = context.store.getHospital(request.params.hospital_id);
    if (!row) throw notFound('unknown_hospital', `No hospital ${request.params.hospital_id}`);
    const hospital = JSON.parse(String(row.payload)) as Hospital;
    const providers = context.store
      .listProvidersForHospital(hospital.hospital_id)
      .map((providerRow) => JSON.parse(String(providerRow.payload)) as Provider);
    return { hospital, providers };
  });
}
