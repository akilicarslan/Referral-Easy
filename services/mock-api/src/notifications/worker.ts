import { newUuid } from '../domain/ids.js';
import { formatInZone, nowUtcIso, toUtcIso } from '../domain/time.js';
import type { MockApiConfig } from '../config.js';
import type { BookingRequestShape } from '../contracts/zod.js';
import type { OutboxRow, Store } from '../db/store.js';
import type { NotificationChannel } from '../contracts/enums.js';
import { INTERNAL_OPERATIONS, type FaultDecision } from '../faults/types.js';
import type { FaultEngine } from '../faults/engine.js';
import { addMinutes } from '../domain/time.js';

export interface OutboxPlan {
  channel: NotificationChannel;
  recipient: string;
  template_key: string;
}

export interface DrainReport {
  examined: number;
  sent: string[];
  failed: string[];
  abandoned: string[];
}

/** Renders the patient-facing message. Testable, and the only place that formats a slot. */
export function renderNotification(
  entry: OutboxRow,
  booking: BookingRequestShape,
  localisedStart: string,
): { subject: string; body: string } {
  const verb = booking.adapter_type === 'review_required' ? 'Referral sent for review' : 'Appointment confirmed';
  const subject = `${verb} — ${localisedStart}`;
  const body = [
    `Booking reference: ${booking.booking_request_id}`,
    `State: ${booking.state}`,
    `When: ${localisedStart}`,
    booking.external_reference ? `Hospital reference: ${booking.external_reference}` : 'Hospital reference: not yet issued',
    entry.channel === 'sms' ? 'Reply STOP to opt out.' : 'Do not reply to this message.',
  ].join('\n');
  return { subject, body };
}

/**
 * The outbox sender.
 *
 * Delivery ALWAYS runs outside the booking transaction. That ordering is the
 * whole point: a failing notification provider can never take a committed
 * booking down with it, and a notification that can never succeed reaches the
 * terminal `abandoned` state instead of retrying forever.
 */
export class OutboxWorker {
  constructor(
    private readonly config: MockApiConfig,
    private readonly store: Store,
    private readonly engine: FaultEngine,
  ) {}

  planForBooking(booking: BookingRequestShape): OutboxPlan[] {
    const phone = booking.payload.contact_phone ?? '+15555550142';
    return [
      { channel: 'email', recipient: 'demo.patient@example.test', template_key: 'booking_state_email' },
      { channel: 'sms', recipient: phone, template_key: 'booking_state_sms' },
    ];
  }

  /** Builds the rows. Called INSIDE the booking transaction by the booking service. */
  buildEntries(booking: BookingRequestShape, now: Date): Array<OutboxRow & { payload: unknown }> {
    const created = toUtcIso(now);
    return this.planForBooking(booking).map((plan) => ({
      outbox_id: newUuid(),
      booking_request_id: booking.booking_request_id,
      channel: plan.channel,
      recipient: plan.recipient,
      template_key: plan.template_key,
      status: 'pending' as const,
      attempts: 0,
      next_attempt_at: null,
      last_error: null,
      created_at: created,
      payload: null,
    }));
  }

  /** Deliveries attempted inline right after a commit, within the request's fault context. */
  deliverForBooking(bookingRequestId: string, decision: FaultDecision | null): DrainReport {
    return this.drain({ bookingRequestId, decision });
  }

  /**
   * One delivery pass.
   *
   * `force` is for a caller that says "deliver now" — `POST /notifications` for
   * one booking, or the retry of one entry. The automatic passes (the delivery
   * right after a commit, and the drain endpoint) respect the retry backoff,
   * because a provider that just failed should not be hammered by a loop; a
   * person or a workflow asking again is a different thing from a schedule, and
   * answering "nothing was due" to an explicit request would make the retry
   * endpoint a no-op that looks like a success.
   */
  drain(
    options: {
      bookingRequestId?: string;
      channel?: NotificationChannel;
      decision?: FaultDecision | null;
      limit?: number;
      force?: boolean;
    } = {},
  ): DrainReport {
    const now = new Date();
    const nowIso = toUtcIso(now);
    const entries = this.store.listOutbox({
      ...(options.bookingRequestId ? { bookingRequestId: options.bookingRequestId } : {}),
    });
    const report: DrainReport = { examined: 0, sent: [], failed: [], abandoned: [] };

    // An explicit `channel` from the caller narrows the pass to the legs it
    // asked about. A channel named by the SCENARIO is a different thing, and
    // the sender below reads it as which leg the fault is about.
    const channel = options.channel;

    for (const entry of entries) {
      if (channel && entry.channel !== channel) continue;
      if (entry.status === 'sent' || entry.status === 'abandoned') continue;
      if (!options.force && entry.next_attempt_at && entry.next_attempt_at > nowIso) continue;
      if (options.limit !== undefined && report.examined >= options.limit) break;
      report.examined += 1;

      const booking = this.store.findBookingById(entry.booking_request_id);
      if (!booking) continue;

      // A scenario that names one channel (`"channel": "sms"`) faults only that
      // channel; the other one still reaches the patient, which is the point of
      // having two. The scenario's occurrence is spent by the leg the fault is
      // about, so naming `sms` neither burns the scenario on the email leg nor
      // silently spares it.
      const decision = this.engine.internalDecision(
        INTERNAL_OPERATIONS.notificationSend,
        options.decision ?? null,
        (preview) => typeof preview.fault.channel !== 'string' || preview.fault.channel === entry.channel,
      );
      const failing = decision?.fault.type === 'notification_failure';
      const attempts = entry.attempts + 1;

      if (failing) {
        const terminal = attempts >= this.config.notificationMaxAttempts;
        this.store.updateOutboxEntry(entry.outbox_id, {
          status: terminal ? 'abandoned' : 'failed',
          attempts,
          last_error: terminal
            ? 'notification provider unavailable — max attempts exhausted, giving up'
            : 'notification provider unavailable',
          next_attempt_at: terminal
            ? null
            : toUtcIso(addMinutes(now, attempts * this.config.notificationBackoffMinutes)),
        });
        (terminal ? report.abandoned : report.failed).push(entry.outbox_id);
        continue;
      }

      // The patient always reads the hospital's wall clock, never the UTC
      // instant that is stored. When there is no appointment yet there is no
      // time to show, and inventing one would be worse than saying so.
      const appointment = this.store.getAppointmentByBooking(entry.booking_request_id);
      const localisedStart = appointment
        ? formatInZone(appointment.start_at, appointment.display_timezone)
        : 'time to be confirmed';
      this.store.updateOutboxEntry(entry.outbox_id, {
        status: 'sent',
        attempts,
        last_error: null,
        next_attempt_at: null,
      });
      report.sent.push(entry.outbox_id);
    }

    return report;
  }

  /** Diagnostic view: what the patient would actually receive. */
  preview(outboxId: string): { subject: string; body: string } | null {
    const entry = this.store.getOutboxEntry(outboxId);
    if (!entry) return null;
    const booking = this.store.findBookingById(entry.booking_request_id);
    if (!booking) return null;
    const appointment = this.store.getAppointmentByBooking(entry.booking_request_id);
    const localisedStart = appointment
      ? formatInZone(appointment.start_at, appointment.display_timezone)
      : 'time to be confirmed';
    return renderNotification(entry, booking, localisedStart);
  }
}
