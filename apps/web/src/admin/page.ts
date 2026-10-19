/**
 * The base every admin page extends: one read, three outcomes, and no stale answer.
 *
 * A page here owns exactly one thing beyond its rendering — the lifecycle of its `read` call — and
 * that lifecycle has two failure modes worth writing down, because both are silent:
 *
 * **A slow response must not overwrite a newer one.** Changing the time range twice quickly leaves
 * two requests in flight; if the first lands second, the page shows the window the operator moved
 * away from. Every load therefore takes a generation number and a result is applied only if its
 * generation is still the current one.
 *
 * **"No data" and "could not ask" are different screens.** `ReadResult` distinguishes them and every
 * page renders both, so a gateway that is down cannot look like a quiet day. The base class refuses
 * to collapse them: `renderRead` demands a branch for each.
 */

import { LitElement, html, nothing, type TemplateResult } from 'lit';

import type { AdminClient, AdminFilters, ReadResult } from './api.js';
import { adminStyles } from './styles.js';

/** A payload of unknown shape, narrowed by the page that asked for it. */
export type Payload = Record<string, unknown>;

/** Anything a page can be asked to draw. */
export type Read = ReadResult<Payload>;

export abstract class AdminPage extends LitElement {
  static override styles = [adminStyles];

  static override properties = {
    client: { attribute: false },
    filters: { attribute: false },
    tail: { attribute: false },
  };

  declare client: AdminClient;
  declare filters: AdminFilters;
  /**
   * Whatever followed the page's id in the hash — `#/traces/<trace_id>` arrives here as the id.
   *
   * Opaque on purpose: the router does not know what a trace id is and neither does the shell, so a
   * page that wants one validates it where the meaning lives. A page with no sub-route ignores it,
   * and one that needs it renders its own "nothing named" state rather than a nearby view.
   */
  declare tail: string | undefined;

  #read: Read | undefined;
  #generation = 0;
  #firstUpdate = true;

  /** The route id, which is also the `/v1/admin/<id>` path and the hash. */
  protected abstract readonly pageId: string;

  /**
   * The path this page reads, which is `pageId` unless a page addresses one thing.
   *
   * A page with a sub-route overrides this — the trace waterfall reads `/v1/admin/traces/<id>`. It is
   * a path and not an id because the read is a different resource, not a filter on the list.
   */
  protected readPath(): string {
    return this.pageId;
  }

  /**
   * Whether the current state can be read at all.
   *
   * `false` stops the request. A page overrides it when its `tail` has to be well-formed before it
   * can name a resource: issuing a request built from a malformed hash segment is how a read ends up
   * pointed somewhere the page did not mean, and the generation guard would not catch it because
   * nothing is stale — it is simply the wrong request.
   */
  protected canRead(): boolean {
    return true;
  }

  /** Draw a live payload. Only called when the read succeeded. */
  protected abstract renderData(payload: Payload): TemplateResult;

  override connectedCallback(): void {
    super.connectedCallback();
    void this.reload();
  }

  override updated(changed: Map<PropertyKey, unknown>): void {
    super.updated(changed);
    // The shell sets `client`, `filters` and `tail` before appending the element, so the first update
    // carries all three as *changes* — and reloading on it would issue a second request for the state
    // `connectedCallback` has already loaded. The generation guard would drop the stale one, which is
    // exactly why the waste would not have shown up as a wrong answer. Later changes are real.
    if (this.#firstUpdate) {
      this.#firstUpdate = false;
      return;
    }
    // `filters` is replaced wholesale by the shell, so an identity check is the right comparison: a
    // new object means the operator changed something. The same goes for the tail — a different
    // trace id is a different read.
    if (changed.has('filters') || changed.has('tail')) {
      void this.reload();
    }
  }

  /** Re-issue the read. Public so a page can offer a retry without a second code path. */
  async reload(): Promise<void> {
    if (this.client === undefined || this.filters === undefined || !this.canRead()) {
      return;
    }
    const generation = ++this.#generation;
    if (this.#read !== undefined) {
      // Clearing a result that exists needs a render; clearing one that does not is a render
      // scheduled for the state already on screen, which is the Lit warning this avoids and, more to
      // the point, a second paint of a page that has not changed.
      this.#read = undefined;
      this.requestUpdate();
    }

    const result = await this.client.read(this.readPath(), this.filters);
    if (generation !== this.#generation) {
      // Superseded. Dropping it rather than applying it is the whole point of the counter: the
      // operator has already moved on, and an older window rendering under a newer control is a
      // chart that disagrees with its own filter.
      return;
    }
    this.#read = result;
    this.requestUpdate();
  }

  override render(): TemplateResult {
    const read = this.#read;
    if (read === undefined) {
      return html`<p class="loading" role="status">Reading the telemetry indices…</p>`;
    }
    switch (read.kind) {
      case 'data':
        return this.renderData(read.data);
      case 'unavailable':
        return unavailableNotice(read.reason);
      case 'failed':
        return failedNotice(read.message, () => {
          void this.reload();
        });
    }
  }
}

/**
 * The server said it has no source.
 *
 * Rendered as a plain statement with the server's own reason, because the reason is the useful part
 * — "OPENSEARCH_URL is unset" and "the feedback query was refused" call for different actions, and
 * a page that replaced both with "no data" would be hiding the one thing it knows.
 */
export function unavailableNotice(reason: string): TemplateResult {
  return html`
    <div class="notice" role="status">
      <strong>No source for this page.</strong>
      <span>${reason}</span>
      <span
        >Nothing is drawn rather than zeroes, because an empty chart and a measured zero look the
        same and only one of them is true (§12).</span
      >
    </div>
  `;
}

/** We could not ask at all. Distinct from the above, and says so. */
export function failedNotice(message: string, retry: () => void): TemplateResult {
  return html`
    <div class="notice error" role="alert">
      <strong>The gateway could not be asked.</strong>
      <span><code>${message}</code></span>
      <span>
        This is not the same as "nothing happened" — the stack may be recording perfectly well.
      </span>
      <button type="button" @click=${retry}>Try again</button>
    </div>
  `;
}

/** A figure: a value, its label, and always its sample. */
export function figure(value: string, label: string, sample: string | undefined): TemplateResult {
  return html`
    <div class="figure">
      <span class="figure-value">${value}</span>
      <span class="figure-label">${label}</span>
      ${sample === undefined ? nothing : html`<span class="figure-sample">${sample}</span>`}
    </div>
  `;
}

/** A card with a heading, an optional note, and a body. */
export function card(
  title: string,
  note: string | undefined,
  body: TemplateResult,
): TemplateResult {
  return html`
    <section class="card">
      <div class="card-head">
        <span class="card-title">${title}</span>
        ${note === undefined ? nothing : html`<span class="card-note">${note}</span>`}
      </div>
      ${body}
    </section>
  `;
}

/** Nothing to draw, and the reason is structural rather than a failure. */
export function empty(message: string): TemplateResult {
  return html`<p class="loading">${message}</p>`;
}
