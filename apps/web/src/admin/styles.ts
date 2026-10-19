/**
 * The admin app's styles.
 *
 * Shares `tokens.css` with the patient client — the colours, spacing and type scale are the same
 * system, and duplicating them would let the two drift into different products — and deliberately
 * differs in **layout**: the patient column is capped at `--re-maxw` (560px) because a patient reads
 * one thing at a time on a phone, and an operator reads a table of many things on a wide screen.
 *
 * Nothing here draws a chart with a library. Every figure is a number, a bar whose width is the
 * value, and the sample count beside it; that is enough for the counts §12 asks for and it keeps the
 * honesty rule enforceable — a bar cannot render without the count it was computed from, because the
 * only helper that draws one requires the sample.
 */

import { css, type CSSResultGroup } from 'lit';

export const adminStyles: CSSResultGroup = css`
  :host {
    display: block;
    background: var(--re-bg);
    color: var(--re-ink);
    font-family: var(--re-font);
    font-size: var(--re-t-body);
    line-height: var(--re-lh-body);
    min-height: 100vh;
  }

  *,
  *::before,
  *::after {
    box-sizing: border-box;
  }

  a {
    color: var(--re-accent);
  }

  h1,
  h2,
  h3 {
    margin: 0;
    line-height: var(--re-lh-tight);
    font-weight: 600;
  }

  h1 {
    font-size: var(--re-t-title);
  }
  h2 {
    font-size: var(--re-t-lead);
  }
  h3 {
    font-size: var(--re-t-label);
  }

  /* ── frame ─────────────────────────────────────────────────────────────── */

  .frame {
    display: grid;
    grid-template-columns: 232px minmax(0, 1fr);
    min-height: 100vh;
  }

  @media (max-width: 860px) {
    .frame {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  .side {
    background: var(--re-surface);
    border-right: 1px solid var(--re-line);
    padding: var(--re-s4);
    display: flex;
    flex-direction: column;
    gap: var(--re-s4);
  }

  @media (max-width: 860px) {
    .side {
      border-right: 0;
      border-bottom: 1px solid var(--re-line);
    }
  }

  .brand {
    display: flex;
    flex-direction: column;
    gap: var(--re-s1);
  }

  .brand-name {
    font-size: var(--re-t-lead);
    font-weight: 600;
  }

  .brand-sub {
    font-size: var(--re-t-meta);
    color: var(--re-ink-2);
  }

  .nav {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  @media (max-width: 860px) {
    .nav {
      flex-direction: row;
      flex-wrap: wrap;
    }
  }

  .nav a {
    display: block;
    padding: var(--re-s2) var(--re-s3);
    border-radius: var(--re-r-sm);
    color: var(--re-ink-2);
    text-decoration: none;
    font-size: var(--re-t-label);
    font-weight: 500;
  }

  .nav a:hover {
    background: var(--re-surface-sunken);
    color: var(--re-ink);
  }

  .nav a[aria-current='page'] {
    background: var(--re-accent-soft);
    color: var(--re-accent);
  }

  .main {
    min-width: 0;
    display: flex;
    flex-direction: column;
  }

  .topbar {
    background: var(--re-surface);
    border-bottom: 1px solid var(--re-line);
    padding: var(--re-s4) var(--re-s5);
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--re-s2) var(--re-s4);
  }

  .topbar-title {
    font-size: var(--re-t-title);
    font-weight: 600;
  }

  .topbar-summary {
    color: var(--re-ink-2);
    font-size: var(--re-t-label);
    flex: 1 1 320px;
    min-width: 0;
  }

  .content {
    padding: var(--re-s5);
    display: flex;
    flex-direction: column;
    gap: var(--re-s5);
    max-width: 1240px;
  }

  /* ── cards ─────────────────────────────────────────────────────────────── */

  .card {
    background: var(--re-surface);
    border: 1px solid var(--re-line);
    border-radius: var(--re-r);
    box-shadow: var(--re-shadow-1);
    padding: var(--re-s4) var(--re-s5) var(--re-s5);
    min-width: 0;
  }

  .card-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--re-s2) var(--re-s3);
    margin-bottom: var(--re-s3);
  }

  .card-title {
    font-size: var(--re-t-lead);
    font-weight: 600;
  }

  .card-note {
    color: var(--re-ink-2);
    font-size: var(--re-t-meta);
    flex: 1 1 260px;
    min-width: 0;
  }

  .grid {
    display: grid;
    gap: var(--re-s4);
  }

  .grid.cols-2 {
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  }

  .grid.cols-3 {
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  }

  /* ── figures ───────────────────────────────────────────────────────────── */

  .figure {
    display: flex;
    flex-direction: column;
    gap: var(--re-s1);
  }

  .figure-value {
    font-size: var(--re-t-display);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
  }

  .figure-value.is-empty {
    color: var(--re-ink-3);
    font-weight: 500;
  }

  .figure-label {
    font-size: var(--re-t-label);
    color: var(--re-ink-2);
  }

  .figure-sample {
    font-size: var(--re-t-meta);
    color: var(--re-ink-3);
  }

  /* A bar is a div with a width, not a chart. Its label and sample sit beside it so the
     number and its provenance are read together. */
  .rows {
    display: flex;
    flex-direction: column;
    gap: var(--re-s2);
  }

  .row {
    display: grid;
    grid-template-columns: minmax(96px, 200px) minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--re-s3);
    font-size: var(--re-t-label);
  }

  @media (max-width: 620px) {
    .row {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  .row-key {
    color: var(--re-ink);
    overflow-wrap: anywhere;
  }

  .row-track {
    background: var(--re-surface-sunken);
    border: 1px solid var(--re-line);
    border-radius: var(--re-r-pill);
    height: 10px;
    overflow: hidden;
  }

  .row-bar {
    height: 100%;
    background: var(--re-accent);
    border-radius: var(--re-r-pill);
  }

  .row-value {
    font-variant-numeric: tabular-nums;
    color: var(--re-ink-2);
    white-space: nowrap;
  }

  .row-sample {
    font-size: var(--re-t-meta);
    color: var(--re-ink-3);
  }

  /* ── table ─────────────────────────────────────────────────────────────── */

  .table-wrap {
    overflow-x: auto;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--re-t-label);
  }

  th,
  td {
    text-align: left;
    padding: var(--re-s2) var(--re-s3);
    border-bottom: 1px solid var(--re-line);
    vertical-align: top;
  }

  th {
    color: var(--re-ink-2);
    font-weight: 600;
    font-size: var(--re-t-meta);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }

  td.num,
  th.num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  tbody tr:last-child td {
    border-bottom: 0;
  }

  /* ── chips and states ──────────────────────────────────────────────────── */

  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--re-s1);
    padding: 2px var(--re-s2);
    border-radius: var(--re-r-pill);
    border: 1px solid var(--re-line);
    background: var(--re-surface-sunken);
    color: var(--re-ink-2);
    font-size: var(--re-t-meta);
    white-space: nowrap;
  }

  .chip.synthetic {
    border-color: var(--re-synthetic-line);
    background: var(--re-synthetic-bg);
    color: var(--re-synthetic);
  }

  .chip.unknown {
    border-color: var(--re-unknown-line);
    background: var(--re-unknown-bg);
    color: var(--re-unknown);
  }

  .chip.error {
    border-color: var(--re-error-line);
    background: var(--re-error-bg);
    color: var(--re-error);
  }

  /* ── notices ───────────────────────────────────────────────────────────── */

  .notice {
    border: 1px solid var(--re-unknown-line);
    background: var(--re-unknown-bg);
    color: var(--re-unknown);
    border-radius: var(--re-r-sm);
    padding: var(--re-s3) var(--re-s4);
    font-size: var(--re-t-label);
    display: flex;
    flex-direction: column;
    gap: var(--re-s1);
  }

  .notice.error {
    border-color: var(--re-error-line);
    background: var(--re-error-bg);
    color: var(--re-error);
  }

  .notice strong {
    font-weight: 600;
  }

  .notice code {
    font-family: var(--re-font-mono);
    font-size: var(--re-t-meta);
    overflow-wrap: anywhere;
  }

  /* ── controls ──────────────────────────────────────────────────────────── */

  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--re-s3);
  }

  .control {
    display: flex;
    flex-direction: column;
    gap: var(--re-s1);
  }

  .control > span {
    font-size: var(--re-t-meta);
    color: var(--re-ink-2);
  }

  select,
  input[type='datetime-local'],
  button {
    font: inherit;
    font-size: var(--re-t-label);
    min-height: 36px;
    padding: 0 var(--re-s3);
    border: 1px solid var(--re-line-strong);
    border-radius: var(--re-r-sm);
    background: var(--re-surface);
    color: var(--re-ink);
  }

  button {
    cursor: pointer;
  }

  button:hover:not(:disabled) {
    border-color: var(--re-accent);
    color: var(--re-accent);
  }

  button:disabled {
    cursor: default;
    color: var(--re-ink-3);
  }

  :focus-visible {
    outline: 2px solid var(--re-accent);
    outline-offset: 2px;
  }

  .loading {
    color: var(--re-ink-2);
    font-size: var(--re-t-label);
  }

  footer.foot {
    padding: var(--re-s4) var(--re-s5) var(--re-s6);
    color: var(--re-ink-3);
    font-size: var(--re-t-meta);
    max-width: 1240px;
  }
`;
