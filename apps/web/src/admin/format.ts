/**
 * Turning the admin payloads into text, with one rule: **`null` is "no data", never zero**.
 *
 * §14 says it for scores (*"N/A and eval_error are not scored as zero"*) and §15 says it for
 * latency
 * percentiles (a zero would plot an impossibly fast system). Both arrive here as `null`, and every
 * function below has a branch for it rather than a `?? 0`, because `?? 0` is exactly the mistake
 * that turns a gap into a measurement and it is invisible at the call site.
 *
 * The same rule governs the *sample*: a number is always rendered with how many things it came from,
 * so a metric computed over two events cannot read like one computed over two thousand.
 */

import type { Bucket, Percentiles, Sample } from './api.js';

/** The one string for "we have no measurement here". Not "0", not "—", not "n/a". */
export const NO_DATA = 'no data';

/** A percentile, in milliseconds, or "no data". */
export function milliseconds(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return NO_DATA;
  }
  // Two decimals below 10ms (where they matter) and one above it (where they do not).
  return `${value < 10 ? value.toFixed(2) : value.toFixed(1)} ms`;
}

/** A rate as a percentage, or "no data". Used for `like_rate`, `participation_rate` and scores. */
export function percentage(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return NO_DATA;
  }
  return `${(value * 100).toFixed(1)}%`;
}

/** A plain number with thousands separators, or "no data". */
export function number(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return NO_DATA;
  }
  return value.toLocaleString('en-US');
}

/**
 * The provenance line under every figure: what it was computed from.
 *
 * Duplicates and truncation are named when present, because both change what a reader should
 * conclude — §15's dedup counts each event once, and a truncated aggregation is a *subset* that
 * would otherwise look like a small total.
 */
export function provenance(sample: Sample | null): string {
  if (sample === null) {
    return 'sample: unknown';
  }
  const parts = [`${sample.count} event${sample.count === 1 ? '' : 's'}`];
  if (sample.duplicated > 0) {
    parts.push(`${sample.duplicated} duplicate${sample.duplicated === 1 ? '' : 's'} collapsed`);
  }
  if (sample.truncated) {
    parts.push(`truncated${sample.other > 0 ? `, ${sample.other} in "other"` : ''}`);
  }
  return `sample: ${parts.join(' · ')}`;
}

/** One row of a bar list: a key, the bar's fraction of the maximum, and the raw value. */
export interface BarRow {
  readonly key: string;
  readonly value: number;
  readonly valueText: string;
  /** 0–100. A row whose value is 0 gets width 0 and still renders — it is a real zero here. */
  readonly percent: number;
  readonly sample: Sample | null;
}

/**
 * Turn buckets into bar rows scaled against the largest value.
 *
 * **The scale is the maximum, and the maximum is stated separately**, because a bar chart with a
 * hidden scale is an implied comparison nobody can check. A bucket whose `value` is `null` is
 * dropped rather than plotted at zero: the aggregation found the key and could not measure it, which
 * is not the same as measuring nothing.
 */
export function barRows(
  buckets: readonly Bucket[],
  format: (value: number) => string = number,
): readonly BarRow[] {
  const measured = buckets
    .map((bucket) => ({ bucket, value: bucket.value }))
    .filter((entry): entry is { bucket: Bucket; value: number } => entry.value !== null);
  const max = measured.reduce((highest, entry) => Math.max(highest, entry.value), 0);
  return measured.map(({ bucket, value }) => ({
    key: bucket.key,
    value,
    valueText: format(value),
    // `max === 0` is every bucket measuring zero; every bar is empty and every number is 0, which is
    // a real and readable state, so it is not special-cased into "no data".
    percent: max === 0 ? 0 : (value / max) * 100,
    sample: bucket.sample ?? null,
  }));
}

/** One key/value line from a `[key, value]` map such as `outcome_breakdown`. */
export function mapRows(
  values: Readonly<Record<string, Sample>>,
  format: (value: number) => string = number,
): readonly BarRow[] {
  return barRows(
    Object.entries(values).map(([key, sample]) => ({ key, value: sample.count, sample })),
    format,
  );
}

/** One percentile set, as the three numbers §15 asks for. */
export interface PercentileRow {
  readonly label: string;
  readonly text: string;
  readonly sample: Sample | null;
}

/**
 * p50/p95/p99 for one distribution.
 *
 * All three are rendered even when some are missing, so a distribution with a p95 and no p99 shows
 * the gap rather than showing two numbers and looking complete.
 */
export function percentileRows(percentiles: Percentiles | undefined | null): readonly PercentileRow[] {
  if (percentiles === null || percentiles === undefined) {
    return [];
  }
  return [
    { label: 'p50', text: milliseconds(percentiles.p50), sample: percentiles.sample },
    { label: 'p95', text: milliseconds(percentiles.p95), sample: percentiles.sample },
    { label: 'p99', text: milliseconds(percentiles.p99), sample: percentiles.sample },
    { label: 'max', text: milliseconds(percentiles.max), sample: percentiles.sample },
  ];
}

/**
 * A duration in milliseconds, for HITL waits and elapsed times.
 *
 * Different unit from `milliseconds` on purpose: an operator reading "4.2 s" is reading a wait, and
 * one reading "4,200 ms" is reading a latency. Same quantity, different question.
 */
export function duration(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return NO_DATA;
  }
  if (value < 1000) {
    return `${value.toFixed(0)} ms`;
  }
  const seconds = value / 1000;
  if (seconds < 90) {
    return `${seconds.toFixed(1)} s`;
  }
  return `${(seconds / 60).toFixed(1)} min`;
}

/** A percentage *without* a decimal, for a share that is not a measurement (a bar's width). */
export function share(value: number, total: number): string {
  if (!Number.isFinite(total) || total <= 0) {
    return NO_DATA;
  }
  return `${((value / total) * 100).toFixed(1)}%`;
}

/**
 * The heading for an unmodelled key such as an outcome class or a guardrail decision.
 *
 * The keys come from §15's vocabulary (`success_full`, `layer=regex decision=block`) and are shown
 * as they are when they are single words, with the separators spaced out when they are not — so a
 * reader sees the recorded value rather than a prettified guess at what it meant.
 */
export function keyLabel(key: string): string {
  return key.replace(/[=|]/g, ' · ').replace(/\s+/g, ' ').trim();
}
