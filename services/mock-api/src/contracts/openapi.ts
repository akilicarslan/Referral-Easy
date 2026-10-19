import { z } from 'zod';
import { SCHEMA_REGISTRY, type SchemaName } from './zod.js';
import type { MockApiConfig } from '../config.js';

export interface RouteDoc {
  method: 'get' | 'post' | 'delete' | 'put';
  path: string;
  summary: string;
  description?: string;
  tags: string[];
  requestSchema?: SchemaName;
  responseSchema?: SchemaName;
  responseStatus?: number;
  params?: Array<{ name: string; description?: string }>;
  query?: Array<{ name: string; description?: string; schema?: SchemaName }>;
}

/** Converts one Zod schema into draft-2020-12 JSON Schema for `components.schemas`. */
export function jsonSchemaFor(schema: z.ZodType): Record<string, unknown> {
  const document = z.toJSONSchema(schema, {
    target: 'draft-2020-12',
    io: 'output',
    unrepresentable: 'any',
  }) as Record<string, unknown>;
  delete document.$schema;
  delete document.$id;
  return document;
}

/**
 * Collects route metadata as the modules register themselves, so the served
 * OpenAPI document is produced from the same Zod definitions that validate the
 * requests. Nothing here is hand-maintained in a second place.
 */
export class OpenApiRegistry {
  private readonly routes: RouteDoc[] = [];

  describe(route: RouteDoc): void {
    this.routes.push(route);
  }

  document(config: MockApiConfig): Record<string, unknown> {
    const components: Record<string, unknown> = {};
    for (const [name, schema] of Object.entries(SCHEMA_REGISTRY)) {
      components[name] = jsonSchemaFor(schema);
    }

    const paths: Record<string, Record<string, unknown>> = {};
    for (const route of this.routes) {
      const operation: Record<string, unknown> = {
        summary: route.summary,
        tags: route.tags,
        operationId: `${route.method}${route.path.replace(/[^a-zA-Z0-9]+(.)?/g, (_, c: string) =>
          c ? c.toUpperCase() : '',
        )}`,
        responses: {
          [String(route.responseStatus ?? 200)]: {
            description: 'Success',
            content: route.responseSchema
              ? { 'application/json': { schema: { $ref: `#/components/schemas/${route.responseSchema}` } } }
              : undefined,
          },
          '4XX': { description: 'Request rejected', content: { 'application/json': { schema: { $ref: '#/components/schemas/ErrorEnvelope' } } } },
          '5XX': { description: 'Upstream or mock fault', content: { 'application/json': { schema: { $ref: '#/components/schemas/ErrorEnvelope' } } } },
        },
      };
      if (route.description) operation.description = route.description;
      if (route.requestSchema) {
        operation.requestBody = {
          required: true,
          content: { 'application/json': { schema: { $ref: `#/components/schemas/${route.requestSchema}` } } },
        };
      }
      if (route.params?.length) {
        operation.parameters = [
          ...(operation.parameters as unknown[] | undefined ?? []),
          ...route.params.map((param) => ({
            name: param.name,
            in: 'path',
            required: true,
            description: param.description,
            schema: { type: 'string' },
          })),
        ];
      }
      if (route.query?.length) {
        operation.parameters = [
          ...((operation.parameters as unknown[]) ?? []),
          ...route.query.map((param) => ({
            name: param.name,
            in: 'query',
            required: false,
            description: param.description,
            schema: { type: 'string' },
          })),
        ];
      }
      paths[route.path] = { ...(paths[route.path] ?? {}), [route.method]: operation };
    }

    return {
      openapi: '3.1.0',
      info: {
        title: 'ReferralEasy mock hospital network API',
        version: '0.1.0',
        description: [
          'Synthetic hospital-network API backing the ReferralEasy demo.',
          'All payloads are synthetic. No provider credentials exist and no LLM is ever called.',
          'Booking truth lives in the response `state` field: `REQUESTED` and `PENDING_REVIEW` are never confirmations,',
          'and a 2xx alone does not mean an appointment exists.',
        ].join(' '),
      },
      servers: [{ url: config.publicUrl }],
      paths,
      components: { schemas: components },
    };
  }
}
