import { createHash, randomUUID } from 'node:crypto';

/**
 * Deterministic identifiers.
 *
 * Everything the mock API generates must be reproducible from its inputs: a
 * replayed booking with the same idempotency key has to produce the SAME
 * identifiers, otherwise "same key ⇒ same booking" would only hold for the rows
 * the database happens to reuse. UUIDv5-style derivation (SHA-1 over a
 * namespace, per RFC 4122 §4.3) gives us that for free while staying a
 * perfectly ordinary UUID to every consumer.
 */

const NAMESPACE = '8f1c4c1e-6a0a-5f2b-9e0d-referraleasy0001'.replace(/[^0-9a-f-]/gi, '0');

function uuidFromBytes(bytes: Buffer): string {
  const hex = bytes.subarray(0, 16).toString('hex').split('');
  hex[12] = '5';
  const variant = Number.parseInt(hex[16] ?? '0', 16);
  hex[16] = ((variant & 0x3) | 0x8).toString(16);
  const value = hex.join('');
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20, 32)}`;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** Stable UUID derived from a seed string. Never random. */
export function deterministicUuid(seed: string): string {
  const digest = createHash('sha1').update(`${NAMESPACE}:${seed}`).digest();
  return uuidFromBytes(digest);
}

export function randomUuid(): string {
  return randomUUID();
}

/** Deterministic-but-unique id for rows created at runtime (a plain random UUID). */
export function newUuid(): string {
  return randomUUID();
}

export function isUuid(value: string): boolean {
  return UUID_PATTERN.test(value);
}

/**
 * Canonical hash of the booking payload — the hashed unit of the approve→book
 * contract. Keys are sorted and the JSON is minified so that field order can
 * never change the hash, and two payloads that differ in ANY field hash
 * differently.
 */
export function payloadHash(payload: Record<string, unknown>): string {
  return createHash('sha256').update(canonicalJson(payload)).digest('hex');
}

export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    return JSON.stringify(value) ?? 'null';
  }
  if (Array.isArray(value)) {
    return `[${value.map((entry) => canonicalJson(entry)).join(',')}]`;
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record)
    .filter((key) => record[key] !== undefined)
    .sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(',')}}`;
}

export function shortHash(seed: string, length = 32): string {
  return createHash('sha256').update(seed).digest('hex').slice(0, length);
}
