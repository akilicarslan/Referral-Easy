/**
 * Evaluations — §14's scores, per run and per metric.
 *
 * §14 is explicit that different judges and different rubrics must never be merged into one series,
 * and this page keeps that by *not* aggregating: it lists the buckets the query returned, keyed by
 * run and metric, and shows each one's own sample. There is deliberately no "average score" card,
 * because the only average this page could compute would be one across runs that may have been
 * scored by different judges under different rubrics — which is the merge the rule forbids.
 *
 * A metric that was not measured shows `n/a`, never zero. §14's rule is that N/A and `eval_error`
 * are not scored as zero, and the server already enforces it — this page's job is not to undo that by
 * rendering an empty list as `0.000`.
 */

import { html, type TemplateResult } from 'lit';

import { number, percentage, provenance } from '../format.js';
import { AdminPage, card, empty, type Payload } from '../page.js';
import { sampleOf } from '../api.js';

export class AdminEvaluations extends AdminPage {
  protected readonly pageId = 'evaluations';

  protected renderData(payload: Payload): TemplateResult {
    const raw = Array.isArray(payload['scores']) ? payload['scores'] : [];
    const rows = raw
      .filter(
        (entry): entry is Record<string, unknown> =>
          typeof entry === 'object' && entry !== null && typeof entry['key'] === 'string',
      )
      .map((entry) => ({
        key: String(entry['key']),
        value: typeof entry['value'] === 'number' ? entry['value'] : null,
        sample: sampleOf(entry['sample']),
      }));

    const measured = rows.filter((row) => row.value !== null);
    const runs = new Set(measured.map((row) => row.key.split('|')[0] ?? row.key));

    return html`
      ${card(
        'Scores',
        'One row per run and metric. Runs are listed separately rather than averaged together: two runs may have been scored by different judges under different rubrics, and §14 forbids merging those into one series.',
        rows.length === 0
          ? empty(
              'No §14 scores were emitted in this window. A score needs an evaluation run, and the replay harness that produces them is not written yet — so this is the honest state, not a broken page.',
            )
          : html`
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Run · metric</th>
                      <th class="num">Mean</th>
                      <th>Sample</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${rows.map(
                      (row) => html`
                        <tr>
                          <td><code>${row.key}</code></td>
                          <td class="num">
                            ${row.value === null
                              ? html`<span class="chip unknown">n/a</span>`
                              : percentage(row.value)}
                          </td>
                          <td class="row-sample">${provenance(row.sample)}</td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
              </div>
            `,
      )}
      ${card(
        'What is shown, and what is not',
        undefined,
        html`
          <div class="table-wrap">
            <table>
              <tbody>
                <tr>
                  <th>Runs in this window</th>
                  <td>${number(runs.size)}</td>
                </tr>
                <tr>
                  <th>Measured metrics</th>
                  <td>
                    ${number(measured.length)}
                    ${rows.length === measured.length
                      ? ''
                      : html`<span class="chip unknown"
                          >${number(rows.length - measured.length)} unmeasured</span
                        >`}
                  </td>
                </tr>
                <tr>
                  <th>Judged by</th>
                  <td>
                    <code>none</code> — ADR-0011 is unresolved, so no judge model is pinned and
                    judge-scored cases report as unmeasured. A fallback rule would answer a question
                    no rule can answer, which is the fabricated score §14's N/A rule exists to stop.
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        `,
      )}
    `;
  }
}

if (customElements.get('re-admin-evaluations') === undefined) {
  customElements.define('re-admin-evaluations', AdminEvaluations);
}

declare global {
  interface HTMLElementTagNameMap {
    're-admin-evaluations': AdminEvaluations;
  }
}
