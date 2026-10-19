import { setTimeout as delay } from 'node:timers/promises';
import type { FastifyInstance, FastifyReply } from 'fastify';
import type { AppContext } from '../context.js';
import { AppError, badRequest, notFound } from '../http/errors.js';
import { parseOrThrow } from '../http/validate.js';
import { bookingResponse, cancelRequest, createBookingRequest, lookupResponse, rescheduleRequest } from '../contracts/zod.js';
import { deterministicUuid } from '../domain/ids.js';
import type { BookingOutcome } from '../booking/service.js';

const MIN_KEY_LENGTH = 8;

function resolveIdempotencyKey(header: string | string[] | undefined, body: { idempotency_key?: string } | undefined): string {
  const fromHeader = Array.isArray(header) ? header[0] : header;
  const key = (fromHeader ?? body?.idempotency_key ?? '').trim();
  if (key.length < MIN_KEY_LENGTH) {
    throw badRequest(
      'missing_idempotency_key',
      `An idempotency key of at least ${MIN_KEY_LENGTH} characters is required: send the \`idempotency-key\` header, or \`idempotency_key\` in the body.`,
    );
  }
  return key;
}

export function registerAppointments(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'post',
    path: '/appointments',
    summary: 'Create a booking request',
    description: [
      'The response `booking.state` is the ONLY authority on whether an appointment exists.',
      'A 200 with `PENDING_REVIEW` or `REQUESTED` is not a confirmation, and there is deliberately no boolean `confirmed` field to misread.',
      'Replaying the same idempotency key with the same payload returns the same booking; replaying it with a different payload is rejected.',
    ].join(' '),
    tags: ['appointments'],
    requestSchema: 'CreateBookingRequest',
    responseSchema: 'BookingResponse',
  });

  app.post('/appointments', async (request, reply) => {
    const body = parseOrThrow(createBookingRequest, request.body ?? {}, 'Booking request');
    const idempotencyKey = resolveIdempotencyKey(request.headers['idempotency-key'], body);
    const vendorDecisionHeader = request.headers['x-vendor-decision'];
    const vendorDecision =
      (Array.isArray(vendorDecisionHeader) ? vendorDecisionHeader[0] : vendorDecisionHeader) === 'decline'
        ? ('decline' as const)
        : ('accept' as const);

    const result = context.booking.createBooking({
      body,
      idempotencyKey,
      decision: request.faultDecision,
      workflowId: body.workflow_id ?? deterministicUuid(`workflow:${idempotencyKey}`),
      commandId: body.command_id ?? deterministicUuid(`command:${idempotencyKey}`),
      vendorDecision,
    });

    return respondWithOutcome(reply, result);
  });

  context.docs.describe({
    method: 'get',
    path: '/appointments/lookup_request',
    summary: 'Reconcile an unknown outcome using the SAME idempotency key',
    description:
      'This is the answer to "did my booking go through?". It never requires a second key, because a second key is how a patient ends up with two appointments.',
    tags: ['appointments'],
    query: [{ name: 'idempotency_key' }],
    responseSchema: 'LookupResponse',
  });

  app.get<{ Querystring: { idempotency_key?: string; key?: string } }>(
    '/appointments/lookup_request',
    async (request) => {
      const key = (request.query.idempotency_key ?? request.query.key ?? '').trim();
      if (key.length < MIN_KEY_LENGTH) {
        throw badRequest('missing_idempotency_key', 'idempotency_key is required and must be at least 8 characters');
      }
      const result = context.booking.lookup(key, request.faultDecision);
      return lookupResponse.parse({
        outcome: result.outcome,
        booking: result.booking,
        appointment: result.appointment,
        attempts: result.attempts,
        reconciliation: result.reconciliation,
        lookup: result.lookup,
      });
    },
  );

  context.docs.describe({
    method: 'get',
    path: '/appointments/{booking_request_id}',
    summary: 'Read one booking request',
    tags: ['appointments'],
    params: [{ name: 'booking_request_id' }],
    responseSchema: 'BookingResponse',
  });

  app.get<{ Params: { booking_request_id: string } }>('/appointments/:booking_request_id', async (request) => {
    const result = context.booking.getBooking(request.params.booking_request_id);
    return bookingResponse.parse({
      outcome: 'created',
      booking: result.booking,
      appointment: result.appointment,
      attempts: result.attempts,
      idempotent_replay: false,
    });
  });

  context.docs.describe({
    method: 'get',
    path: '/appointments',
    summary: 'List booking requests',
    tags: ['appointments'],
    query: [{ name: 'patient_id' }, { name: 'state' }],
  });

  app.get<{ Querystring: { patient_id?: string; state?: string } }>('/appointments', async (request) => {
    const bookings = context.booking.listBookings({
      ...(request.query.patient_id ? { patientId: request.query.patient_id } : {}),
      ...(request.query.state ? { state: request.query.state } : {}),
    });
    return { bookings, count: bookings.length };
  });

  context.docs.describe({
    method: 'post',
    path: '/appointments/{booking_request_id}/cancel',
    summary: 'Cancel an appointment',
    tags: ['appointments'],
    params: [{ name: 'booking_request_id' }],
    requestSchema: 'CancelRequest',
  });

  app.post<{ Params: { booking_request_id: string } }>(
    '/appointments/:booking_request_id/cancel',
    async (request) => {
      const body = parseOrThrow(cancelRequest, request.body ?? {}, 'Cancel request');
      const result = context.booking.cancel(request.params.booking_request_id, {
        decision: request.faultDecision,
        ...(body.reason ? { reason: body.reason } : {}),
      });
      return {
        outcome: result.outcome,
        booking: result.booking,
        appointment: result.appointment,
        idempotent_replay: result.idempotent_replay,
      };
    },
  );

  context.docs.describe({
    method: 'post',
    path: '/appointments/{booking_request_id}/reschedule',
    summary: 'Move an appointment to a new slot',
    description:
      'The replacement is created first and verified before the original is cancelled. If the new slot cannot be secured, the original booking stays in force — never the other way round.',
    tags: ['appointments'],
    params: [{ name: 'booking_request_id' }],
    requestSchema: 'RescheduleRequest',
    responseSchema: 'RescheduleResponse',
  });

  app.post<{ Params: { booking_request_id: string } }>(
    '/appointments/:booking_request_id/reschedule',
    async (request) => {
      const body = parseOrThrow(rescheduleRequest, request.body ?? {}, 'Reschedule request');
      const vendorDecisionHeader = request.headers['x-vendor-decision'];
      const vendorDecision =
        (Array.isArray(vendorDecisionHeader) ? vendorDecisionHeader[0] : vendorDecisionHeader) === 'decline'
          ? ('decline' as const)
          : ('accept' as const);
      const result = context.booking.reschedule(request.params.booking_request_id, body, {
        decision: request.faultDecision,
        vendorDecision,
      });
      return {
        outcome: result.outcome,
        booking: result.booking,
        appointment: result.appointment,
        superseded_booking: result.superseded_booking,
        superseded_appointment: result.superseded_appointment,
        idempotent_replay: result.idempotent_replay,
        old_booking_cancelled: result.old_booking_cancelled,
        cancellation_deferred_reason: result.cancellation_deferred_reason,
      };
    },
  );
}

/**
 * The single exit for the write path.
 *
 * A timed-out booking is answered with a transport failure, but the envelope
 * still carries the booking id and the reconcile path: an unknown outcome must
 * be resolvable, and it must be resolvable with the SAME key.
 */
export async function respondWithOutcome(reply: FastifyReply, result: BookingOutcome): Promise<unknown> {
  if (result.outcome === 'timeout_before_commit' || result.outcome === 'timeout_after_commit') {
    const delayMs = Math.min(Math.max(result.delayMs, 0), 120_000);
    // The caller's client gives up long before this lands; that IS the fault.
    if (delayMs > 0) await delay(delayMs);
    reply.header('x-mock-fault', result.outcome);
    return reply.code(504).send({
      error: {
        code: 'booking_outcome_unknown',
        message:
          result.outcome === 'timeout_after_commit'
            ? 'The hospital did not answer in time. A booking MAY exist: reconcile with the same idempotency key before retrying.'
            : 'The request timed out before the hospital committed anything. No booking exists.',
        request_id: String(reply.request.id),
        fault_type: result.outcome,
        scenario_id: null,
        retry_after_seconds: null,
        detail: {
          attempt_result: result.outcome,
          booking_request_id: result.booking.booking_request_id,
          idempotency_key: result.booking.idempotency_key,
          booking_state: result.booking.state,
          booking_exists: result.outcome === 'timeout_after_commit',
          reconcile_with: `/appointments/lookup_request?idempotency_key=${encodeURIComponent(result.booking.idempotency_key)}`,
        },
      },
    });
  }

  const status = result.httpStatus;
  if (status >= 400) {
    reply.header('x-mock-fault', result.outcome);
    if (result.retryAfterSeconds !== null) reply.header('retry-after', String(result.retryAfterSeconds));
    const code =
      result.outcome === 'malformed_response'
        ? 'malformed_upstream_response'
        : result.outcome === 'rate_limited'
          ? 'upstream_rate_limited'
          : result.outcome === 'upstream_unavailable'
            ? 'upstream_unavailable'
            : 'booking_not_created';
    return reply.code(status).send({
      error: {
        code,
        message: messageForOutcome(result.outcome),
        request_id: String(reply.request.id),
        fault_type: null,
        scenario_id: null,
        retry_after_seconds: result.retryAfterSeconds,
        detail: {
          attempt_result: result.outcome,
          booking_request_id: result.booking.booking_request_id,
          booking_state: result.booking.state,
        },
      },
    });
  }

  const body = bookingResponse.parse({
    outcome: result.outcome,
    booking: result.booking,
    appointment: result.appointment,
    attempts: result.attempts,
    idempotent_replay: result.idempotent_replay,
  });
  return reply.code(status).send(body);
}

function messageForOutcome(outcome: string): string {
  switch (outcome) {
    case 'malformed_response':
      return 'The hospital response did not match its documented schema; nothing was booked';
    case 'rate_limited':
      return 'The hospital scheduling system is rate limiting this caller; nothing was booked';
    case 'upstream_unavailable':
      return 'The hospital scheduling system is unavailable; nothing was booked';
    default:
      return 'The booking was not created';
  }
}

export { AppError, notFound, MIN_KEY_LENGTH };
