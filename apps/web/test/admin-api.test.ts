import { describe, expect, it, vi } from 'vitest';

import { AdminClient, queryString, sampleOf } from '../src/admin/api.js';

/** A fetch double that answers a scripted sequence and records what it was asked. */
function fetcher(responses: readonly Response[]): {
  fetch: typeof fetch;
  calls: { url: string; init: RequestInit | undefined }[];
} {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  let index = 0;
  const impl = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    calls.push({ url: String(input), init: init ?? undefined });
    const response = responses[Math.min(index, responses.length - 1)];
    index += 1;
    if (response === undefined) {
      throw new Error('no scripted response');
    }
    return Promise.resolve(response);
  };
  return { fetch: impl as unknown as typeof fetch, calls };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const SESSION = { access_token: 'fixture-token', token_type: 'Bearer', fixture: true };

describe('queryString', () => {
  it('carries the window and nothing else when no dimension is set', () => {
    expect(queryString({ range: 'day' })).toBe('range=day');
  });

  it('repeats a dimension rather than joining it', () => {
    // FastAPI reads `list[str]` from repeated parameters. A comma-joined value would arrive as one
    // agent named "scheduling,triage" and filter everything out — silently, as an empty chart.
    const parsed = new URLSearchParams(
      queryString({ range: 'custom', from: '2026-10-01T00:00', to: '2026-10-02T00:00', agents: ['scheduling', 'triage'] }),
    );
    expect(parsed.getAll('agents')).toEqual(['scheduling', 'triage']);
    expect(parsed.get('from')).toBe('2026-10-01T00:00');
    expect(parsed.get('to')).toBe('2026-10-02T00:00');
  });

  it('omits an unset filter rather than sending it empty', () => {
    // `agents=` would be a filter for the empty string, which §15 keeps out of the vocabulary: an
    // event with no agent would match and the chart would be about a dimension nobody chose.
    const parsed = new URLSearchParams(queryString({ range: 'day', synthetic: undefined, agents: [] }));
    expect(parsed.has('synthetic')).toBe(false);
    expect(parsed.has('agents')).toBe(false);
  });
});

describe('AdminClient.read', () => {
  it('mints one session for concurrent reads', async () => {
    // Six panels opening at once must not mint six tokens; the shell loads several reads per page.
    const { fetch, calls } = fetcher([json(SESSION), json({ available: true }), json({ available: true })]);
    const client = new AdminClient('http://gateway', 'coordinator@demo', fetch);

    await Promise.all([client.read('overview', { range: 'day' }), client.read('guardrails', { range: 'day' })]);

    const sessions = calls.filter((call) => call.url.includes('/v1/auth/admin-session'));
    expect(sessions).toHaveLength(1);
  });

  it('sends the bearer token and the window', async () => {
    const { fetch, calls } = fetcher([json(SESSION), json({ available: true })]);
    const client = new AdminClient('http://gateway', 'coordinator@demo', fetch);

    await client.read('feedback', { range: 'month', timezone: 'UTC' });

    const read = calls[1];
    expect(read?.url).toBe('http://gateway/v1/admin/feedback?range=month&timezone=UTC');
    expect((read?.init?.headers as Record<string, string>)['authorization']).toBe('Bearer fixture-token');
  });

  it('reports the servers own no-source reason as unavailable', async () => {
    const { fetch } = fetcher([
      json(SESSION),
      json({ available: false, reason: 'OPENSEARCH_URL is unset' }),
    ]);
    const client = new AdminClient('http://gateway', 'coordinator@demo', fetch);

    const result = await client.read('overview', { range: 'day' });

    expect(result).toEqual({ kind: 'unavailable', reason: 'OPENSEARCH_URL is unset' });
  });

  it('keeps "we could not ask" apart from "there is no source"', async () => {
    // The whole reason this client exists. A gateway that is down must not render as a quiet day,
    // and the two outcomes carry different text to different screens.
    const { fetch } = fetcher([json(SESSION), json({ error: { code: 'not_found' } }, 500)]);
    const client = new AdminClient('http://gateway', 'coordinator@demo', fetch);

    const result = await client.read('overview', { range: 'day' });

    expect(result.kind).toBe('failed');
  });

  it('surfaces the error envelope message rather than the status alone', async () => {
    const { fetch } = fetcher([
      json(SESSION),
      json({ error: { code: 'contract_violation', message: "range='week' is not one of day, month, custom" } }, 422),
    ]);
    const client = new AdminClient('http://gateway', 'coordinator@demo', fetch);

    const result = await client.read('overview', { range: 'day' });

    expect(result).toEqual({
      kind: 'failed',
      message: "range='week' is not one of day, month, custom (contract_violation)",
    });
  });

  it('reports a transport failure as failed, never as unavailable', async () => {
    // The session call fails too, and that path has to answer rather than throw: a `read` that
    // rejected would leave its panel on "reading…" forever with an unhandled rejection in the
    // console, which is the least visible of the three outcomes.
    const impl = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));
    const client = new AdminClient('http://gateway', 'coordinator@demo', impl as unknown as typeof fetch);

    const result = await client.read('overview', { range: 'day' });

    expect(result).toEqual({ kind: 'failed', message: 'the gateway could not be reached for a session: Failed to fetch' });
  });

  it('refuses a 200 that is not JSON, rather than treating it as data', async () => {
    const { fetch } = fetcher([json(SESSION), new Response('<html>proxy error</html>', { status: 200 })]);
    const client = new AdminClient('http://gateway', 'coordinator@demo', fetch);

    const result = await client.read('overview', { range: 'day' });

    expect(result.kind).toBe('failed');
    expect(result.kind === 'failed' && result.message).toContain('no JSON body');
  });

  it('raises a named error when the session itself cannot be minted', async () => {
    const client = new AdminClient('http://gateway', 'coordinator@demo', fetcher([
      json({ error: { message: 'no admin token issuer is configured' } }, 404),
    ]).fetch);

    // The *client-level* guarantee: `read` answers. `AdminSessionError` is what the minting path
    // raises internally, and a caller never sees it.
    const result = await client.read('overview', { range: 'day' });
    expect(result).toEqual({
      kind: 'failed',
      message: 'no admin token issuer is configured',
    });
  });

  it('drops the session when a read is refused 401, so the next read mints a fresh one', async () => {
    const { fetch, calls } = fetcher([
      json(SESSION),
      json({ error: { code: 'unauthenticated', message: 'session expired' } }, 401),
      json(SESSION),
      json({ available: true, events: {} }),
    ]);
    const client = new AdminClient('http://gateway', 'coordinator@demo', fetch);

    const refused = await client.read('overview', { range: 'day' });
    const recovered = await client.read('overview', { range: 'day' });

    expect(refused.kind).toBe('failed');
    expect(recovered.kind).toBe('data');
    expect(calls.filter((call) => call.url.includes('admin-session'))).toHaveLength(2);
  });

  it('mints again after reset', async () => {
    const { fetch, calls } = fetcher([
      json(SESSION),
      json({ available: true }),
      json(SESSION),
      json({ available: true }),
    ]);
    const client = new AdminClient('http://gateway', 'coordinator@demo', fetch);

    await client.read('overview', { range: 'day' });
    client.reset();
    await client.read('overview', { range: 'day' });

    expect(calls.filter((call) => call.url.includes('admin-session'))).toHaveLength(2);
    expect(calls.filter((call) => call.url.includes('/v1/admin/'))).toHaveLength(2);
  });
});

describe('sampleOf', () => {
  it('reads a sample', () => {
    expect(sampleOf({ count: 2, documents: 3, duplicated: 1, truncated: false, other: 0 })).toEqual({
      count: 2,
      documents: 3,
      duplicated: 1,
      truncated: false,
      other: 0,
    });
  });

  it('returns null rather than filling in a count of zero', () => {
    // A missing sample is a gap, and `?? 0` here would report "computed from nothing" as
    // "computed from zero events" — which reads as a quiet window rather than a broken payload.
    expect(sampleOf(undefined)).toBeNull();
    expect(sampleOf({})).toBeNull();
    expect(sampleOf({ documents: 4 })).toBeNull();
    expect(sampleOf('2')).toBeNull();
  });
});
