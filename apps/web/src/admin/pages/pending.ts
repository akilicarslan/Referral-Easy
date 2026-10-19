/**
 * The pages whose source does not exist yet, and what each one is waiting for.
 *
 * Five of §12's nine pages are on this file: three because their *read* is not written yet though
 * the data is there, and two because the feature they would report is a later phase. They are one
 * file because they share one job — say precisely what is missing — and not because they are the
 * same page.
 *
 * **Why a page rather than a placeholder.** A dashboard with five blank cards is indistinguishable
 * from five broken ones, and this project's whole discipline is that an absence has to be visible and
 * specific. So each of these names the endpoint it will call, where the data already is, and the
 * phase that owns it. When the read lands, the page is replaced by one that reads; until then,
 * nothing here can be mistaken for a measurement.
 *
 * They deliberately do **not** extend `AdminPage`: there is no read to make, and a class that
 * inherited the read lifecycle while never reading would be the first place a stub could quietly
 * start rendering something.
 */

import { LitElement, html, type TemplateResult } from 'lit';

import { adminStyles } from '../styles.js';

export interface PendingPageOptions {
  /** The route id and the `/v1/admin/<id>` path it will call once it exists. */
  readonly id: string;
  /** What the page will show, in §12's terms. */
  readonly willShow: string;
  /** Where the data already is, or why there is none yet. */
  readonly source: string;
  /** The phase that owns it, and the spec section it comes from. */
  readonly owner: string;
}

/**
 * Build a pending page class.
 *
 * A factory rather than five near-identical classes: the difference between them is entirely in the
 * text, and five copies would be five places for one of them to drift into claiming something is
 * nearly ready when it is not.
 */
export function pendingPage(options: PendingPageOptions): CustomElementConstructor {
  return class extends LitElement {
    static override styles = [adminStyles];

    override render(): TemplateResult {
      return html`
        <section class="card">
          <div class="card-head">
            <span class="card-title">Not built yet</span>
            <span class="card-note"
              ><code>/v1/admin/${options.id}</code> does not answer, so this page draws nothing.</span
            >
          </div>
          <div class="table-wrap">
            <table>
              <tbody>
                <tr>
                  <th>Will show</th>
                  <td>${options.willShow}</td>
                </tr>
                <tr>
                  <th>Source</th>
                  <td>${options.source}</td>
                </tr>
                <tr>
                  <th>Owner</th>
                  <td>${options.owner}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="figure-sample">
            An empty chart and a measured zero look the same on a dashboard, and only one of them is
            true — so this page states which one you are looking at instead of drawing it (§12).
          </p>
        </section>
      `;
    }
  };
}
