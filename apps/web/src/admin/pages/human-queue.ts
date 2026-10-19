/**
 * Human Queue — §11's operator queue, oldest first (§12).
 *
 * **Read-only, and it says so.** Claiming, releasing and resolving happen in the workers service
 * (`python -m referraleasy_workers handoff ...`), because that is where §11's queue lives and where
 * the database's own compare-and-set decides every race. A second implementation of the claim rules
 * on this page is the last thing a race needs, so the page shows the queue and tells an operator
 * where the work is done rather than offering buttons that would have to re-implement it.
 *
 * **An expired claim is shown as expired, not applied.** A ticket whose lease has lapsed still reads
 * `claimed` in the row until something reclaims it, and this page reports the row plus the expiry.
 * A queue that hid it would disagree with the claim that is about to succeed.
 */

import { html, type TemplateResult } from 'lit';

import { duration, number } from '../format.js';
import { AdminPage, card, empty, type Payload } from '../page.js';

interface Ticket {
  handoff_id?: unknown;
  workflow_id?: unknown;
  ticket_ref?: unknown;
  reason?: unknown;
  state?: unknown;
  claim_generation?: unknown;
  claimed_by?: unknown;
  created_at?: unknown;
  claimed_at?: unknown;
  claim_expires_at?: unknown;
  claimed_expired?: unknown;
  wait_ms?: unknown;
}

export class AdminHumanQueue extends AdminPage {
  protected readonly pageId = 'human-queue';

  protected renderData(payload: Payload): TemplateResult {
    const tickets = (Array.isArray(payload['tickets']) ? payload['tickets'] : []).filter(
      (row): row is Ticket => typeof row === 'object' && row !== null,
    );
    const counts = (payload['counts'] ?? {}) as Record<string, unknown>;
    const truncated = payload['truncated'] === true;

    return html`
      ${card(
        'The queue',
        `${number(typeof counts['open'] === 'number' ? counts['open'] : null)} open · ${number(
          typeof counts['claimed'] === 'number' ? counts['claimed'] : null,
        )} claimed${counts['claimed_expired'] ? ` · ${number(Number(counts['claimed_expired']))} of those expired` : ''}`,
        tickets.length === 0
          ? empty(
              'No ticket is waiting. That is a measured empty queue rather than an unreadable one — the read answered and found no live rows.',
            )
          : html`
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Ticket</th>
                      <th>Reason</th>
                      <th>State</th>
                      <th>Held by</th>
                      <th class="num">Waiting</th>
                      <th>Claim expires</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${tickets.map(
                      (ticket) => html`
                        <tr>
                          <td><code>${String(ticket.ticket_ref ?? '')}</code></td>
                          <td>${String(ticket.reason ?? '')}</td>
                          <td>
                            <span class="chip ${ticket.claimed_expired === true ? 'error' : ''}"
                              >${String(ticket.state ?? '')}${ticket.claimed_expired === true
                                ? ' · expired'
                                : ''}</span
                            >
                          </td>
                          <td>${ticket.claimed_by ? String(ticket.claimed_by) : '—'}</td>
                          <td class="num">
                            ${duration(
                              typeof ticket.wait_ms === 'number' ? ticket.wait_ms : null,
                            )}
                          </td>
                          <td class="row-sample">
                            ${ticket.claim_expires_at
                              ? String(ticket.claim_expires_at)
                              : 'not claimed'}
                          </td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
              </div>
              ${truncated
                ? html`<p class="figure-sample">
                    This is a full page, so there may be more tickets than are listed. A truncated
                    queue is not an empty one.
                  </p>`
                : ''}
            `,
      )}
      ${card(
        'Where the work is done',
        undefined,
        html`<p>${String(payload['note'] ?? '')}</p>
          <p class="figure-sample">
            The wait is computed from the row's own timestamps rather than stored, and it is the
            workflow's wait — §15 keeps a human's response time out of the latency distributions that
            §16 alerts on, so a slow morning for the people on call does not look like a slow
            platform.
          </p>`,
      )}
    `;
  }
}

if (customElements.get('re-admin-human-queue') === undefined) {
  customElements.define('re-admin-human-queue', AdminHumanQueue);
}

declare global {
  interface HTMLElementTagNameMap {
    're-admin-human-queue': AdminHumanQueue;
  }
}
