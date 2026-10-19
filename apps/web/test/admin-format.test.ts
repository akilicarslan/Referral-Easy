import { describe, expect, it } from 'vitest';

import {
  NO_DATA,
  barRows,
  duration,
  keyLabel,
  mapRows,
  milliseconds,
  number,
  percentage,
  percentileRows,
  provenance,
  share,
} from '../src/admin/format.js';
import type { Bucket, Percentiles, Sample } from '../src/admin/api.js';

const SAMPLE: Sample = { count: 2, documents: 3, duplicated: 1, truncated: false, other: 0 };

function bucket(key: string, value: number | null): Bucket {
  return { key, value, sample: SAMPLE };
}

describe('a missing value is never a zero', () => {
  // §14 and §15 both turn on this, and `?? 0` at any of these call sites would be invisible.
  it.each([
    ['milliseconds', milliseconds],
    ['percentage', percentage],
    ['number', number],
    ['duration', duration],
  ])('renders %s(null) as "no data"', (_name, format) => {
    expect(format(null)).toBe(NO_DATA);
    expect(format(undefined)).toBe(NO_DATA);
    expect(format(Number.NaN)).toBe(NO_DATA);
  });

  it('does render a real zero as zero', () => {
    // The other half: refusing to print 0 would make a measured zero look like a gap.
    expect(number(0)).toBe('0');
    expect(percentage(0)).toBe('0.0%');
    expect(milliseconds(0)).toBe('0.00 ms');
    expect(duration(0)).toBe('0 ms');
  });
});

describe('provenance', () => {
  it('names the count and stays quiet when there is nothing else to say', () => {
    expect(provenance({ count: 2, documents: 2, duplicated: 0, truncated: false, other: 0 })).toBe(
      'sample: 2 events',
    );
  });

  it('reports collapsed duplicates when the index wrote an event twice', () => {
    expect(provenance(SAMPLE)).toContain('1 duplicate collapsed');
  });

  it('reports truncation, which is what makes a total a subset', () => {
    const truncated = provenance({
      count: 10,
      documents: 10,
      duplicated: 0,
      truncated: true,
      other: 4,
    });
    expect(truncated).toContain('truncated');
    expect(truncated).toContain('4 in "other"');
  });

  it('says the sample is unknown rather than inventing one', () => {
    expect(provenance(null)).toBe('sample: unknown');
  });
});

describe('barRows', () => {
  it('scales to the largest value', () => {
    const rows = barRows([bucket('a', 10), bucket('b', 5), bucket('c', 1)]);
    expect(rows.map((row) => row.percent)).toEqual([100, 50, 10]);
  });

  it('drops a bucket that could not be measured instead of plotting it at zero', () => {
    // The aggregation found the key and could not measure it. That is not the same as measuring
    // nothing, and a bar at zero asserts the second.
    const rows = barRows([bucket('a', 10), bucket('unmeasured', null)]);
    expect(rows.map((row) => row.key)).toEqual(['a']);
  });

  it('keeps a real zero, because every bucket measuring zero is a readable state', () => {
    const rows = barRows([bucket('a', 0), bucket('b', 0)]);
    expect(rows).toHaveLength(2);
    expect(rows.every((row) => row.percent === 0)).toBe(true);
    expect(rows[0]?.valueText).toBe('0');
  });

  it('carries each bucket its own sample', () => {
    expect(barRows([bucket('a', 1)])[0]?.sample).toEqual(SAMPLE);
  });
});

describe('mapRows', () => {
  it('counts by sample count, not by a value the map does not have', () => {
    const rows = mapRows({ success_full: SAMPLE, empty: { ...SAMPLE, count: 1 } });
    expect(rows.map((row) => row.value)).toEqual([2, 1]);
  });
});

describe('percentileRows', () => {
  const percentiles: Percentiles = {
    metric: 'latency.turn.ms',
    p50: 100,
    p95: 250.25,
    p99: null,
    min: 10,
    max: 900,
    sample: SAMPLE,
  };

  it('renders all four, so a missing p99 shows as a gap rather than as absent', () => {
    const rows = percentileRows(percentiles);
    expect(rows.map((row) => row.label)).toEqual(['p50', 'p95', 'p99', 'max']);
    expect(rows.find((row) => row.label === 'p99')?.text).toBe(NO_DATA);
  });

  it('has nothing to say about a distribution that was not returned', () => {
    expect(percentileRows(undefined)).toEqual([]);
    expect(percentileRows(null)).toEqual([]);
  });
});

describe('units', () => {
  it('distinguishes a latency from a wait', () => {
    // Same quantity, different question: "4,200 ms" is a measurement, "4.2 s" is something a person
    // waited through.
    expect(milliseconds(4200)).toBe('4200.0 ms');
    expect(duration(4200)).toBe('4.2 s');
    expect(duration(180_000)).toBe('3.0 min');
  });

  it('refuses a share over a denominator of zero', () => {
    expect(share(0, 0)).toBe(NO_DATA);
    expect(share(1, 4)).toBe('25.0%');
  });
});

describe('keyLabel', () => {
  it('spaces out the separators the vocabulary uses, without prettifying the value', () => {
    expect(keyLabel('layer=regex decision=block')).toBe('layer · regex decision · block');
    expect(keyLabel('success_full')).toBe('success_full');
  });
});
