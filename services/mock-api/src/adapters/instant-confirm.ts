import type { Store } from '../db/store.js';
import { nowUtcIso } from '../domain/time.js';
import { UpstreamError, type AdapterCancelResult, type AdapterCreateInput, type AdapterLookupResult, type BookingAdapter, type NormalisedBooking } from './types.js';

/**
 * `instant_confirm` — a classic scheduling API.
 *
 * Vendor shape: a flat REST envelope with `ok` + a nested `appointment` object
 * carrying `appointment_ref` and an UPPERCASE single-shot status
 * (`SCHEDULED` / `DECLINED`). There is no queue and no review step: a 201 with
 * `SCHEDULED` IS the appointment.
 *
 * Status model: one-shot. SCHEDULED -> CONFIRMED, DECLINED -> REJECTED.
 */
export class InstantConfirmAdapter implements BookingAdapter {
  readonly adapter_type = 'instant_confirm' as const;

  constructor(private readonly store: Store) {}

  create(input: AdapterCreateInput): NormalisedBooking {
    const vendorRequest = {
      api_version: 'v2',
      client_key: input.idempotencyKey,
      site_code: vendorCode('SITE', input.hospitalId),
      practitioner: { practitioner_code: vendorCode('PRV', input.providerId) },
      slot: { start: input.startAt, end: input.endAt, tz_hint: input.displayTimezone },
      service_line: input.visitType === 'first_visit' ? 'NEW' : 'ESTABLISHED',
      patient_ref: input.patientId,
    };

    if (input.fault === 'malformed_schema') {
      // The vendor answers 200, but the body is not the documented shape.
      const malformed = { ok: true, appointment: { appointment_ref: null, status: 42 } };
      const ref = this.record(input, 'create', vendorRequest, malformed, 200, input.fault);
      throw new UpstreamError({
        httpStatus: 200,
        code: 'malformed_upstream_response',
        message: 'instant_confirm create response did not match the vendor schema',
        faultType: input.fault,
        rawResponseRef: ref,
      });
    }

    const declined = input.vendor_decision === 'decline';
    const vendorResponse = {
      ok: true,
      appointment: {
        appointment_ref: declined
          ? null
          : `NL-${input.startAt.slice(0, 10).replace(/-/g, '')}-${input.idempotencyKey.slice(-6).toUpperCase()}`,
        status: declined ? 'DECLINED' : 'SCHEDULED',
        slot: { start: input.startAt, end: input.endAt },
        confirmed_at: declined ? null : nowUtcIso(),
        decline_reason: declined ? 'PROVIDER_NOT_ACCEPTING_THIS_REFERRAL' : null,
        cancel_window_hours: 24,
      },
    };
    const ref = this.record(input, 'create', vendorRequest, vendorResponse, 201, input.fault);

    let duplicateDelivered = false;
    if (input.fault === 'duplicate_event') {
      // The same event is delivered a second time for the SAME client key.
      this.record(input, 'create', vendorRequest, vendorResponse, 201, input.fault, true);
      duplicateDelivered = true;
    }

    return {
      state: declined ? 'REJECTED' : 'CONFIRMED',
      external_reference: vendorResponse.appointment.appointment_ref,
      verified: !declined,
      verification_method: declined ? null : 'create_response',
      vendor_status: vendorResponse.appointment.status,
      raw_response_ref: ref,
      duplicate_delivered: duplicateDelivered,
    };
  }

  lookup(input: { idempotencyKey: string; hospitalId: string; externalReference: string | null; fault: string | null }): AdapterLookupResult {
    const vendorRequest = { api_version: 'v2', client_key: input.idempotencyKey };
    const found = input.externalReference !== null;
    const vendorResponse = found
      ? {
          ok: true,
          appointment: {
            appointment_ref: input.externalReference,
            status: 'SCHEDULED',
            slot: null,
          },
        }
      : { ok: true, appointment: null, not_found_reason: 'NO_SUCH_CLIENT_KEY' };
    const ref = this.recordRaw(input.hospitalId, 'lookup', vendorRequest, vendorResponse, 200, null);
    if (!found) {
      return {
        outcome: 'not_booked',
        state: null,
        external_reference: null,
        vendor_status: 'NOT_FOUND',
        raw_response_ref: ref,
      };
    }
    return {
      outcome: 'created',
      state: 'CONFIRMED',
      external_reference: input.externalReference,
      vendor_status: 'SCHEDULED',
      raw_response_ref: ref,
    };
  }

  cancel(input: { externalReference: string | null; hospitalId: string; idempotencyKey: string; fault: string | null }): AdapterCancelResult {
    const vendorRequest = { api_version: 'v2', appointment_ref: input.externalReference, client_key: input.idempotencyKey };
    const vendorResponse = { ok: true, appointment_ref: input.externalReference, status: 'CANCELLED' };
    const ref = this.recordRaw(input.hospitalId, 'cancel', vendorRequest, vendorResponse, 200, null);
    return {
      cancelled: true,
      state: 'CANCELLED',
      vendor_status: 'CANCELLED',
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

function vendorCode(prefix: string, uuid: string): string {
  return `${prefix}-${uuid.replace(/-/g, '').slice(0, 10).toUpperCase()}`;
}
