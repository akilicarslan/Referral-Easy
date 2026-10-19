import { html, nothing, type TemplateResult } from 'lit';
import { ReferralElement } from './internal/referral-element.js';
import { sharedStyles } from './internal/shared-styles.js';
import { listField, stringField } from './internal/fields.js';
import {
  describeDegradation,
  describeReason,
  describeFreshness,
  hoursSince,
  SYNTHETIC_LABEL,
  SYNTHETIC_TITLE,
} from './internal/truth.js';

/**
 * Shown when the directory could not answer the question that was asked.
 *
 * This component exists so that "we could not look" never gets rendered as
 * "there is nothing". Those two produce the same empty list and mean opposite
 * things to a patient deciding where to go.
 *
 * The `unchecked` list is the important part: it names what was *not* verified,
 * one line each, so the answer cannot be read as a negative. Whatever the
 * backend last knew (`last_known`) is shown separately and labelled with its
 * age, rather than being merged into the current answer.
 */
export class DirectoryFallbackCard extends ReferralElement {
  static readonly componentName = 'DirectoryFallbackCard';
  static readonly elementTag = 're-directory-fallback-card';

  static override styles = [sharedStyles];

  override render(): TemplateResult {
    const bookingLookup = this.str('action') === 'scheduling:lookup_request';
    const title = this.str('title') ?? (bookingLookup ? 'Booking confirmation pending' : 'We could not check the directory');
    const body = this.str('body') ?? (bookingLookup
      ? this.serverNotice ?? 'The booking request has been sent. It is not confirmed yet.'
      : undefined);
    const reasonCode = this.str('reason_code');
    const reasonDetail = this.str('reason_detail');
    const degraded = describeDegradation(this.str('degraded'));
    const unchecked = listField(this.rawProps, 'unchecked')
      .map((entry) => stringField(entry, 'label') ?? stringField(entry, 'name'))
      .filter((entry): entry is string => entry !== undefined);
    const canRetry = this.bool('can_retry') === true;
    const retryLabel = this.str('retry_label') ?? 'Try again';
    const lastKnown = this.record('last_known');
    const synthetic = this.bool('synthetic') === true;

    const reasonSentence = describeReason(reasonCode);

    return html`
      <section class="card" aria-labelledby="dfc-title">
        <div class="head">
          <h2 class="title" id="dfc-title">${title}</h2>
          ${synthetic
            ? html`<span class="chip synthetic" title=${SYNTHETIC_TITLE}>${SYNTHETIC_LABEL}</span>`
            : nothing}
          <span class="chip unknown">
            <span aria-hidden="true">?</span>Not verified
          </span>
        </div>

        ${body !== undefined
          ? html`<p class="subtitle">${body}</p>`
          : html`<p class="subtitle">
              Nothing below is a "no". It is an absence of an answer, and the two are different.
            </p>`}

        ${degraded !== undefined
          ? html`
              <p class="notice degraded" role="status">
                <span class="glyph" aria-hidden="true">${degraded.glyph}</span>
                <span><strong>Reduced lookup.</strong> ${degraded.sentence}</span>
              </p>
            `
          : nothing}

        ${reasonSentence !== undefined
          ? html`
              <p class="notice unknown" role="status">
                <span class="glyph" aria-hidden="true">?</span>
                <span>
                  ${reasonSentence}
                  ${reasonDetail !== undefined ? html` ${reasonDetail}` : nothing}
                  ${reasonCode !== undefined
                    ? html`<br /><span class="mono">${reasonCode}</span>`
                    : nothing}
                </span>
              </p>
            `
          : nothing}

        ${unchecked.length > 0
          ? html`
              <div class="unchecked">
                <p class="eyebrow">What we could not check</p>
                <ul class="plain">
                  ${unchecked.map((item) => html`<li>${item}</li>`)}
                </ul>
              </div>
            `
          : nothing}

        ${lastKnown !== undefined ? this.renderLastKnown(lastKnown) : nothing}

        ${this.serverNotice !== undefined && this.serverNotice !== body
          ? html`<p class="notice info" role="status">
              <span class="glyph" aria-hidden="true">i</span><span>${this.serverNotice}</span>
            </p>`
          : nothing}

        ${canRetry
          ? html`
              <div class="actions">
                <button class="secondary" type="button" ?disabled=${this.isDisabled || this.isSubmitted}
                  @click=${() => this.emitAction('directory.retry', {})}>
                  ${retryLabel}
                </button>
              </div>
            `
          : nothing}
      </section>
    `;
  }

  /**
   * The last answer we had, fenced off from the current one.
   *
   * Shown with its age and with the explicit statement that it may be out of
   * date, so it cannot be mistaken for a fresh result.
   */
  private renderLastKnown(lastKnown: Record<string, unknown>): TemplateResult {
    const checkedAt = stringField(lastKnown, 'checked_at');
    const freshness = describeFreshness(hoursSince(checkedAt));
    const label = stringField(lastKnown, 'label') ?? stringField(lastKnown, 'summary');
    const value = stringField(lastKnown, 'value');

    const rows = Object.entries(lastKnown).filter(
      ([key, entry]) =>
        typeof entry === 'string' &&
        key !== 'checked_at' &&
        key !== 'label' &&
        key !== 'summary' &&
        key !== 'value',
    ) as [string, string][];

    return html`
      <div class="last-known">
        <p class="eyebrow">Last known</p>
        ${label !== undefined
          ? html`<p class="option-name">${label}${value !== undefined ? `: ${value}` : ''}</p>`
          : nothing}
        ${rows.length > 0
          ? html`<dl class="facts">
              ${rows.map(
                ([key, entry]) =>
                  html`<dt>${key.replace(/_/g, ' ')}</dt><dd>${entry}</dd>`,
              )}
            </dl>`
          : nothing}
        <p class="meta">
          ${freshness.phrase.length > 0
            ? html`Recorded ${freshness.phrase}. `
            : html`No timestamp was provided for this record. `}
          It may already be out of date, and it is not a fresh check.
        </p>
      </div>
    `;
  }
}

if (customElements.get(DirectoryFallbackCard.elementTag) === undefined) {
  customElements.define(DirectoryFallbackCard.elementTag, DirectoryFallbackCard);
}

declare global {
  interface HTMLElementTagNameMap {
    're-directory-fallback-card': DirectoryFallbackCard;
  }
}
