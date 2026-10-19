import { html, nothing, type TemplateResult } from 'lit';
import { PickerBase } from './internal/picker-base.js';
import {
  metaLine,
  numberField,
  renderCoverageChip,
  renderCoverageNotice,
  stringField,
} from './internal/fields.js';
import { describeCoverage, describeFreshness, hoursSince } from './internal/truth.js';

/**
 * STEP 1 of the fixed order (§4): hospitals first.
 *
 * Nothing here may show a doctor or a time.
 *
 * Coverage is a *section variant of this component*, not a ninth component: when
 * the backend sends a `coverage` field the list gains a network readout and each
 * row gains its own chip. When it does not, rows say "Not checked" rather than
 * implying the hospital is in network.
 *
 * Nothing is preselected. `selected_id: null` renders as an unanswered question,
 * because that is exactly what it is.
 */
export class HospitalPicker extends PickerBase {
  static readonly componentName = 'HospitalPicker';
  static readonly elementTag = 're-hospital-picker';

  protected readonly groupName = 'hospital';
  protected readonly noun = 'hospital';
  protected readonly optionsKey = 'hospitals';
  protected readonly defaultTitle = 'Choose a hospital';
  protected readonly defaultEmptyReason = 'No hospitals are available to choose from right now.';

  protected override renderBanner(): TemplateResult | typeof nothing {
    const coverage = this.str('coverage');
    if (coverage === undefined) {
      return nothing;
    }

    const freshness = this.record('coverage_freshness');
    const ageHours =
      typeof freshness?.['age_hours'] === 'number'
        ? (freshness['age_hours'] as number)
        : hoursSince(
            typeof freshness?.['checked_at'] === 'string'
              ? (freshness['checked_at'] as string)
              : undefined,
          );

    return renderCoverageNotice(describeCoverage(coverage, ageHours));
  }

  protected override renderRow(
    hospital: Record<string, unknown>,
    index: number,
    selected: string | undefined,
    locked: boolean,
  ): TemplateResult {
    const id = stringField(hospital, 'id') ?? `hospital-${index}`;
    const name = stringField(hospital, 'name') ?? 'Name not provided';
    const distance = numberField(hospital, 'distance_km');
    const meta = metaLine(
      stringField(hospital, 'city'),
      stringField(hospital, 'region'),
      distance === undefined ? undefined : `${distance} km away`,
    );

    const lastSeen = stringField(hospital, 'last_seen');
    const unavailable = hospital['accepting_referrals'] === false;

    return this.renderRadio({
      id,
      checked: selected === id,
      disabled: locked || unavailable,
      onChoose: () => {
        this.choose(hospital, index);
      },
      body: html`
        <span class="option-name">${name}</span>
        ${meta !== undefined ? html`<span class="option-meta">${meta}</span>` : nothing}
        ${renderCoverageChip(hospital)}
        ${unavailable
          ? html`<span class="option-note">Not accepting referrals at the moment.</span>`
          : nothing}
        ${lastSeen !== undefined
          ? html`<span class="option-note"
              >Record last seen ${describeFreshness(hoursSince(lastSeen)).phrase || 'at an unknown time'}.</span
            >`
          : nothing}
      `,
    });
  }

  protected override choose(hospital: Record<string, unknown>, index: number): void {
    this.emitAction('hospital.select', {
      hospital_id: stringField(hospital, 'id') ?? `hospital-${index}`,
      hospital_name: stringField(hospital, 'name') ?? null,
    });
  }
}

if (customElements.get(HospitalPicker.elementTag) === undefined) {
  customElements.define(HospitalPicker.elementTag, HospitalPicker);
}

declare global {
  interface HTMLElementTagNameMap {
    're-hospital-picker': HospitalPicker;
  }
}
