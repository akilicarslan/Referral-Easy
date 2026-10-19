/**
 * Fault Scenarios — §20's eight, what each one injects, and what it is expected to prove (§12).
 *
 * This page is the one §12 singles out: *"This page provides portfolio evidence that the
 * technologies work; fabricated static success charts are not used."* It is portfolio evidence, and
 * canned
 * success charts are explicitly forbidden.
 *
 * So it renders the **definitions**: the fault step, the outcome classes that count as correct, and
 * the assertions verbatim from `fixtures/scenarios.json` — the same file the offline evaluation
 * validates its case ids against, so the two cannot disagree about what a scenario is.
 *
 * **And it says what it does not have.** §12 also requires a link per scenario to the real trace and
 * metrics a run produced. No run has produced any: the Scenario Runner is Phase 7.4. The read says
 * `runs.linked: false` with the reason, and this page repeats it rather than leaving a column of
 * blanks a reader would take for "ran, and nothing happened".
 */

import { html, nothing, type TemplateResult } from 'lit';

import { number } from '../format.js';
import { AdminPage, card, empty, type Payload } from '../page.js';

interface Scenario {
  scenario_id?: unknown;
  number?: unknown;
  title?: unknown;
  summary?: unknown;
  fault_scenarios?: unknown;
  expected_result_kinds?: unknown;
  assertions?: unknown;
  reproducible_from_fixtures?: unknown;
  non_fixture_dependencies?: unknown;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map((entry) => String(entry)) : [];
}

export class AdminScenarios extends AdminPage {
  protected readonly pageId = 'scenarios';

  protected renderData(payload: Payload): TemplateResult {
    const scenarios = (Array.isArray(payload['scenarios']) ? payload['scenarios'] : []).filter(
      (entry): entry is Scenario => typeof entry === 'object' && entry !== null,
    );
    const runs = (payload['runs'] ?? {}) as { linked?: unknown; reason?: unknown };

    if (scenarios.length === 0) {
      return card('Scenarios', undefined, empty('The manifest carried no scenarios.'));
    }

    return html`
      ${card(
        'Not run yet',
        undefined,
        html`<p>
          ${runs.linked === true
            ? 'These scenarios have run.'
            : String(runs.reason ?? 'No run has been linked to these scenarios.')}
        </p>
        <p class="figure-sample">
          What is shown below is what each scenario is <em>defined</em> to do. A definition is not a
          result, and this page will not draw one as the other — §12 forbids canned success charts,
          and an empty “result” column reads as a scenario that ran and produced nothing.
        </p>`,
      )}
      ${scenarios.map((scenario) => this.#scenario(scenario))}
    `;
  }

  #scenario(scenario: Scenario): TemplateResult {
    const faults = strings(scenario.fault_scenarios);
    const kinds = strings(scenario.expected_result_kinds);
    const assertions = strings(scenario.assertions);
    const nonFixture = strings(scenario.non_fixture_dependencies);
    const source = scenario.reproducible_from_fixtures === true ? 'reproducible' : 'partial';

    return card(
      `${String(scenario.number ?? '')}. ${String(scenario.title ?? '')}`,
      `${String(scenario.scenario_id ?? '')} · ${source}`,
      html`
        <p>${String(scenario.summary ?? '')}</p>

        <div class="table-wrap">
          <table>
            <tbody>
              <tr>
                <th>Fault step</th>
                <td>
                  ${faults.length === 0
                    ? html`<span class="chip">none injected</span>`
                    : faults.map((fault) => html`<code>${fault}</code> `)}
                </td>
              </tr>
              <tr>
                <th>Correct outcomes</th>
                <td>
                  ${kinds.length === 0
                    ? html`<span class="chip unknown">not declared</span>`
                    : kinds.map((kind) => html`<span class="chip">${kind}</span> `)}
                </td>
              </tr>
              ${nonFixture.length === 0
                ? nothing
                : html`<tr>
                    <th>Needs more than fixtures</th>
                    <td>
                      ${nonFixture.map((item) => html`<code>${item}</code> `)}
                      <div class="figure-sample">
                        This scenario cannot be reproduced from the committed fixtures alone, so it
                        cannot be replayed offline. Named rather than hidden.
                      </div>
                    </td>
                  </tr>`}
            </tbody>
          </table>
        </div>

        <h3>What it must prove</h3>
        <ol>
          ${assertions.map((assertion) => html`<li>${assertion}</li>`)}
        </ol>
        <p class="figure-sample">
          ${number(assertions.length)} assertion${assertions.length === 1 ? '' : 's'}, quoted from
          <code>fixtures/scenarios.json</code> — the same file the offline evaluation validates its
          case ids against.
        </p>
      `,
    );
  }
}

if (customElements.get('re-admin-scenarios') === undefined) {
  customElements.define('re-admin-scenarios', AdminScenarios);
}

declare global {
  interface HTMLElementTagNameMap {
    're-admin-scenarios': AdminScenarios;
  }
}
