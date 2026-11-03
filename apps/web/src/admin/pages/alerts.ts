/**
 * Alerts — §16's rules, their thresholds, and the kill switch (§12).
 *
 * The page's hardest job is the one it does not look like it is doing: **not saying "nothing is
 * wrong"**. When the scheduler (`referraleasy_workers.alerts`) has not recorded a run, the page leads
 * with that and its reason. When it has, it shows each rule's *last state* — `firing`, `ok` or
 * `unmeasured`, where the third is never drawn as the second — the transitions it produced, and the
 * mock emails composed for them. A rule the scheduler could not evaluate is listed with why.
 *
 * The thresholds come from the gateway, which reads them from `referraleasy_alerts` — the same
 * package the rules themselves live in, so a page and a rule cannot disagree about what fires.
 * Nothing here restates §16's numbers.
 */

import { html, type TemplateResult } from 'lit';

import { number } from '../format.js';
import { AdminPage, card, empty, type Payload } from '../page.js';

interface RuleRow {
  rule?: unknown;
  threshold?: unknown;
  severity?: unknown;
  unmeasured_is_not_ok?: unknown;
}

interface SwitchState {
  engaged?: unknown;
  reason?: unknown;
  since?: unknown;
  by?: unknown;
}

const SEVERITY_CLASS: Readonly<Record<string, string>> = {
  page: 'error',
  warn: 'unknown',
  info: '',
};

export class AdminAlerts extends AdminPage {
  protected readonly pageId = 'alerts';

  protected renderData(payload: Payload): TemplateResult {
    const rules = (Array.isArray(payload['rules']) ? payload['rules'] : []).filter(
      (row): row is RuleRow => typeof row === 'object' && row !== null,
    );
    const evaluation = (payload['evaluation'] ?? {}) as {
      ran?: unknown;
      reason?: unknown;
      last_run?: Record<string, unknown>;
      states?: unknown;
      events?: unknown;
    };
    const outbox = (payload['outbox'] ?? {}) as {
      messages?: unknown;
      counts?: unknown;
      sends?: unknown;
      note?: unknown;
    };
    const killSwitch = (payload['kill_switch'] ?? {}) as SwitchState;

    return html`
      ${this.#killSwitch(killSwitch)}
      ${evaluation.ran === true ? this.#evaluated(evaluation) : this.#notEvaluated(evaluation)}
      ${card(
        'Rules',
        `${number(rules.length)} starting rules from §16`,
        rules.length === 0
          ? empty('The gateway did not return any rules.')
          : html`
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Rule</th>
                      <th>Fires when</th>
                      <th>Severity</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${rules.map(
                      (row) => html`
                        <tr>
                          <td><code>${String(row.rule ?? '')}</code></td>
                          <td>${String(row.threshold ?? '')}</td>
                          <td>
                            <span class="chip ${SEVERITY_CLASS[String(row.severity)] ?? ''}"
                              >${String(row.severity ?? '')}</span
                            >
                          </td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
              </div>
            `,
      )}
      ${card(
        'Mock outbox',
        outbox.sends === false ? 'composes and records; never sends' : undefined,
        html`
          <p>${String(outbox.note ?? '')}</p>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>State</th>
                  <th class="num">Messages</th>
                </tr>
              </thead>
              <tbody>
                ${Object.entries((outbox.counts ?? {}) as Record<string, number>).map(
                  ([state, count]) => html`
                    <tr>
                      <td>${state}</td>
                      <td class="num">${number(count)}</td>
                    </tr>
                  `,
                )}
              </tbody>
            </table>
          </div>
          <p class="figure-sample">
            A delivery failure does not open a loop (§16): the retry policy is bounded and terminal,
            and nothing in the outbox can create a new alert to fail on again.
          </p>
        `,
      )}
    `;
  }

  #notEvaluated(evaluation: { reason?: unknown }): TemplateResult {
    return card(
      'Nothing has been evaluated',
      undefined,
      html`<p>${String(evaluation.reason ?? 'No evaluation has run.')}</p>
        <p class="figure-sample">
          An empty alert list is not a clean bill of health. Every rule below reports
          <em>not enough data</em> until it can measure something, and never “ok” — a service that
          stopped emitting has no failures and no evidence, which is the moment an alert matters
          most.
        </p>`,
    );
  }

  #evaluated(evaluation: {
    last_run?: Record<string, unknown>;
    states?: unknown;
    events?: unknown;
  }): TemplateResult {
    const run = evaluation.last_run ?? {};
    const rows = (Array.isArray(evaluation.states) ? evaluation.states : []).filter(
      (row): row is Record<string, unknown> => typeof row === 'object' && row !== null,
    );
    const events = (Array.isArray(evaluation.events) ? evaluation.events : []).filter(
      (row): row is Record<string, unknown> => typeof row === 'object' && row !== null,
    );
    const skipped = (Array.isArray(run['skipped']) ? run['skipped'] : []).filter(
      (row): row is Record<string, unknown> => typeof row === 'object' && row !== null,
    );
    const stateClass = (state: unknown): string =>
      state === 'firing' ? 'error' : state === 'unmeasured' ? 'unknown' : '';
    return html`
      ${card(
        'Last evaluation',
        `by ${String(run['scheduler'] ?? 'the scheduler')} at ${String(run['finished_at'] ?? '')}`,
        html`
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>Series</th>
                  <th>State</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                ${rows.map(
                  (row) => html`
                    <tr>
                      <td><code>${String(row['rule'] ?? '')}</code></td>
                      <td>${String(row['series'] ?? '') || '—'}</td>
                      <td>
                        <span class="chip ${stateClass(row['last_state'])}"
                          >${String(row['last_state'] ?? '')}${row['firing'] === true &&
                          row['last_state'] !== 'firing'
                            ? ' · still open'
                            : ''}</span
                        >
                      </td>
                      <td>${String(row['last_detail'] ?? '')}</td>
                    </tr>
                  `,
                )}
                ${skipped.map(
                  (row) => html`
                    <tr>
                      <td><code>${String(row['rule'] ?? '')}</code></td>
                      <td>—</td>
                      <td><span class="chip unknown">not evaluated</span></td>
                      <td>${String(row['reason'] ?? '')}</td>
                    </tr>
                  `,
                )}
              </tbody>
            </table>
          </div>
          <p class="figure-sample">
            <em>unmeasured</em> leaves an open alert open: a rule that cannot decide has not resolved
            anything, so a gap in telemetry never closes an incident.
          </p>
        `,
      )}
      ${card(
        'Transitions',
        `${number(events.length)} most recent`,
        events.length === 0
          ? empty('No rule has opened or resolved an alert yet.')
          : html`<div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>At</th>
                    <th>Rule</th>
                    <th>Event</th>
                    <th>Severity</th>
                  </tr>
                </thead>
                <tbody>
                  ${events.map(
                    (row) => html`
                      <tr>
                        <td class="row-sample">${String(row['at'] ?? '')}</td>
                        <td><code>${String(row['rule'] ?? '')}</code></td>
                        <td>
                          <span class="chip ${row['kind'] === 'opened' ? 'error' : ''}"
                            >${String(row['kind'] ?? '')}</span
                          >
                        </td>
                        <td>${String(row['severity'] ?? '')}</td>
                      </tr>
                    `,
                  )}
                </tbody>
              </table>
            </div>`,
      )}
    `;
  }

  #killSwitch(state: SwitchState): TemplateResult {
    const engaged = state.engaged === true;
    return card(
      'Kill switch',
      'stops booking writes; read-only guidance continues',
      html`
        <p>
          <span class="chip ${engaged ? 'error' : ''}">${engaged ? 'engaged' : 'released'}</span>
          ${engaged
            ? html` — ${String(state.reason ?? 'no reason recorded')}
                ${state.by ? html` (by ${String(state.by)})` : ''}`
            : ''}
        </p>
        <p class="figure-sample">
          While it is engaged a booking write is refused with <code>booking_halted</code>, and
          nothing else changes: the help card, the event stream, a workflow's own transitions and
          this dashboard all keep working. A switch that stopped everything would be an outage with a
          friendlier name — and it would hide the workflows an operator is trying to read.
        </p>
      `,
    );
  }
}

if (customElements.get('re-admin-alerts') === undefined) {
  customElements.define('re-admin-alerts', AdminAlerts);
}

declare global {
  interface HTMLElementTagNameMap {
    're-admin-alerts': AdminAlerts;
  }
}
