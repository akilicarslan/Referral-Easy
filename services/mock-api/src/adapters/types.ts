import type { BookingState, FaultType, HospitalAdapterType, VisitType } from '../contracts/enums.js';

/** What the booking pipeline hands to an adapter. Vendor-agnostic on purpose. */
export interface AdapterCreateInput {
  idempotencyKey: string;
  hospitalId: string;
  hospitalName: string;
  providerId: string;
  patientId: string;
  slotId: string;
  visitType: VisitType;
  startAt: string;
  endAt: string;
  displayTimezone: string;
  documentIds: string[];
  fault: FaultType | null;
  /**
   * Demo control (`x-vendor-decision: decline`). Both hospitals must be able to
   * refuse, otherwise the REJECTED state and the `rejected_by_hospital` attempt
   * result would be unreachable and untestable.
   */
  vendor_decision: 'accept' | 'decline';
}

/**
 * The ONE internal shape every hospital must normalise to. If a vendor status
 * or vendor field name ever appears here, the adapter boundary has leaked.
 */
export interface NormalisedBooking {
  state: BookingState;
  external_reference: string | null;
  verified: boolean;
  verification_method: 'create_response' | 'lookup_request' | null;
  /** Vendor status BEFORE normalisation, kept for evidence only. */
  vendor_status: string;
  /** Id of the stored raw vendor payload; never shown to a caller. */
  raw_response_ref: string;
  /** True when the hospital delivered the same event twice for one key. */
  duplicate_delivered: boolean;
}

export interface AdapterLookupResult {
  outcome: 'created' | 'not_booked' | 'unknown';
  state: BookingState | null;
  external_reference: string | null;
  vendor_status: string;
  raw_response_ref: string;
}

export interface AdapterCancelResult {
  cancelled: boolean;
  state: BookingState;
  vendor_status: string;
  raw_response_ref: string;
}

export interface BookingAdapter {
  readonly adapter_type: HospitalAdapterType;
  create(input: AdapterCreateInput): NormalisedBooking;
  lookup(input: { idempotencyKey: string; hospitalId: string; externalReference: string | null; fault: FaultType | null }): AdapterLookupResult;
  cancel(input: { externalReference: string | null; hospitalId: string; idempotencyKey: string; fault: FaultType | null }): AdapterCancelResult;
}

/** Transport-level failure raised by the simulated hospital edge. */
export class UpstreamError extends Error {
  readonly httpStatus: number;
  readonly code: string;
  readonly faultType: FaultType | null;
  readonly retryAfterSeconds: number | null;
  readonly rawResponseRef: string | null;

  constructor(init: {
    httpStatus: number;
    code: string;
    message: string;
    faultType?: FaultType | null;
    retryAfterSeconds?: number | null;
    rawResponseRef?: string | null;
  }) {
    super(init.message);
    this.name = 'UpstreamError';
    this.httpStatus = init.httpStatus;
    this.code = init.code;
    this.faultType = init.faultType ?? null;
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
    this.rawResponseRef = init.rawResponseRef ?? null;
  }
}
