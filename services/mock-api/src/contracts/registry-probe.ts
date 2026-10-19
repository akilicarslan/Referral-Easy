import { SCHEMA_REGISTRY, type SchemaName } from './zod.js';
import { jsonSchemaFor as toJsonSchema } from './openapi.js';

/**
 * Thin accessors over the schema registry, used by the contract-shape test to
 * assert that the served OpenAPI document and the frozen schemas stay aligned.
 */
export const SCHEMA_REGISTRY_NAMES = Object.keys(SCHEMA_REGISTRY) as SchemaName[];

export function jsonSchemaFor(name: SchemaName): Record<string, unknown> {
  return toJsonSchema(SCHEMA_REGISTRY[name]);
}
