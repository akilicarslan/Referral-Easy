/**
 * Human Queue — §11's operator queue, oldest first (§12).
 *
 * **Actions are the workers' own.** Claim, release and resolve post to per-ticket gateway routes that
 * run the workers' `HumanQueue` — the same code as `python -m referraleasy_workers handoff ...` — so
 * the database's compare-and-set still decides every race. The page sends the claim generation it
 * displayed; a ticket that moved since then loses with the gateway's reason, shown here, and the
 * queue is read again. Resolution offers only the states the gateway lists (`return_states`), and a
 * booking-consent state is never among them: resolving a ticket is not the patient's approval.
 *
 * **An expired claim is shown as expired, not applied.** A ticket whose lease has lapsed still reads
 * `claimed` in the row until something reclaims it, and this page reports the row plus the expiry.
 * A queue that hid it would disagree with the claim that is about to succeed.
 */

import { html, nothing, type TemplateResult } from 'lit';

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

  /** The last action's answer, shown above the queue until the next one. */
  #notice: { ok: boolean; text: string } | undefined;
  #busy = false;

  async #act(ticket: Ticket, action: 'claim' | 'release' | 'resolve', extra = {}): Promise<void> {
    if (this.#busy || typeof ticket.handoff_id !== 'string') {
      return;
    }
    this.#busy = true;
    this.requestUpdate();
    const generation =
      typeof ticket.claim_generation === 'number' ? ticket.claim_generation : undefined;
    const result = await this.client.act(`human-queue/${ticket.handoff_id}/${action}`, {
      ...(generation === undefined ? {} : { expected_generation: generation }),
      ...extra,
    });
    this.#busy = false;
    this.#notice =
      result.kind === 'done'
        ? { ok: true, text: `${action} of ${String(ticket.ticket_ref ?? '')} recorded.` }
        : { ok: false, text: result.message };
    await this.reload();
  }

  #resolve(ticket: Ticket, form: HTMLFormElement): void {
    const data = new FormData(form);
    const returnState = String(data.get('return_state') ?? '');
    const resolution = String(data.get('resolution') ?? '').trim();
    if (!returnState || !resolution) {
      this.#notice = { ok: false, text: 'A resolution needs a return state and a short reason.' };
      this.requestUpdate();
      return;
    }
    void this.#act(ticket, 'resolve', { return_state: returnState, resolution });
  }

  #actions(ticket: Ticket, returnStates: readonly string[]): TemplateResult | typeof nothing {
    const mine = ticket.state === 'claimed' && ticket.claimed_by === this.client.subject;
    const claimable = ticket.state === 'open' || ticket.claimed_expired === true;
    if (mine) {
      return html`
        <button ?disabled=${this.#busy} @click=${() => void this.#act(ticket, 'release')}>
          Release
        </button>
        <form
          class="resolve"
          @submit=${(event: SubmitEvent) => {
            event.preventDefault();
            this.#resolve(ticket, event.currentTarget as HTMLFormElement);
          }}
        >
          <select name="return_state" aria-label="Return the workflow to">
            ${returnStates.map((state) => html`<option value=${state}>${state}</option>`)}
          </select>
          <input name="resolution" aria-label="Resolution" placeholder="What was done" />
          <button type="submit" ?disabled=${this.#busy}>Resolve</button>
        </form>
      `;
    }
    if (claimable) {
      return html`<button ?disabled=${this.#busy} @click=${() => void this.#act(ticket, 'claim')}>
        Claim
      </button>`;
    }
    return nothing;
  }

  protected renderData(payload: Payload): TemplateResult {
    const returnStates = (
      Array.isArray(payload['return_states']) ? payload['return_states'] : []
    ).map(String);
    const tickets = (Array.isArray(payload['tickets']) ? payload['tickets'] : []).filter(
      (row): row is Ticket => typeof row === 'object' && row !== null,
    );
    const counts = (payload['counts'] ?? {}) as Record<string, unknown>;
    const truncated = payload['truncated'] === true;

    return html`
      ${this.#notice === undefined
        ? nothing
        : html`<p class="chip ${this.#notice.ok ? '' : 'error'}" role="status">
            ${this.#notice.text}
          </p>`}
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
                      <th>Action</th>
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
                          <td>${this.#actions(ticket, returnStates)}</td>
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
        'How the actions work',
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
