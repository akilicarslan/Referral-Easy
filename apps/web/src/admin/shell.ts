/**
 * The admin shell: the frame, the nav, the shared filter bar, and the outlet.
 *
 * **The filters live here, not on the pages.** §12's filter set applies to every chart, and a bar
 * duplicated per page is a bar that will eventually disagree with its neighbour about what "today"
 * means. The shell owns one `AdminFilters`, passes it down as a property, and every page re-reads
 * when it changes — which is also why `AdminPage` keys its reload on the identity of that object.
 *
 * **An unknown hash is a page.** `resolveRoute` answers `undefined` for a route that is not one of
 * the nine and the shell renders the list it does have, so a stale bookmark says "no such page"
 * rather than quietly showing the overview and reporting success for something nobody built.
 */

import { LitElement, html, nothing, type TemplateResult } from 'lit';

import { AdminClient, type AdminFilters, type RangeName } from './api.js';
import { pageElement } from './pages/registry.js';
import { ADMIN_ROUTES, hashFor, watchRoute, type AdminRoute, type RouteMatch } from './router.js';
import { adminStyles } from './styles.js';

/**
 * The zones the telemetry query accepts without a lookup (`_KNOWN_ZONES` in
 * `packages/telemetry/.../query.py`). Listed here because the browser cannot read a Python constant,
 * and the duplication is safe in one direction: the server validates the zone it is given, so a zone
 * added there and not here is still reachable by URL and refused visibly rather than ignored.
 */
export const TIMEZONES: readonly string[] = [
  'America/Chicago',
  'UTC',
  'America/New_York',
  'America/Los_Angeles',
  'Europe/Istanbul',
];

const RANGES: readonly { value: RangeName; label: string }[] = [
  { value: 'day', label: 'Today' },
  { value: 'month', label: 'This month' },
  { value: 'custom', label: 'Custom range' },
];

export class AdminShell extends LitElement {
  static override styles = [adminStyles];

  static override properties = {
    client: { attribute: false },
    match: { attribute: false },
  };

  declare client: AdminClient;
  declare match: RouteMatch | undefined;

  /** The page being shown, for the header and the nav highlight. */
  get route(): AdminRoute | undefined {
    return this.match?.route;
  }

  #filters: AdminFilters = { range: 'day', timezone: 'America/Chicago' };
  #stopWatching: (() => void) | undefined;

  override connectedCallback(): void {
    super.connectedCallback();
    this.#stopWatching = watchRoute((match) => {
      this.match = match;
    });
  }

  override disconnectedCallback(): void {
    this.#stopWatching?.();
    this.#stopWatching = undefined;
    super.disconnectedCallback();
  }

  #page(): HTMLElement | undefined {
    const match = this.match;
    if (match === undefined) {
      return undefined;
    }
    const tag = pageElement(match.route.id);
    if (tag === undefined) {
      return undefined;
    }
    const element = document.createElement(tag) as HTMLElement & {
      client: AdminClient;
      filters: AdminFilters;
      tail: string | undefined;
    };
    element.client = this.client;
    element.filters = this.#filters;
    // Every page gets the tail, and every page decides what it means. The shell does not know that
    // one of them is a trace id, which is what keeps a second sub-route from needing a change here.
    element.tail = match.tail;
    return element;
  }

  #setFilters(patch: Partial<AdminFilters>): void {
    // A new object every time: `AdminPage.updated` compares by identity, and mutating in place would
    // leave every page believing nothing had changed.
    this.#filters = { ...this.#filters, ...patch };
    this.requestUpdate();
  }

  #renderControls(): TemplateResult {
    const filters = this.#filters;
    return html`
      <div class="controls" role="group" aria-label="Filters">
        <label class="control">
          <span>Range</span>
          <select
            @change=${(event: Event) => {
              const value = (event.target as HTMLSelectElement).value as RangeName;
              this.#setFilters({ range: value });
            }}
          >
            ${RANGES.map(
              (option) => html`
                <option value=${option.value} ?selected=${filters.range === option.value}>
                  ${option.label}
                </option>
              `,
            )}
          </select>
        </label>

        <label class="control">
          <span>Timezone</span>
          <select
            @change=${(event: Event) => {
              this.#setFilters({ timezone: (event.target as HTMLSelectElement).value });
            }}
          >
            ${TIMEZONES.map(
              (zone) => html`
                <option value=${zone} ?selected=${filters.timezone === zone}>${zone}</option>
              `,
            )}
          </select>
        </label>

        ${filters.range === 'custom'
          ? html`
              <label class="control">
                <span>From</span>
                <input
                  type="datetime-local"
                  .value=${filters.from ?? ''}
                  @change=${(event: Event) => {
                    this.#setFilters({ from: (event.target as HTMLInputElement).value });
                  }}
                />
              </label>
              <label class="control">
                <span>To</span>
                <input
                  type="datetime-local"
                  .value=${filters.to ?? ''}
                  @change=${(event: Event) => {
                    this.#setFilters({ to: (event.target as HTMLInputElement).value });
                  }}
                />
              </label>
            `
          : nothing}

        <label class="control">
          <span>Traffic</span>
          <select
            @change=${(event: Event) => {
              const value = (event.target as HTMLSelectElement).value;
              // `undefined` rather than a third label: §15 has no "unlabelled" value, and "both"
              // is the absence of the filter rather than a value it could take.
              this.#setFilters(
                value === 'both'
                  ? { synthetic: undefined }
                  : { synthetic: value as 'synthetic' | 'live_demo' },
              );
            }}
          >
            <option value="both" ?selected=${filters.synthetic === undefined}>Both labels</option>
            <option value="synthetic" ?selected=${filters.synthetic === 'synthetic'}>
              Synthetic only
            </option>
            <option value="live_demo" ?selected=${filters.synthetic === 'live_demo'}>
              Live-demo only
            </option>
          </select>
        </label>
      </div>
    `;
  }

  #renderBody(): TemplateResult {
    const route = this.route;
    if (route === undefined) {
      return html`
        <section class="card">
          <div class="card-head">
            <span class="card-title">No such page</span>
          </div>
          <p>
            <code>${window.location.hash || '(no hash)'}</code> is not one of this admin app's pages.
            Nothing is shown rather than a nearby page, because a wrong address that renders
            something plausible is worse than one that says so.
          </p>
          <ul>
            ${ADMIN_ROUTES.map(
              (known) => html`<li><a href=${hashFor(known.id)}>${known.title}</a></li>`,
            )}
          </ul>
        </section>
      `;
    }
    const element = this.#page();
    if (element === undefined) {
      return html`
        <section class="card">
          <div class="card-head">
            <span class="card-title">${route.title}</span>
          </div>
          <p>
            This page's element is not registered in this bundle, so nothing is drawn. That is a
            build problem, not a data problem.
          </p>
        </section>
      `;
    }
    return html`${element}`;
  }

  override render(): TemplateResult {
    const route = this.route;
    return html`
      <div class="frame">
        <nav class="side" aria-label="Admin navigation">
          <div class="brand">
            <span class="brand-name">ReferralEasy</span>
            <span class="brand-sub">OPERATIONS WORKSPACE</span>
          </div>
          <div class="nav">
            ${ADMIN_ROUTES.map(
              (known) => html`
                <a
                  href=${hashFor(known.id)}
                  aria-current=${route?.id === known.id ? 'page' : nothing}
                  >${known.title}</a
                >
              `,
            )}
          </div>
          <a class="side-link" href="/">↗ Create a demo referral</a>
          <span class="chip synthetic" title="Synthetic fixture traffic only. Not a live deployment."
            >Synthetic data</span
          >
        </nav>

        <div class="main">
          <header class="topbar">
            <span class="topbar-title">${route?.title ?? 'Not found'}</span>
            <span class="topbar-summary">${route?.summary ?? ''}</span>
          </header>
          <div class="content">
            ${this.#renderControls()} ${this.#renderBody()}
          </div>
          <footer class="foot">
            Local demo · Metrics include sample counts. Missing measurements are shown as unavailable.
          </footer>
        </div>
      </div>
    `;
  }
}

if (customElements.get('re-admin-shell') === undefined) {
  customElements.define('re-admin-shell', AdminShell);
}

declare global {
  interface HTMLElementTagNameMap {
    're-admin-shell': AdminShell;
  }
}
