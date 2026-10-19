/**
 * Overview — §12's first page: what the stack recorded, and how much of it there was.
 *
 * Every figure is drawn from the telemetry indices, and every one carries its sample. The page is
 * deliberately all counts and no judgement: it does not say "healthy" or "degraded", because a
 * threshold that decided that would be this file's invention rather than §15's vocabulary. What it
 * does do is keep apart the things that are easy to collapse — an empty outcome class from a
 * missing one, a fast p50 from a fast p99, an API failure at the transport from a refusal by policy.
 */

import { html, nothing, type TemplateResult } from 'lit';

import { mapRows, milliseconds, number, percentileRows, provenance, barRows, keyLabel } from '../format.js';
import { AdminPage, card, empty, figure, type Payload } from '../page.js';
import { sampleOf, type Percentiles, type Sample } from '../api.js';

export class AdminOverview extends AdminPage {
  protected readonly pageId = 'overview';

  protected renderData(payload: Payload): TemplateResult {
    const window = payload['window'] as Record<string, string> | undefined;
    const events = sampleOf(payload['events']);
    const triggers = sampleOf(payload['agent_triggers']);
    const outcomes = (payload['outcomes'] ?? {}) as Record<string, Sample>;
    const latency = (payload['latency'] ?? {}) as Record<string, Percentiles>;
    const failures = Array.isArray(payload['api_failures']) ? payload['api_failures'] : [];
    const series = payload['series'] as
      | { interval?: string; timezone?: string; points?: readonly Record<string, unknown>[]; sample?: unknown }
      | undefined;

    return html`
      <div class="grid cols-3">
        ${figure(number(events?.count), 'Events recorded', events === null ? undefined : provenance(events))}
        ${figure(
          number(triggers?.count),
          'Agent triggers',
          triggers === null ? undefined : provenance(triggers),
        )}
        ${window === undefined
          ? nothing
          : figure(
              `${window['range'] ?? ''}`,
              `Window · ${window['timezone'] ?? 'unknown zone'} · buckets of ${window['interval'] ?? '?'}`,
              `${window['start'] ?? ''} → ${window['end'] ?? ''}`,
            )}
      </div>

      ${this.#outcomes(outcomes, events)}
      <div class="grid cols-2">${this.#latency(latency)}</div>
      ${this.#failures(failures)} ${this.#series(series)} ${this.#indexHealth(payload['index_health'])}
    `;
  }

  #outcomes(outcomes: Record<string, Sample>, events: Sample | null): TemplateResult {
    const rows = mapRows(outcomes);
    return card(
      'Outcomes',
      'Recorded attempt outcomes. Empty results and policy refusals are counted separately from errors.',
      rows.length === 0
        ? empty(
            events === null
              ? 'No outcome events were found in this window.'
              : `No outcome events in this window (${events.count} events recorded in total).`,
          )
        : html`
            <div class="rows">
              ${rows.map(
                (row) => html`
                  <div class="row">
                    <span class="row-key">${keyLabel(row.key).replace(/_/g, ' ')}</span>
                    <span class="row-track"
                      ><span class="row-bar" style="width:${row.percent.toFixed(1)}%"></span
                    ></span>
                    <span class="row-value"
                      >${row.valueText}
                      ${row.sample === null
                        ? nothing
                        : html`<span class="row-sample">· ${provenance(row.sample)}</span>`}</span
                    >
                  </div>
                `,
              )}
            </div>
          `,
    );
  }

  #latency(latency: Record<string, Percentiles>): TemplateResult[] {
    const kinds = ['attempt', 'invocation', 'turn'] as const;
    return kinds.map((kind) => {
      const entry = latency[kind];
      const rows = percentileRows(entry);
      return card(
        `${kind} latency`,
        kind === 'attempt'
          ? 'Time for one attempt, including timeouts.'
          : kind === 'invocation'
            ? 'One agent invocation: every attempt plus its tool work.'
            : 'One conversational turn — what the patient waited through.',
        rows.length === 0
          ? empty(`No ${kind} latency was recorded in this window.`)
          : html`
              <div class="grid cols-3">
                ${rows
                  .filter((row) => row.label !== 'max')
                  .map((row) => figure(row.text, row.label, undefined))}
              </div>
              <p class="figure-sample">
                ${entry === undefined ? 'no data' : `max ${milliseconds(entry.max)}`} ·
                ${provenance(entry?.sample ?? null)}
              </p>
            `,
      );
    });
  }

  #failures(failures: readonly unknown[]): TemplateResult {
    const buckets = failures as { key?: unknown; value?: unknown; sample?: unknown }[];
    const rows = barRows(
      buckets
        .filter((bucket) => typeof bucket.key === 'string')
        .map((bucket) => ({
          key: String(bucket.key),
          value: typeof bucket.value === 'number' ? bucket.value : null,
          sample: sampleOf(bucket.sample) ?? {
            count: 0,
            documents: 0,
            duplicated: 0,
            truncated: false,
            other: 0,
          },
        })),
    );
    return card(
      'API failures',
      'Where a downstream call failed, by endpoint and status. A failure here is a transport outcome, never a policy refusal.',
      rows.length === 0
        ? empty('No API failures were recorded in this window.')
        : html`
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Endpoint / status</th>
                    <th class="num">Count</th>
                    <th>Sample</th>
                  </tr>
                </thead>
                <tbody>
                  ${rows.map(
                    (row) => html`
                      <tr>
                        <td><code>${row.key}</code></td>
                        <td class="num">${row.valueText}</td>
                        <td class="row-sample">${provenance(row.sample)}</td>
                      </tr>
                    `,
                  )}
                </tbody>
              </table>
            </div>
          `,
    );
  }

  #series(
    series:
      | { interval?: string; timezone?: string; points?: readonly Record<string, unknown>[]; sample?: unknown }
      | undefined,
  ): TemplateResult {
    const points = series?.points ?? [];
    const overall = sampleOf(series?.sample);
    const rows = points
      .map((point) => ({
        key: String(point['at'] ?? ''),
        sample: sampleOf(point['sample']),
      }))
      .filter((row): row is { key: string; sample: Sample } => row.sample !== null);
    if (rows.length === 0) {
      return card(
        'Over time',
        undefined,
        empty('Nothing was recorded in this window, so there is no series to draw.'),
      );
    }
    const max = rows.reduce((highest, row) => Math.max(highest, row.sample.count), 0);
    return card(
      'Over time',
      `${series?.interval ?? 'bucket'} buckets in ${series?.timezone ?? 'the requested zone'} · ${provenance(overall)}`,
      html`
        <div class="rows">
          ${rows.map(
            (row) => html`
              <div class="row">
                <span class="row-key">${row.key.slice(0, 16)}</span>
                <span class="row-track"
                  ><span
                    class="row-bar"
                    style="width:${max === 0 ? 0 : ((row.sample.count / max) * 100).toFixed(1)}%"
                  ></span
                ></span>
                <span class="row-value">${number(row.sample.count)}</span>
              </div>
            `,
          )}
        </div>
      `,
    );
  }

  #indexHealth(health: unknown): TemplateResult {
    if (typeof health !== 'object' || health === null) {
      return card('Indices', undefined, empty('Index health was not reported.'));
    }
    const record = health as { indices?: readonly Record<string, unknown>[]; retention_days?: unknown };
    const indices = record.indices ?? [];
    return card(
      'Indices and retention',
      'Telemetry is kept for 7 days; evaluations for 30 days. Only existing indices are listed.',
      indices.length === 0
        ? empty('The events index does not exist yet, so nothing has been indexed.')
        : html`
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Index</th>
                    <th class="num">Documents</th>
                  </tr>
                </thead>
                <tbody>
                  ${indices.map(
                    (row) => html`
                      <tr>
                        <td><code>${String(row['index'] ?? '')}</code></td>
                        <td class="num">${number(typeof row['docs'] === 'number' ? row['docs'] : null)}</td>
                      </tr>
                    `,
                  )}
                </tbody>
              </table>
            </div>
          `,
    );
  }
}

if (customElements.get('re-admin-overview') === undefined) {
  customElements.define('re-admin-overview', AdminOverview);
}

declare global {
  interface HTMLElementTagNameMap {
    're-admin-overview': AdminOverview;
  }
}
