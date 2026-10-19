import { newUuid, payloadHash } from '../domain/ids.js';
import { nowUtcIso, toUtcIso } from '../domain/time.js';
import { conflict, notFound, AppError } from '../http/errors.js';
import type { MockApiConfig } from '../config.js';
import type {
  AppointmentRecordShape,
  BookingRequestShape,
  CreateBookingRequest,
  RescheduleResponse,
} from '../contracts/zod.js';
import type { BookingAttemptResult, BookingState, FaultType } from '../contracts/enums.js';
import type { BookingAttemptRow, OutboxRow, SlotRecord, Store } from '../db/store.js';
import type { AdapterRegistry } from '../adapters/registry.js';
import { UpstreamError } from '../adapters/types.js';
import type { FaultEngine } from '../faults/engine.js';
import { INTERNAL_OPERATIONS, type FaultDecision, type FaultSpec } from '../faults/types.js';
import { fixtureHints } from '../faults/plugin.js';
import type { OutboxWorker } from '../notifications/worker.js';

export interface BookingOutcome {
  outcome: BookingAttemptResult;
  booking: BookingRequestShape;
  appointment: AppointmentRecordShape | null;
  attempts: BookingAttemptRow[];
  idempotent_replay: boolean;
  httpStatus: number;
  /**
   * How long the transport must stall before answering. The wait lives in the
   * route, not here: blocking the event loop for the simulated 30s timeout
   * would freeze every other request and make concurrency untestable.
   */
  delayMs: number;
  retryAfterSeconds: number | null;
}

export interface CreateBookingCommand {
  body: CreateBookingRequest;
  idempotencyKey: string;
  decision: FaultDecision | null;
  workflowId: string;
  commandId: string;
  rescheduleOf?: string | null;
  vendorDecision?: 'accept' | 'decline';
  now?: Date;
}

const HTTP_STATUS_BY_OUTCOME: Record<BookingAttemptResult, number> = {
  created: 200,
  duplicate_replayed: 200,
  slot_conflict: 409,
  rejected_by_hospital: 200,
  timeout_before_commit: 504,
  timeout_after_commit: 504,
  malformed_response: 502,
  rate_limited: 429,
  upstream_unavailable: 503,
  outcome_unknown: 202,
};

export function statusForOutcome(outcome: BookingAttemptResult): number {
  return HTTP_STATUS_BY_OUTCOME[outcome];
}

/** Terminal-for-now states. `REQUESTED` is deliberately NOT one of them. */
const CANCELLABLE_STATES: BookingState[] = ['REQUESTED', 'PENDING_REVIEW', 'CONFIRMED', 'CANCEL_PENDING'];

/**
 * The version the stale-slot refusal reports as current: what the fixture
 * declares when it declares one, else one past the stored row. A refusal that
 * says "the slot moved on" has to name the version the caller re-presents
 * against, or the workflow it sends back to the board cannot do so.
 */
export function declaredCurrentVersion(fault: FaultSpec | null, storedVersion: number): number {
  const declared = fault?.current_version;
  return typeof declared === 'number' ? declared : storedVersion + 1;
}

/**
 * The keys a fault fixture uses to name the subject of its condition. The
 * fixture set declares exactly one (`stale_slot_409` names a slot), and the
 * rule is written for the mechanism rather than for that file: a condition
 * about a subject is a condition about THAT subject.
 */
const SUBJECT_KEYS = ['slot_id', 'provider_id', 'hospital_id'] as const;

export class BookingService {
  constructor(
    private readonly config: MockApiConfig,
    private readonly store: Store,
    private readonly adapters: AdapterRegistry,
    private readonly engine: FaultEngine,
    private readonly outbox: OutboxWorker,
  ) {}

  private faultType(decision: FaultDecision | null): FaultType | null {
    return decision?.fault.type ?? null;
  }

  /**
   * The decision, if it is about the slot this booking presents.
   *
   * An armed scenario that names a subject describes a condition about that
   * subject: the fixture says "this slot moved on", not "the next booking is
   * refused". Handing it to a booking for another slot would refuse a booking
   * the fixture says nothing about, and would spend the scenario's single
   * occurrence doing it — so the occurrence is given back to the counter and
   * the booking is created normally.
   */
  private decisionAbout(decision: FaultDecision | null, presented: BookingRequestShape['payload']): FaultDecision | null {
    if (!decision) return null;
    const mismatch = SUBJECT_KEYS.some((key) => {
      const declared = decision.fault[key];
      const requested = presented[key];
      return typeof declared === 'string' && typeof requested === 'string' && declared !== requested;
    });
    if (!mismatch) return decision;
    this.engine.release(decision);
    return null;
  }

  /** The hashed unit of the approve→book contract. */
  payloadFor(body: CreateBookingRequest): BookingRequestShape['payload'] {
    return {
      patient_id: body.patient_id,
      provider_id: body.provider_id,
      hospital_id: body.hospital_id,
      slot_id: body.slot_id,
      visit_type: body.visit_type,
      ...(body.document_ids ? { document_ids: body.document_ids } : {}),
      ...(body.contact_phone === undefined ? {} : { contact_phone: body.contact_phone }),
    };
  }

  /**
   * The write path.
   *
   * Order of operations, and why:
   *  1. replay check — same key + same payload returns the SAME booking, no new row.
   *  2. claim transaction — the slot is claimed and the booking row is written
   *     inside one BEGIN IMMEDIATE, with a conditional UPDATE as the arbiter.
   *  3. the hospital call.
   *  4. commit transaction — appointment + outbox + attempt land together.
   *  5. notifications, after the commit, so their failure is not the booking's problem.
   */
  createBooking(command: CreateBookingCommand): BookingOutcome {
    const now = command.now ?? new Date();
    const nowIso = toUtcIso(now);
    const payload = this.payloadFor(command.body);
    const hash = payloadHash(payload as unknown as Record<string, unknown>);
    const key = command.idempotencyKey;

    /* --- 1. replay ------------------------------------------------------ */
    const existing = this.store.findBookingByKey(key);
    if (existing) {
      return this.replay(existing, hash);
    }

    /* --- 2. directory + slot validation --------------------------------- */
    const hospital = this.store.getHospital(payload.hospital_id);
    if (!hospital) throw notFound('unknown_hospital', `No hospital ${payload.hospital_id}`);
    const provider = this.store.getProvider(payload.provider_id);
    if (!provider) throw notFound('unknown_provider', `No provider ${payload.provider_id}`);
    const slot = this.store.getSlot(payload.slot_id);
    if (!slot) throw notFound('unknown_slot', `No slot ${payload.slot_id}`);
    const hospitalPayload = JSON.parse(String(hospital.payload)) as { adapter_type: string; timezone: string; name: string };
    if (slot.provider_id !== payload.provider_id || slot.hospital_id !== payload.hospital_id) {
      throw new AppError({
        status: 400,
        code: 'slot_mismatch',
        message: 'The slot does not belong to the requested provider and hospital',
        detail: { slot_provider_id: slot.provider_id, slot_hospital_id: slot.hospital_id },
      });
    }
    if (slot.visit_type !== payload.visit_type) {
      throw new AppError({
        status: 400,
        code: 'visit_type_mismatch',
        message: 'The slot is offered for a different visit type',
        detail: { slot_visit_type: slot.visit_type, requested: payload.visit_type },
      });
    }

    /* --- 3. fault-driven paths that never commit ------------------------- */
    /* A fault fixture names the subject its condition is about — `stale_slot_409`
       names one slot — and a booking for a different slot is not the request that
       condition describes. It is served normally, and the occurrence the decision
       was holding goes back: see `decisionAbout`. */
    const decision = this.decisionAbout(command.decision, payload);
    const fault = this.faultType(decision);
    /* What the caller says it is holding. Presenting no version is not the same
       fact as presenting a stale one, and the envelope reports which of the two
       happened instead of a number nobody sent. */
    const presentedVersion = command.body.expected_slot_version ?? null;

    /* The stale-version refusal is the hospital's answer, not a race this
       service lost: the fault stands for "the slot moved on between the board
       and the booking", so it is refused WITHOUT running the claim. The
       conditional UPDATE never touches the row, the slot's version does not
       move, and no booking is written. */
    if (fault === 'stale_slot_409') {
      throw new AppError({
        status: 409,
        code: 'slot_conflict',
        message: 'The slot changed since it was presented; re-fetch availability before booking',
        detail: {
          attempt_result: 'slot_conflict',
          slot_id: payload.slot_id,
          slot_status: slot.status,
          slot_version: slot.version,
          /* The caller's own number, or null. */
          presented_version: presentedVersion,
          /* What the hospital reports the slot stands at now: the number the
             caller has to re-fetch and re-present against. */
          current_version: declaredCurrentVersion(decision?.fault ?? null, slot.version),
          stale_version_presented: true,
          // The fixture's own numbers stay visible where this service answers
          // from its own state instead of from the client's presentation.
          ...(decision ? { fault_params: fixtureHints(decision.fault) } : {}),
        },
        faultType: fault,
        scenarioId: decision?.scenario_id ?? null,
      });
    }

    // The request's own fault wins; an armed scenario that names the upstream
    // hop (`POST /internal/upstream/booking`) still applies when the inbound
    // route itself was not part of the scenario. Both timeouts are decided
    // against this, because both describe the hospital call, not the route.
    const upstream = this.engine.internalDecision(INTERNAL_OPERATIONS.upstreamBooking, decision);
    const upstreamFault = fault ?? upstream?.fault.type ?? null;

    // A fixture can mark an occurrence as `committed: true`: the hospital did
    // write it, so the retry is answered normally instead of timing out again.
    if (upstreamFault === 'timeout_before_commit' && upstream?.fault.committed !== true) {
      return this.timeoutBeforeCommit({ command, payload, hash, nowIso, slot, hospitalName: String(hospitalPayload.name) });
    }

    /* --- 4. claim transaction ------------------------------------------- */
    const bookingRequestId = newUuid();
    const booking: BookingRequestShape = {
      booking_request_id: bookingRequestId,
      workflow_id: command.workflowId,
      command_id: command.commandId,
      idempotency_key: key,
      payload_hash: hash,
      payload,
      approval_id: command.body.approval_id ?? null,
      state: 'REQUESTED',
      adapter_type: hospitalPayload.adapter_type as BookingRequestShape['adapter_type'],
      external_reference: null,
      attempt_count: 1,
      reconciliation: null,
      created_at: nowIso,
      updated_at: nowIso,
      reschedule_of: command.rescheduleOf ?? null,
    };

    const claim = this.store.transaction(() => {
      // A concurrent request with the same key may have won the race between
      // the replay check above and this transaction. The UNIQUE index on
      // idempotency_keys is the structural guard; this read is the fast path.
      const raced = this.store.findBookingByKey(key);
      if (raced) return { kind: 'race' as const, booking: raced };
      const claimed = this.store.claimSlot(payload.slot_id, presentedVersion);
      if (!claimed.claimed) return { kind: 'conflict' as const, slot: claimed.slot };
      this.store.insertBooking(booking);
      this.store.recordIdempotencyKey(key, 'booking_create', hash, bookingRequestId, nowIso);
      return { kind: 'claimed' as const };
    });

    if (claim.kind === 'race') return this.replay(claim.booking, hash);
    if (claim.kind === 'conflict') {
      /* Reached when the caller presented a version that was already behind the
         row — a real lost race, decided by the conditional UPDATE. The
         stale-version FAULT is refused above, before the claim, so this
         envelope never has to speak for a version nobody presented. */
      throw new AppError({
        status: 409,
        code: 'slot_conflict',
        message: 'The slot is no longer available',
        detail: {
          attempt_result: 'slot_conflict',
          slot_id: payload.slot_id,
          slot_status: claim.slot?.status ?? 'unknown',
          slot_version: claim.slot?.version ?? null,
          presented_version: presentedVersion,
          stale_version_presented: false,
          // The fixture's own numbers stay visible where this service answers
          // from its own state instead of from the client's presentation.
          ...(decision ? { fault_params: fixtureHints(decision.fault) } : {}),
        },
        faultType: decision?.fault.type ?? null,
        scenarioId: decision?.scenario_id ?? null,
      });
    }

    /* --- 5. the hospital call -------------------------------------------- */
    const adapter = this.adapters.for(booking.adapter_type);
    let normalised;
    try {
      normalised = adapter.create({
        idempotencyKey: key,
        hospitalId: payload.hospital_id,
        hospitalName: hospitalPayload.name,
        providerId: payload.provider_id,
        patientId: payload.patient_id,
        slotId: payload.slot_id,
        visitType: payload.visit_type,
        startAt: slot.start_at,
        endAt: slot.end_at,
        displayTimezone: slot.display_timezone,
        documentIds: payload.document_ids ?? [],
        fault: upstreamFault,
        vendor_decision: command.vendorDecision ?? 'accept',
      });
    } catch (error) {
      return this.handleUpstreamFailure({ error, booking, payload, nowIso, slot });
    }

    /* --- 6. commit: appointment + outbox + attempt, together -------------- */
    const commit = this.store.transaction(() => {
      const state = normalised.state;
      const appointment: AppointmentRecordShape = {
        appointment_id: newUuid(),
        booking_request_id: bookingRequestId,
        hospital_id: payload.hospital_id,
        provider_id: payload.provider_id,
        patient_id: payload.patient_id,
        slot_id: payload.slot_id,
        start_at: slot.start_at,
        end_at: slot.end_at,
        display_timezone: slot.display_timezone,
        state,
        verified: normalised.verified,
        ...(normalised.verification_method ? { verification_method: normalised.verification_method } : {}),
        synthetic: true,
      };

      if (state === 'REJECTED') {
        // The hospital refused: give the slot back, keep the record of the refusal.
        this.store.releaseSlot(payload.slot_id);
        this.store.insertAppointment(appointment);
        this.store.updateBookingState(bookingRequestId, {
          state: 'REJECTED',
          externalReference: normalised.external_reference,
          updatedAt: nowIso,
        });
        this.store.insertAttempt(bookingRequestId, {
          attempt: 1,
          idempotency_key: key,
          started_at: booking.created_at,
          finished_at: nowIso,
          result: 'rejected_by_hospital',
          http_status: 200,
          raw_response_ref: normalised.raw_response_ref,
        });
        return { appointment, outcome: 'rejected_by_hospital' as BookingAttemptResult, entries: [] as OutboxRow[] };
      }

      this.store.markSlotBooked(payload.slot_id);
      this.store.insertAppointment(appointment);
      this.store.updateBookingState(bookingRequestId, {
        state,
        externalReference: normalised.external_reference,
        updatedAt: nowIso,
      });

      const isTimeoutAfterCommit = upstreamFault === 'timeout_after_commit';
      if (isTimeoutAfterCommit) {
        // The hospital committed, the caller never heard about it. The booking
        // is real and UNVERIFIED, and reconciliation is what resolves it.
        this.store.updateBookingState(bookingRequestId, {
          state,
          updatedAt: nowIso,
          reconciliation: { status: 'pending', started_at: nowIso, lookup_attempts: 0 },
        });
      }

      const updated = this.store.findBookingById(bookingRequestId)!;
      const entries = this.outbox.buildEntries(updated, now);
      for (const entry of entries) {
        this.store.insertOutboxEntry(entry);
      }

      this.store.insertAttempt(bookingRequestId, {
        attempt: 1,
        idempotency_key: key,
        started_at: booking.created_at,
        finished_at: nowIso,
        result: isTimeoutAfterCommit ? 'timeout_after_commit' : 'created',
        http_status: isTimeoutAfterCommit ? null : 200,
        raw_response_ref: normalised.raw_response_ref,
      });

      if (normalised.duplicate_delivered) {
        // The hospital delivered the same event twice; the second delivery is
        // recorded as a replay and must NOT create a second appointment.
        this.store.insertAttempt(bookingRequestId, {
          attempt: 2,
          idempotency_key: key,
          started_at: nowIso,
          finished_at: nowIso,
          result: 'duplicate_replayed',
          http_status: null,
          raw_response_ref: normalised.raw_response_ref,
        });
      }

      return {
        appointment,
        outcome: (isTimeoutAfterCommit ? 'timeout_after_commit' : 'created') as BookingAttemptResult,
        entries,
      };
    });

    const fresh = this.store.findBookingById(bookingRequestId)!;
    const outcome = commit.outcome;

    if (upstreamFault === 'timeout_after_commit') {
      // Deliver nothing: the caller sees a timeout even though the write stuck.
      return {
        outcome,
        booking: fresh,
        appointment: this.store.getAppointmentByBooking(bookingRequestId) ?? commit.appointment,
        attempts: this.store.listAttempts(bookingRequestId),
        idempotent_replay: false,
        httpStatus: statusForOutcome(outcome),
        delayMs: this.decisionDelay(decision, 30_000),
        retryAfterSeconds: null,
      };
    }

    if (commit.entries.length > 0) {
      // Outside the transaction, on purpose: a notification provider failure
      // must not be able to roll the booking back.
      this.outbox.deliverForBooking(bookingRequestId, decision);
    }

    return {
      outcome,
      booking: fresh,
      appointment: this.store.getAppointmentByBooking(bookingRequestId) ?? commit.appointment,
      attempts: this.store.listAttempts(bookingRequestId),
      idempotent_replay: false,
      httpStatus: statusForOutcome(outcome),
      delayMs: this.decisionDelay(decision, 0),
      retryAfterSeconds: null,
    };
  }

  /* ------------------------------------------------------------------ */

  private replay(existing: BookingRequestShape, incomingHash: string): BookingOutcome {
    if (existing.payload_hash !== incomingHash) {
      throw conflict(
        'idempotency_key_reuse',
        'This idempotency key was already used for a different payload. Mint a new key instead of reusing this one.',
        {
          idempotency_key: existing.idempotency_key,
          booking_request_id: existing.booking_request_id,
          stored_payload_hash: existing.payload_hash,
          presented_payload_hash: incomingHash,
        },
      );
    }
    this.store.touchIdempotencyKey(existing.idempotency_key, nowUtcIso());
    const attempts = this.store.listAttempts(existing.booking_request_id);
    return {
      outcome: 'duplicate_replayed',
      booking: existing,
      appointment: this.store.getAppointmentByBooking(existing.booking_request_id) ?? null,
      attempts,
      idempotent_replay: true,
      httpStatus: 200,
      delayMs: 0,
      retryAfterSeconds: null,
    };
  }

  private handleUpstreamFailure(input: {
    error: unknown;
    booking: BookingRequestShape;
    payload: BookingRequestShape['payload'];
    nowIso: string;
    slot: SlotRecord;
  }): BookingOutcome {
    const { error, booking, payload, nowIso } = input;
    const key = booking.idempotency_key;
    let result: BookingAttemptResult = 'outcome_unknown';
    let httpStatus = 502;
    let rawResponseRef: string | null = null;
    let message = 'The hospital returned an unexpected response';
    let retryAfterSeconds: number | null = null;

    if (error instanceof UpstreamError) {
      rawResponseRef = error.rawResponseRef;
      if (error.code === 'malformed_upstream_response') {
        result = 'malformed_response';
        httpStatus = 502;
        message = 'The hospital response did not match its documented schema';
      } else if (error.httpStatus === 503) {
        result = 'upstream_unavailable';
        httpStatus = 503;
        message = 'The hospital scheduling system is unavailable';
      } else if (error.httpStatus === 429) {
        result = 'rate_limited';
        httpStatus = 429;
        retryAfterSeconds = error.retryAfterSeconds ?? 5;
        message = 'The hospital scheduling system is rate limiting this caller';
      } else if (error.httpStatus === 504) {
        result = 'outcome_unknown';
        httpStatus = 504;
        message = 'The hospital did not answer in time; the outcome is unknown';
      }
    }

    const reconciled = this.store.transaction(() => {
      // The hospital never took the slot, so the claim goes back to the pool.
      this.store.releaseSlot(payload.slot_id);
      this.store.insertAttempt(booking.booking_request_id, {
        attempt: 1,
        idempotency_key: key,
        started_at: booking.created_at,
        finished_at: nowIso,
        result,
        http_status: httpStatus,
        raw_response_ref: rawResponseRef,
      });
      this.store.updateBookingState(booking.booking_request_id, {
        updatedAt: nowIso,
        reconciliation: {
          status: 'resolved_not_booked',
          started_at: nowIso,
          lookup_attempts: 0,
          escalation_reason: `upstream failure: ${result}`,
        },
      });
      return this.store.findBookingById(booking.booking_request_id)!;
    });

    return {
      outcome: result,
      booking: reconciled,
      appointment: null,
      attempts: this.store.listAttempts(booking.booking_request_id),
      idempotent_replay: false,
      httpStatus,
      delayMs: 0,
      retryAfterSeconds,
    };
  }

  private timeoutBeforeCommit(input: {
    command: CreateBookingCommand;
    payload: BookingRequestShape['payload'];
    hash: string;
    nowIso: string;
    slot: SlotRecord;
    hospitalName: string;
  }): BookingOutcome {
    const { command, payload, hash, nowIso } = input;
    const key = command.idempotencyKey;
    const hospital = JSON.parse(String(this.store.getHospital(payload.hospital_id)?.payload)) as { adapter_type: string };
    const bookingRequestId = newUuid();
    const booking: BookingRequestShape = {
      booking_request_id: bookingRequestId,
      workflow_id: command.workflowId,
      command_id: command.commandId,
      idempotency_key: key,
      payload_hash: hash,
      payload,
      approval_id: command.body.approval_id ?? null,
      state: 'REQUESTED',
      adapter_type: hospital.adapter_type as BookingRequestShape['adapter_type'],
      external_reference: null,
      attempt_count: 1,
      reconciliation: {
        status: 'resolved_not_booked',
        started_at: nowIso,
        lookup_attempts: 0,
        escalation_reason: 'request timed out before the hospital committed anything',
      },
      created_at: nowIso,
      updated_at: nowIso,
      reschedule_of: command.rescheduleOf ?? null,
    };

    // Nothing is claimed and nothing is committed: the slot stays available for
    // anyone else, and the key exists only so the caller can reconcile.
    const stored = this.store.transaction(() => {
      this.store.insertBooking(booking);
      this.store.recordIdempotencyKey(key, 'booking_create', hash, bookingRequestId, nowIso);
      this.store.insertAttempt(bookingRequestId, {
        attempt: 1,
        idempotency_key: key,
        started_at: nowIso,
        finished_at: nowIso,
        result: 'timeout_before_commit',
        http_status: null,
        raw_response_ref: null,
      });
      return this.store.findBookingById(bookingRequestId)!;
    });

    return {
      outcome: 'timeout_before_commit',
      booking: stored,
      appointment: null,
      attempts: this.store.listAttempts(bookingRequestId),
      idempotent_replay: false,
      httpStatus: statusForOutcome('timeout_before_commit'),
      delayMs: this.decisionDelay(command.decision, 30_000),
      retryAfterSeconds: null,
    };
  }

  private decisionDelay(decision: FaultDecision | null, fallback: number): number {
    const delay = decision?.fault.delay_ms;
    if (delay === undefined || delay === null) return fallback;
    // Tests set small delays; a negative or absurd one is clamped rather than trusted.
    return Math.min(Math.max(delay, 0), 120_000);
  }

  /* ------------------------------------------------------------- reconcile */

  /**
   * `lookup_request`. Answerable by the SAME idempotency key the caller already
   * holds, so an unknown outcome never requires minting a second key — which is
   * exactly how a duplicate booking gets created in the wild.
   */
  lookup(idempotencyKey: string, decision: FaultDecision | null): {
    outcome: BookingAttemptResult;
    booking: BookingRequestShape;
    appointment: AppointmentRecordShape | null;
    attempts: BookingAttemptRow[];
    reconciliation: BookingRequestShape['reconciliation'];
    lookup: { outcome: 'created' | 'not_booked' | 'unknown'; state: BookingState | null; external_reference: string | null; reconciled_at: string; looked_up_vendor_payload: string | null };
  } {
    const booking = this.store.findBookingByKey(idempotencyKey);
    if (!booking) {
      throw notFound('unknown_idempotency_key', 'No booking was ever created with this idempotency key', {
        idempotency_key: idempotencyKey,
      });
    }
    const nowIso = nowUtcIso();
    const adapter = this.adapters.for(booking.adapter_type);
    const lookup = adapter.lookup({
      idempotencyKey,
      hospitalId: booking.payload.hospital_id,
      externalReference: booking.external_reference ?? null,
      fault: decision?.fault.type ?? null,
    });

    const attemptsBefore = this.store.listAttempts(booking.booking_request_id);
    const priorLookups = booking.reconciliation?.lookup_attempts ?? 0;
    const existingAppointment = this.store.getAppointmentByBooking(booking.booking_request_id) ?? null;

    let state: BookingState = booking.state;
    let appointment = existingAppointment;
    let reconciliationStatus = booking.reconciliation?.status ?? null;
    const escalated = priorLookups + 1 >= 5 && lookup.outcome === 'unknown';

    if (lookup.outcome === 'created') {
      state = lookup.state ?? booking.state;
      if (existingAppointment) {
        this.store.updateAppointmentState(existingAppointment.appointment_id, {
          state,
          verified: true,
          verificationMethod: 'lookup_request',
        });
        appointment = { ...existingAppointment, state, verified: true, verification_method: 'lookup_request' };
      } else {
        // The hospital has the booking but we never wrote it: finish the job.
        const slot = this.store.getSlot(booking.payload.slot_id);
        const recovered: AppointmentRecordShape = {
          appointment_id: newUuid(),
          booking_request_id: booking.booking_request_id,
          hospital_id: booking.payload.hospital_id,
          provider_id: booking.payload.provider_id,
          patient_id: booking.payload.patient_id,
          slot_id: booking.payload.slot_id,
          start_at: slot?.start_at ?? nowIso,
          end_at: slot?.end_at ?? null,
          display_timezone: slot?.display_timezone ?? 'UTC',
          state,
          verified: true,
          verification_method: 'lookup_request',
          synthetic: true,
        };
        this.store.insertAppointment(recovered);
        if (slot) this.store.markSlotBooked(slot.slot_id);
        appointment = recovered;
      }
      reconciliationStatus = 'resolved_booked';
    } else if (lookup.outcome === 'not_booked') {
      reconciliationStatus = 'resolved_not_booked';
      if (existingAppointment) {
        this.store.updateAppointmentState(existingAppointment.appointment_id, { state: 'CANCELLED', verified: false });
        appointment = { ...existingAppointment, state: 'CANCELLED', verified: false };
      }
      // Nothing live claims the slot any more, so it goes back to the pool.
      this.store.releaseSlot(booking.payload.slot_id);
    } else {
      reconciliationStatus = escalated ? 'escalated' : 'pending';
    }

    this.store.updateBookingState(booking.booking_request_id, {
      ...(lookup.outcome === 'created' ? { state } : {}),
      updatedAt: nowIso,
      reconciliation: {
        status: reconciliationStatus ?? 'pending',
        started_at: booking.reconciliation?.started_at ?? nowIso,
        lookup_attempts: priorLookups + 1,
        last_lookup_at: nowIso,
        escalation_reason: escalated ? 'no answer from the hospital after repeated lookups' : null,
      },
    });

    // The outcome reported by a lookup is the RESOLVED one. While the
    // reconciliation is still pending the answer is `outcome_unknown` — which
    // is not a rejection, and is never rounded down to one.
    const resolvedOutcome: BookingAttemptResult =
      reconciliationStatus === 'resolved_booked'
        ? 'created'
        : reconciliationStatus === 'pending' || reconciliationStatus === 'escalated'
          ? 'outcome_unknown'
          : (attemptsBefore[attemptsBefore.length - 1]?.result ?? 'outcome_unknown');

    const updated = this.store.findBookingById(booking.booking_request_id)!;
    return {
      outcome: resolvedOutcome,
      booking: updated,
      appointment,
      attempts: attemptsBefore,
      reconciliation: updated.reconciliation,
      lookup: {
        outcome: lookup.outcome,
        state: lookup.state,
        external_reference: lookup.external_reference,
        reconciled_at: nowIso,
        looked_up_vendor_payload: lookup.raw_response_ref,
      },
    };
  }

  /* ---------------------------------------------------------------- cancel */

  cancel(bookingRequestId: string, options: { reason?: string; decision: FaultDecision | null }): BookingOutcome {
    const booking = this.store.findBookingById(bookingRequestId);
    if (!booking) throw notFound('unknown_booking_request', `No booking ${bookingRequestId}`);
    const nowIso = nowUtcIso();
    const appointment = this.store.getAppointmentByBooking(bookingRequestId) ?? null;

    if (booking.state === 'CANCELLED') {
      return {
        outcome: 'duplicate_replayed',
        booking,
        appointment,
        attempts: this.store.listAttempts(bookingRequestId),
        idempotent_replay: true,
        httpStatus: 200,
        delayMs: 0,
        retryAfterSeconds: null,
      };
    }
    if (!CANCELLABLE_STATES.includes(booking.state)) {
      throw conflict('not_cancellable', `A booking in state ${booking.state} cannot be cancelled`, {
        state: booking.state,
      });
    }

    this.store.transaction(() => {
      this.store.updateBookingState(bookingRequestId, { state: 'CANCEL_PENDING', updatedAt: nowIso });
      if (appointment) this.store.updateAppointmentState(appointment.appointment_id, { state: 'CANCEL_PENDING' });
    });

    const adapter = this.adapters.for(booking.adapter_type);
    const result = adapter.cancel({
      externalReference: booking.external_reference ?? null,
      hospitalId: booking.payload.hospital_id,
      idempotencyKey: booking.idempotency_key,
      fault: options.decision?.fault.type ?? null,
    });

    const committed = this.store.transaction(() => {
      this.store.updateBookingState(bookingRequestId, { state: 'CANCELLED', updatedAt: nowIso });
      const current = this.store.getAppointmentByBooking(bookingRequestId);
      if (current) this.store.updateAppointmentState(current.appointment_id, { state: 'CANCELLED', verified: true });
      if (result.cancelled) this.store.releaseSlot(booking.payload.slot_id);
      return this.store.findBookingById(bookingRequestId)!;
    });

    return {
      outcome: 'created',
      booking: committed,
      appointment: this.store.getAppointmentByBooking(bookingRequestId) ?? null,
      attempts: this.store.listAttempts(bookingRequestId),
      idempotent_replay: false,
      httpStatus: 200,
      delayMs: 0,
      retryAfterSeconds: null,
    };
  }

  /* ------------------------------------------------------------ reschedule */

  /**
   * §2.5: the old booking is cancelled only AFTER the replacement is verified.
   * If the new slot turns out to be taken, the patient still has their original
   * appointment — the failure mode this ordering exists to prevent is a patient
   * left with nothing because the system cancelled first and booked second.
   */
  reschedule(
    bookingRequestId: string,
    body: { new_slot_id: string; idempotency_key: string; expected_slot_version?: number; reason?: string },
    options: { decision: FaultDecision | null; vendorDecision?: 'accept' | 'decline' },
  ): {
    outcome: BookingAttemptResult;
    booking: BookingRequestShape;
    appointment: AppointmentRecordShape | null;
    superseded_booking: BookingRequestShape | null;
    superseded_appointment: AppointmentRecordShape | null;
    idempotent_replay: boolean;
    old_booking_cancelled: boolean;
    cancellation_deferred_reason: string | null;
    httpStatus: number;
    delayMs: number;
    retryAfterSeconds: number | null;
  } {
    const original = this.store.findBookingById(bookingRequestId);
    if (!original) throw notFound('unknown_booking_request', `No booking ${bookingRequestId}`);
    if (original.state === 'CANCELLED' || original.state === 'REJECTED') {
      throw conflict('not_reschedulable', `A booking in state ${original.state} cannot be rescheduled`, {
        state: original.state,
      });
    }

    const created = this.createBooking({
      body: {
        patient_id: original.payload.patient_id,
        provider_id: original.payload.provider_id,
        hospital_id: original.payload.hospital_id,
        slot_id: body.new_slot_id,
        visit_type: original.payload.visit_type,
        ...(original.payload.document_ids ? { document_ids: original.payload.document_ids } : {}),
        ...(original.payload.contact_phone === undefined ? {} : { contact_phone: original.payload.contact_phone }),
        idempotency_key: body.idempotency_key,
        ...(body.expected_slot_version !== undefined ? { expected_slot_version: body.expected_slot_version } : {}),
      },
      idempotencyKey: body.idempotency_key,
      decision: options.decision,
      workflowId: original.workflow_id,
      commandId: newUuid(),
      rescheduleOf: original.booking_request_id,
      ...(options.vendorDecision ? { vendorDecision: options.vendorDecision } : {}),
    });

    const verified = created.appointment?.verified === true && created.appointment.state === 'CONFIRMED';
    if (!verified) {
      return {
        outcome: created.outcome,
        booking: created.booking,
        appointment: created.appointment,
        superseded_booking: null,
        superseded_appointment: null,
        idempotent_replay: created.idempotent_replay,
        old_booking_cancelled: false,
        cancellation_deferred_reason:
          created.appointment === null
            ? 'the replacement booking was not created, so the original stays in force'
            : `the replacement is in state ${created.appointment.state} and is not verified yet`,
        httpStatus: created.httpStatus,
        delayMs: created.delayMs,
        retryAfterSeconds: created.retryAfterSeconds,
      };
    }

    const cancelled = this.cancel(original.booking_request_id, { decision: options.decision, ...(body.reason ? { reason: body.reason } : {}) });
    return {
      outcome: created.outcome,
      booking: created.booking,
      appointment: created.appointment,
      superseded_booking: cancelled.booking,
      superseded_appointment: this.store.getAppointmentByBooking(original.booking_request_id) ?? null,
      idempotent_replay: created.idempotent_replay,
      old_booking_cancelled: true,
      cancellation_deferred_reason: null,
      httpStatus: created.httpStatus,
      delayMs: created.delayMs,
      retryAfterSeconds: created.retryAfterSeconds,
    };
  }

  /* ---------------------------------------------------------------- reads */

  getBooking(bookingRequestId: string): {
    booking: BookingRequestShape;
    appointment: AppointmentRecordShape | null;
    attempts: BookingAttemptRow[];
  } {
    const booking = this.store.findBookingById(bookingRequestId);
    if (!booking) throw notFound('unknown_booking_request', `No booking ${bookingRequestId}`);
    return {
      booking,
      appointment: this.store.getAppointmentByBooking(bookingRequestId) ?? null,
      attempts: this.store.listAttempts(bookingRequestId),
    };
  }

  listBookings(filter: { patientId?: string; state?: string }): BookingRequestShape[] {
    return this.store.listBookings(filter);
  }
}

export type { RescheduleResponse };
