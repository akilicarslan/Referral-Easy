/**
 * The admin app's client for `/v1/admin/*` (§12, ADR-0039).
 *
 * **Three outcomes, not two.** Every read answers as one of:
 *
 *   - `data`        — the cluster answered and here is what it found;
 *   - `unavailable` — the server has no source, or its query failed, and it said which;
 *   - `failed`      — we could not ask at all (offline, a 500, an expired session).
 *
 * The last two are the reason this file exists rather than a bare `fetch`. Collapsing them into one
 * "no data" state is the exact failure §12 forbids: a dashboard whose gateway is down would render
 * the same empty charts as one that looked and found nothing, and the reader cannot tell a quiet
 * week from a broken deployment. `unavailable` comes from a **200** with `available: false`; `failed`
 * comes from everything else, and carries the envelope's own message when the gateway sent one.
 *
 * The session is the demo profile's fixture token (`POST /v1/auth/admin-session`), which the gateway
 * only registers when it is running as a demo and labels `fixture: true` in its answer. This client
 * is therefore honest about what it holds: a token that proves nothing, for a stack that has no
 * identity provider. A real deployment replaces this with the audience-checked identity §9 names.
 */

import type { AdminRoute } from './router.js';

/** The gateway's base URL, resolved once. Same rule as the patient client: build-time or same-origin. */
export const GATEWAY_BASE: string =
  (import.meta.env['VITE_GATEWAY_URL'] as string | undefined) ?? window.location.origin;

/** §12's window. `custom` carries its own bounds; the others are computed from the clock. */
export type RangeName = 'day' | 'month' | 'custom';

/**
 * The filter set.
 *
 * Every dimension but `range` is `?: T | undefined`, and the explicit `| undefined` is deliberate
 * under `exactOptionalPropertyTypes`: clearing a filter means setting the key to `undefined`, and
 * without it a caller would have to delete the property — which a `readonly` interface does not
 * allow, leaving "clear the agent filter" with no expression. Absent and `undefined` mean the same
 * thing to `queryString`, and neither contributes a parameter.
 */
export interface AdminFilters {
  readonly range: RangeName;
  readonly timezone?: string | undefined;
  readonly from?: string | undefined;
  readonly to?: string | undefined;
  readonly agents?: readonly string[] | undefined;
  readonly agentVersions?: readonly string[] | undefined;
  readonly releases?: readonly string[] | undefined;
  readonly models?: readonly string[] | undefined;
  readonly synthetic?: 'synthetic' | 'live_demo' | undefined;
}

/** What every measurement in a live payload carries (§12's sample count, §15's dedup signal). */
export interface Sample {
  readonly count: number;
  readonly documents: number;
  readonly duplicated: number;
  readonly truncated: boolean;
  readonly other: number;
}

/** A percentile set. `null` is "no data", never zero — §15 keeps timeouts in the distribution. */
export interface Percentiles {
  readonly metric: string;
  readonly p50: number | null;
  readonly p95: number | null;
  readonly p99: number | null;
  readonly min: number | null;
  readonly max: number | null;
  readonly sample: Sample;
}

export interface Bucket {
  readonly key: string;
  readonly value: number | null;
  /**
   * `null` when the payload carried no readable sample.
   *
   * Nullable rather than required because the payload crosses a process boundary: the type says what
   * may arrive, not what should. A view that has to handle the missing case is a view that cannot
   * render a bucket as though it had been measured.
   */
  readonly sample: Sample | null;
}

/** The window the server actually used, resolved in the requested zone. */
export interface ResolvedWindow {
  readonly range: string;
  readonly timezone: string;
  readonly start: string;
  readonly end: string;
  readonly interval: string;
}

export type ReadResult<T> =
  | { readonly kind: 'data'; readonly data: T & { window?: ResolvedWindow } }
  | { readonly kind: 'unavailable'; readonly reason: string }
  | { readonly kind: 'failed'; readonly message: string };

/** The error envelope every gateway refusal shares (§31). */
interface Envelope {
  readonly error?: { readonly code?: string; readonly message?: string };
}

export class AdminSessionError extends Error {}

/**
 * Build the query string for a read.
 *
 * Repeated parameters rather than a comma-joined value, because that is what FastAPI's
 * `list[str]` reads and what the route tests assert on. An absent dimension contributes nothing —
 * sending `agents=` would be a filter for the empty string, which §15 keeps out of the vocabulary.
 */
export function queryString(filters: AdminFilters): string {
  const params = new URLSearchParams();
  params.set('range', filters.range);
  if (filters.timezone !== undefined) params.set('timezone', filters.timezone);
  if (filters.from !== undefined) params.set('from', filters.from);
  if (filters.to !== undefined) params.set('to', filters.to);
  if (filters.synthetic !== undefined) params.set('synthetic', filters.synthetic);
  for (const [key, values] of [
    ['agents', filters.agents],
    ['agent_versions', filters.agentVersions],
    ['releases', filters.releases],
    ['models', filters.models],
  ] as const) {
    for (const value of values ?? []) params.append(key, value);
  }
  return params.toString();
}

export class AdminClient {
  #token: string | undefined;
  #session: Promise<string> | undefined;

  constructor(
    private readonly baseUrl: string = GATEWAY_BASE,
    private readonly subject = 'coordinator@demo',
    private readonly fetchImpl: typeof fetch = fetch,
  ) { this.fetchImpl = fetchImpl.bind(globalThis); }

  /**
   * Read one page.
   *
   * **Never throws.** A failure to mint the session, a transport error and a refusal all arrive as
   * `failed` with a message. The alternative — letting the session error propagate — means every
   * caller needs a `try`, and the one that forgets leaves a panel on "reading…" forever with an
   * unhandled rejection in the console. A dashboard that hangs silently is the worst of the three
   * outcomes, so the contract here is that `read` always answers.
   */
  async read(
    page: AdminRoute['id'],
    filters: AdminFilters,
  ): Promise<ReadResult<Record<string, unknown>>> {
    let token: string;
    try {
      token = await this.#sessionToken();
    } catch (problem) {
      return { kind: 'failed', message: describe(problem) };
    }

    let response: Response;
    try {
      response = await this.fetchImpl(
        `${this.#url(`/v1/admin/${page}`)}?${queryString(filters)}`,
        { headers: { authorization: `Bearer ${token}` } },
      );
    } catch (problem) {
      return { kind: 'failed', message: describe(problem) };
    }

    const body = await this.#json(response);
    if (body === undefined) {
      return { kind: 'failed', message: `the gateway answered ${response.status} with no JSON body` };
    }
    if (!response.ok) {
      // An expired or refused session is worth one retry on the next read rather than six failed
      // panels: the token is dropped so the next call mints a fresh one.
      if (response.status === 401) {
        this.reset();
      }
      return { kind: 'failed', message: refusal(body, response.status) };
    }
    if (body['available'] === false) {
      // The server has a shape for this and used it. Passing the reason through verbatim is what
      // lets a page say *why* it has nothing rather than restating "no data".
      return { kind: 'unavailable', reason: String(body['reason'] ?? 'no reason given') };
    }
    return { kind: 'data', data: body };
  }

  /** Forget the session, so the next read mints a new one. Used when a read answers 401. */
  reset(): void {
    this.#token = undefined;
    this.#session = undefined;
  }

  async #sessionToken(): Promise<string> {
    if (this.#token !== undefined) {
      return this.#token;
    }
    // One in-flight request, not one per panel: the overview page issues several reads at once and
    // six sessions for one page load would be six tokens for one visitor.
    this.#session ??= this.#mint().finally(() => {
      this.#session = undefined;
    });
    return this.#session;
  }

  async #mint(): Promise<string> {
    let response: Response;
    try {
      response = await this.fetchImpl(this.#url('/v1/auth/admin-session'), {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ subject: this.subject }),
      });
    } catch (problem) {
      throw new AdminSessionError(
        `the gateway could not be reached for a session: ${describe(problem)}`,
      );
    }
    const body = await this.#json(response);
    if (!response.ok || body === undefined) {
      throw new AdminSessionError(
        body === undefined
          ? `the session route answered ${response.status} with no JSON body`
          : refusal(body, response.status),
      );
    }
    const token = body['access_token'];
    if (typeof token !== 'string' || token.length === 0) {
      throw new AdminSessionError('the session route answered without an access_token');
    }
    this.#token = token;
    return token;
  }

  async #json(response: Response): Promise<Record<string, unknown> | undefined> {
    try {
      const parsed: unknown = await response.json();
      return typeof parsed === 'object' && parsed !== null
        ? (parsed as Record<string, unknown>)
        : undefined;
    } catch {
      return undefined;
    }
  }

  #url(path: string): string {
    return `${this.baseUrl.replace(/\/+$/, '')}${path}`;
  }
}

/** A refusal's own words when it sent them, and the status when it did not. */
function refusal(body: Record<string, unknown>, status: number): string {
  const envelope = body as Envelope;
  const message = envelope.error?.message;
  const code = envelope.error?.code;
  if (typeof message === 'string' && message.length > 0) {
    return code === undefined ? message : `${message} (${code})`;
  }
  return `the gateway refused the read with ${status}`;
}

function describe(problem: unknown): string {
  return problem instanceof Error ? problem.message : String(problem);
}

/** Read a sample out of a payload without trusting it. Missing keys become `null`, never zero. */
export function sampleOf(value: unknown): Sample | null {
  if (typeof value !== 'object' || value === null) return null;
  const record = value as Record<string, unknown>;
  const count = record['count'];
  if (typeof count !== 'number') return null;
  return {
    count,
    documents: typeof record['documents'] === 'number' ? record['documents'] : count,
    duplicated: typeof record['duplicated'] === 'number' ? record['duplicated'] : 0,
    truncated: record['truncated'] === true,
    other: typeof record['other'] === 'number' ? record['other'] : 0,
  };
}
