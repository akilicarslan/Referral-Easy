import { html, nothing, type TemplateResult } from 'lit';
import { PickerBase } from './internal/picker-base.js';
import { stringField } from './internal/fields.js';
import { formatInstant } from './internal/truth.js';

/**
 * STEP 3 of the fixed order (§4): slots, for the chosen doctor.
 *
 * Two rules are load-bearing here.
 *
 * 1. **No auto-selected slot.** An empty `selected_id` renders as an unanswered
 *    question. A pre-highlighted first slot is the single easiest way to book a
 *    patient into a time they never agreed to.
 *
 * 2. **A slot time without a zone is not a time.** Every rendered instant carries
 *    its IANA zone. When the payload omits `display_timezone` the component says
 *    the zone is unconfirmed rather than quietly formatting in the browser's
 *    zone and presenting that as the hospital's.
 */
export class SlotPicker extends PickerBase {
  static readonly componentName = 'SlotPicker';
  static readonly elementTag = 're-slot-picker';

  protected readonly groupName = 'slot';
  protected readonly noun = 'slot';
  protected readonly optionsKey = 'slots';
  protected readonly defaultTitle = 'Choose a time';
  protected readonly defaultEmptyReason = 'There are no open slots for this doctor right now.';

  protected override renderBanner(): TemplateResult | typeof nothing {
    const providerName = this.str('provider_name');
    const timeZone = this.str('display_timezone');

    if (providerName === undefined && timeZone === undefined) {
      return nothing;
    }

    return html`
      <p class="subtitle hospital-context">
        ${providerName !== undefined
          ? html`Times for <strong>${providerName}</strong>.`
          : nothing}
        ${timeZone !== undefined
          ? html` Shown in <span class="mono">${timeZone}</span>.`
          : html` <strong>Time zone not provided</strong> — times below are shown in this
              device's zone and may not match the hospital's.`}
      </p>
    `;
  }

  protected override renderRow(
    slot: Record<string, unknown>,
    index: number,
    selected: string | undefined,
    locked: boolean,
  ): TemplateResult {
    const id = stringField(slot, 'id') ?? `slot-${index}`;
    const timeZone = stringField(slot, 'timezone') ?? this.str('display_timezone');
    const start = formatInstant(stringField(slot, 'start_at'), timeZone);
    const end = formatInstant(stringField(slot, 'end_at'), timeZone);
    const visitType = stringField(slot, 'visit_type');
    const location = stringField(slot, 'location');
    const taken = slot['available'] === false;

    // The end time is only shown when both ends are known and share a zone —
    // otherwise "09:10" next to a different day's end time misleads.
    const range =
      end.text !== 'Time not provided' && stringField(slot, 'end_at') !== undefined
        ? `${start.text} – ${end.text.slice(-5)}`
        : start.text;

    return this.renderRadio({
      id,
      checked: selected === id,
      disabled: locked || taken,
      onChoose: () => {
        this.choose(slot, index);
      },
      body: html`
        <span class="option-name">${range}</span>
        <span class="option-meta">
          ${start.timeZoneKnown
            ? start.zone
            : `${start.zone} (hospital zone not confirmed)`}
          ${visitType !== undefined ? ` · ${visitType}` : nothing}
        </span>
        ${location !== undefined
          ? html`<span class="option-note">${location}</span>`
          : nothing}
        ${taken
          ? html`<span class="option-note">This slot was taken. Pick another.</span>`
          : nothing}
      `,
    });
  }

  protected override choose(slot: Record<string, unknown>, index: number): void {
    this.emitAction('slot.select', {
      slot_id: stringField(slot, 'id') ?? `slot-${index}`,
      start_at: stringField(slot, 'start_at') ?? null,
      provider_id: this.str('provider_id') ?? null,
    });
  }
}

if (customElements.get(SlotPicker.elementTag) === undefined) {
  customElements.define(SlotPicker.elementTag, SlotPicker);
}

declare global {
  interface HTMLElementTagNameMap {
    're-slot-picker': SlotPicker;
  }
}
