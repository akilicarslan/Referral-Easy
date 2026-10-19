import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { notFound } from '../http/errors.js';
import type { Provider } from '../contracts/types.js';

interface ProviderQuery {
  hospital_id?: string;
  specialty?: string;
  language?: string;
  accepts_new_patients?: string;
  plan_id?: string;
}

export function registerProviders(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'get',
    path: '/providers',
    summary: 'List providers, optionally filtered',
    description:
      '`language` matches a BCP-47 tag against the provider language skills; `specialty` compares the free-text directory field exactly, in code, never by model judgement.',
    tags: ['providers'],
    query: [
      { name: 'hospital_id' },
      { name: 'specialty' },
      { name: 'language' },
      { name: 'accepts_new_patients' },
      { name: 'plan_id' },
    ],
  });

  app.get<{ Querystring: ProviderQuery }>('/providers', async (request) => {
    const query = request.query;
    let providers = context.store.listProviders().map((row) => JSON.parse(String(row.payload)) as Provider);

    if (query.hospital_id) providers = providers.filter((p) => p.hospital_ids.includes(query.hospital_id as string));
    if (query.specialty) providers = providers.filter((p) => p.specialty === query.specialty);
    if (query.language) {
      const wanted = query.language.toLowerCase();
      providers = providers.filter((p) => p.languages.some((skill) => skill.language.toLowerCase().startsWith(wanted)));
    }
    if (query.accepts_new_patients !== undefined) {
      const wanted = query.accepts_new_patients !== 'false';
      providers = providers.filter((p) => p.accepts_new_patients === wanted);
    }
    if (query.plan_id) {
      const planRow = context.store.getPlan(query.plan_id);
      const payer = planRow ? (JSON.parse(String(planRow.payload)) as { payer_name: string }).payer_name : null;
      providers = providers.filter((p) => (payer ? p.accepts_insurance.includes(payer) : true));
    }
    return { providers, count: providers.length };
  });

  context.docs.describe({
    method: 'get',
    path: '/providers/{provider_id}',
    summary: 'Fetch one provider',
    tags: ['providers'],
    params: [{ name: 'provider_id' }],
  });

  app.get<{ Params: { provider_id: string } }>('/providers/:provider_id', async (request) => {
    const row = context.store.getProvider(request.params.provider_id);
    if (!row) throw notFound('unknown_provider', `No provider ${request.params.provider_id}`);
    return JSON.parse(String(row.payload)) as Provider;
  });
}
