import type { FastifyInstance } from 'fastify';
import type { AppContext } from '../context.js';
import { badRequest, notFound } from '../http/errors.js';
import type { OutboxRow } from '../db/store.js';
import type { NotificationOutboxEntry } from '../contracts/types.js';

export function toOutboxContract(entry: OutboxRow): NotificationOutboxEntry {
  return {
    outbox_id: entry.outbox_id,
    booking_request_id: entry.booking_request_id,
    channel: entry.channel,
    ...(entry.recipient ? { recipient: entry.recipient } : {}),
    ...(entry.template_key ? { template_key: entry.template_key } : {}),
    status: entry.status,
    attempts: entry.attempts,
    next_attempt_at: entry.next_attempt_at,
    last_error: entry.last_error,
    created_at: entry.created_at,
  };
}

/**
 * The notification outbox.
 *
 * Entries are written inside the booking transaction and delivered outside it.
 * `abandoned` is a terminal state: a notification that can never be delivered
 * stops there instead of retrying until the end of time.
 */
export function registerNotifications(app: FastifyInstance, context: AppContext): void {
  context.docs.describe({
    method: 'get',
    path: '/notifications/outbox',
    summary: 'List outbox entries',
    tags: ['notifications'],
    query: [{ name: 'booking_request_id' }, { name: 'status' }],
  });

  app.get<{ Querystring: { booking_request_id?: string; status?: string } }>('/notifications/outbox', async (request) => {
    const entries = context.store
      .listOutbox({
        ...(request.query.booking_request_id ? { bookingRequestId: request.query.booking_request_id } : {}),
        ...(request.query.status ? { status: request.query.status } : {}),
      })
      .map(toOutboxContract);
    return {
      entries,
      count: entries.length,
      by_status: entries.reduce<Record<string, number>>((accumulator, entry) => {
        accumulator[entry.status] = (accumulator[entry.status] ?? 0) + 1;
        return accumulator;
      }, {}),
    };
  });

  context.docs.describe({
    method: 'get',
    path: '/notifications/outbox/{outbox_id}',
    summary: 'Read one outbox entry',
    tags: ['notifications'],
    params: [{ name: 'outbox_id' }],
    responseSchema: 'NotificationOutboxEntry',
  });

  app.get<{ Params: { outbox_id: string } }>('/notifications/outbox/:outbox_id', async (request) => {
    const entry = context.store.getOutboxEntry(request.params.outbox_id);
    if (!entry) throw notFound('unknown_outbox_entry', `No outbox entry ${request.params.outbox_id}`);
    return toOutboxContract(entry);
  });

  context.docs.describe({
    method: 'get',
    path: '/notifications/outbox/{outbox_id}/preview',
    summary: 'Render what the patient would receive, in the hospital timezone',
    tags: ['notifications'],
    params: [{ name: 'outbox_id' }],
  });

  app.get<{ Params: { outbox_id: string } }>('/notifications/outbox/:outbox_id/preview', async (request) => {
    const preview = context.outbox.preview(request.params.outbox_id);
    if (!preview) throw notFound('unknown_outbox_entry', `No outbox entry ${request.params.outbox_id}`);
    return preview;
  });

  context.docs.describe({
    method: 'post',
    path: '/notifications',
    summary: 'Deliver the pending notifications for one booking (internal send)',
    description:
      'The fault-facing entry point: a notification provider failure is answered here and NEVER touches the booking. The response says so explicitly with `booking_still_committed`.',
    tags: ['notifications'],
  });

  app.post<{ Body: { booking_request_id?: string; idempotency_key?: string; channel?: 'email' | 'sms' } }>(
    '/notifications',
    async (request, reply) => {
      const body = (request.body ?? {}) as { booking_request_id?: string; idempotency_key?: string; channel?: 'email' | 'sms' };
      const booking = body.booking_request_id
        ? context.store.findBookingById(body.booking_request_id)
        : body.idempotency_key
          ? context.store.findBookingByKey(body.idempotency_key)
          : undefined;
      if (!booking) {
        throw notFound(
          'unknown_booking_request',
          'Provide booking_request_id or idempotency_key of a booking that exists',
          { booking_request_id: body.booking_request_id ?? null, idempotency_key: body.idempotency_key ?? null },
        );
      }
      if (body.channel && body.channel !== 'email' && body.channel !== 'sms') {
        throw badRequest('unknown_channel', 'channel must be email or sms', { channel: body.channel });
      }

      const report = context.outbox.drain({
        bookingRequestId: booking.booking_request_id,
        ...(body.channel ? { channel: body.channel } : {}),
        decision: request.faultDecision,
        /* A caller asking for delivery is asking for it now: the automatic
           backoff between attempts belongs to the loop, not to this request. */
        force: true,
      });
      // This is the invariant worth answering with an error code: the delivery
      // failed, and the booking did not.
      const undelivered = [...report.failed, ...report.abandoned];
      const stillCommitted = context.store.findBookingById(booking.booking_request_id)!;
      const payload = {
        booking_request_id: booking.booking_request_id,
        booking_state: stillCommitted.state,
        booking_still_committed: true,
        report,
      };

      if (undelivered.length > 0) {
        const entry = context.store.getOutboxEntry(undelivered[0]!);
        return reply.code(502).send({
          error: {
            code: 'notification_undeliverable',
            message: 'The notification could not be delivered; the appointment is unaffected',
            request_id: String(request.id),
            fault_type: request.faultDecision?.fault.type ?? 'notification_failure',
            scenario_id: request.faultDecision?.scenario_id ?? null,
            retry_after_seconds: null,
            detail: {
              ...payload,
              channel: entry?.channel ?? body.channel ?? null,
              outbox_status: entry?.status ?? null,
              attempts: entry?.attempts ?? null,
            },
          },
        });
      }
      return reply.code(200).send(payload);
    },
  );

  context.docs.describe({
    method: 'post',
    path: '/notifications/drain',
    summary: 'Run one delivery pass over the outbox',
    description:
      'The AUTOMATIC pass: an entry is attempted only when its retry backoff has come due, and the optional `booking_request_id` narrows the pass to one booking. An explicit delivery request (`POST /notifications`, the single-entry retry) is not this: it means now.',
    tags: ['notifications'],
  });

  app.post<{ Body: { booking_request_id?: string; channel?: 'email' | 'sms' } }>('/notifications/drain', async (request) => {
    const body = (request.body ?? {}) as { booking_request_id?: string; channel?: 'email' | 'sms' };
    const report = context.outbox.drain({
      ...(body.booking_request_id ? { bookingRequestId: body.booking_request_id } : {}),
      ...(body.channel ? { channel: body.channel } : {}),
      decision: request.faultDecision,
    });
    return report;
  });

  context.docs.describe({
    method: 'post',
    path: '/notifications/outbox/{outbox_id}/retry',
    summary: 'Re-attempt one outbox entry',
    tags: ['notifications'],
    params: [{ name: 'outbox_id' }],
  });

  app.post<{ Params: { outbox_id: string } }>('/notifications/outbox/:outbox_id/retry', async (request) => {
    const entry = context.store.getOutboxEntry(request.params.outbox_id);
    if (!entry) throw notFound('unknown_outbox_entry', `No outbox entry ${request.params.outbox_id}`);
    if (entry.status === 'abandoned') {
      // Reopening an abandoned entry is a deliberate operator action, not a
      // retry; the loop must not restart itself.
      context.store.updateOutboxEntry(entry.outbox_id, {
        status: 'pending',
        attempts: 0,
        last_error: null,
        next_attempt_at: null,
      });
    }
    /* `force`: the caller named this entry, so the automatic backoff between
       attempts does not apply — "retry this one" answered with "nothing was
       due" would be a silent no-op. The attempt still counts, so a provider
       that keeps failing reaches the ceiling instead of retrying forever. */
    const report = context.outbox.drain({
      bookingRequestId: entry.booking_request_id,
      channel: entry.channel,
      decision: request.faultDecision,
      force: true,
    });
    const updated = context.store.getOutboxEntry(entry.outbox_id);
    return { report, entry: updated ? toOutboxContract(updated) : null };
  });
}
