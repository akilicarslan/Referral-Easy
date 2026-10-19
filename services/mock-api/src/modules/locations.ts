import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { notFound } from '../http/errors.js';
import type { Location } from '../contracts/types.js';

export function registerLocations(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'get',
    path: '/locations',
    summary: 'List all synthetic locations',
    tags: ['locations'],
  });

  app.get('/locations', async () => {
    const locations = context.store.listLocations().map((row) => JSON.parse(String(row.payload)) as Location);
    return { locations, count: locations.length };
  });

  context.docs.describe({
    method: 'get',
    path: '/locations/{location_id}',
    summary: 'Fetch one location',
    tags: ['locations'],
    params: [{ name: 'location_id' }],
  });

  app.get<{ Params: { location_id: string } }>('/locations/:location_id', async (request) => {
    const row = context.store.getLocation(request.params.location_id);
    if (!row) throw notFound('unknown_location', `No location ${request.params.location_id}`);
    return JSON.parse(String(row.payload)) as Location;
  });
}
