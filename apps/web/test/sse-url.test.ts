/**
 * `buildUrl` — the one part of the stream client that is testable without an `EventSource`.
 *
 * This file exists because the function had no tests at all, while doing two things that are easy to
 * get subtly wrong and hard to notice: carrying the replay cursor, and (since the gateway requires a
 * patient session on the stream route) carrying the token. Both end up in a URL, which means a
 * mistake shows up as an authentication failure or a silent replay-from-zero, not as a crash.
 *
 * jsdom has no `EventSource`, so the transport's happy path still has no test — that is a real gap
 * and it is recorded in `apps/web/README.md`, not papered over here.
 */

import { describe, expect, it } from 'vitest';

import { buildUrl } from '../src/stream/sse-client.js';

const BASE = 'https://api.example.test';
const PATH = '/v1/workflows/{workflowId}/events';
const WORKFLOW = '11111111-1111-4111-8111-111111111111';

describe('buildUrl', () => {
  it('substitutes the workflow id into the path', () => {
    const url = new URL(buildUrl(BASE, PATH, WORKFLOW, undefined));
    expect(url.pathname).toBe(`/v1/workflows/${WORKFLOW}/events`);
  });

  it('omits both query parameters when it has neither', () => {
    const url = new URL(buildUrl(BASE, PATH, WORKFLOW, undefined));
    expect([...url.searchParams.keys()]).toEqual([]);
  });

  it('does not add an empty token to the URL', () => {
    // An empty string is falsy but `!== undefined`, so the naive check writes `?token=`, which the
    // gateway reads as a present-and-invalid credential rather than as no credential at all.
    const url = new URL(buildUrl(BASE, PATH, WORKFLOW, undefined, ''));
    expect(url.searchParams.has('token')).toBe(false);
  });

  it('percent-encodes a token rather than corrupting it', () => {
    // Base64url is not the only thing a gateway might issue: `+`, `/` and `=` are all ordinary in
    // base64, and a hand-built query string turns a `+` into a space. The failure then looks like
    // an expired session.
    const token = 'a+b/c=d';
    const url = new URL(buildUrl(BASE, PATH, WORKFLOW, undefined, token));
    expect(url.searchParams.get('token')).toBe(token);
    expect(url.search).toContain('token=a%2Bb%2Fc%3Dd');
  });

  it('carries the replay cursor and the token together', () => {
    const url = new URL(buildUrl(BASE, PATH, WORKFLOW, 'event-42', 'tok'));
    expect(url.searchParams.get('last_event_id')).toBe('event-42');
    expect(url.searchParams.get('token')).toBe('tok');
  });

  it('tolerates a trailing slash on the base URL', () => {
    // The base comes from configuration, and `https://host/` + `/v1/...` would otherwise produce
    // `//v1/...` — a path most routers 404 without explaining why.
    const url = new URL(buildUrl('https://api.example.test/', PATH, WORKFLOW, undefined));
    expect(url.pathname).toBe(`/v1/workflows/${WORKFLOW}/events`);
  });

  it('encodes a workflow id that would otherwise change the path', () => {
    const url = new URL(buildUrl(BASE, PATH, 'a/b', undefined));
    expect(url.pathname).toBe('/v1/workflows/a%2Fb/events');
  });
});
