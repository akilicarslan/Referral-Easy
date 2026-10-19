import type { Store } from '../db/store.js';
import { nowUtcIso } from '../domain/time.js';
import { UpstreamError, type AdapterCancelResult, type AdapterCreateInput, type AdapterLookupResult, type BookingAdapter, type NormalisedBooking } from './types.js';

/**
 * `review_required` — a referral intake queue.
 *
 * Deliberately nothing like the other vendor: the envelope is a nested `case`
 * wrapper, the request is expressed as a WINDOW rather than a single slot, the
 * identifying field is a queue ticket instead of a client key, and the response
 * answers 202 with a QUEUE STATE rather than an appointment.
 *
 * Status model: multi-step. AWAITING_REVIEW is a real, long-lived state and it
 * is not a confirmation. AWAITING_REVIEW -> PENDING_REVIEW, ACCEPTED ->
 * CONFIRMED (only once a decision exists), REFUSED -> REJECTED.
 *
 * Both vendors must reach `NormalisedBooking` with identical field names; no
 * `queue_state`, `case_number` or `service_category` may escape this file.
 */
export class ReviewRequiredAdapter implements BookingAdapter {
  readonly adapter_type = 'review_required' as const;

  constructor(private readonly store: Store) {}

  create(input: AdapterCreateInput): NormalisedBooking {
    const vendorRequest = {
      case: {
        patient_ref: input.patientId,
        provider_ref: vendorRef('SHP', input.providerId),
        facility_ref: vendorRef('FAC', input.hospitalId),
        referral_ticket: input.idempotencyKey,
        requested_window: { from: input.startAt, to: input.endAt, tz_hint: input.displayTimezone },
        service_category: input.visitType === 'first_visit' ? 'SPECIALIST_NEW' : 'SPECIALIST_FOLLOW_UP',
        attachments: input.documentIds.map((documentId) => ({ document_ref: documentId })),
      },
    };

    if (input.fault === 'malformed_schema') {
      const malformed = { ticket_status: 'QUEUED' };
      const ref = this.record(input, 'create', vendorRequest, malformed, 202, input.fault);
      throw new UpstreamError({
        httpStatus: 200,
        code: 'malformed_upstream_response',
        message: 'review_required intake response did not match the vendor schema',
        faultType: input.fault,
        rawResponseRef: ref,
      });
    }

    const declined = input.vendor_decision === 'decline';
    const caseNumber = `SA-${input.startAt.slice(0, 4)}-${input.idempotencyKey.slice(-5).toUpperCase()}`;
    const vendorResponse = {
      ticket_status: 'QUEUED',
      case_number: caseNumber,
      decision: declined
        ? { queue_state: 'REFUSED', decided_at: nowUtcIso(), sla_hours: 48, refusal_code: 'OUT_OF_CATCHMENT' }
        : { queue_state: 'AWAITING_REVIEW', decided_at: null, sla_hours: 48 },
    };
    const ref = this.record(input, 'create', vendorRequest, vendorResponse, 202, input.fault);

    let duplicateDelivered = false;
    if (input.fault === 'duplicate_event') {
      this.record(input, 'create', vendorRequest, vendorResponse, 202, input.fault, true);
      duplicateDelivered = true;
    }

    // The case exists upstream, but there is NO appointment yet: the intake
    // queue is the hospital's answer, and it is not a confirmation. A refused
    // case is a definite answer, so it normalises to REJECTED.
    return {
      state: declined ? 'REJECTED' : 'PENDING_REVIEW',
      external_reference: caseNumber,
      verified: false,
      verification_method: null,
      vendor_status: vendorResponse.decision.queue_state,
      raw_response_ref: ref,
      duplicate_delivered: duplicateDelivered,
    };
  }

  lookup(input: { idempotencyKey: string; hospitalId: string; externalReference: string | null; fault: string | null }): AdapterLookupResult {
    const vendorRequest = { lookup_by: 'referral_ticket', referral_ticket: input.idempotencyKey };
    const found = input.externalReference !== null;
    const vendorResponse = found
      ? {
          case_number: input.externalReference,
          ticket_status: 'PROCESSING',
          decision: { queue_state: 'AWAITING_REVIEW', decided_at: null, sla_hours: 48 },
        }
      : { case_number: null, ticket_status: 'UNKNOWN_TICKET', decision: null };
    const ref = this.recordRaw(input.hospitalId, 'lookup', vendorRequest, vendorResponse, 200, null, input.idempotencyKey);
    if (!found) {
      return {
        outcome: 'not_booked',
        state: null,
        external_reference: null,
        vendor_status: 'UNKNOWN_TICKET',
        raw_response_ref: ref,
      };
    }
    return {
      outcome: 'created',
      state: 'PENDING_REVIEW',
      external_reference: input.externalReference,
      vendor_status: 'AWAITING_REVIEW',
      raw_response_ref: ref,
    };
  }

  cancel(input: { externalReference: string | null; hospitalId: string; idempotencyKey: string; fault: string | null }): AdapterCancelResult {
    const vendorRequest = { case_number: input.externalReference, withdraw_reason: 'PATIENT_REQUEST' };
    const vendorResponse = { case_number: input.externalReference, ticket_status: 'WITHDRAWN', decision: { queue_state: 'WITHDRAWN' } };
    const ref = this.recordRaw(input.hospitalId, 'cancel', vendorRequest, vendorResponse, 200, null, input.idempotencyKey);
    return {
      cancelled: true,
      state: 'CANCELLED',
      vendor_status: 'WITHDRAWN',
      raw_response_ref: ref,
    };
  }

  private record(
    input: AdapterCreateInput,
    operation: string,
    request: unknown,
    response: unknown,
    httpStatus: number,
    faultType: string | null,
    duplicate = false,
  ): string {
    return this.recordRaw(input.hospitalId, duplicate ? `${operation}:duplicate` : operation, request, response, httpStatus, faultType, input.idempotencyKey);
  }

  private recordRaw(
    hospitalId: string,
    operation: string,
    request: unknown,
    response: unknown,
    httpStatus: number,
    faultType: string | null,
    idempotencyKey: string | null = null,
  ): string {
    const id = this.store.recordVendorCall({
      hospitalId,
      adapterType: this.adapter_type,
      operation,
      idempotencyKey,
      request,
      response,
      httpStatus,
      faultType: faultType as never,
      createdAt: nowUtcIso(),
    });
    return `vendor_call:${id}`;
  }
}

function vendorRef(prefix: string, uuid: string): string {
  return `${prefix}-${uuid.replace(/-/g, '').slice(0, 8).toUpperCase()}`;
}
