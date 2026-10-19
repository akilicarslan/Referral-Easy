/**
 * Agents & Versions — which agent, prompt and model versions produced the traffic (§12, §13).
 *
 * §15 puts `agent.role`, `agent.version`, `agent.release` and `model.name` on every event, so the
 * four dimensions are read straight from the events index and counted. Nothing is joined: a version
 * is listed because events carried it, which is the only claim this page can support.
 *
 * **What it does not show, and says so.** §12 also requires the Agent Card, the tool/skill/sub-agent
 * versions and a sample input/output pair. Those live in the A2A cards and the release manifest, not
 * in the telemetry indices — so this page reports the half it can measure and names the half it
 * cannot, rather than approximating a card from a version string.
 */

import { html, nothing, type TemplateResult } from 'lit';

import { barRows, keyLabel, number, provenance } from '../format.js';
import { AdminPage, card, empty, type Payload } from '../page.js';
import { sampleOf, type Sample } from '../api.js';

interface BucketShape {
  key: string;
  value: number | null;
  sample: Sample;
}

const LABELS: Readonly<Record<string, string>> = {
  'agent.role': 'Agent role',
  'agent.version': 'Agent version',
  'agent.release': 'Release',
  'model.name': 'Model',
};

export class AdminAgents extends AdminPage {
  protected readonly pageId = 'agents';

  protected renderData(payload: Payload): TemplateResult {
    const dimensions = (payload['dimensions'] ?? {}) as Record<string, BucketShape[]>;
    const names = Object.keys(dimensions);
    if (names.length === 0) {
      return card('Versions', undefined, empty('No dimension values were returned.'));
    }
    return html`
      <div class="grid cols-2">
        ${names.map((name) => this.#dimension(name, dimensions[name] ?? []))}
      </div>
      ${card(
        'Not on this page',
        undefined,
        html`<p>
          §12 also asks for the Agent Card, the tool, skill and sub-agent versions and a sample
          input/output pair. Those live in the A2A cards and the release manifest rather than in the
          telemetry indices, and the read that would surface them here is not written. Nothing on this
          page stands in for them.
        </p>`,
      )}
    `;
  }

  #dimension(name: string, buckets: readonly BucketShape[]): TemplateResult {
    const rows = barRows(
      buckets.map((bucket) => ({
        key: bucket.key,
        value: bucket.value,
        sample: sampleOf(bucket.sample),
      })),
    );
    const anyTruncated = rows.some((row) => row.sample?.truncated === true || (row.sample?.other ?? 0) > 0);
    return card(
      LABELS[name] ?? name,
      name,
      rows.length === 0
        ? empty(`No events in this window carried a ${LABELS[name] ?? name}.`)
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
            ${anyTruncated
              ? html`<p class="figure-sample">
                  More values exist than are listed here. A truncated list is not a complete one, and
                  the count beside each value is what it was measured from.
                </p>`
              : nothing}
          `,
    );
  }
}

/** A count of distinct values, for the page's own summary — not a §12 measurement. */
export function distinctCount(buckets: readonly BucketShape[]): string {
  return number(buckets.length);
}

if (customElements.get('re-admin-agents') === undefined) {
  customElements.define('re-admin-agents', AdminAgents);
}

declare global {
  interface HTMLElementTagNameMap {
    're-admin-agents': AdminAgents;
  }
}
