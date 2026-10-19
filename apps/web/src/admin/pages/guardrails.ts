/**
 * Guardrails — what each §9 layer decided, counted.
 *
 * The page's whole value is in what it refuses to combine. §9 measures each layer separately, and a
 * blocked input is a *decision by a specific layer*, so the buckets stay keyed by layer and decision
 * rather than summed into "X blocked". A layer that never ran has no bucket here and no row, which
 * is different from a layer that ran and allowed everything — the first means the dependency is off
 * and §9 forbids a booking while it is.
 */

import { html, nothing, type TemplateResult } from 'lit';

import { barRows, keyLabel, provenance } from '../format.js';
import { AdminPage, card, empty, type Payload } from '../page.js';
import { sampleOf } from '../api.js';

export class AdminGuardrails extends AdminPage {
  protected readonly pageId = 'guardrails';

  protected renderData(payload: Payload): TemplateResult {
    const raw = Array.isArray(payload['decisions']) ? payload['decisions'] : [];
    const rows = barRows(
      raw
        .filter(
          (entry): entry is Record<string, unknown> =>
            typeof entry === 'object' && entry !== null && typeof entry['key'] === 'string',
        )
        .map((entry) => ({
          key: String(entry['key']),
          value: typeof entry['value'] === 'number' ? entry['value'] : null,
          sample: sampleOf(entry['sample']) ?? {
            count: 0,
            documents: 0,
            duplicated: 0,
            truncated: false,
            other: 0,
          },
        })),
    );

    const layers = new Map<string, number>();
    for (const row of rows) {
      const layer = row.key.split(' ')[0] ?? row.key;
      layers.set(layer, (layers.get(layer) ?? 0) + row.value);
    }

    return html`
      ${card(
        'Decisions by layer',
        'A bar per layer and decision. A layer with no row did not run in this window — §9 refuses a booking while any layer is unconfigured, so this is not the same as a layer that allowed everything.',
        rows.length === 0
          ? empty('No guardrail decisions were recorded in this window.')
          : html`
              <div class="rows">
                ${rows.map(
                  (row) => html`
                    <div class="row">
                      <span class="row-key">${keyLabel(row.key)}</span>
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
      )}
      ${card(
        'Layers seen',
        'The layers that produced at least one decision above. Listed from the data rather than from the configured order, so this page cannot report a layer that never spoke.',
        layers.size === 0
          ? empty('No layer produced a decision in this window.')
          : html`
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Layer</th>
                      <th class="num">Decisions</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${[...layers.entries()]
                      .sort((left, right) => right[1] - left[1])
                      .map(
                        ([layer, count]) => html`
                          <tr>
                            <td><code>${keyLabel(layer)}</code></td>
                            <td class="num">${count.toLocaleString('en-US')}</td>
                          </tr>
                        `,
                      )}
                  </tbody>
                </table>
              </div>
            `,
      )}
    `;
  }
}

if (customElements.get('re-admin-guardrails') === undefined) {
  customElements.define('re-admin-guardrails', AdminGuardrails);
}

declare global {
  interface HTMLElementTagNameMap {
    're-admin-guardrails': AdminGuardrails;
  }
}
