/**
 * Feedback — §12's three numbers, which are three numbers on purpose.
 *
 * The spec's requirement is unusually specific: *"raw count, like rate and participation rate
 * separate"*. They are easy to collapse into one "satisfaction score", and collapsing them loses the
 * distinction that matters — a high like rate over four votes out of two hundred responses and a
 * mediocre one over ninety of a hundred are different facts about different things, and a single
 * percentage cannot say which one you are looking at.
 *
 * The denominator is **turns**, which the payload states rather than leaving implied: §15 measures
 * every turn, so the population a vote could have come from is a number the stack already has. It is
 * shown next to the rate rather than folded into it.
 */

import { html, type TemplateResult } from 'lit';

import { number, percentage, provenance } from '../format.js';
import { AdminPage, card, empty, figure, type Payload } from '../page.js';
import { sampleOf, type Sample } from '../api.js';

interface Summary {
  raw_count?: number;
  likes?: number;
  dislikes?: number;
  total_responses?: number;
  like_rate?: number | null;
  participation_rate?: number | null;
  truncated?: boolean;
  other_responses?: number;
}

export class AdminFeedback extends AdminPage {
  protected readonly pageId = 'feedback';

  protected renderData(payload: Payload): TemplateResult {
    const summary = (payload['summary'] ?? {}) as Summary;
    const denominator = (payload['denominator'] ?? {}) as {
      label?: string;
      metric?: string;
      sample?: unknown;
    };
    const turns = sampleOf(denominator.sample) as Sample | null;

    if (summary.raw_count === undefined && turns === null) {
      return card('Feedback', undefined, empty('The feedback summary carried no counts.'));
    }

    return html`
      <div class="grid cols-3">
        ${figure(
          number(summary.raw_count ?? null),
          'Responses with a vote',
          'One per response, after collapsing to the last active vote.',
        )}
        ${figure(
          percentage(summary.like_rate ?? null),
          'Like rate',
          summary.raw_count === undefined
            ? undefined
            : `over ${number(summary.raw_count)} votes · ${number(summary.likes ?? null)} likes, ${number(
                summary.dislikes ?? null,
              )} dislikes`,
        )}
        ${figure(
          percentage(summary.participation_rate ?? null),
          'Participation rate',
          turns === null
            ? 'denominator unavailable'
            : `over ${number(turns.count)} ${denominator.label ?? 'turns'}`,
        )}
      </div>

      ${card(
        'How these were computed',
        undefined,
        html`
          <div class="table-wrap">
            <table>
              <tbody>
                <tr>
                  <th>Denominator</th>
                  <td>
                    <code>${denominator.metric ?? 'unknown'}</code> —
                    ${denominator.label ?? 'unnamed'}, ${provenance(turns)}
                  </td>
                </tr>
                <tr>
                  <th>Responses rated</th>
                  <td>${number(summary.raw_count ?? null)}</td>
                </tr>
                <tr>
                  <th>Responses in the window</th>
                  <td>
                    ${number(summary.total_responses ?? null)}
                    — the responses nobody rated are exactly the ones absent from the votes, which is
                    why this cannot be derived from them.
                  </td>
                </tr>
                ${summary.truncated === true
                  ? html`
                      <tr>
                        <th>Truncated</th>
                        <td>
                          Yes — the aggregation hit its response limit and
                          ${number(summary.other_responses ?? null)} further responses were not
                          examined. The rates above are a subset.
                        </td>
                      </tr>
                    `
                  : ''}
              </tbody>
            </table>
          </div>
        `,
      )}
      ${card(
        'Last vote wins',
        undefined,
        html`<p>
          A response that was thumbed up and then down is counted once, as a dislike. The collapse
          happens in the query (a <code>top_hits</code> of size 1 per response), not here, and its one
          imprecision is recorded: two votes within the same millisecond tie on the timestamp and are
          resolved by insertion order.
        </p>`)}
    `;
  }
}

if (customElements.get('re-admin-feedback') === undefined) {
  customElements.define('re-admin-feedback', AdminFeedback);
}

declare global {
  interface HTMLElementTagNameMap {
    're-admin-feedback': AdminFeedback;
  }
}
