import { html, nothing, type TemplateResult } from 'lit';
import { ReferralElement } from './referral-element.js';
import { sharedStyles } from './shared-styles.js';
import { describeCount, describeSample, SYNTHETIC_LABEL, SYNTHETIC_TITLE } from './truth.js';

/**
 * The shell shared by the three selection steps.
 *
 * Hospitals, doctors and slots are three different questions but the same
 * *shape* of answer: a counted, ordered list of options where exactly one may be
 * chosen, and where the count must carry its sample size. Keeping the shell in
 * one place is what stops the three screens drifting apart, and stops each one
 * inventing its own way of saying "there is nothing here".
 *
 * What each subclass supplies:
 *   - the rows themselves (`renderRow`)
 *   - the radio group name and the action to raise
 *   - an empty-state sentence for when the list is genuinely empty
 */
export abstract class PickerBase extends ReferralElement {
  static override styles = [sharedStyles];

  /** `name` attribute shared by the radio inputs in this group. */
  protected abstract readonly groupName: string;

  /** Singular noun used when describing the count. */
  protected abstract readonly noun: string;

  /** The props key holding the option array. */
  protected abstract readonly optionsKey: string;

  /** Fallback heading when the server sends none. */
  protected abstract readonly defaultTitle: string;

  /** Shown when the server sent no `empty_reason` and the list is empty. */
  protected abstract readonly defaultEmptyReason: string;

  /** One option row. `locked` means the group accepts no further input. */
  protected abstract renderRow(
    option: Record<string, unknown>,
    index: number,
    selected: string | undefined,
    locked: boolean,
  ): TemplateResult;

  /** Raise the selection action for this step. */
  protected abstract choose(option: Record<string, unknown>, index: number): void;

  protected get options(): readonly Record<string, unknown>[] {
    return Array.isArray(this.rawProps[this.optionsKey]) ? this.list(this.optionsKey) : this.list('options');
  }

  protected get selectedId(): string | undefined {
    return this.str('selected_id');
  }

  /** True once the answer is in or the server has closed the question. */
  protected get locked(): boolean {
    return this.isDisabled || this.isSubmitted;
  }

  override render(): TemplateResult {
    const title = this.str('title') ?? this.defaultTitle;
    const subtitle = this.str('subtitle');
    const options = this.options;
    const synthetic = this.bool('synthetic') === true;

    return html`
      <section class="card" aria-labelledby="picker-title">
        <div class="head">
          <h2 class="title" id="picker-title">${title}</h2>
          ${synthetic
            ? html`<span class="chip synthetic" title=${SYNTHETIC_TITLE}>${SYNTHETIC_LABEL}</span>`
            : nothing}
        </div>

        ${subtitle ? html`<p class="subtitle">${subtitle}</p>` : nothing}
        ${this.renderBanner()}
        ${this.renderServerNotice()}

        ${options.length === 0
          ? html`<p class="empty" role="status">${this.emptyReason()}</p>`
          : html`
              <fieldset
                class="options"
                role="radiogroup"
                aria-labelledby="picker-title"
                ?disabled=${this.locked}
              >
                <legend class="visually-hidden">${title}</legend>
                ${options.map((option, index) =>
                  this.renderRow(option, index, this.selectedId, this.locked),
                )}
              </fieldset>
            `}

        <p class="meta sample">${describeSample(options.length, this.num('total'))}</p>

        ${options.length > 0 && this.selectedId === undefined
          ? html`<p class="meta hint">Nothing is selected yet. Choose one option to continue.</p>`
          : nothing}
      </section>
    `;
  }

  /** Extra banner above the list (coverage, availability). Subclasses opt in. */
  protected renderBanner(): TemplateResult | typeof nothing {
    return nothing;
  }

  protected renderServerNotice(): TemplateResult | typeof nothing {
    const notice = this.serverNotice;
    if (notice === undefined) {
      return nothing;
    }
    return html`<p class="notice info" role="status">
      <span class="glyph" aria-hidden="true">i</span><span>${notice}</span>
    </p>`;
  }

  protected emptyReason(): string {
    return this.str('empty_reason') ?? this.defaultEmptyReason;
  }

  /** Option count sentence, always paired with its sample caption. */
  protected countText(options: readonly unknown[]): string {
    return describeCount(options.length, { noun: this.noun }).text;
  }

  /**
   * A radio row.
   *
   * Native radios rather than ARIA-bolted buttons: arrow-key group navigation,
   * screen-reader grouping and label association all come free, and none of them
   * have to be re-implemented correctly.
   */
  protected renderRadio(options: {
    readonly id: string;
    readonly checked: boolean;
    readonly disabled: boolean;
    readonly onChoose: () => void;
    readonly body: TemplateResult;
  }): TemplateResult {
    return html`
      <label class="option ${options.checked ? 'selected' : ''} ${options.disabled ? 'disabled' : ''}">
        <input
          type="radio"
          name=${this.groupName}
          value=${options.id}
          .checked=${options.checked}
          ?disabled=${options.disabled}
          @change=${options.onChoose}
        />
        <span class="option-body">${options.body}</span>
      </label>
    `;
  }
}
