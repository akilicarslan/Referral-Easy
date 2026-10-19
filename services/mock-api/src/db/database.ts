import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

/**
 * Store choice: SQLite through Node's built-in `node:sqlite`.
 *
 * The invariants that matter here — one live booking per slot, one booking per
 * idempotency key, booking + outbox written atomically — need a real database
 * with real UNIQUE constraints and real transactions, not a Map. The built-in
 * engine gives exactly that (SQLite 3.50) with no native build and no
 * postinstall script, which matters because pnpm's script sandbox blocks
 * `better-sqlite3`'s binary download unless the WORKSPACE ROOT opts in via
 * `pnpm.onlyBuiltDependencies` — a file outside this service's ownership.
 */

const SCHEMA = `
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS locations (
  location_id   TEXT PRIMARY KEY,
  city          TEXT NOT NULL,
  state         TEXT NOT NULL,
  timezone      TEXT NOT NULL,
  payload       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hospitals (
  hospital_id       TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  location_id       TEXT NOT NULL,
  timezone          TEXT NOT NULL,
  adapter_type      TEXT NOT NULL CHECK (adapter_type IN ('instant_confirm','review_required')),
  payload           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS providers (
  provider_id   TEXT PRIMARY KEY,
  specialty     TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  payload       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_hospitals (
  hospital_id   TEXT NOT NULL,
  provider_id   TEXT NOT NULL,
  PRIMARY KEY (hospital_id, provider_id)
);

CREATE TABLE IF NOT EXISTS insurance_plans (
  plan_id   TEXT PRIMARY KEY,
  payload   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS network_records (
  record_id      TEXT PRIMARY KEY,
  plan_id        TEXT NOT NULL,
  provider_id    TEXT NOT NULL,
  hospital_id    TEXT,
  status         TEXT NOT NULL CHECK (status IN ('in_network','out_of_network','unknown')),
  effective_from TEXT NOT NULL,
  effective_to   TEXT,
  evidence_id    TEXT NOT NULL,
  payload        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS referral_requirements (
  requirement_id TEXT PRIMARY KEY,
  hospital_id    TEXT NOT NULL,
  plan_id        TEXT,
  visit_type     TEXT NOT NULL,
  payload        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_fixtures (
  document_id TEXT PRIMARY KEY,
  patient_id  TEXT NOT NULL,
  kind        TEXT NOT NULL,
  issued_at   TEXT NOT NULL,
  payload     TEXT NOT NULL
);

-- The citation the other tables point at. A network record, a provider blurb and
-- a referral requirement all carry an evidence_id; storing the evidence makes
-- those citations resolvable instead of decorative.
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id   TEXT PRIMARY KEY,
  source        TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  authoritative INTEGER NOT NULL,
  retrieved_at  TEXT NOT NULL,
  payload       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage_evidence_snapshots (
  snapshot_id   TEXT PRIMARY KEY,
  captured_at   TEXT NOT NULL,
  authoritative INTEGER NOT NULL,
  payload       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slots (
  slot_id          TEXT PRIMARY KEY,
  provider_id      TEXT NOT NULL,
  hospital_id      TEXT NOT NULL,
  start_at         TEXT NOT NULL,           -- UTC instant, RFC 3339 with Z
  end_at           TEXT NOT NULL,           -- UTC instant, RFC 3339 with Z
  display_timezone TEXT NOT NULL,           -- hospital IANA zone, rendering only
  visit_type       TEXT NOT NULL CHECK (visit_type IN ('first_visit','return_visit')),
  status           TEXT NOT NULL CHECK (status IN ('available','held','booked','expired')),
  version          INTEGER NOT NULL DEFAULT 0,
  payload          TEXT NOT NULL
);

-- A provider cannot have two live slots at the same instant.
CREATE UNIQUE INDEX IF NOT EXISTS ux_slots_provider_start
  ON slots(provider_id, start_at) WHERE status <> 'expired';

CREATE TABLE IF NOT EXISTS booking_requests (
  booking_request_id  TEXT PRIMARY KEY,
  workflow_id         TEXT NOT NULL,
  command_id          TEXT NOT NULL,
  idempotency_key     TEXT NOT NULL,
  payload_hash        TEXT NOT NULL,
  state               TEXT NOT NULL,
  adapter_type        TEXT NOT NULL CHECK (adapter_type IN ('instant_confirm','review_required')),
  external_reference  TEXT,
  attempt_count       INTEGER NOT NULL DEFAULT 0,
  reconciliation_status TEXT,
  reconciliation_started_at TEXT,
  reconciliation_lookup_attempts INTEGER,
  reconciliation_last_lookup_at TEXT,
  reconciliation_escalation_reason TEXT,
  payload             TEXT NOT NULL,
  approval_id         TEXT,
  reschedule_of       TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);

-- ONE booking row per idempotency key: the durable half of the no-double-booking guard.
CREATE UNIQUE INDEX IF NOT EXISTS ux_booking_requests_idempotency_key
  ON booking_requests(idempotency_key);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  idempotency_key    TEXT PRIMARY KEY,
  scope              TEXT NOT NULL,
  request_hash       TEXT NOT NULL,
  booking_request_id TEXT,
  created_at         TEXT NOT NULL,
  last_seen_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS booking_attempts (
  attempt_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_request_id TEXT NOT NULL REFERENCES booking_requests(booking_request_id),
  attempt            INTEGER NOT NULL,
  idempotency_key    TEXT NOT NULL,
  result             TEXT NOT NULL,
  http_status        INTEGER,
  raw_response_ref   TEXT,
  started_at         TEXT NOT NULL,
  finished_at        TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_booking_attempts_number
  ON booking_attempts(booking_request_id, attempt);

CREATE TABLE IF NOT EXISTS appointments (
  appointment_id     TEXT PRIMARY KEY,
  booking_request_id TEXT NOT NULL REFERENCES booking_requests(booking_request_id),
  hospital_id        TEXT NOT NULL,
  provider_id        TEXT NOT NULL,
  patient_id         TEXT NOT NULL,
  slot_id            TEXT NOT NULL,
  start_at           TEXT NOT NULL,
  end_at             TEXT,
  display_timezone   TEXT NOT NULL,
  state              TEXT NOT NULL,
  verified           INTEGER NOT NULL DEFAULT 0,
  verification_method TEXT,
  synthetic          INTEGER NOT NULL DEFAULT 1,
  payload            TEXT
);

-- THE double-booking guard. A slot can carry at most one live appointment;
-- the database, not the request handler, is the arbiter.
CREATE UNIQUE INDEX IF NOT EXISTS ux_appointments_live_slot
  ON appointments(slot_id)
  WHERE state IN ('REQUESTED','PENDING_REVIEW','CONFIRMED','CANCEL_PENDING');

CREATE TABLE IF NOT EXISTS notification_outbox (
  outbox_id          TEXT PRIMARY KEY,
  booking_request_id TEXT NOT NULL REFERENCES booking_requests(booking_request_id),
  channel            TEXT NOT NULL CHECK (channel IN ('email','sms')),
  recipient          TEXT,
  template_key       TEXT,
  status             TEXT NOT NULL CHECK (status IN ('pending','sent','failed','abandoned')),
  attempts           INTEGER NOT NULL DEFAULT 0,
  next_attempt_at    TEXT,
  last_error         TEXT,
  created_at         TEXT NOT NULL,
  payload            TEXT
);

CREATE INDEX IF NOT EXISTS ix_outbox_drainable
  ON notification_outbox(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS vendor_calls (
  vendor_call_id INTEGER PRIMARY KEY AUTOINCREMENT,
  hospital_id    TEXT NOT NULL,
  adapter_type   TEXT NOT NULL,
  operation      TEXT NOT NULL,
  idempotency_key TEXT,
  request        TEXT NOT NULL,
  response       TEXT,
  http_status    INTEGER,
  fault_type     TEXT,
  created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_vendor_calls_key ON vendor_calls(idempotency_key, operation);

CREATE TABLE IF NOT EXISTS fault_counters (
  scenario_id TEXT NOT NULL,
  match_key   TEXT NOT NULL,
  count       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (scenario_id, match_key)
);

CREATE TABLE IF NOT EXISTS fault_events (
  fault_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  scenario_id    TEXT,
  fault_type     TEXT NOT NULL,
  method         TEXT NOT NULL,
  path           TEXT NOT NULL,
  occurrence     INTEGER,
  request_seq    INTEGER NOT NULL,
  seed           INTEGER NOT NULL,
  mode           TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_log (
  request_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  method      TEXT NOT NULL,
  path        TEXT NOT NULL,
  scenario_id TEXT,
  fault_type  TEXT,
  seed        INTEGER NOT NULL,
  created_at  TEXT NOT NULL
);
`;

export class Database {
  readonly raw: DatabaseSync;
  private transactionDepth = 0;

  constructor(path: string) {
    if (path !== ':memory:') {
      mkdirSync(dirname(path), { recursive: true });
    }
    this.raw = new DatabaseSync(path);
    this.raw.exec('PRAGMA journal_mode = WAL;');
    this.raw.exec('PRAGMA busy_timeout = 5000;');
    this.raw.exec('PRAGMA foreign_keys = ON;');
    this.raw.exec(SCHEMA);
  }

  /**
   * Runs `fn` inside a single database transaction. Nested calls join the outer
   * transaction, which is what lets the booking pipeline compose "claim the
   * slot", "write the booking" and "write the outbox entries" without ever
   * splitting them across two commits.
   *
   * `IMMEDIATE` takes the write lock up front so two concurrent claims cannot
   * both read `available` and then write.
   */
  transaction<T>(fn: () => T): T {
    if (this.transactionDepth > 0) {
      this.transactionDepth += 1;
      try {
        return fn();
      } finally {
        this.transactionDepth -= 1;
      }
    }
    this.raw.exec('BEGIN IMMEDIATE');
    this.transactionDepth = 1;
    try {
      const result = fn();
      this.raw.exec('COMMIT');
      return result;
    } catch (error) {
      try {
        this.raw.exec('ROLLBACK');
      } catch {
        // The transaction is already gone; the original error is what matters.
      }
      throw error;
    } finally {
      this.transactionDepth = 0;
    }
  }

  close(): void {
    this.raw.close();
  }
}

/** True when the error came from a violated UNIQUE/PRIMARY KEY constraint. */
export function isUniqueViolation(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /UNIQUE constraint failed|constraint failed/i.test(message);
}
