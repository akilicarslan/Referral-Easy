import { html, nothing, type TemplateResult } from 'lit';
import { ReferralElement } from './internal/referral-element.js';
import { sharedStyles } from './internal/shared-styles.js';
import { describeDegradation, SYNTHETIC_LABEL, SYNTHETIC_TITLE } from './internal/truth.js';
import { listField, metaLine, stringField } from './internal/fields.js';

/**
 * The last look before anything is sent.
 *
 * This is the only component in the catalog that can cause a booking, so it is
 * built to make the patient's yes mean something:
 *
 *   - **Consent is an explicit act.** When `requires_consent` is set, the submit
 *     button is disabled until the box is ticked, and the reason is written next
 *     to the button rather than encoded in its grey.
 *
 *   - **Nothing is submitted twice.** The button disables itself for the duration
 *     of the request. The real guarantee is the `command_id` the app mints once
 *     per intent (`src/stream/command.ts`), so a retry is deduplicated server-side
 *     even if the UI misses a beat.
 *
 *   - **The summary is shown as it will be sent**, including anything the
 *     backend marked incomplete, rather than a tidied version the patient did not
 *     agree to.
 */
export class BookingReview extends ReferralElement {
  static readonly componentName = 'BookingReview';
  static readonly elementTag = 're-booking-review';

  static override styles = [sharedStyles];

  #consented = false;
  /** True from the click until the server sends a new payload for this instance. */
  #inFlight = false;

  /**
   * A fresh payload from the server means the previous attempt concluded, so the
   * in-flight latch is released. This is what stops the button staying dead
   * after a failed request.
   */
  override willUpdate(changed: Map<PropertyKey, unknown>): void {
    if (changed.has('node') || changed.has('context')) {
      this.#inFlight = false;
    }
  }

  override render(): TemplateResult {
    const title = this.str('title') ?? 'Review your referral';
    const requiresConsent = this.bool('requires_consent') !== false;
    const consentText = this.str('consent_text') ?? 'I approve sending the referral request for the details shown above.';
    const submitLabel = this.str('submit_label') ?? 'Send referral request';
    const synthetic = this.bool('synthetic') === true;
    const degraded = describeDegradation(this.str('degraded'));

    const submitted = this.isSubmitted;
    const locked = this.isDisabled || submitted;
    // An absent consent label still requires agreement to the standard wording.
    const consentMissing = requiresConsent && !this.#consented;
    const blocked = locked || consentMissing || this.#inFlight;

    return html`
      <section class="card" aria-labelledby="br-title">
        <div class="head">
          <h2 class="title" id="br-title">${title}</h2>
          ${synthetic
            ? html`<span class="chip synthetic" title=${SYNTHETIC_TITLE}>${SYNTHETIC_LABEL}</span>`
            : nothing}
        </div>

        ${this.serverNotice !== undefined
          ? html`<p class="notice info" role="status">
              <span class="glyph" aria-hidden="true">i</span><span>${this.serverNotice}</span>
            </p>`
          : nothing}

        ${degraded !== undefined
          ? html`
              <p class="notice degraded" role="status">
                <span class="glyph" aria-hidden="true">${degraded.glyph}</span>
                <span><strong>Reduced lookup.</strong> ${degraded.sentence}</span>
              </p>
            `
          : nothing}

        ${this.renderFacts()}

        ${this.renderAttachments()}

        ${consentText !== undefined
          ? html`
              <label class="option consent">
                <input
                  type="checkbox"
                  .checked=${this.#consented}
                  ?disabled=${locked}
                  @change=${(event: Event) => {
                    this.#consented = (event.target as HTMLInputElement).checked;
                    this.requestUpdate();
                  }}
                />
                <span class="option-body">
                  <span class="option-name">I agree to this referral being sent</span>
                  <span class="option-note">${consentText}</span>
                </span>
              </label>
            `
          : nothing}

        <div class="actions">
          <button
            class="primary block"
            type="button"
            ?disabled=${blocked}
            @click=${() => this.submit()}
          >
            ${submitted
              ? 'Request sent'
              : this.#inFlight
                ? 'Sending…'
                : submitLabel}
          </button>
        </div>

        ${this.#renderWhyDisabled(consentMissing, submitted, locked)}
      </section>
    `;
  }

  /**
   * Say why the button is unavailable.
   *
   * A greyed-out control with no explanation is a dead end; every disabled state
   * here has a sentence.
   */
  #renderWhyDisabled(
    consentMissing: boolean,
    submitted: boolean,
    locked: boolean,
  ): TemplateResult | typeof nothing {
    if (this.#inFlight) {
      return html`<p class="meta hint" role="status">Sending your request. This button is locked until the hospital answers.</p>`;
    }
    if (submitted) {
      return html`<p class="meta hint" role="status">This request has already been sent. Nothing further is needed from you.</p>`;
    }
    if (locked) {
      return html`<p class="meta hint" role="status">The hospital has paused this step.</p>`;
    }
    if (consentMissing) {
      return html`<p class="meta hint" role="status">Tick the agreement above to enable sending.</p>`;
    }
    return nothing;
  }

  private renderFacts(): TemplateResult | typeof nothing {
    const rows: { label: string; value: string }[] = [];

    const hospital = this.record('hospital');
    const hospitalName = hospital ? stringField(hospital, 'name') : undefined;
    if (hospitalName !== undefined) {
      rows.push({ label: 'Hospital', value: hospitalName });
    }

    const provider = this.record('provider');
    const providerName = provider ? stringField(provider, 'name') : undefined;
    const specialty = provider ? stringField(provider, 'specialty') : undefined;
    if (providerName !== undefined) {
      rows.push({ label: 'Doctor', value: metaLine(providerName, specialty) ?? providerName });
    }

    const slot = this.record('slot');
    if (slot !== undefined) {
      const label = stringField(slot, 'label') ?? stringField(slot, 'start_at');
      const zone = stringField(slot, 'timezone');
      if (label !== undefined) {
        rows.push({ label: 'When', value: zone !== undefined ? `${label} (${zone})` : label });
      }
    }

    const patient = this.record('patient_summary');
    if (patient !== undefined) {
      for (const [key, value] of Object.entries(patient)) {
        if (typeof value === 'string' && value.length > 0) {
          rows.push({ label: humanise(key), value });
        }
      }
    }

    // The gateway's canonical review props carry the exact payload whose hash consent binds.
    const payload = this.record('booking_payload');
    if (payload !== undefined) {
      for (const key of ['hospital_id', 'provider_id', 'slot_id', 'visit_type']) {
        const value = stringField(payload, key);
        if (value !== undefined) rows.push({ label: humanise(key), value });
      }
    }

    if (rows.length === 0) {
      return html`
        <p class="empty" role="status">
          No details were included in this summary, so there is nothing to review yet.
        </p>
      `;
    }

    return html`
      <dl class="facts">
        ${rows.map((row) => html`<dt>${row.label}</dt><dd>${row.value}</dd>`)}
      </dl>
    `;
  }

  private renderAttachments(): TemplateResult | typeof nothing {
    const attachments = listField(this.rawProps, 'attachments');
    if (attachments.length === 0) {
      return nothing;
    }

    return html`
      <div class="attachments">
        <p class="eyebrow">Attached</p>
        <ul class="plain">
          ${attachments.map(
            (attachment) =>
              html`<li>
                ${stringField(attachment, 'kind') ?? 'Document'}
                ${stringField(attachment, 'filename') !== undefined
                  ? html`<span class="mono">${stringField(attachment, 'filename')}</span>`
                  : nothing}
                ${attachment['synthetic'] === true
                  ? html`<span class="chip synthetic">${SYNTHETIC_LABEL}</span>`
                  : nothing}
              </li>`,
          )}
        </ul>
      </div>
    `;
  }

  private submit(): void {
    if (this.isDisabled || this.isSubmitted || this.#inFlight) {
      return;
    }
    if (this.bool('requires_consent') !== false && !this.#consented) {
      return;
    }

    this.#inFlight = true;
    this.requestUpdate();

    this.emitAction('booking.submit', {
      consented: this.#consented,
      // The app pairs this with the server-issued action and a command_id it
      // mints once per intent, so a double click cannot become two bookings.
    });
  }
}

function humanise(key: string): string {
  const spaced = key.replace(/_/g, ' ');
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

if (customElements.get(BookingReview.elementTag) === undefined) {
  customElements.define(BookingReview.elementTag, BookingReview);
}

declare global {
  interface HTMLElementTagNameMap {
    're-booking-review': BookingReview;
  }
}
