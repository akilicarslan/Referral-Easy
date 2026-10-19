import { html, type TemplateResult } from 'lit';
import { describeCoverage, hoursSince, type CoverageReadout } from './truth.js';

/**
 * Defensive readers for the free-form records the gateway sends.
 *
 * Every one of these returns `undefined` rather than a substitute value, so the
 * caller is forced to decide what a missing field *means* and say it on screen.
 * A default like `''` or `'Unknown hospital'` would turn a missing payload into
 * a confident-looking claim.
 */

export function stringField(source: Record<string, unknown>, key: string): string | undefined {
  const value = source[key];
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

export function numberField(source: Record<string, unknown>, key: string): number | undefined {
  const value = source[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

export function boolField(source: Record<string, unknown>, key: string): boolean | undefined {
  const value = source[key];
  return typeof value === 'boolean' ? value : undefined;
}

export function recordField(
  source: Record<string, unknown>,
  key: string,
): Record<string, unknown> | undefined {
  const value = source[key];
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return undefined;
  }
  return value as Record<string, unknown>;
}

export function listField(
  source: Record<string, unknown>,
  key: string,
): readonly Record<string, unknown>[] {
  const value = source[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (item): item is Record<string, unknown> =>
      item !== null && typeof item === 'object' && !Array.isArray(item),
  );
}

/** Build a `city · region · 4 km away` line, omitting whatever is missing. */
export function metaLine(...parts: (string | undefined)[]): string | undefined {
  const present = parts.filter((part): part is string => part !== undefined && part.length > 0);
  return present.length > 0 ? present.join(' · ') : undefined;
}

/**
 * A row's network chip.
 *
 * A missing `coverage` renders as "Not checked" — never as in-network, and never
 * as out-of-network. An absent field is an absence of evidence, and the contract
 * is explicit that `unknown` must be shown as unknown rather than smoothed into
 * "no".
 */
export function renderCoverageChip(hospital: Record<string, unknown>): TemplateResult {
  const coverage = stringField(hospital, 'coverage');
  const explicitAge = numberField(hospital, 'coverage_age_hours');
  const readout = describeCoverage(
    coverage,
    explicitAge ?? hoursSince(stringField(hospital, 'coverage_checked_at')),
  );

  return html`
    <span class="chip ${readout.tone} option-chip">
      <span aria-hidden="true">${readout.glyph}</span>${readout.label}
    </span>
  `;
}

/** A standalone notice built from a coverage readout. */
export function renderCoverageNotice(readout: CoverageReadout): TemplateResult {
  return html`
    <p class="notice ${readout.tone} coverage-banner" role="status">
      <span class="glyph" aria-hidden="true">${readout.glyph}</span>
      <span><strong>${readout.label}.</strong> ${readout.sentence}</span>
    </p>
  `;
}
