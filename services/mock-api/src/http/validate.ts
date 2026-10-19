import type { z } from 'zod';
import { badRequest } from './errors.js';

/**
 * Validates against the same Zod object that also generates the OpenAPI
 * document, so a payload accepted at runtime is always a payload the published
 * contract describes.
 */
export function parseOrThrow<T extends z.ZodType>(schema: T, value: unknown, what: string): z.infer<T> {
  const result = schema.safeParse(value);
  if (result.success) return result.data;
  throw badRequest('invalid_request', `${what} did not match the contract`, {
    issues: result.error.issues.map((issue) => ({
      path: issue.path.join('.'),
      code: issue.code,
      message: issue.message,
    })),
  });
}

export function requireParam(value: unknown, name: string): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw badRequest('missing_parameter', `Missing required parameter \`${name}\``);
  }
  return value;
}
