/**
 * Trace Explorer — the list of recent traces, and one trace's waterfall (§12, §15).
 *
 * Two views in one page, selected by the hash: `#/traces` lists, `#/traces/<trace_id>` opens one.
 * The tail is the router's, and the *meaning* of it is this file's — a trace id is 32 hex characters,
 * and anything else is refused here rather than encoded into a request path. That is also why the
 * check is a shape check and not "whatever came back": a URL built from an unchecked hash segment is
 * a read pointed wherever the hash says.
 *
 * **The waterfall is drawn from start times, not from span order.** Spans arrive sorted by start; the
 * bar's left edge is each span's offset from the trace's first span and its width is its own
 * duration, both as a fraction of the trace's total extent. A waterfall drawn from the returned order
 * would look plausible and be wrong for every concurrent span — which is most of them.
 *
 * **Percentages that do not add up are honest here.** Spans overlap; a child's bar sits inside its
 * parent's. The one number that *is* stated is the slowest span, because that is what the list shows
 * too, and the two have to agree.
 */

import { html, nothing, type TemplateResult } from 'lit';

import { milliseconds, number, provenance } from '../format.js';
import { AdminPage, card, empty, type Payload } from '../page.js';
import { sampleOf } from '../api.js';

/** §15's trace ids are W3C trace context ids: 32 lowercase hex characters. */
const TRACE_ID = /^[0-9a-f]{32}$/i;

interface TraceRow {
  trace_id?: unknown;
  service_name?: unknown;
  entry_span?: unknown;
  started_at?: unknown;
  duration_ms?: unknown;
  span_count?: unknown;
  sample?: unknown;
}

interface SpanRow {
  span_id?: unknown;
  parent_span_id?: unknown;
  name?: unknown;
  service_name?: unknown;
  kind?: unknown;
  started_at?: unknown;
  duration_ms?: unknown;
  attributes?: unknown;
}

export class AdminTraces extends AdminPage {
  protected readonly pageId = 'traces';

  #tailIsATraceId(): boolean {
    return this.tail !== undefined && TRACE_ID.test(this.tail);
  }

  protected override readPath(): string {
    return this.#tailIsATraceId() ? `traces/${this.tail ?? ''}` : this.pageId;
  }

  protected override canRead(): boolean {
    // A malformed tail is refused before any request is built from it. The check is here as well as
    // in `render` so the *request* is stopped, not just the drawing.
    return this.tail === undefined || this.#tailIsATraceId();
  }

  override render(): TemplateResult {
    // The detail view validates the tail before any request goes out, so an id that is not an id
    // never becomes a path segment.
    if (this.tail !== undefined && !this.#tailIsATraceId()) {
      return card(
        'Not a trace id',
        undefined,
        html`<p>
          <code>${this.tail}</code> is not a trace id. §15's ids are 32 hex characters, so this is a
          malformed link rather than a trace that is missing — and nothing was requested.
        </p>
        <p><a href="#/traces">Back to the trace list</a></p>`,
      );
    }
    return super.render();
  }

  protected renderData(payload: Payload): TemplateResult {
    return this.tail === undefined ? this.#list(payload) : this.#waterfall(payload);
  }

  // -- the list ---------------------------------------------------------------------------------

  #list(payload: Payload): TemplateResult {
    const rows = (Array.isArray(payload['traces']) ? payload['traces'] : []).filter(
      (row): row is TraceRow => typeof row === 'object' && row !== null,
    );
    if (rows.length === 0) {
      return card(
        'Recent traces',
        undefined,
        empty(
          'No traces were found in this window. Spans are written by the collector through Data Prepper, so an empty list means either nothing ran or nothing was exported — the sample below the table is what distinguishes them.',
        ),
      );
    }
    return card(
      'Recent traces',
      'Newest first, by each trace\'s first span. A row opens the waterfall.',
      html`
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Trace</th>
                <th>Entry span</th>
                <th>Service</th>
                <th class="num">Spans</th>
                <th class="num">Slowest span</th>
                <th>Sample</th>
              </tr>
            </thead>
            <tbody>
              ${rows.map((row) => {
                const id = String(row.trace_id ?? '');
                return html`
                  <tr>
                    <td><a href="#/traces/${id}"><code>${id.slice(0, 16)}…</code></a></td>
                    <td>${String(row.entry_span ?? '')}</td>
                    <td>${String(row.service_name ?? '')}</td>
                    <td class="num">${number(typeof row.span_count === 'number' ? row.span_count : null)}</td>
                    <td class="num">
                      ${milliseconds(typeof row.duration_ms === 'number' ? row.duration_ms : null)}
                    </td>
                    <td class="row-sample">${provenance(sampleOf(row.sample))}</td>
                  </tr>
                `;
              })}
            </tbody>
          </table>
        </div>
      `,
    );
  }

  // -- one trace --------------------------------------------------------------------------------

  #waterfall(payload: Payload): TemplateResult {
    const spans = (Array.isArray(payload['spans']) ? payload['spans'] : []).filter(
      (row): row is SpanRow => typeof row === 'object' && row !== null,
    );
    const traceId = String(payload['trace_id'] ?? this.tail ?? '');
    if (spans.length === 0) {
      return card(
        'No spans for this trace',
        `trace ${traceId}`,
        html`<p>
            Either the id does not exist, or it is older than §15's seven-day retention for telemetry
            indices. The two are the same answer from here, so this page does not guess between them.
          </p>
          <p><a href="#/traces">Back to the trace list</a></p>`,
      );
    }

    const starts = spans
      .map((span) => Date.parse(String(span.started_at ?? '')))
      .filter((value) => Number.isFinite(value));
    const origin = starts.length > 0 ? Math.min(...starts) : 0;
    // A span with no duration has no width. It keeps `null` all the way to the screen — mapping it to
    // zero would draw it as instantaneous, which is the one thing a span missing its duration did
    // not say.
    const timelines = spans.map((span) => {
      const start = Date.parse(String(span.started_at ?? ''));
      const offset = Number.isFinite(start) ? start - origin : 0;
      const duration = typeof span.duration_ms === 'number' ? span.duration_ms : null;
      return { offset, duration };
    });
    const slowest = timelines.reduce(
      (highest, entry) => Math.max(highest, entry.duration ?? 0),
      0,
    );
    // The extent is where the LAST span ends, not the slowest span added to the farthest start. Those
    // differ whenever the slowest span begins at the origin — which is the common case, since the
    // entry span is usually the longest — and the wrong one draws every bar short.
    const extent = timelines.reduce(
      (widest, entry) => Math.max(widest, entry.offset + (entry.duration ?? 0)),
      0,
    );

    return html`
      ${card(
        'Waterfall',
        `trace ${traceId} · ${number(spans.length)} span${spans.length === 1 ? '' : 's'} · slowest ${milliseconds(slowest)}`,
        html`
          <div class="rows">
            ${spans.map((span, index) => {
              const { offset, duration } = timelines[index] ?? { offset: 0, duration: null };
              const left = extent === 0 ? 0 : (offset / extent) * 100;
              const width = extent === 0 ? 0 : Math.max(((duration ?? 0) / extent) * 100, 0.5);
              const attributes =
                typeof span.attributes === 'object' && span.attributes !== null
                  ? Object.entries(span.attributes as Record<string, unknown>)
                  : [];
              return html`
                <div class="row">
                  <span class="row-key">
                    ${String(span.name ?? '')}
                    <span class="row-sample">${String(span.service_name ?? '')}</span>
                  </span>
                  <span class="row-track" style="position:relative">
                    <span
                      class="row-bar"
                      style="position:absolute;left:${left.toFixed(2)}%;width:${width.toFixed(2)}%"
                    ></span>
                  </span>
                  <span class="row-value">${milliseconds(duration)}</span>
                </div>
                ${attributes.length === 0
                  ? nothing
                  : html`<div class="row-sample" style="margin-bottom:var(--re-s2)">
                      ${attributes.map(
                        ([key, value]) => html`<code>${key}=${render(value)}</code> `,
                      )}
                    </div>`}
              `;
            })}
          </div>
        `,
      )}
      ${card(
        'How this is drawn',
        undefined,
        html`<p>
          Each bar starts at its span's offset from the trace's first span and is as wide as its own
          duration, both as a fraction of the trace's extent. Bars overlap because spans do — a child
          runs inside its parent — so the widths are not shares of the trace and do not add up to
          100%. The number in the column is the span's own duration, which is what the list shows
          too.
        </p>`,
      )}
    `;
  }
}

/** Attribute values are whatever a service attached; an object or a list is shown as JSON. */
function render(value: unknown): string {
  if (value === null || value === undefined) return 'null';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

if (customElements.get('re-admin-traces') === undefined) {
  customElements.define('re-admin-traces', AdminTraces);
}

declare global {
  interface HTMLElementTagNameMap {
    're-admin-traces': AdminTraces;
  }
}
