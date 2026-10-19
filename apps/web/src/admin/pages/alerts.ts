/**
 * Alerts — §16's rules, their thresholds, and the kill switch (§12).
 *
 * The page's hardest job is the one it does not look like it is doing: **not saying "nothing is
 * wrong"**. Nothing has been evaluated — the scheduler that queries the telemetry indices on an
 * interval is the remaining half of Phase 7.3 — and an alerting page that drew an empty list of
 * firing alerts would be read as a healthy system. So the page leads with what has *not* happened,
 * then shows what would have to be true for each rule to fire.
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
    const evaluation = (payload['evaluation'] ?? {}) as { ran?: unknown; reason?: unknown };
    const outbox = (payload['outbox'] ?? {}) as {
      messages?: unknown;
      counts?: unknown;
      sends?: unknown;
      note?: unknown;
    };
    const killSwitch = (payload['kill_switch'] ?? {}) as SwitchState;

    return html`
      ${this.#killSwitch(killSwitch)}
      ${card(
        'Nothing has been evaluated',
        undefined,
        html`<p>${String(evaluation.reason ?? 'No evaluation has run.')}</p>
          <p class="figure-sample">
            An empty alert list is not a clean bill of health. Every rule below reports
            <em>not enough data</em> until it can measure something, and never “ok” — a service that
            stopped emitting has no failures and no evidence, which is the moment an alert matters
            most.
          </p>`,
      )}
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
