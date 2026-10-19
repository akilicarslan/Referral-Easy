import { html, nothing, type TemplateResult } from 'lit';
import { ReferralElement } from './internal/referral-element.js';
import { sharedStyles } from './internal/shared-styles.js';
import { listField, stringField } from './internal/fields.js';
import { describeReason, SYNTHETIC_LABEL, SYNTHETIC_TITLE } from './internal/truth.js';

/**
 * Shown when a person has taken the referral over.
 *
 * The contract routes `no_source` degradation and `human_owned` policy blocks
 * here, which means a patient reaches this card at the point of highest anxiety
 * in the flow. Three things follow from that:
 *
 *   - **Say what was already handed over.** `handed_over` is listed so the
 *     patient does not have to retell it. Being asked to repeat yourself after
 *     an automated failure is the fastest way to lose trust in the whole thing.
 *
 *   - **Give a real window, or admit there is none.** `response_window` is
 *     shown when present. When it is absent the card says so rather than
 *     inventing a reassuring "shortly".
 *
 *   - **Never claim a human is actively watching.** The card states that it has
 *     been handed over, which is what we know. It does not claim anyone has
 *     picked it up.
 */
export class HumanHandoffCard extends ReferralElement {
  static readonly componentName = 'HumanHandoffCard';
  static readonly elementTag = 're-human-handoff-card';

  static override styles = [sharedStyles];

  override render(): TemplateResult {
    if (this.bool('consent_required') === true) {
      const summary = stringField(this.record('handoff') ?? {}, 'patient_visible_summary');
      return html`
        <section class="card" aria-labelledby="hhc-consent-title">
          <h2 class="title" id="hhc-consent-title">Approve a coordinator review</h2>
          <p class="subtitle">Review the summary below. A coordinator receives it only after you approve.</p>
          <p class="notice info">${summary ?? 'No summary is available.'}</p>
          <p>This approval does not book an appointment.</p>
          <button class="primary block" type="button" ?disabled=${this.isDisabled || this.isSubmitted || !summary}
            @click=${() => this.emitAction('handoff.approve', { consented: true })}>
            Share this summary with a coordinator
          </button>
        </section>
      `;
    }
    const title = this.str('title') ?? 'A person is taking this over';
    const body = this.str('body');
    const reasonCode = this.str('reason_code');
    const reasonDetail = this.str('reason_detail');
    const responseWindow = this.str('response_window');
    const reference = this.str('reference');
    const synthetic = this.bool('synthetic') === true;
    const handedOver = listField(this.rawProps, 'handed_over')
      .map((entry) => stringField(entry, 'label') ?? stringField(entry, 'name'))
      .filter((entry): entry is string => entry !== undefined);
    const channels = listField(this.rawProps, 'contact_channels');

    const reasonSentence = describeReason(reasonCode);

    return html`
      <section class="card" aria-labelledby="hhc-title">
        <div class="head">
          <h2 class="title" id="hhc-title">${title}</h2>
          ${synthetic
            ? html`<span class="chip synthetic" title=${SYNTHETIC_TITLE}>${SYNTHETIC_LABEL}</span>`
            : nothing}
          <span class="chip neutral"><span aria-hidden="true">→</span>With a person</span>
        </div>

        ${body !== undefined
          ? html`<p class="subtitle">${body}</p>`
          : html`<p class="subtitle">
              Your referral has been passed to a member of the hospital's referral team. Nothing
              further is needed from you right now.
            </p>`}

        ${reasonSentence !== undefined
          ? html`
              <p class="notice info" role="status">
                <span class="glyph" aria-hidden="true">i</span>
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

        ${handedOver.length > 0
          ? html`
              <div class="handed-over">
                <p class="eyebrow">Already passed on — you will not be asked again</p>
                <ul class="plain">
                  ${handedOver.map((item) => html`<li>${item}</li>`)}
                </ul>
              </div>
            `
          : nothing}

        ${channels.length > 0 ? this.renderChannels(channels) : nothing}

        <p class="notice info">
          <span class="glyph" aria-hidden="true">i</span>
          <span>
            ${responseWindow !== undefined
              ? html`Expect to hear back <strong>${responseWindow}</strong>.`
              : html`No response window has been given, so we cannot say how long this will
                  take.`}
            This referral is queued for a person; it is not being actively worked on yet.
          </span>
        </p>

        ${reference !== undefined
          ? html`<p class="reference">
              <span class="eyebrow">Quote this reference</span><br />${reference}
            </p>`
          : nothing}
      </section>
    `;
  }

  private renderChannels(channels: readonly Record<string, unknown>[]): TemplateResult {
    return html`
      <div class="channels">
        <p class="eyebrow">How to reach them</p>
        <dl class="facts">
          ${channels.map((channel) => {
            const label = stringField(channel, 'label') ?? stringField(channel, 'type') ?? 'Contact';
            const value =
              stringField(channel, 'value') ??
              stringField(channel, 'address') ??
              stringField(channel, 'number');
            return html`<dt>${label}</dt><dd>${value ?? 'No details provided'}</dd>`;
          })}
        </dl>
      </div>
    `;
  }
}

if (customElements.get(HumanHandoffCard.elementTag) === undefined) {
  customElements.define(HumanHandoffCard.elementTag, HumanHandoffCard);
}

declare global {
  interface HTMLElementTagNameMap {
    're-human-handoff-card': HumanHandoffCard;
  }
}
