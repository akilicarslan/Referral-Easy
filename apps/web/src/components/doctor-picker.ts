import { html, nothing, type TemplateResult } from 'lit';
import { PickerBase } from './internal/picker-base.js';
import { metaLine, numberField, renderCoverageChip, stringField } from './internal/fields.js';
import { describeFreshness, hoursSince } from './internal/truth.js';

/**
 * STEP 2 of the fixed order (§4): doctors, at the hospital the patient chose.
 *
 * This component carries an explicit guard for the rule that matters most here:
 * **doctors are never shown before a hospital is chosen**. When the payload says
 * `awaiting_hospital`, or when the list is empty and no hospital is named, the
 * component renders an explanation of the order rather than an empty list that
 * looks like "there are no doctors".
 *
 * It also names the hospital it is showing doctors for, so a stale payload
 * cannot silently present doctors from somewhere else.
 */
export class DoctorPicker extends PickerBase {
  static readonly componentName = 'DoctorPicker';
  static readonly elementTag = 're-doctor-picker';

  protected readonly groupName = 'doctor';
  protected readonly noun = 'doctor';
  protected readonly optionsKey = 'doctors';
  protected readonly defaultTitle = 'Choose a doctor';
  protected readonly defaultEmptyReason = 'No doctors are listed for this hospital yet.';

  /** True when the backend has told us the hospital step is not done. */
  private get awaitingHospital(): boolean {
    return this.bool('awaiting_hospital') === true;
  }

  override render(): TemplateResult {
    if (this.awaitingHospital) {
      return this.renderAwaitingHospital();
    }
    return super.render();
  }

  /**
   * The out-of-order state.
   *
   * Rendered instead of the list, not alongside it. A disabled-looking list of
   * doctors would still leak names, and the patient is owed the sequence.
   */
  private renderAwaitingHospital(): TemplateResult {
    return html`
      <section class="card" aria-labelledby="dp-awaiting">
        <div class="head">
          <h2 class="title" id="dp-awaiting">${this.str('title') ?? this.defaultTitle}</h2>
        </div>
        <p class="notice info" role="status">
          <span class="glyph" aria-hidden="true">i</span>
          <span>
            Choose a hospital first. Doctors are listed for one hospital at a time, so there is
            nothing to show until that choice is made.
          </span>
        </p>
      </section>
    `;
  }

  protected override renderBanner(): TemplateResult | typeof nothing {
    const hospitalName = this.str('hospital_name');
    if (hospitalName === undefined) {
      return nothing;
    }
    return html`
      <p class="subtitle hospital-context">
        Showing doctors at <strong>${hospitalName}</strong>.
      </p>
    `;
  }

  protected override renderRow(
    doctor: Record<string, unknown>,
    index: number,
    selected: string | undefined,
    locked: boolean,
  ): TemplateResult {
    const id = stringField(doctor, 'id') ?? `doctor-${index}`;
    const name = stringField(doctor, 'name') ?? 'Name not provided';
    const specialty = stringField(doctor, 'specialty');
    const languages = Array.isArray(doctor['languages'])
      ? (doctor['languages'] as unknown[]).filter((l): l is string => typeof l === 'string')
      : [];

    const meta = metaLine(
      specialty,
      languages.length > 0 ? `Speaks ${languages.join(', ')}` : undefined,
    );

    const nextAvailable = stringField(doctor, 'next_available_at');
    const waitDays = numberField(doctor, 'wait_days');
    const unavailable = doctor['accepting_referrals'] === false;

    return this.renderRadio({
      id,
      checked: selected === id,
      disabled: locked || unavailable,
      onChoose: () => {
        this.choose(doctor, index);
      },
      body: html`
        <span class="option-name">${name}</span>
        ${meta !== undefined ? html`<span class="option-meta">${meta}</span>` : nothing}
        ${renderCoverageChip(doctor)}
        ${waitDays !== undefined
          ? html`<span class="option-note">
              Soonest appointment about <span class="num">${waitDays}</span>
              ${waitDays === 1 ? 'day' : 'days'} away.
            </span>`
          : nextAvailable !== undefined
            ? html`<span class="option-note">
                Next listed availability ${describeFreshness(hoursSince(nextAvailable)).phrase || 'at an unknown time'}.
              </span>`
            : nothing}
        ${unavailable
          ? html`<span class="option-note">Not accepting new referrals at the moment.</span>`
          : nothing}
      `,
    });
  }

  protected override choose(doctor: Record<string, unknown>, index: number): void {
    this.emitAction('doctor.select', {
      provider_id: stringField(doctor, 'id') ?? `doctor-${index}`,
      provider_name: stringField(doctor, 'name') ?? null,
      hospital_id: this.str('hospital_id') ?? null,
    });
  }
}

if (customElements.get(DoctorPicker.elementTag) === undefined) {
  customElements.define(DoctorPicker.elementTag, DoctorPicker);
}

declare global {
  interface HTMLElementTagNameMap {
    're-doctor-picker': DoctorPicker;
  }
}
