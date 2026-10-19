import { html, nothing, type TemplateResult } from 'lit';
import { ReferralElement } from './internal/referral-element.js';
import { sharedStyles } from './internal/shared-styles.js';
import {
  SYNTHETIC_LABEL,
  SYNTHETIC_TITLE,
} from './internal/truth.js';
import { listField, stringField } from './internal/fields.js';

/** Input types this component knows how to render natively. */
const SUPPORTED_TYPES = new Set(['text', 'email', 'tel', 'date', 'select', 'textarea']);

/**
 * The one real form in the flow.
 *
 * Three properties make this component different from the pickers:
 *
 * 1. **It collects patient data, and patient data never travels through
 *    LLM-generated HTML.** The server sends *field descriptors* — `{name, label,
 *    type, required}` — and the client maps each known `type` onto a native
 *    input. A descriptor naming a type this component does not implement renders
 *    a visible "unsupported field" note. Nothing the server sends is ever
 *    injected as markup: every value below goes through Lit's text bindings, so
 *    a hostile payload renders as visible characters, not as elements.
 *
 * 2. **The backend validates; the client only advises.** Local checks exist so
 *    the patient is not made to wait for an obvious omission, and are labelled
 *    as provisional. Server-supplied `errors` always win and are marked as such.
 *
 * 3. **Document attachment is synthetic (§2.3).** The fixture documents are shown
 *    as a labelled list, each marked as a fixture, so a demo can never be mistaken
 *    for a real upload. A document flagged `contains_injection_attempt` is shown
 *    with that flag rather than hidden — hiding it would make the evaluation
 *    dishonest.
 */
export class PatientDetailsForm extends ReferralElement {
  static readonly componentName = 'PatientDetailsForm';
  static readonly elementTag = 're-patient-details-form';

  static override styles = [sharedStyles];

  /**
   * Field names that failed the local, provisional check.
   *
   * Server-supplied errors always take precedence over these; a local error
   * clears as soon as the patient edits the field.
   */
  #advisoryErrors = new Map<string, string>();

  /** Fixture document ids the patient has ticked. */
  #attached = new Set<string>();

  override render(): TemplateResult {
    const title = this.str('title') ?? 'Your details';
    const subtitle = this.str('subtitle');
    const fields = this.list('fields');
    const documents = this.list('documents');
    const serverErrors = this.record('errors') ?? {};
    const values = this.record('values') ?? {};
    const synthetic = this.bool('synthetic') === true;
    const locked = this.isDisabled || this.isSubmitted;
    const submitLabel = this.str('submit_label') ?? 'Submit details';

    const unsupported = fields.filter(
      (field) => !SUPPORTED_TYPES.has(stringField(field, 'type') ?? 'text'),
    );

    return html`
      <section class="card" aria-labelledby="pdf-title">
        <div class="head">
          <h2 class="title" id="pdf-title">${title}</h2>
          ${synthetic
            ? html`<span class="chip synthetic" title=${SYNTHETIC_TITLE}>${SYNTHETIC_LABEL}</span>`
            : nothing}
        </div>

        ${subtitle !== undefined ? html`<p class="subtitle">${subtitle}</p>` : nothing}

        ${this.serverNotice !== undefined
          ? html`<p class="notice info" role="status">
              <span class="glyph" aria-hidden="true">i</span><span>${this.serverNotice}</span>
            </p>`
          : nothing}

        ${fields.length === 0
          ? html`<p class="empty" role="status">
              No detail fields were provided, so there is nothing to fill in yet.
            </p>`
          : html`
              <form class="fields" @submit=${(event: Event) => this.onSubmit(event)} novalidate>
                ${fields.map((field) => this.renderField(field, serverErrors, values, locked))}

                ${unsupported.length > 0
                  ? html`
                      <p class="notice unknown" role="status">
                        <span class="glyph" aria-hidden="true">?</span>
                        <span>
                          ${unsupported.length}
                          ${unsupported.length === 1 ? 'field uses a type' : 'fields use types'}
                          this client cannot render
                          (${unsupported
                            .map((field) => stringField(field, 'type') ?? 'unknown')
                            .join(', ')}). They are not shown rather than shown incorrectly.
                        </span>
                      </p>
                    `
                  : nothing}

                ${documents.length > 0 ? this.renderDocuments(documents, locked) : nothing}

                <p class="meta advisory">
                  These checks happen on your device and are provisional. Your details are
                  validated by the hospital when you submit.
                </p>

                <button class="primary block" type="submit" ?disabled=${locked}>
                  ${locked ? 'Submitted' : submitLabel}
                </button>
              </form>
            `}

        ${this.isSubmitted
          ? html`<p class="meta" role="status">Your details have been submitted.</p>`
          : nothing}
      </section>
    `;
  }

  // ── Fields ───────────────────────────────────────────────────────────────

  private renderField(
    field: Record<string, unknown>,
    serverErrors: Record<string, unknown>,
    values: Record<string, unknown>,
    locked: boolean,
  ): TemplateResult | typeof nothing {
    const name = stringField(field, 'name');
    if (name === undefined) {
      // A descriptor with no name cannot be posted anywhere. Rendering an
      // unlabelled input would collect data we could not send.
      return nothing;
    }

    const type = stringField(field, 'type') ?? 'text';
    if (!SUPPORTED_TYPES.has(type)) {
      return nothing; // Reported in the aggregate notice above.
    }

    const label = stringField(field, 'label') ?? name;
    const required = field['required'] === true;
    const hint = stringField(field, 'hint');
    const autocomplete = stringField(field, 'autocomplete');
    const options = listField(field, 'options');

    const serverError = typeof serverErrors[name] === 'string' ? (serverErrors[name] as string) : undefined;
    const advisoryError = this.#advisoryErrors.get(name);
    const error = serverError ?? advisoryError;
    const errorId = `err-${name}`;
    const hintId = `hint-${name}`;
    const describedBy = [error !== undefined ? errorId : undefined, hint !== undefined ? hintId : undefined]
      .filter((value): value is string => value !== undefined)
      .join(' ');

    const prefill = values[name];
    const value = typeof prefill === 'string' ? prefill : '';

    const common = {
      id: `field-${name}`,
      name,
      required,
      'aria-invalid': error !== undefined ? 'true' : 'false',
      'aria-describedby': describedBy.length > 0 ? describedBy : undefined,
      '?disabled': locked,
      '@input': (event: Event) => this.onFieldInput(event),
    };

    let control: TemplateResult;
    if (type === 'textarea') {
      control = html`<textarea ...=${common} rows="3" .value=${value}></textarea>`;
    } else if (type === 'select') {
      control = html`
        <select ...=${common} .value=${value}>
          <option value="">Please choose</option>
          ${options.map((option) => {
            const optionValue = stringField(option, 'value') ?? '';
            const optionLabel = stringField(option, 'label') ?? optionValue;
            return html`<option value=${optionValue}>${optionLabel}</option>`;
          })}
        </select>
      `;
    } else {
      control = html`
        <input
          ...=${common}
          type=${type === 'date' ? 'date' : type}
          autocomplete=${autocomplete ?? 'off'}
          .value=${value}
        />
      `;
    }

    return html`
      <div class="field">
        <label for="field-${name}">
          ${label}${required ? html` <span class="req" aria-hidden="true">*</span>` : nothing}
          ${required ? html`<span class="visually-hidden">(required)</span>` : nothing}
        </label>
        ${control}
        ${hint !== undefined && error === undefined
          ? html`<span class="field-hint" id=${hintId}>${hint}</span>`
          : nothing}
        ${error !== undefined
          ? html`<span class="field-error" id=${errorId} role="alert">
              ${error}${serverError !== undefined ? ' (from the hospital)' : ' (provisional)'}
            </span>`
          : nothing}
      </div>
    `;
  }

  private onFieldInput(event: Event): void {
    const target = event.target;
    if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement)) {
      return;
    }
    // Editing clears the local advisory; the server's verdict is re-issued on
    // the next payload and is not ours to clear.
    if (this.#advisoryErrors.delete(target.name)) {
      this.requestUpdate();
    }
  }

  // ── Documents (§2.3) ─────────────────────────────────────────────────────

  private renderDocuments(
    documents: readonly Record<string, unknown>[],
    locked: boolean,
  ): TemplateResult {
    return html`
      <fieldset class="documents" ?disabled=${locked} aria-describedby="doc-note">
        <legend class="doc-legend">Attach a document</legend>
        <p class="meta" id="doc-note">
          These are fixture documents from the offline profile. Nothing is uploaded and no real
          record is read.
        </p>
        ${documents.map((document, index) => this.renderDocument(document, index, locked))}
      </fieldset>
    `;
  }

  private renderDocument(
    document: Record<string, unknown>,
    index: number,
    locked: boolean,
  ): TemplateResult {
    const documentId = stringField(document, 'document_id') ?? `fixture-${index}`;
    const kind = stringField(document, 'kind') ?? 'document';
    const filename = stringField(document, 'filename');
    const synthetic = document['synthetic'] !== false;
    const injection = document['contains_injection_attempt'] === true;

    return html`
      <label class="option document">
        <input
          type="checkbox"
          name="documents"
          value=${documentId}
          ?disabled=${locked}
          @change=${(event: Event) => this.onDocumentToggle(event, documentId)}
        />
        <span class="option-body">
          <span class="option-name">${kind}</span>
          ${filename !== undefined ? html`<span class="option-meta mono">${filename}</span>` : nothing}
          ${synthetic
            ? html`<span class="chip synthetic option-chip">${SYNTHETIC_LABEL}</span>`
            : nothing}
          ${injection
            ? html`<span class="chip error option-chip">
                <span aria-hidden="true">✕</span>Injection attempt
              </span>`
            : nothing}
          ${injection
            ? html`<span class="option-note">
                This fixture contains a prompt-injection attempt. It is shown so the flow can be
                evaluated against it, not because it is safe.
              </span>`
            : nothing}
        </span>
      </label>
    `;
  }

  private onDocumentToggle(event: Event, documentId: string): void {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) {
      return;
    }
    if (target.checked) {
      this.#attached.add(documentId);
    } else {
      this.#attached.delete(documentId);
    }
  }

  // ── Submit ───────────────────────────────────────────────────────────────

  private onSubmit(event: Event): void {
    event.preventDefault();
    if (this.isDisabled || this.isSubmitted) {
      return;
    }

    const collected = this.#collect();
    this.#advisoryErrors = validateLocally(this.list('fields'), collected);
    if (this.#advisoryErrors.size > 0) {
      // Stop before the round trip, but say the check was local.
      this.requestUpdate();
      return;
    }

    this.emitAction('patient.details.submit', {
      values: collected,
      document_ids: [...this.#attached],
    });
  }

  #collect(): Record<string, string> {
    const root = this.renderRoot;
    const collected: Record<string, string> = {};
    for (const element of root.querySelectorAll('input[name], select[name], textarea[name]')) {
      if (
        element instanceof HTMLInputElement ||
        element instanceof HTMLSelectElement ||
        element instanceof HTMLTextAreaElement
      ) {
        if (element.name === 'documents') {
          continue;
        }
        collected[element.name] = element.value;
      }
    }
    return collected;
  }
}

/**
 * Local, provisional validation.
 *
 * Deliberately minimal. Its job is to spare the patient an obvious round trip,
 * not to be the authority — the backend validates against the component schema,
 * and anything stricter here would reject input the server would have accepted.
 */
function validateLocally(
  fields: readonly Record<string, unknown>[],
  values: Record<string, string>,
): Map<string, string> {
  const errors = new Map<string, string>();

  for (const field of fields) {
    const name = stringField(field, 'name');
    if (name === undefined || field['required'] !== true) {
      continue;
    }
    const value = values[name]?.trim() ?? '';
    if (value.length === 0) {
      errors.set(name, 'This is needed before we can send your referral.');
    }
  }

  return errors;
}

if (customElements.get(PatientDetailsForm.elementTag) === undefined) {
  customElements.define(PatientDetailsForm.elementTag, PatientDetailsForm);
}

declare global {
  interface HTMLElementTagNameMap {
    're-patient-details-form': PatientDetailsForm;
  }
}
