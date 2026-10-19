import type { DatabaseSync } from 'node:sqlite';
import type {
  AppointmentRecordShape,
  BookingRequestShape,
  CoverageResultShape,
  SlotShape,
} from '../contracts/zod.js';
import type {
  BookingAttemptResult,
  BookingState,
  DocumentKind,
  FaultType,
  NotificationChannel,
  OutboxStatus,
  ReconciliationStatus,
} from '../contracts/enums.js';
import type { Database } from './database.js';

type Row = Record<string, any>;

function asString(value: unknown): string {
  return typeof value === 'string' ? value : String(value ?? '');
}

function asNumber(value: unknown): number {
  return typeof value === 'number' ? value : Number(value ?? 0);
}

function asBool(value: unknown): boolean {
  return asNumber(value) === 1;
}

function parseJson<T>(value: unknown): T {
  return JSON.parse(asString(value)) as T;
}

export interface SlotClaimResult {
  claimed: boolean;
  slot: SlotRecord | null;
}

export interface SlotRecord extends SlotShape {
  version: number;
}

export interface BookingAttemptRow {
  attempt: number;
  idempotency_key: string;
  started_at: string;
  finished_at: string | null;
  result: BookingAttemptResult;
  http_status: number | null;
  raw_response_ref: string | null;
}

export interface OutboxRow {
  outbox_id: string;
  booking_request_id: string;
  channel: NotificationChannel;
  recipient: string | null;
  template_key: string | null;
  status: OutboxStatus;
  attempts: number;
  next_attempt_at: string | null;
  last_error: string | null;
  created_at: string;
}

/**
 * Every read and write the API performs. Reads are deliberately thin — this is
 * a mock, so the interesting logic lives in the booking pipeline rather than in
 * query composition.
 */
export class Store {
  readonly db: DatabaseSync;
  private readonly database: Database;

  constructor(database: Database) {
    this.database = database;
    this.db = database.raw;
  }

  /**
   * Runs `fn` in one database transaction. Exposed here so the booking pipeline
   * can compose a claim, a booking write and its outbox entries into a single
   * atomic unit without reaching for the raw handle.
   */
  transaction<T>(fn: () => T): T {
    return this.database.transaction(fn);
  }

  /* ------------------------------------------------------------- directory */

  upsertDirectoryRow(
    table: 'locations' | 'hospitals' | 'providers' | 'insurance_plans',
    id: string,
    columns: Array<[string, string | number | null]>,
    payload: unknown,
  ): void {
    const names = [...columns.map(([name]) => name), 'payload'];
    const placeholders = names.map(() => '?').join(', ');
    const values = [...columns.map(([, value]) => value), JSON.stringify(payload)];
    this.db
      .prepare(`INSERT OR REPLACE INTO ${table} (${[idColumn(table), ...names].join(', ')}) VALUES (?, ${placeholders})`)
      .run(id, ...values);
  }

  getLocation(locationId: string): Row | undefined {
    return this.db.prepare('SELECT payload FROM locations WHERE location_id = ?').get(locationId) as Row | undefined;
  }

  listLocations(): Row[] {
    return this.db.prepare('SELECT payload FROM locations ORDER BY location_id').all() as Row[];
  }

  getHospital(hospitalId: string): Row | undefined {
    return this.db.prepare('SELECT payload FROM hospitals WHERE hospital_id = ?').get(hospitalId) as Row | undefined;
  }

  listHospitals(): Row[] {
    return this.db.prepare('SELECT payload FROM hospitals ORDER BY name').all() as Row[];
  }

  getProvider(providerId: string): Row | undefined {
    return this.db.prepare('SELECT payload FROM providers WHERE provider_id = ?').get(providerId) as Row | undefined;
  }

  listProviders(): Row[] {
    return this.db.prepare('SELECT payload FROM providers ORDER BY display_name').all() as Row[];
  }

  listProvidersForHospital(hospitalId: string): Row[] {
    return this.db
      .prepare(
        `SELECT p.payload AS payload FROM providers p
         JOIN provider_hospitals ph ON ph.provider_id = p.provider_id
         WHERE ph.hospital_id = ? ORDER BY p.display_name`,
      )
      .all(hospitalId) as Row[];
  }

  linkProviderHospital(providerId: string, hospitalId: string): void {
    this.db
      .prepare('INSERT OR IGNORE INTO provider_hospitals (hospital_id, provider_id) VALUES (?, ?)')
      .run(hospitalId, providerId);
  }

  getPlan(planId: string): Row | undefined {
    return this.db.prepare('SELECT payload FROM insurance_plans WHERE plan_id = ?').get(planId) as Row | undefined;
  }

  listPlans(): Row[] {
    return this.db.prepare('SELECT payload FROM insurance_plans ORDER BY plan_id').all() as Row[];
  }

  upsertNetworkRecord(record: Record<string, unknown> & { record_id: string }): void {
    this.db
      .prepare(
        `INSERT OR REPLACE INTO network_records
           (record_id, plan_id, provider_id, hospital_id, status, effective_from, effective_to, evidence_id, payload)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        record.record_id,
        asString(record.plan_id),
        asString(record.provider_id),
        record.hospital_id === null || record.hospital_id === undefined ? null : asString(record.hospital_id),
        asString(record.status),
        asString(record.effective_from),
        record.effective_to === null || record.effective_to === undefined ? null : asString(record.effective_to),
        asString(record.evidence_id),
        JSON.stringify(record),
      );
  }

  listNetworkRecords(planId: string, providerId: string, hospitalId?: string | null): Row[] {
    const sql =
      hospitalId === undefined || hospitalId === null
        ? 'SELECT payload FROM network_records WHERE plan_id = ? AND provider_id = ? ORDER BY effective_from DESC'
        : 'SELECT payload FROM network_records WHERE plan_id = ? AND provider_id = ? AND (hospital_id IS NULL OR hospital_id = ?) ORDER BY effective_from DESC';
    const params = hospitalId === undefined || hospitalId === null ? [planId, providerId] : [planId, providerId, hospitalId];
    return this.db.prepare(sql).all(...params) as Row[];
  }

  upsertReferralRequirement(requirement: Record<string, unknown> & { requirement_id: string }): void {
    this.db
      .prepare(
        `INSERT OR REPLACE INTO referral_requirements (requirement_id, hospital_id, plan_id, visit_type, payload)
         VALUES (?, ?, ?, ?, ?)`,
      )
      .run(
        requirement.requirement_id,
        asString(requirement.hospital_id),
        requirement.plan_id === null || requirement.plan_id === undefined ? null : asString(requirement.plan_id),
        asString(requirement.visit_type),
        JSON.stringify(requirement),
      );
  }

  listReferralRequirements(hospitalId: string, visitType: string, planId?: string | null): Row[] {
    return this.db
      .prepare(
        `SELECT payload FROM referral_requirements
         WHERE hospital_id = ? AND visit_type = ? AND (plan_id IS NULL OR plan_id = ?)
         ORDER BY plan_id IS NULL DESC`,
      )
      .all(hospitalId, visitType, planId ?? null) as Row[];
  }

  upsertDocument(document: Record<string, unknown> & { document_id: string }): void {
    this.db
      .prepare(
        `INSERT OR REPLACE INTO document_fixtures (document_id, patient_id, kind, issued_at, payload)
         VALUES (?, ?, ?, ?, ?)`,
      )
      .run(
        document.document_id,
        asString(document.patient_id),
        asString(document.kind),
        asString(document.issued_at),
        JSON.stringify(document),
      );
  }

  listDocuments(patientId: string): Row[] {
    return this.db
      .prepare('SELECT payload FROM document_fixtures WHERE patient_id = ? ORDER BY issued_at')
      .all(patientId) as Row[];
  }

  getDocument(documentId: string): Row | undefined {
    return this.db.prepare('SELECT payload FROM document_fixtures WHERE document_id = ?').get(documentId) as
      | Row
      | undefined;
  }

  /* -------------------------------------------------------------- evidence */

  upsertEvidence(record: Record<string, unknown> & { evidence_id: string }): void {
    this.db
      .prepare(
        `INSERT OR REPLACE INTO evidence (evidence_id, source, source_id, authoritative, retrieved_at, payload)
         VALUES (?, ?, ?, ?, ?, ?)`,
      )
      .run(
        record.evidence_id,
        asString(record.source),
        asString(record.source_id),
        record.authoritative === true ? 1 : 0,
        asString(record.retrieved_at),
        JSON.stringify(record),
      );
  }

  getEvidence(evidenceId: string): Row | undefined {
    return this.db.prepare('SELECT payload FROM evidence WHERE evidence_id = ?').get(evidenceId) as Row | undefined;
  }

  listEvidence(): Row[] {
    return this.db.prepare('SELECT payload FROM evidence ORDER BY evidence_id').all() as Row[];
  }

  listEvidenceForSource(sourceId: string): Row[] {
    return this.db
      .prepare('SELECT payload FROM evidence WHERE source_id = ? ORDER BY retrieved_at DESC')
      .all(sourceId) as Row[];
  }

  /**
   * Everything said about one subject — a provider or a hospital — including the
   * rows whose `source_id` names a *part* of it (`provider:<id>#profile_text`,
   * `hospital:<id>#directory_blurb`). A coverage answer rests on the subject's
   * own directory evidence as much as on the plan-level records, so a citation
   * trail that skipped the variant spellings would silently drop the very row
   * that contradicts the record.
   *
   * `subjectId` is a uuid-bearing key, so it holds no LIKE metacharacters.
   */
  listEvidenceForSubject(subjectId: string): Row[] {
    return this.db
      .prepare('SELECT payload FROM evidence WHERE source_id = ? OR source_id LIKE ? ORDER BY retrieved_at DESC')
      .all(subjectId, `${subjectId}#%`) as Row[];
  }

  countEvidence(): number {
    return (this.db.prepare('SELECT COUNT(*) AS n FROM evidence').get() as { n: number }).n;
  }

  upsertCoverageSnapshot(record: Record<string, unknown> & { snapshot_id: string }): void {
    this.db
      .prepare(
        `INSERT OR REPLACE INTO coverage_evidence_snapshots (snapshot_id, captured_at, authoritative, payload)
         VALUES (?, ?, ?, ?)`,
      )
      .run(
        record.snapshot_id,
        asString(record.captured_at),
        record.authoritative === true ? 1 : 0,
        JSON.stringify(record),
      );
  }

  listCoverageSnapshots(): Row[] {
    return this.db
      .prepare('SELECT payload FROM coverage_evidence_snapshots ORDER BY captured_at DESC')
      .all() as Row[];
  }

  /* ----------------------------------------------------------------- slots */

  /**
   * `INSERT OR IGNORE`, not `REPLACE`: re-seeding on a restart must never
   * resurrect a slot that a live booking has already claimed.
   */
  insertSlot(slot: Record<string, unknown>): void {
    this.db
      .prepare(
        `INSERT OR IGNORE INTO slots
           (slot_id, provider_id, hospital_id, start_at, end_at, display_timezone, visit_type, status, version, payload)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        asString(slot.slot_id),
        asString(slot.provider_id),
        asString(slot.hospital_id),
        asString(slot.start_at),
        asString(slot.end_at),
        asString(slot.display_timezone),
        asString(slot.visit_type),
        asString(slot.status),
        asNumber(slot.version ?? 0),
        JSON.stringify(slot),
      );
  }

  getSlot(slotId: string): SlotRecord | undefined {
    const row = this.db.prepare('SELECT * FROM slots WHERE slot_id = ?').get(slotId) as Row | undefined;
    return row ? hydrateSlot(row) : undefined;
  }

  listSlots(filter: {
    hospitalId: string;
    providerId: string;
    visitType: string;
    earliest?: string | null;
    latest?: string | null;
  }): SlotRecord[] {
    const conditions = ['hospital_id = ?', 'provider_id = ?', 'visit_type = ?', "status = 'available'"];
    const params: Array<string> = [filter.hospitalId, filter.providerId, filter.visitType];
    if (filter.earliest) {
      conditions.push('start_at >= ?');
      params.push(filter.earliest);
    }
    if (filter.latest) {
      conditions.push('start_at <= ?');
      params.push(filter.latest);
    }
    const rows = this.db
      .prepare(`SELECT * FROM slots WHERE ${conditions.join(' AND ')} ORDER BY start_at`)
      .all(...params) as Row[];
    return rows.map(hydrateSlot);
  }

  /**
   * Claims a slot for a booking. The conditional UPDATE is the concurrency
   * arbiter: `status = 'available' AND version = ?` in the WHERE clause means
   * two racing requests cannot both observe success, and the surrounding
   * BEGIN IMMEDIATE guarantees the loser reads the winner's write.
   */
  claimSlot(slotId: string, expectedVersion: number | null): SlotClaimResult {
    const slot = this.getSlot(slotId);
    if (!slot) return { claimed: false, slot: null };
    if (slot.status !== 'available') return { claimed: false, slot };
    if (expectedVersion !== null && slot.version !== expectedVersion) return { claimed: false, slot };
    const result = this.db
      .prepare("UPDATE slots SET status = 'held', version = version + 1 WHERE slot_id = ? AND status = 'available' AND version = ?")
      .run(slotId, slot.version);
    const changes = asNumber((result as Row).changes);
    return { claimed: changes === 1, slot: { ...slot, status: changes === 1 ? 'held' : slot.status } };
  }

  markSlotBooked(slotId: string): void {
    this.db.prepare("UPDATE slots SET status = 'booked', version = version + 1 WHERE slot_id = ?").run(slotId);
  }

  /**
   * Gives a slot back to the pool — after a declined claim, a refused booking,
   * or a cancellation.
   *
   * The NOT EXISTS guard is what makes it safe to call from every one of those
   * places: a slot is only freed when no live appointment still claims it, so
   * releasing a cancelled booking's slot can never hand a different patient's
   * appointment to the next caller. The version bump invalidates any client
   * still holding the old version, so a stale view cannot re-claim it either.
   */
  releaseSlot(slotId: string): void {
    this.db
      .prepare(
        `UPDATE slots SET status = 'available', version = version + 1
          WHERE slot_id = ?
            AND status IN ('held', 'booked')
            AND NOT EXISTS (
              SELECT 1 FROM appointments
               WHERE appointments.slot_id = slots.slot_id
                 AND appointments.state IN ('REQUESTED','PENDING_REVIEW','CONFIRMED','CANCEL_PENDING')
            )`,
      )
      .run(slotId);
  }

  expireSlot(slotId: string): void {
    this.db.prepare("UPDATE slots SET status = 'expired' WHERE slot_id = ?").run(slotId);
  }

  /* -------------------------------------------------------------- bookings */

  findBookingByKey(idempotencyKey: string): BookingRequestShape | undefined {
    const row = this.db
      .prepare('SELECT * FROM booking_requests WHERE idempotency_key = ?')
      .get(idempotencyKey) as Row | undefined;
    return row ? hydrateBooking(row) : undefined;
  }

  findBookingById(bookingRequestId: string): BookingRequestShape | undefined {
    const row = this.db
      .prepare('SELECT * FROM booking_requests WHERE booking_request_id = ?')
      .get(bookingRequestId) as Row | undefined;
    return row ? hydrateBooking(row) : undefined;
  }

  listBookings(filter: { patientId?: string; state?: string; adapterType?: string } = {}): BookingRequestShape[] {
    const conditions: string[] = [];
    const params: string[] = [];
    if (filter.state) {
      conditions.push("json_extract(payload, '$.patient_id') IS NOT NULL");
      conditions.push('state = ?');
      params.push(filter.state);
    }
    const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
    const rows = this.db
      .prepare(`SELECT * FROM booking_requests ${where} ORDER BY created_at DESC LIMIT 200`)
      .all(...params) as Row[];
    const bookings = rows.map(hydrateBooking);
    return filter.patientId ? bookings.filter((b) => b.payload.patient_id === filter.patientId) : bookings;
  }

  insertBooking(booking: BookingRequestShape): void {
    this.db
      .prepare(
        `INSERT INTO booking_requests
           (booking_request_id, workflow_id, command_id, idempotency_key, payload_hash, state, adapter_type,
            external_reference, attempt_count, reconciliation_status, reconciliation_started_at,
            reconciliation_lookup_attempts, reconciliation_last_lookup_at, reconciliation_escalation_reason,
            payload, approval_id, reschedule_of, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        booking.booking_request_id,
        booking.workflow_id,
        booking.command_id,
        booking.idempotency_key,
        booking.payload_hash,
        booking.state,
        booking.adapter_type,
        booking.external_reference ?? null,
        booking.attempt_count,
        booking.reconciliation?.status ?? null,
        booking.reconciliation?.started_at ?? null,
        booking.reconciliation?.lookup_attempts ?? null,
        booking.reconciliation?.last_lookup_at ?? null,
        booking.reconciliation?.escalation_reason ?? null,
        JSON.stringify(booking.payload),
        booking.approval_id ?? null,
        booking.reschedule_of ?? null,
        booking.created_at,
        booking.updated_at,
      );
  }

  updateBookingState(
    bookingRequestId: string,
    update: {
      state?: BookingState;
      externalReference?: string | null;
      attemptCount?: number;
      updatedAt: string;
      reconciliation?: {
        status: ReconciliationStatus;
        started_at: string;
        lookup_attempts: number;
        last_lookup_at?: string | null;
        escalation_reason?: string | null;
      } | null;
    },
  ): void {
    const sets: string[] = ['updated_at = ?'];
    const params: Array<string | number | null> = [update.updatedAt];
    if (update.state !== undefined) {
      sets.push('state = ?');
      params.push(update.state);
    }
    if (update.externalReference !== undefined) {
      sets.push('external_reference = ?');
      params.push(update.externalReference);
    }
    if (update.attemptCount !== undefined) {
      sets.push('attempt_count = ?');
      params.push(update.attemptCount);
    }
    if (update.reconciliation !== undefined) {
      sets.push(
        'reconciliation_status = ?',
        'reconciliation_started_at = ?',
        'reconciliation_lookup_attempts = ?',
        'reconciliation_last_lookup_at = ?',
        'reconciliation_escalation_reason = ?',
      );
      params.push(
        update.reconciliation?.status ?? null,
        update.reconciliation?.started_at ?? null,
        update.reconciliation?.lookup_attempts ?? null,
        update.reconciliation?.last_lookup_at ?? null,
        update.reconciliation?.escalation_reason ?? null,
      );
    }
    params.push(bookingRequestId);
    this.db.prepare(`UPDATE booking_requests SET ${sets.join(', ')} WHERE booking_request_id = ?`).run(...params);
  }

  insertAttempt(bookingRequestId: string, attempt: BookingAttemptRow): void {
    this.db
      .prepare(
        `INSERT INTO booking_attempts
           (booking_request_id, attempt, idempotency_key, result, http_status, raw_response_ref, started_at, finished_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        bookingRequestId,
        attempt.attempt,
        attempt.idempotency_key,
        attempt.result,
        attempt.http_status,
        attempt.raw_response_ref,
        attempt.started_at,
        attempt.finished_at,
      );
  }

  listAttempts(bookingRequestId: string): BookingAttemptRow[] {
    const rows = this.db
      .prepare('SELECT * FROM booking_attempts WHERE booking_request_id = ? ORDER BY attempt')
      .all(bookingRequestId) as Row[];
    return rows.map((row) => ({
      attempt: asNumber(row.attempt),
      idempotency_key: asString(row.idempotency_key),
      started_at: asString(row.started_at),
      finished_at: row.finished_at === null ? null : asString(row.finished_at),
      result: asString(row.result) as BookingAttemptResult,
      http_status: row.http_status === null ? null : asNumber(row.http_status),
      raw_response_ref: row.raw_response_ref === null ? null : asString(row.raw_response_ref),
    }));
  }

  countAppointmentsForSlot(slotId: string): number {
    const row = this.db
      .prepare('SELECT COUNT(*) AS n FROM appointments WHERE slot_id = ?')
      .get(slotId) as Row | undefined;
    return asNumber(row?.n);
  }

  /* ---------------------------------------------------------- appointments */

  insertAppointment(appointment: AppointmentRecordShape & { payload?: unknown }): void {
    this.db
      .prepare(
        `INSERT INTO appointments
           (appointment_id, booking_request_id, hospital_id, provider_id, patient_id, slot_id, start_at, end_at,
            display_timezone, state, verified, verification_method, synthetic, payload)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        appointment.appointment_id,
        appointment.booking_request_id,
        appointment.hospital_id,
        appointment.provider_id,
        appointment.patient_id,
        appointment.slot_id ?? null,
        appointment.start_at,
        appointment.end_at ?? null,
        appointment.display_timezone,
        appointment.state,
        appointment.verified ? 1 : 0,
        appointment.verification_method ?? null,
        appointment.synthetic ? 1 : 0,
        appointment.payload === undefined ? null : JSON.stringify(appointment.payload),
      );
  }

  getAppointmentByBooking(bookingRequestId: string): AppointmentRecordShape | undefined {
    const row = this.db
      .prepare('SELECT * FROM appointments WHERE booking_request_id = ? ORDER BY rowid DESC LIMIT 1')
      .get(bookingRequestId) as Row | undefined;
    return row ? hydrateAppointment(row) : undefined;
  }

  getAppointmentBySlot(slotId: string): AppointmentRecordShape | undefined {
    const row = this.db
      .prepare("SELECT * FROM appointments WHERE slot_id = ? AND state NOT IN ('CANCELLED','REJECTED') ORDER BY rowid DESC LIMIT 1")
      .get(slotId) as Row | undefined;
    return row ? hydrateAppointment(row) : undefined;
  }

  updateAppointmentState(
    appointmentId: string,
    update: { state: BookingState; verified?: boolean; verificationMethod?: 'create_response' | 'lookup_request' },
  ): void {
    const sets = ['state = ?'];
    const params: Array<string | number> = [update.state];
    if (update.verified !== undefined) {
      sets.push('verified = ?');
      params.push(update.verified ? 1 : 0);
    }
    if (update.verificationMethod !== undefined) {
      sets.push('verification_method = ?');
      params.push(update.verificationMethod);
    }
    params.push(appointmentId);
    this.db.prepare(`UPDATE appointments SET ${sets.join(', ')} WHERE appointment_id = ?`).run(...params);
  }

  /* ------------------------------------------------------------ idempotency */

  recordIdempotencyKey(
    key: string,
    scope: string,
    requestHash: string,
    bookingRequestId: string | null,
    now: string,
  ): void {
    this.db
      .prepare(
        `INSERT INTO idempotency_keys (idempotency_key, scope, request_hash, booking_request_id, created_at, last_seen_at)
         VALUES (?, ?, ?, ?, ?, ?)`,
      )
      .run(key, scope, requestHash, bookingRequestId, now, now);
  }

  getIdempotencyKey(key: string): Row | undefined {
    return this.db.prepare('SELECT * FROM idempotency_keys WHERE idempotency_key = ?').get(key) as Row | undefined;
  }

  touchIdempotencyKey(key: string, now: string): void {
    this.db.prepare('UPDATE idempotency_keys SET last_seen_at = ? WHERE idempotency_key = ?').run(now, key);
  }

  /* ---------------------------------------------------------------- outbox */

  insertOutboxEntry(entry: OutboxRow & { payload?: unknown }): void {
    this.db
      .prepare(
        `INSERT INTO notification_outbox
           (outbox_id, booking_request_id, channel, recipient, template_key, status, attempts, next_attempt_at, last_error, created_at, payload)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        entry.outbox_id,
        entry.booking_request_id,
        entry.channel,
        entry.recipient,
        entry.template_key,
        entry.status,
        entry.attempts,
        entry.next_attempt_at,
        entry.last_error,
        entry.created_at,
        entry.payload === undefined ? null : JSON.stringify(entry.payload),
      );
  }

  listOutbox(filter: { bookingRequestId?: string; status?: string } = {}): OutboxRow[] {
    const conditions: string[] = [];
    const params: string[] = [];
    if (filter.bookingRequestId) {
      conditions.push('booking_request_id = ?');
      params.push(filter.bookingRequestId);
    }
    if (filter.status) {
      conditions.push('status = ?');
      params.push(filter.status);
    }
    const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
    const rows = this.db
      .prepare(`SELECT * FROM notification_outbox ${where} ORDER BY created_at`)
      .all(...params) as Row[];
    return rows.map(hydrateOutbox);
  }

  getOutboxEntry(outboxId: string): OutboxRow | undefined {
    const row = this.db.prepare('SELECT * FROM notification_outbox WHERE outbox_id = ?').get(outboxId) as Row | undefined;
    return row ? hydrateOutbox(row) : undefined;
  }

  updateOutboxEntry(
    outboxId: string,
    update: {
      status: OutboxStatus;
      attempts: number;
      last_error?: string | null;
      next_attempt_at?: string | null;
    },
  ): void {
    this.db
      .prepare(
        'UPDATE notification_outbox SET status = ?, attempts = ?, last_error = ?, next_attempt_at = ? WHERE outbox_id = ?',
      )
      .run(update.status, update.attempts, update.last_error ?? null, update.next_attempt_at ?? null, outboxId);
  }

  /* ----------------------------------------------------------- vendor calls */

  recordVendorCall(call: {
    hospitalId: string;
    adapterType: string;
    operation: string;
    idempotencyKey: string | null;
    request: unknown;
    response: unknown;
    httpStatus: number | null;
    faultType: FaultType | null;
    createdAt: string;
  }): number {
    const result = this.db
      .prepare(
        `INSERT INTO vendor_calls
           (hospital_id, adapter_type, operation, idempotency_key, request, response, http_status, fault_type, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        call.hospitalId,
        call.adapterType,
        call.operation,
        call.idempotencyKey,
        JSON.stringify(call.request),
        call.response === null || call.response === undefined ? null : JSON.stringify(call.response),
        call.httpStatus,
        call.faultType,
        call.createdAt,
      );
    return asNumber((result as Row).lastInsertRowid);
  }

  countVendorCalls(filter: { idempotencyKey: string; operation: string }): number {
    const row = this.db
      .prepare('SELECT COUNT(*) AS n FROM vendor_calls WHERE idempotency_key = ? AND operation = ?')
      .get(filter.idempotencyKey, filter.operation) as Row | undefined;
    return asNumber(row?.n);
  }

  listVendorCalls(filter: { idempotencyKey?: string; hospitalId?: string; operation?: string } = {}): Row[] {
    const conditions: string[] = [];
    const params: string[] = [];
    if (filter.idempotencyKey) {
      conditions.push('idempotency_key = ?');
      params.push(filter.idempotencyKey);
    }
    if (filter.hospitalId) {
      conditions.push('hospital_id = ?');
      params.push(filter.hospitalId);
    }
    if (filter.operation) {
      conditions.push('operation = ?');
      params.push(filter.operation);
    }
    const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
    return this.db.prepare(`SELECT * FROM vendor_calls ${where} ORDER BY vendor_call_id`).all(...params) as Row[];
  }

  /* ------------------------------------------------------------ fault state */

  nextRequestSequence(): number {
    const result = this.db
      .prepare('INSERT INTO request_log (method, path, seed, created_at) VALUES (?, ?, ?, ?)')
      .run('', '', 0, new Date().toISOString());
    return asNumber((result as Row).lastInsertRowid);
  }

  nextOccurrence(scenarioId: string, matchKey: string): number {
    const existing = this.db
      .prepare('SELECT count FROM fault_counters WHERE scenario_id = ? AND match_key = ?')
      .get(scenarioId, matchKey) as Row | undefined;
    const next = (existing ? asNumber(existing.count) : 0) + 1;
    this.db
      .prepare(
        `INSERT INTO fault_counters (scenario_id, match_key, count) VALUES (?, ?, ?)
         ON CONFLICT(scenario_id, match_key) DO UPDATE SET count = ?`,
      )
      .run(scenarioId, matchKey, next, next);
    return next;
  }

  /**
   * Hands one occurrence back.
   *
   * A fault fixture that names its subject (`stale_slot_409` names one slot)
   * describes a condition about THAT subject. A request about a different one
   * was served normally, so it must not spend the attempt the fixture was
   * authored to demonstrate — otherwise arming a scenario would be a
   * single-use resource whose one use went to an unrelated request, and the
   * demo that arms it would find it already spent.
   *
   * The decision's own event row goes with it: the fault log records faults
   * that were served, and `/_faults/state` would otherwise report a refusal
   * that never happened.
   */
  releaseOccurrence(scenarioId: string, matchKey: string, event: { requestSeq: number; occurrence: number; faultType: string }): void {
    this.db
      .prepare('UPDATE fault_counters SET count = count - 1 WHERE scenario_id = ? AND match_key = ? AND count > 0')
      .run(scenarioId, matchKey);
    this.db
      .prepare(
        'DELETE FROM fault_events WHERE scenario_id = ? AND request_seq = ? AND occurrence = ? AND fault_type = ?',
      )
      .run(scenarioId, event.requestSeq, event.occurrence, event.faultType);
  }

  peekOccurrence(scenarioId: string, matchKey: string): number {
    const row = this.db
      .prepare('SELECT count FROM fault_counters WHERE scenario_id = ? AND match_key = ?')
      .get(scenarioId, matchKey) as Row | undefined;
    return row ? asNumber(row.count) : 0;
  }

  recordFaultEvent(event: {
    scenarioId: string | null;
    faultType: FaultType;
    method: string;
    path: string;
    occurrence: number | null;
    requestSeq: number;
    seed: number;
    mode: string;
    createdAt: string;
  }): void {
    this.db
      .prepare(
        `INSERT INTO fault_events (scenario_id, fault_type, method, path, occurrence, request_seq, seed, mode, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        event.scenarioId,
        event.faultType,
        event.method,
        event.path,
        event.occurrence,
        event.requestSeq,
        event.seed,
        event.mode,
        event.createdAt,
      );
  }

  listFaultEvents(): Row[] {
    return this.db.prepare('SELECT * FROM fault_events ORDER BY fault_event_id').all() as Row[];
  }

  /**
   * Wipes the fault state and REWINDS the request sequence.
   *
   * Rewinding matters: the random mode draws from (seed, request sequence), so
   * a reset that left the counter where it was would make the run after a reset
   * un-reproducible — the exact property the reset exists to restore.
   */
  resetFaultState(): void {
    this.db.exec(
      `DELETE FROM fault_counters;
       DELETE FROM fault_events;
       DELETE FROM request_log;
       DELETE FROM sqlite_sequence WHERE name = 'request_log';`,
    );
  }

  resetEverything(): void {
    this.db.exec(
      `DELETE FROM appointments; DELETE FROM booking_attempts; DELETE FROM notification_outbox;
       DELETE FROM booking_requests; DELETE FROM idempotency_keys; DELETE FROM vendor_calls;
       DELETE FROM fault_counters; DELETE FROM fault_events; DELETE FROM request_log;
       UPDATE slots SET status = 'available', version = 0 WHERE status <> 'expired';`,
    );
  }

}

function idColumn(table: string): string {
  switch (table) {
    case 'locations':
      return 'location_id';
    case 'hospitals':
      return 'hospital_id';
    case 'providers':
      return 'provider_id';
    case 'insurance_plans':
      return 'plan_id';
    default:
      throw new Error(`No id column known for ${table}`);
  }
}

function hydrateSlot(row: Row): SlotRecord {
  const payload = parseJson<Record<string, unknown>>(row.payload);
  return {
    ...(payload as unknown as SlotShape),
    status: asString(row.status) as SlotRecord['status'],
    version: asNumber(row.version),
  };
}

function hydrateBooking(row: Row): BookingRequestShape {
  const reconciliation =
    row.reconciliation_status === null || row.reconciliation_status === undefined
      ? null
      : {
          status: asString(row.reconciliation_status) as ReconciliationStatus,
          started_at: asString(row.reconciliation_started_at),
          lookup_attempts: asNumber(row.reconciliation_lookup_attempts),
          last_lookup_at: row.reconciliation_last_lookup_at === null ? null : asString(row.reconciliation_last_lookup_at),
          escalation_reason:
            row.reconciliation_escalation_reason === null ? null : asString(row.reconciliation_escalation_reason),
        };
  return {
    booking_request_id: asString(row.booking_request_id),
    workflow_id: asString(row.workflow_id),
    command_id: asString(row.command_id),
    idempotency_key: asString(row.idempotency_key),
    payload_hash: asString(row.payload_hash),
    payload: parseJson<BookingRequestShape['payload']>(row.payload),
    approval_id: row.approval_id === null ? null : asString(row.approval_id),
    state: asString(row.state) as BookingState,
    adapter_type: asString(row.adapter_type) as BookingRequestShape['adapter_type'],
    external_reference: row.external_reference === null ? null : asString(row.external_reference),
    attempt_count: asNumber(row.attempt_count),
    reconciliation,
    created_at: asString(row.created_at),
    updated_at: asString(row.updated_at),
    reschedule_of: row.reschedule_of === null ? null : asString(row.reschedule_of),
  } as BookingRequestShape;
}

function hydrateAppointment(row: Row): AppointmentRecordShape {
  return {
    appointment_id: asString(row.appointment_id),
    booking_request_id: asString(row.booking_request_id),
    hospital_id: asString(row.hospital_id),
    provider_id: asString(row.provider_id),
    patient_id: asString(row.patient_id),
    slot_id: row.slot_id === null ? undefined : asString(row.slot_id),
    start_at: asString(row.start_at),
    end_at: row.end_at === null ? null : asString(row.end_at),
    display_timezone: asString(row.display_timezone),
    state: asString(row.state) as BookingState,
    verified: asBool(row.verified),
    verification_method:
      row.verification_method === null || row.verification_method === undefined
        ? undefined
        : (asString(row.verification_method) as AppointmentRecordShape['verification_method']),
    synthetic: true,
  };
}

function hydrateOutbox(row: Row): OutboxRow {
  return {
    outbox_id: asString(row.outbox_id),
    booking_request_id: asString(row.booking_request_id),
    channel: asString(row.channel) as NotificationChannel,
    recipient: row.recipient === null ? null : asString(row.recipient),
    template_key: row.template_key === null ? null : asString(row.template_key),
    status: asString(row.status) as OutboxStatus,
    attempts: asNumber(row.attempts),
    next_attempt_at: row.next_attempt_at === null ? null : asString(row.next_attempt_at),
    last_error: row.last_error === null ? null : asString(row.last_error),
    created_at: asString(row.created_at),
  };
}

export type { CoverageResultShape, DocumentKind };
