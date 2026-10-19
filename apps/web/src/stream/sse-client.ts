import { sse$StreamEvent } from '@referraleasy/contracts';

/**
 * The SSE client.
 *
 * Responsibilities, in the order the contract specifies them:
 *
 *   1. **Connect and track `seq`.** `seq` is monotonic per workflow, so a gap
 *      means we missed an event. That is not a cosmetic problem: a missed
 *      `surface_update` means the patient is looking at a stale question.
 *
 *   2. **Replay from the last `event_id` on reconnect.** `event_id` is
 *      content-addressed, and replaying from it must reproduce the same sequence
 *      and must not create a second booking. We send it both as the standard
 *      `Last-Event-ID` header (which `EventSource` maintains itself from the
 *      `id:` field) and as a `last_event_id` query parameter, because which one
 *      a given gateway honours is a server detail we do not control.
 *
 *   3. **Parse defensively.** A malformed or unrecognised event is reported and
 *      skipped. It must never take the connection down: a stream that throws on
 *      one bad frame leaves the patient on a blank screen, and a blank screen is
 *      a failure.
 *
 * The transport is injectable so tests can drive reconnect behaviour
 * deterministically, without a network.
 */

export type StreamStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface StreamDiagnostic {
  readonly code: string;
  readonly message: string;
}

/** What the client needs from a transport. Kept tiny so it is trivial to fake. */
export interface StreamTransport {
  /**
   * Open a connection.
   *
   * @param url Absolute URL to connect to.
   * @param lastEventId Replay cursor, or undefined for a first connection.
   * @param handlers Callbacks for the connection lifecycle.
   * @returns A function that closes this connection.
   */
  open(
    url: string,
    lastEventId: string | undefined,
    handlers: {
      onOpen: () => void;
      onEvent: (id: string | undefined, data: string) => void;
      onError: (recoverable: boolean) => void;
    },
  ): () => void;
}

export interface SseClientOptions {
  /** Base URL of the gateway, e.g. `http://localhost:8000`. */
  readonly baseUrl: string;
  /** Path template. `{workflowId}` is substituted. */
  readonly path?: string;
  /**
   * Session token for the stream, when the gateway requires one.
   *
   * It travels as a **query parameter**, and that is a limitation rather than a preference:
   * `EventSource` can send no headers at all, so `Authorization` is not available to it. The
   * gateway accepts `?token=` only in its demo profile and refuses it in production, for the
   * documented reason that a token in a URL ends up in access logs, referrers and browser history
   * (ADR-0031).
   *
   * So: this is what makes the stream reachable from a browser today. A deployment that needs
   * header-based auth must supply a `transport` that can set headers — which is exactly why
   * `transport` is injectable.
   */
  readonly token?: string;
  readonly transport?: StreamTransport;
  readonly onEvent: (event: sse$StreamEvent) => void;
  readonly onStatus: (status: StreamStatus) => void;
  readonly onDiagnostic?: (diagnostic: StreamDiagnostic) => void;
  /** Milliseconds to wait before the first reconnect attempt. */
  readonly retryBaseMs?: number;
  /** Ceiling for the backoff. */
  readonly retryMaxMs?: number;
}

const DEFAULT_PATH = '/v1/workflows/{workflowId}/events';

export class SseClient {
  readonly #options: SseClientOptions;
  readonly #transport: StreamTransport;

  /** Highest `seq` applied so far, or -1 before the first event. */
  #lastSeq = -1;
  /** `event_id` to replay from. */
  #lastEventId: string | undefined;
  #status: StreamStatus = 'idle';
  #close: (() => void) | undefined;
  #retryTimer: ReturnType<typeof setTimeout> | undefined;
  #attempt = 0;
  #workflowId: string | undefined;
  #generation = 0;

  constructor(options: SseClientOptions) {
    this.#options = options;
    this.#transport = options.transport ?? new EventSourceTransport();
  }

  get status(): StreamStatus {
    return this.#status;
  }

  get lastSeq(): number {
    return this.#lastSeq;
  }

  get lastEventId(): string | undefined {
    return this.#lastEventId;
  }

  /** Connect for a workflow. Calling it again switches workflow and resets the cursor. */
  connect(workflowId: string, cursor?: { seq: number; eventId: string | undefined }): void {
    this.disconnect();
    this.#workflowId = workflowId;
    this.#lastSeq = cursor?.seq ?? -1;
    this.#lastEventId = cursor?.eventId;
    this.#attempt = 0;
    this.#open(false);
  }

  disconnect(): void {
    this.#generation += 1;
    if (this.#retryTimer !== undefined) {
      clearTimeout(this.#retryTimer);
      this.#retryTimer = undefined;
    }
    this.#close?.();
    this.#close = undefined;
    this.#setStatus('closed');
  }

  /**
   * Force a replay from the current cursor.
   *
   * Used when a `seq` gap is detected, and exposed so the UI can offer a manual
   * retry when a surface looks stale.
   */
  replay(): void {
    if (this.#workflowId === undefined) {
      return;
    }
    this.#options.onDiagnostic?.({
      code: 'REPLAY_REQUESTED',
      message: `Replaying from seq ${this.#lastSeq}.`,
    });
    this.#stopConnection();
    this.#open(true);
  }

  #stopConnection(): void {
    this.#generation += 1;
    if (this.#retryTimer !== undefined) {
      clearTimeout(this.#retryTimer);
      this.#retryTimer = undefined;
    }
    this.#close?.();
    this.#close = undefined;
  }

  // ── internals ────────────────────────────────────────────────────────────

  #open(isReplay: boolean): void {
    const workflowId = this.#workflowId;
    if (workflowId === undefined) {
      return;
    }

    this.#setStatus(isReplay ? 'reconnecting' : this.#attempt === 0 ? 'connecting' : 'reconnecting');

    const url = buildUrl(
      this.#options.baseUrl,
      this.#options.path ?? DEFAULT_PATH,
      workflowId,
      this.#lastEventId,
      this.#options.token,
    );

    const generation = ++this.#generation;
    const close = this.#transport.open(url, this.#lastEventId, {
      onOpen: () => {
        if (generation !== this.#generation) return;
        this.#attempt = 0;
        this.#setStatus('open');
      },
      onEvent: (id, data) => {
        if (generation !== this.#generation) return;
        this.#handleFrame(id, data);
      },
      // `id` is the SSE `id:` field. It is deliberately NOT used as the replay
      // cursor: the cursor must be the contract's `event_id`, because that is
      // what the `last_event_id` query parameter means. `EventSource` maintains
      // its own `Last-Event-ID` header from it, which covers the header case.
      onError: (recoverable) => {
        if (generation !== this.#generation) return;
        this.#stopConnection();
        if (!recoverable) {
          this.#setStatus('closed');
          return;
        }
        this.#scheduleReconnect();
      },
    });
    // A transport may report an error synchronously while opening (e.g. no EventSource).
    if (generation === this.#generation) this.#close = close;
    else close();
  }

  #handleFrame(_id: string | undefined, data: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      this.#options.onDiagnostic?.({
        code: 'SSE_UNPARSEABLE_FRAME',
        message: 'A stream frame was not valid JSON and was skipped.',
      });
      return;
    }

    const result = sse$StreamEvent.safeParse(parsed);
    if (!result.success) {
      // A schema-invalid frame is a server bug, not a reason to drop the
      // patient's session. Report it and keep the connection.
      this.#options.onDiagnostic?.({
        code: 'SSE_INVALID_EVENT',
        message: `A stream event did not match the contract and was skipped: ${result.error.issues
          .map((issue) => issue.path.join('.') || '(root)')
          .join(', ')}`,
      });
      return;
    }

    const event = result.data;
    if (event.workflow_id !== this.#workflowId) {
      this.#options.onDiagnostic?.({ code: 'SSE_WRONG_WORKFLOW', message: 'An event for another workflow was dropped.' });
      return;
    }

    if (event.seq <= this.#lastSeq) {
      // Already applied. Replay is allowed to resend; applying twice is the
      // store's problem to solve, not a reason to drop the frame here.
      return;
    }

    if (this.#lastSeq >= 0 && event.seq > this.#lastSeq + 1) {
      this.#options.onDiagnostic?.({
        code: 'SSE_SEQ_GAP',
        message: `Missed ${event.seq - this.#lastSeq - 1} event(s) between seq ${this.#lastSeq} and ${event.seq}. Replaying.`,
      });
      // Keep the last contiguous cursor: advancing to this frame would skip the missing cards.
      this.replay();
      return;
    }

    this.#lastSeq = event.seq;
    this.#lastEventId = event.event_id;
    this.#options.onEvent(event);
  }

  #scheduleReconnect(): void {
    if (this.#retryTimer !== undefined) {
      return;
    }
    const base = this.#options.retryBaseMs ?? 500;
    const max = this.#options.retryMaxMs ?? 10_000;
    const delay = Math.min(max, base * 2 ** this.#attempt);
    this.#attempt += 1;

    this.#setStatus('reconnecting');
    this.#retryTimer = setTimeout(() => {
      this.#retryTimer = undefined;
      this.#open(true);
    }, delay);
  }

  #setStatus(status: StreamStatus): void {
    if (this.#status === status) {
      return;
    }
    this.#status = status;
    this.#options.onStatus(status);
  }
}

/** Build the events URL, including the replay cursor and session token when we have them. */
export function buildUrl(
  baseUrl: string,
  path: string,
  workflowId: string,
  lastEventId: string | undefined,
  token?: string,
): string {
  const base = baseUrl.replace(/\/+$/, '');
  const resolved = path.replace('{workflowId}', encodeURIComponent(workflowId));
  const url = new URL(`${base}${resolved.startsWith('/') ? '' : '/'}${resolved}`);
  if (lastEventId !== undefined) {
    // Belt and braces: `EventSource` also sends Last-Event-ID from the `id:`
    // field, but a proxy can strip unknown headers and cannot strip a query
    // parameter. Which one the gateway honours is not ours to decide.
    url.searchParams.set('last_event_id', lastEventId);
  }
  if (token !== undefined && token !== '') {
    // `new URL()` percent-encodes on assignment, so a token containing `+`, `/` or `=` survives
    // the round trip intact. Setting it by hand-building the query string is how base64 tokens
    // turn into authentication failures that look like expiry.
    url.searchParams.set('token', token);
  }
  return url.toString();
}

/** Native transport. SseClient owns reconnects, so every failed source is closed. */
export class EventSourceTransport implements StreamTransport {
  open(
    url: string,
    _lastEventId: string | undefined,
    handlers: {
      onOpen: () => void;
      onEvent: (id: string | undefined, data: string) => void;
      onError: (recoverable: boolean) => void;
    },
  ): () => void {
    if (typeof EventSource === 'undefined') {
      // jsdom and very old browsers. Report it as unrecoverable rather than
      // pretending the stream is alive.
      handlers.onError(false);
      return () => {};
    }

    // The demo session travels in the URL; cookies are not used.
    const source = new EventSource(url);

    source.onopen = () => {
      handlers.onOpen();
    };
    source.onmessage = (message: MessageEvent<string>) => {
      handlers.onEvent(message.lastEventId === '' ? undefined : message.lastEventId, message.data);
    };
    source.onerror = () => {
      // `EventSource` reports both transient and terminal failures here and
      // reconnects on its own for the former. `readyState === CLOSED` is the
      // only signal that it has given up.
      const recoverable = source.readyState !== EventSource.CLOSED;
      source.close();
      handlers.onError(recoverable);
    };

    return () => {
      source.close();
    };
  }
}
