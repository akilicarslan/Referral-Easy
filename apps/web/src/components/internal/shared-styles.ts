import { css, type CSSResultGroup } from 'lit';

/**
 * Styles shared by every component's shadow root.
 *
 * This deliberately mirrors the primitives in `styles/base.css`. The duplication
 * is the cost of Shadow DOM encapsulated components: a component rendered inside
 * an A2UI surface cannot rely on a document-level stylesheet reaching it. Design
 * tokens are NOT duplicated — custom properties inherit through a shadow
 * boundary, so `var(--re-ink)` resolves to the same value in both places, and a
 * single change in `tokens.css` still moves every component.
 */
export const sharedStyles: CSSResultGroup = css`
  :host {
    display: block;
    color: var(--re-ink);
    font-family: var(--re-font);
    font-size: var(--re-t-body);
    line-height: var(--re-lh-body);
  }

  :host([hidden]) {
    display: none;
  }

  *, *::before, *::after { box-sizing: border-box; }

  h2,
  h3,
  p {
    margin: 0;
  }

  .card {
    background: var(--re-surface);
    border: 1px solid var(--re-line);
    border-radius: var(--re-r);
    padding: var(--re-s4);
  }

  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--re-s3);
    margin-bottom: var(--re-s1);
  }

  .title {
    font-size: var(--re-t-lead);
    font-weight: 600;
    line-height: var(--re-lh-tight);
  }

  .subtitle {
    font-size: var(--re-t-label);
    color: var(--re-ink-2);
    margin-top: var(--re-s1);
  }

  .eyebrow {
    font-size: var(--re-t-meta);
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--re-ink-3);
  }

  .meta {
    font-size: var(--re-t-meta);
    color: var(--re-ink-3);
    line-height: var(--re-lh-body);
  }

  .num {
    font-variant-numeric: tabular-nums;
    font-feature-settings: 'tnum' 1;
  }

  hr {
    border: 0;
    border-top: 1px solid var(--re-line);
    margin: var(--re-s4) 0;
  }

  /* ── Buttons ─────────────────────────────────────────────────────────── */
  button {
    font: inherit;
    font-size: var(--re-t-body);
    font-weight: 600;
    cursor: pointer;
    border-radius: var(--re-r);
    border: 1px solid transparent;
    min-height: var(--re-tap);
    padding: 0 var(--re-s5);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--re-s2);
    transition: background-color 150ms var(--re-ease), border-color 150ms var(--re-ease);
    -webkit-tap-highlight-color: transparent;
  }

  button:focus-visible {
    outline: 2px solid var(--re-accent);
    outline-offset: 2px;
  }

  button.primary {
    background: var(--re-accent);
    color: var(--re-accent-ink);
  }
  button.primary:hover:not(:disabled) {
    background: var(--re-accent-hover);
  }
  button.primary:active:not(:disabled) {
    background: var(--re-accent-active);
  }

  button.secondary {
    background: var(--re-surface);
    color: var(--re-ink);
    border-color: var(--re-line-strong);
  }
  button.secondary:hover:not(:disabled) {
    background: var(--re-surface-sunken);
  }

  button.quiet {
    background: transparent;
    color: var(--re-accent);
    padding: 0 var(--re-s3);
  }
  button.quiet:hover:not(:disabled) {
    background: var(--re-accent-soft);
  }

  button:disabled {
    background: var(--re-bg);
    color: var(--re-ink-3);
    border-color: var(--re-line);
    cursor: not-allowed;
  }

  button.block {
    width: 100%;
  }

  /* ── Chips: glyph + word. Colour reinforces, it never carries the message. ── */
  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--re-s1);
    padding: 2px var(--re-s2);
    border-radius: var(--re-r-pill);
    border: 1px solid;
    font-size: var(--re-t-meta);
    font-weight: 600;
    line-height: 1.6;
    white-space: nowrap;
  }

  .chip.ok {
    background: var(--re-ok-bg);
    color: var(--re-ok);
    border-color: var(--re-ok-line);
  }
  .chip.requested {
    background: var(--re-requested-bg);
    color: var(--re-requested);
    border-color: var(--re-requested-line);
  }
  .chip.unknown {
    background: var(--re-unknown-bg);
    color: var(--re-unknown);
    border-color: var(--re-unknown-line);
  }
  .chip.degraded {
    background: var(--re-degraded-bg);
    color: var(--re-degraded);
    border-color: var(--re-degraded-line);
  }
  .chip.error {
    background: var(--re-error-bg);
    color: var(--re-error);
    border-color: var(--re-error-line);
  }
  .chip.synthetic {
    background: var(--re-synthetic-bg);
    color: var(--re-synthetic);
    border-color: var(--re-synthetic-line);
  }
  .chip.neutral {
    background: var(--re-surface-sunken);
    color: var(--re-ink-2);
    border-color: var(--re-line);
  }

  /* ── Notices ─────────────────────────────────────────────────────────── */
  .notice {
    display: flex;
    gap: var(--re-s2);
    padding: var(--re-s3);
    border-radius: var(--re-r-sm);
    border: 1px solid;
    font-size: var(--re-t-label);
    line-height: var(--re-lh-body);
  }
  .notice .glyph {
    flex: none;
  }
  .notice.unknown {
    background: var(--re-unknown-bg);
    border-color: var(--re-unknown-line);
    color: var(--re-unknown);
  }
  .notice.degraded {
    background: var(--re-degraded-bg);
    border-color: var(--re-degraded-line);
    color: var(--re-degraded);
  }
  .notice.error {
    background: var(--re-error-bg);
    border-color: var(--re-error-line);
    color: var(--re-error);
  }
  .notice.info {
    background: var(--re-surface-sunken);
    border-color: var(--re-line);
    color: var(--re-ink-2);
  }

  /* ── Empty state: never just blank ───────────────────────────────────── */
  .empty {
    padding: var(--re-s5) var(--re-s4);
    border: 1px dashed var(--re-line-strong);
    border-radius: var(--re-r);
    background: var(--re-surface-sunken);
    text-align: center;
    color: var(--re-ink-2);
    font-size: var(--re-t-label);
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }

  .mono {
    font-family: var(--re-font-mono);
    font-size: var(--re-t-meta);
  }

  /* ── Option list (hospitals, doctors, slots) ─────────────────────────── */
  .options {
    display: flex;
    flex-direction: column;
    /* The 2px here plus each row's own padding keeps adjacent rows at least
       8px apart, so a mistimed thumb does not select the neighbour. */
    gap: var(--re-s2);
    border: 0;
    margin: var(--re-s4) 0 0;
    padding: 0;
    min-inline-size: 0;
  }

  .options:disabled {
    opacity: 1;
  }

  .option {
    display: flex;
    align-items: flex-start;
    gap: var(--re-s3);
    /* 44px minimum, met by content height plus this padding. */
    padding: var(--re-s3);
    border: 1px solid var(--re-line);
    border-radius: var(--re-r);
    background: var(--re-surface);
    cursor: pointer;
    transition: border-color 150ms var(--re-ease), background-color 150ms var(--re-ease);
  }

  .option:hover:not(.disabled) {
    border-color: var(--re-line-strong);
    background: var(--re-surface-sunken);
  }

  .option.selected {
    border-color: var(--re-accent);
    background: var(--re-accent-soft);
  }

  .option.disabled {
    cursor: not-allowed;
    background: var(--re-surface-sunken);
  }

  .option:focus-within {
    outline: 2px solid var(--re-accent);
    outline-offset: 2px;
  }

  .option input[type='radio'],
  .option input[type='checkbox'] {
    flex: none;
    /* A 20px control inside a 44px row, with the row as the real hit target. */
    margin: var(--re-s3) 0 0;
    width: 20px;
    height: 20px;
    accent-color: var(--re-accent);
    cursor: inherit;
  }

  .option input[type='radio'] {
    appearance: none;
    border: 1.5px solid var(--re-line-strong);
    border-radius: 50%;
    background: var(--re-surface);
  }
  .option input[type='radio']:checked {
    border: 6px solid var(--re-accent);
  }
  .option input[type='checkbox'] {
    appearance: none;
    border: 1.5px solid var(--re-line-strong);
    border-radius: 4px;
    background: var(--re-surface);
  }
  .option input[type='checkbox']:checked {
    border-color: var(--re-accent);
    background: var(--re-accent);
  }
  .option input[type='checkbox']:checked::after {
    content: '✓';
    display: block;
    color: white;
    font-size: 16px;
    line-height: 17px;
    text-align: center;
  }
  @media (forced-colors: active) {
    .option input[type='radio'], .option input[type='checkbox'] { appearance: auto; }
    .option input[type='checkbox']:checked::after { content: none; }
  }

  .option-body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }

  .option-name {
    font-size: var(--re-t-body);
    font-weight: 600;
    line-height: var(--re-lh-tight);
  }

  .option-meta {
    font-size: var(--re-t-label);
    color: var(--re-ink-2);
  }

  .option-note {
    font-size: var(--re-t-meta);
    color: var(--re-ink-2);
    line-height: var(--re-lh-body);
  }

  .option-chip {
    align-self: flex-start;
    margin-top: var(--re-s1);
  }

  .option-extra {
    margin-top: var(--re-s2);
    display: flex;
    flex-wrap: wrap;
    gap: var(--re-s2);
  }

  .coverage-banner {
    margin: var(--re-s3) 0 0;
  }

  .sample {
    margin-top: var(--re-s3);
  }

  .hint {
    margin-top: var(--re-s2);
  }

  /* ── Definition rows (review, receipt) ───────────────────────────────── */
  dl.facts {
    margin: 0;
    display: grid;
    grid-template-columns: minmax(0, 38%) minmax(0, 1fr);
    gap: var(--re-s2) var(--re-s4);
  }

  dl.facts dt {
    font-size: var(--re-t-label);
    color: var(--re-ink-2);
    min-width: 0;
  }

  dl.facts dd {
    margin: 0;
    font-size: var(--re-t-label);
    font-weight: 600;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  /* ── Form fields ─────────────────────────────────────────────────────── */
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--re-s1);
  }

  .field label {
    font-size: var(--re-t-label);
    font-weight: 600;
    color: var(--re-ink);
  }

  .field input[type='text'],
  .field input[type='email'],
  .field input[type='tel'],
  .field input[type='date'],
  .field select,
  .field textarea {
    font: inherit;
    font-size: var(--re-t-body);
    min-height: var(--re-tap);
    padding: var(--re-s2) var(--re-s3);
    border: 1px solid var(--re-line-strong);
    border-radius: var(--re-r-sm);
    background: var(--re-surface);
    color: var(--re-ink);
    width: 100%;
  }

  .field input:focus-visible,
  .field select:focus-visible,
  .field textarea:focus-visible {
    outline: 2px solid var(--re-accent);
    outline-offset: 1px;
    border-color: var(--re-accent);
  }

  .field input[aria-invalid='true'] {
    border-color: var(--re-error);
  }

  .field .field-error {
    font-size: var(--re-t-meta);
    color: var(--re-error);
  }

  .field .field-hint {
    font-size: var(--re-t-meta);
    color: var(--re-ink-3);
  }

  .req {
    color: var(--re-error);
  }

  form.fields {
    display: flex;
    flex-direction: column;
    gap: var(--re-s4);
    margin-top: var(--re-s4);
  }

  fieldset.documents {
    display: flex;
    flex-direction: column;
    gap: var(--re-s2);
    border: 0;
    border-top: 1px solid var(--re-line);
    margin: var(--re-s2) 0 0;
    padding: var(--re-s4) 0 0;
    min-inline-size: 0;
  }

  .doc-legend {
    font-size: var(--re-t-body);
    font-weight: 600;
    padding: 0;
    margin-bottom: var(--re-s1);
  }

  .advisory {
    margin: 0;
  }

  /* ── Receipt / status block ──────────────────────────────────────────── */
  .status-block {
    display: flex;
    align-items: flex-start;
    gap: var(--re-s3);
    padding: var(--re-s4);
    border-radius: var(--re-r);
    border: 1px solid;
  }
  .status-block.ok {
    background: var(--re-ok-bg);
    border-color: var(--re-ok-line);
  }
  .status-block.requested {
    background: var(--re-requested-bg);
    border-color: var(--re-requested-line);
  }
  .status-block.unknown {
    background: var(--re-unknown-bg);
    border-color: var(--re-unknown-line);
  }
  .status-block.error {
    background: var(--re-error-bg);
    border-color: var(--re-error-line);
  }
  .status-block .glyph {
    font-size: var(--re-t-lead);
    line-height: 1.2;
    flex: none;
  }
  .status-block .status-title {
    font-size: var(--re-t-lead);
    font-weight: 600;
    line-height: var(--re-lh-tight);
  }
  .status-block .status-sentence {
    font-size: var(--re-t-label);
    margin-top: var(--re-s1);
  }
  .status-block.ok .status-title,
  .status-block.ok .status-sentence {
    color: var(--re-ok);
  }
  .status-block.requested .status-title,
  .status-block.requested .status-sentence {
    color: var(--re-requested);
  }
  .status-block.unknown .status-title,
  .status-block.unknown .status-sentence {
    color: var(--re-unknown);
  }
  .status-block.error .status-title,
  .status-block.error .status-sentence {
    color: var(--re-error);
  }

  .reference {
    margin-top: var(--re-s3);
    padding: var(--re-s3);
    background: var(--re-surface-sunken);
    border: 1px solid var(--re-line);
    border-radius: var(--re-r-sm);
    font-family: var(--re-font-mono);
    font-size: var(--re-t-meta);
    overflow-wrap: anywhere;
  }

  ul.plain {
    margin: var(--re-s2) 0 0;
    padding-left: var(--re-s4);
    font-size: var(--re-t-label);
    color: var(--re-ink-2);
  }
  ul.plain li {
    margin-bottom: 2px;
  }

  .actions {
    display: flex;
    gap: var(--re-s3);
    margin-top: var(--re-s4);
    flex-wrap: wrap;
  }
  .actions button {
    flex: 1 1 auto;
  }

  .consent,
  .attachments {
    margin-top: var(--re-s4);
  }

  .consent {
    align-items: flex-start;
  }

  dl.facts + dl.facts {
    margin-top: var(--re-s4);
  }

  .unchecked,
  .last-known,
  .handed-over,
  .channels {
    margin-top: var(--re-s4);
  }

  .last-known {
    padding: var(--re-s3);
    border: 1px dashed var(--re-line-strong);
    border-radius: var(--re-r-sm);
    background: var(--re-surface-sunken);
  }

  .last-known dl.facts {
    margin-top: var(--re-s2);
  }
`;
