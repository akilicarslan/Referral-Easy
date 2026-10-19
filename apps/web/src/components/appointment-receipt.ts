import { html, nothing, type TemplateResult } from 'lit';
import { ReferralElement } from './internal/referral-element.js';
import { sharedStyles } from './internal/shared-styles.js';
import {
  classifyBookingState,
  describeDegradation,
  formatInstant,
  SYNTHETIC_LABEL,
  SYNTHETIC_TITLE,
} from './internal/truth.js';
import { metaLine, recordField, stringField } from './internal/fields.js';

/**
 * The receipt.
 *
 * This is the component where a small UI decision becomes a false statement, so
 * it is deliberately the most constrained one in the catalog.
 *
 *   - `REQUESTED` / `PENDING_REVIEW` render as **"Requested — the hospital will
 *     confirm"** with an amber, hollow-marker treatment. They never get a tick,
 *     never get the word "confirmed", and never get the confirmed colour.
 *     HTTP 200 is not confirmation.
 *
 *   - An unrecognised or absent state renders as **unknown**, with the reason we
 *     cannot confirm it. It does not fall back to the confident branch; the
 *     default arm of `classifyBookingState` is `unknown` on purpose.
 *
 *   - A confirmation reference is only shown when the state is confirmed. A
 *     booking reference on a request that has not been accepted reads like a
 *     ticket, and it is not one.
 *
 *   - Degradation is shown. An outcome produced without the directory is a
 *     different outcome and the patient is entitled to know which one they got.
 */
export class AppointmentReceipt extends ReferralElement {
  static readonly componentName = 'AppointmentReceipt';
  static readonly elementTag = 're-appointment-receipt';

  static override styles = [sharedStyles];

  override render(): TemplateResult {
    const appointment = this.record('appointment');
    const state = this.str('state') ?? stringField(appointment ?? {}, 'state');
    const readout = classifyBookingState(state);
    const hospital = this.record('hospital') ?? recordField(appointment ?? {}, 'hospital');
    const provider = this.record('provider') ?? recordField(appointment ?? {}, 'provider');
    const slot = this.record('slot') ?? recordField(appointment ?? {}, 'slot');

    const timeZone =
      this.str('display_timezone') ??
      (appointment ? stringField(appointment, 'display_timezone') : undefined);
    const startAt =
      (slot ? stringField(slot, 'start_at') : undefined) ??
      (appointment ? stringField(appointment, 'start_at') : undefined);
    const when = formatInstant(startAt, timeZone);

    const synthetic = this.bool('synthetic') === true;
    const degraded = describeDegradation(this.str('degraded'));
    const nextStep = this.str('next_step');
    const reference = this.str('confirmation_reference') ?? stringField(appointment ?? {}, 'external_ref');
    const derivedFrom = this.str('derived_from');
    const reconcilesAt = this.str('reconciles_at');
    const bookingRequestId = this.str('booking_request_id');

    return html`
      <section class="card" aria-labelledby="ar-title">
        <div class="head">
          <h2 class="title" id="ar-title">${this.str('title') ?? 'Your appointment'}</h2>
          ${synthetic
            ? html`<span class="chip synthetic" title=${SYNTHETIC_TITLE}>${SYNTHETIC_LABEL}</span>`
            : nothing}
        </div>

        ${this.renderStatus(readout)}

        ${degraded !== undefined
          ? html`
              <p class="notice degraded" role="status">
                <span class="glyph" aria-hidden="true">${degraded.glyph}</span>
                <span><strong>Reduced lookup.</strong> ${degraded.sentence}</span>
              </p>
            `
          : nothing}

        ${this.renderFacts({ hospital, provider, slot, when, timeZone, appointment })}

        ${nextStep !== undefined
          ? html`<p class="notice info"><span class="glyph" aria-hidden="true">i</span><span>${nextStep}</span></p>`
          : nothing}

        ${readout.certainty === 'confirmed' && reference !== undefined
          ? html`<p class="reference"><span class="eyebrow">Confirmation reference</span><br />${reference}</p>`
          : nothing}

        ${readout.certainty !== 'confirmed' && reference !== undefined
          ? html`
              <p class="notice info">
                <span class="glyph" aria-hidden="true">i</span>
                <span>
                  The hospital's reference for this request is
                  <span class="mono">${reference}</span>. It is a request reference, not a
                  confirmation.
                </span>
              </p>
            `
          : nothing}

        ${bookingRequestId !== undefined && bookingRequestId !== reference
          ? html`<p class="meta" style="margin-top: var(--re-s3)">
              Booking request <span class="mono">${bookingRequestId}</span>
            </p>`
          : nothing}

        ${derivedFrom !== undefined
          ? html`<p class="meta" style="margin-top: var(--re-s2)">
              Outcome derived from <span class="mono">${derivedFrom}</span>, not from a direct
              confirmation.
            </p>`
          : nothing}

        ${reconcilesAt !== undefined
          ? html`<p class="meta" style="margin-top: var(--re-s2)">
              We will check again ${relativeWhen(reconcilesAt)}.
            </p>`
          : nothing}
      </section>
    `;
  }

  /**
   * The headline status.
   *
   * The wording comes entirely from `classifyBookingState`, so no branch of this
   * component can produce confirmation language for an unconfirmed booking.
   */
  private renderStatus(readout: ReturnType<typeof classifyBookingState>): TemplateResult {
    return html`
      <div class="status-block ${readout.tone}" role="status" aria-live="polite">
        <span class="glyph" aria-hidden="true">${readout.glyph}</span>
        <span>
          <span class="status-title">${readout.label}</span>
          <span class="status-sentence">${readout.sentence}</span>
        </span>
      </div>
    `;
  }

  private renderFacts(context: {
    hospital: Record<string, unknown> | undefined;
    provider: Record<string, unknown> | undefined;
    slot: Record<string, unknown> | undefined;
    when: ReturnType<typeof formatInstant>;
    timeZone: string | undefined;
    appointment: Record<string, unknown> | undefined;
  }): TemplateResult | typeof nothing {
    const rows: { label: string; value: string }[] = [];

    const hospitalName = context.hospital ? stringField(context.hospital, 'name') : undefined;
    if (hospitalName !== undefined) {
      rows.push({ label: 'Hospital', value: hospitalName });
    }

    const providerName = context.provider ? stringField(context.provider, 'name') : undefined;
    const specialty = context.provider ? stringField(context.provider, 'specialty') : undefined;
    if (providerName !== undefined) {
      rows.push({
        label: 'Doctor',
        value: metaLine(providerName, specialty) ?? providerName,
      });
    }

    const relative = context.appointment ? stringField(context.appointment, 'when') : undefined;
    const whenText =
      context.when.text !== 'Time not provided'
        ? context.when.text
        : (relative ?? undefined);

    if (whenText !== undefined) {
      rows.push({ label: 'When', value: whenText });
    }

    const visitType =
      (context.slot ? stringField(context.slot, 'visit_type') : undefined) ??
      (context.appointment ? stringField(context.appointment, 'visit_type') : undefined);
    if (visitType !== undefined) {
      rows.push({ label: 'Visit type', value: visitType });
    }

    if (context.timeZone !== undefined) {
      rows.push({ label: 'Time zone', value: context.timeZone });
    } else if (whenText !== undefined) {
      rows.push({
        label: 'Time zone',
        value: `${context.when.zone} — hospital's zone was not provided`,
      });
    }

    if (rows.length === 0) {
      return html`
        <p class="empty" role="status">
          No appointment details were included in this message. The status above is all we know.
        </p>
      `;
    }

    return html`
      <dl class="facts">
        ${rows.map(
          (row) => html`<dt>${row.label}</dt>
            <dd>${row.value}</dd>`,
        )}
      </dl>
    `;
  }
}

/** "in 30 minutes" / "in 2 hours" / "at an unspecified time". */
function relativeWhen(iso: string): string {
  const target = new Date(iso);
  if (Number.isNaN(target.getTime())) {
    return 'at a time we could not read';
  }
  const minutes = Math.round((target.getTime() - Date.now()) / 60_000);
  if (minutes <= 0) {
    return 'shortly';
  }
  if (minutes < 60) {
    return `in about ${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`;
  }
  const hours = Math.round(minutes / 60);
  return `in about ${hours} ${hours === 1 ? 'hour' : 'hours'}`;
}

if (customElements.get(AppointmentReceipt.elementTag) === undefined) {
  customElements.define(AppointmentReceipt.elementTag, AppointmentReceipt);
}

declare global {
  interface HTMLElementTagNameMap {
    're-appointment-receipt': AppointmentReceipt;
  }
}
