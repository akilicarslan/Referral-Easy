import { LitElement, css, html, nothing, type TemplateResult } from 'lit';
import { ref, createRef, type Ref } from 'lit/directives/ref.js';
import { sharedStyles } from '../components/internal/shared-styles.js';
import {
  classifyBookingState,
  describeDegradation,
  describeReason,
  SYNTHETIC_LABEL,
  SYNTHETIC_TITLE,
} from '../components/internal/truth.js';
import { mountSurface, type SurfaceMount, type SurfaceRuntime } from '../a2ui/adapter.js';
import type { StoreSnapshot, SurfaceStore } from '../stream/surface-store.js';
import type { StreamStatus } from '../stream/sse-client.js';

/**
 * The page around the surface.
 *
 * The surface renders the eight components; this element renders the things that
 * surround them and that a component cannot say about itself:
 *
 *   - the phase (connecting, live, degraded, complete, interrupted, error)
 *   - the workflow state and its reason code
 *   - transport and adapter diagnostics
 *   - the honest blank state, which is a rendered screen and not an empty page
 *
 * **A blank screen is a failure.** Every branch below produces something a
 * patient can read, including the one where nothing has arrived yet.
 */
/**
 * Tones the page primitive can render.
 *
 * `requested` is a first-class tone rather than a shade of `ok`: an outcome
 * still awaiting the hospital's answer is its own state, and it must not be able
 * to borrow the confirmed one by accident.
 */
type PageTone = 'ok' | 'info' | 'requested' | 'unknown' | 'degraded' | 'error';

export class ReferralApp extends LitElement {
  static override properties = {
    store: { attribute: false },
    runtime: { attribute: false },
    streamStatus: { attribute: false },
  };

  declare store: SurfaceStore | undefined;
  declare runtime: SurfaceRuntime | undefined;
  declare streamStatus: StreamStatus;

  static override styles = [
    sharedStyles,
    css`
      .shell {
        display: flex;
        flex-direction: column;
        gap: var(--re-s4);
        padding: var(--re-s4) var(--re-gutter) var(--re-s6);
      }

      .page-head {
        display: flex;
        gap: var(--re-s3);
      }

      .page-glyph {
        font-size: var(--re-t-title);
        line-height: 1.1;
        flex: none;
      }
      .page-glyph.ok {
        color: var(--re-ok);
      }
      .page-glyph.info {
        color: var(--re-accent);
      }
      .page-glyph.unknown {
        color: var(--re-unknown);
      }
      .page-glyph.requested {
        color: var(--re-requested);
      }
      .page-glyph.degraded {
        color: var(--re-degraded);
      }
      .page-glyph.error {
        color: var(--re-error);
      }

      .page-title {
        font-size: var(--re-t-title);
        font-weight: 600;
        line-height: var(--re-lh-tight);
        margin: 0 0 var(--re-s1);
      }

      .skeleton-stack {
        display: flex;
        flex-direction: column;
        gap: var(--re-s3);
        margin-top: var(--re-s4);
      }

      .surfaces {
        display: flex;
        flex-direction: column;
        gap: var(--re-s4);
      }

      .surfaces ::slotted(*) {
        display: block;
      }

      .notices {
        display: flex;
        flex-direction: column;
        gap: var(--re-s2);
      }

      .diagnostics {
        border: 1px solid var(--re-line);
        border-radius: var(--re-r-sm);
        background: var(--re-surface-sunken);
        padding: var(--re-s3);
        font-size: var(--re-t-label);
      }

      .diagnostics summary {
        cursor: pointer;
        font-weight: 600;
        min-height: 24px;
      }

      .footer {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: var(--re-s2);
        padding-top: var(--re-s4);
        border-top: 1px solid var(--re-line);
      }
    `,
  ];

  #snapshot: StoreSnapshot | undefined;
  /** One ref and one mount handle per surface, keyed by surface id. */
  readonly #slots = new Map<string, Ref<HTMLElement>>();
  readonly #mounts = new Map<string, SurfaceMount>();

  constructor() {
    super();
    this.streamStatus = 'idle';
  }

  override createRenderRoot(): HTMLElement | DocumentFragment {
    return this.attachShadow({ mode: 'open' });
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    for (const mount of this.#mounts.values()) {
      mount.unmount();
    }
    this.#mounts.clear();
    this.#slots.clear();
  }

  override render(): TemplateResult {
    const snapshot = this.#snapshot;

    return html`
      <div class="shell">
        ${this.renderPhase(snapshot)} ${this.renderProgress(snapshot)}
        ${this.renderNotices(snapshot)} ${this.renderDiagnostics(snapshot)}
        ${this.renderSurface(snapshot)} ${this.renderFooter(snapshot)}
      </div>
    `;
  }

  /**
   * Push a new snapshot in.
   *
   * The store publishes through its `onChange` callback rather than exposing a
   * subscribe method, so the entry point owns the wiring and this element stays
   * a pure renderer of whatever it was last handed.
   */
  set snapshot(value: StoreSnapshot) {
    this.#snapshot = value;
    // Surfaces that have gone away must release their mounts.
    for (const surfaceId of [...this.#mounts.keys()]) {
      if (!value.surfaces.some((surface) => surface.surfaceId === surfaceId)) {
        this.#mounts.get(surfaceId)?.unmount();
        this.#mounts.delete(surfaceId);
        this.#slots.delete(surfaceId);
      }
    }
    this.requestUpdate();
  }

  get snapshot(): StoreSnapshot | undefined {
    return this.#snapshot;
  }

  // ── phase ────────────────────────────────────────────────────────────────

  private renderPhase(snapshot: StoreSnapshot | undefined): TemplateResult | typeof nothing {
    if (snapshot === undefined) {
      return this.page({
        tone: 'info',
        glyph: '…',
        title: 'Connecting',
        body: 'Waiting for the first update. Nothing has been received yet.',
        skeleton: true,
      });
    }

    switch (snapshot.phase) {
      case 'connecting':
        return this.page({
          tone: 'info',
          glyph: '…',
          title: 'Connecting',
          body: 'Reaching the referral service. Nothing has been received yet.',
          skeleton: true,
        });

      case 'live':
        return snapshot.surfaces.every((surface) => surface.instanceIds.length === 0)
          ? this.page({ tone: 'info', glyph: '…', title: 'Referral in progress', body: 'There is no question to answer yet. Updates will appear here when the referral service has them.' })
          : nothing;

      case 'degraded': {
        const degraded = describeDegradation(snapshot.workflow?.degraded);
        return this.page({
          tone: 'degraded',
          glyph: degraded?.glyph ?? '△',
          title: 'Reduced lookup',
          body:
            degraded?.sentence ??
            'Part of the lookup pipeline was unavailable, so these answers are less complete than usual.',
        });
      }

      case 'complete':
        return this.renderOutcome(snapshot);

      case 'interrupted':
        return this.page({
          tone: 'unknown',
          glyph: '?',
          title: 'Connection lost',
          body:
            'The stream ended before the workflow reported an outcome. This is not a confirmation and not a failure — we simply do not know yet. Reconnecting will replay from the last event we received.',
          retry: true,
        });

      case 'error':
        return this.page({
          tone: 'error',
          glyph: '✕',
          title: 'Something went wrong here',
          body:
            snapshot.error ??
            'The client could not continue. The referral itself is unaffected — nothing was submitted from this screen.',
          retry: true,
        });
    }
  }

  private renderOutcome(snapshot: StoreSnapshot): TemplateResult | typeof nothing {
    const terminal = snapshot.terminal;
    if (terminal === undefined) {
      return nothing;
    }

    // Tone and glyph come from `classifyBookingState`, not from the state name.
    // The obvious version — "anything that is not FAILED must have gone well" —
    // paints `BOOKING_UNVERIFIED` green with a tick, which is precisely the
    // claim this project forbids. The classifier's default arm is `unknown`.
    const readout = classifyBookingState(terminal.state);
    const cancelled = terminal.state === 'CANCELLED' || terminal.state === 'ABANDONED';
    const handedOver = terminal.state === 'HUMAN_HANDOFF';

    const tone: PageTone = handedOver ? 'info' : cancelled ? 'unknown' : readout.tone;
    const glyph = handedOver ? '→' : cancelled ? '○' : readout.glyph;

    return this.page({
      tone,
      glyph,
      title:
        terminal.state === 'BOOKED'
          ? 'Workflow finished'
          : `Workflow finished — ${terminal.state.toLowerCase().replace(/_/g, ' ')}`,
      body:
        describeReason(terminal.reasonCode) ??
        (cancelled
          ? 'This referral was stopped before a booking was made.'
          : readout.sentence),
    });
  }

  // ── progress and notices ─────────────────────────────────────────────────

  private renderProgress(snapshot: StoreSnapshot | undefined): TemplateResult | typeof nothing {
    const progress = snapshot?.progress;
    if (progress === undefined) {
      return nothing;
    }
    return html`
      <p class="notice info" role="status" aria-live="polite">
        <span class="glyph" aria-hidden="true">…</span>
        <span>
          ${progress.text}
          <br />
          <span class="meta">In progress — not a final answer.</span>
        </span>
      </p>
    `;
  }

  private renderNotices(snapshot: StoreSnapshot | undefined): TemplateResult | typeof nothing {
    const notices = snapshot?.notices ?? [];
    if (notices.length === 0) {
      return nothing;
    }
    return html`
      <div class="notices" aria-live="polite">
        ${notices.map(
          (notice) => html`
            <p class="notice info">
              <span class="glyph" aria-hidden="true">i</span>
              <span>${notice.text}</span>
            </p>
          `,
        )}
      </div>
    `;
  }

  // ── diagnostics ──────────────────────────────────────────────────────────

  private renderDiagnostics(snapshot: StoreSnapshot | undefined): TemplateResult | typeof nothing {
    const diagnostics = snapshot?.diagnostics ?? [];
    if (diagnostics.length === 0) {
      return nothing;
    }
    return html`
      <details class="diagnostics">
        <summary>
          ${diagnostics.length} ${diagnostics.length === 1 ? 'problem' : 'problems'} in the stream
        </summary>
        <ul class="plain">
          ${diagnostics.map(
            (diagnostic) => html`
              <li><span class="mono">${diagnostic.code}</span> — ${diagnostic.message}</li>
            `,
          )}
        </ul>
      </details>
    `;
  }

  // ── the surface itself ───────────────────────────────────────────────────

  private renderSurface(snapshot: StoreSnapshot | undefined): TemplateResult | typeof nothing {
    if (this.runtime === undefined) {
      return this.page({
        tone: 'error',
        glyph: '✕',
        title: 'No renderer',
        body: 'The surface runtime was not attached, so nothing can be displayed.',
      });
    }

    const surfaces = snapshot?.surfaces ?? [];
    if (surfaces.length === 0) {
      // Handled by renderPhase, which renders a skeleton / explanation.
      return nothing;
    }

    return html`
      <div class="surfaces">
        ${surfaces.map((surface) => {
          const slot = this.#slotFor(surface.surfaceId);
          return html`
            <div
              class="surface-slot"
              data-surface-id=${surface.surfaceId}
              ${ref(slot)}
            ></div>
          `;
        })}
      </div>
    `;
  }

  /** The ref for a surface, created on first use. */
  #slotFor(surfaceId: string): Ref<HTMLElement> {
    let slot = this.#slots.get(surfaceId);
    if (slot === undefined) {
      slot = createRef<HTMLElement>();
      this.#slots.set(surfaceId, slot);
    }
    return slot;
  }

  /**
   * Lit calls the ref with the element after the update commits.
   *
   * Mounting is done on a microtask so the container is in the DOM and has its
   * final layout before the surface is appended — measuring before that would
   * size the surface against a zero-width box.
   */
  protected override updated(): void {
    if (this.runtime === undefined) {
      return;
    }
    for (const [surfaceId, slot] of this.#slots) {
      const element = slot.value;
      if (element === undefined || this.#mounts.has(surfaceId)) {
        continue;
      }
      this.#mounts.set(surfaceId, mountSurface(element, this.runtime, surfaceId));
    }
  }

  private renderFooter(snapshot: StoreSnapshot | undefined): TemplateResult {
    const workflow = snapshot?.workflow;
    const seq = snapshot?.seq ?? -1;

    return html`
      <footer class="footer">
        <span class="chip neutral">
          <span aria-hidden="true">●</span>${this.streamStatus}
        </span>
        ${workflow !== undefined
          ? html`
              <span class="chip neutral">
                ${workflow.state.replace(/_/g, ' ').toLowerCase()}
              </span>
              <span class="chip neutral">v<span class="num">${workflow.stateVersion}</span></span>
            `
          : nothing}
        ${workflow?.degraded != null
          ? html`<span class="chip degraded">
              <span aria-hidden="true">△</span>${workflow.degraded.replace(/_/g, ' ')}
            </span>`
          : nothing}
        <span class="chip synthetic" title=${SYNTHETIC_TITLE}>${SYNTHETIC_LABEL}</span>
        <span class="meta">last seq <span class="num">${seq}</span></span>
      </footer>
    `;
  }

  // ── the page primitive ───────────────────────────────────────────────────

  private page(input: {
    tone: PageTone;
    glyph: string;
    title: string;
    body: string;
    retry?: boolean;
    skeleton?: boolean;
  }): TemplateResult {
    return html`
      <section class="card page" aria-live="polite">
        <div class="page-head">
          <span class="page-glyph ${input.tone}" aria-hidden="true">${input.glyph}</span>
          <div>
            <h1 class="page-title">${input.title}</h1>
            <p class="subtitle">${input.body}</p>
          </div>
        </div>

        ${input.retry === true
          ? html`
              <div class="actions">
                <button
                  class="secondary"
                  type="button"
                  @click=${() => this.dispatchEvent(new CustomEvent('re-retry', { bubbles: true, composed: true }))}
                >
                  Reconnect and replay
                </button>
              </div>
            `
          : nothing}
        ${input.skeleton === true
          ? html`
              <div class="skeleton-stack" aria-hidden="true">
                <div class="skeleton" style="height: 16px; width: 40%"></div>
                <div class="skeleton" style="height: 44px"></div>
                <div class="skeleton" style="height: 44px"></div>
              </div>
            `
          : nothing}
      </section>
    `;
  }
}

if (customElements.get('re-referral-app') === undefined) {
  customElements.define('re-referral-app', ReferralApp);
}

declare global {
  interface HTMLElementTagNameMap {
    're-referral-app': ReferralApp;
  }
}
