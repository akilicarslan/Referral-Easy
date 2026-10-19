import { describe, expect, it } from 'vitest';
import { Database, isUniqueViolation } from '../src/db/database.js';
import { payloadHash } from '../src/domain/ids.js';
import { jsonSchemaFor, SCHEMA_REGISTRY_NAMES, } from '../src/contracts/registry-probe.js';

describe('service skeleton', () => {
  it('exposes every frozen contract schema through the registry', () => {
    expect(SCHEMA_REGISTRY_NAMES).toContain('BookingRequest');
    expect(SCHEMA_REGISTRY_NAMES).toContain('NotificationOutboxEntry');
    expect(SCHEMA_REGISTRY_NAMES).toContain('AvailabilityResult');
    const booking = jsonSchemaFor('BookingRequest');
    expect(booking.additionalProperties).toBe(false);
  });

  it('enforces a real UNIQUE constraint in SQLite', () => {
    const db = new Database(':memory:');
    db.raw.exec(
      `INSERT INTO booking_requests (booking_request_id, workflow_id, command_id, idempotency_key, payload_hash, state, adapter_type, attempt_count, payload, created_at, updated_at)
       VALUES ('b1','w1','c1','key-0001','${'0'.repeat(64)}','REQUESTED','instant_confirm',0,'{}','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`,
    );
    db.raw.exec("INSERT INTO slots (slot_id, provider_id, hospital_id, start_at, end_at, display_timezone, visit_type, status, version, payload) VALUES ('s1','p1','h1','2026-01-01T09:00:00Z','2026-01-01T09:30:00Z','America/Chicago','first_visit','available',0,'{}')");
    const insertAppointment = (appointmentId: string) =>
      db.raw.prepare(
        `INSERT INTO appointments (appointment_id, booking_request_id, hospital_id, provider_id, patient_id, slot_id, start_at, display_timezone, state, verified)
         VALUES (?, 'b1', 'h1', 'p1', 'pa1', 's1', '2026-01-01T09:00:00Z', 'America/Chicago', 'CONFIRMED', 0)`,
      ).run(appointmentId);
    insertAppointment('a1');
    let caught: unknown;
    try {
      insertAppointment('a2');
    } catch (error) {
      caught = error;
    }
    expect(isUniqueViolation(caught)).toBe(true);
    db.close();
  });

  it('hashes a payload independently of key order', () => {
    expect(payloadHash({ a: 1, b: 'x' })).toBe(payloadHash({ b: 'x', a: 1 }));
    expect(payloadHash({ a: 1, b: 'x' })).not.toBe(payloadHash({ a: 1, b: 'y' }));
  });
});
