import { describe, expect, it } from 'vitest';

import {
  ADMIN_ROUTES,
  DEFAULT_ROUTE_ID,
  hashFor,
  parseHash,
  resolveRoute,
} from '../src/admin/router.js';
import { pageElement, registeredTags } from '../src/admin/pages/registry.js';

describe('the route table', () => {
  it('is §12s nine pages, with unique ids', () => {
    expect(ADMIN_ROUTES.map((route) => route.id)).toEqual([
      'overview',
      'agents',
      'traces',
      'evaluations',
      'guardrails',
      'feedback',
      'scenarios',
      'human-queue',
      'alerts',
    ]);
    expect(new Set(ADMIN_ROUTES.map((route) => route.id)).size).toBe(ADMIN_ROUTES.length);
  });

  it('gives every route a summary, because the page header shows one and an empty one is a blank', () => {
    for (const route of ADMIN_ROUTES) {
      expect(route.title.length).toBeGreaterThan(0);
      expect(route.summary.length).toBeGreaterThan(0);
    }
  });

  it('names a default that exists', () => {
    expect(ADMIN_ROUTES.some((route) => route.id === DEFAULT_ROUTE_ID)).toBe(true);
  });
});

describe('parseHash', () => {
  it('accepts every spelling a person can type', () => {
    // None of these is wrong enough to deserve a 404 screen.
    for (const hash of ['#/traces', '#traces', '/traces', 'traces', '#/traces/', 'traces/']) {
      expect(parseHash(hash), hash).toBe('traces');
    }
  });

  it('reports an empty hash as nothing named, not as a default', () => {
    // The distinction matters: `resolveRoute` turns "nothing named" into the landing page, and an
    // *unknown* name into an error. Collapsing them here would make a wrong address land quietly.
    expect(parseHash('')).toBeUndefined();
    expect(parseHash('#')).toBeUndefined();
    expect(parseHash('#/')).toBeUndefined();
    expect(parseHash('/')).toBeUndefined();
  });
});

describe('resolveRoute', () => {
  it('resolves a known id', () => {
    expect(resolveRoute('#/feedback')?.route.title).toBe('Feedback');
  });

  it('falls back to the default only when nothing was named', () => {
    expect(resolveRoute('')?.route.id).toBe(DEFAULT_ROUTE_ID);
    expect(resolveRoute('#')?.route.id).toBe(DEFAULT_ROUTE_ID);
  });

  it('refuses an unknown route rather than showing a nearby page', () => {
    // A stale bookmark that renders the overview reports success for a page nobody built. The tail
    // does not change this: a route that is not there matches nothing, however it is suffixed.
    expect(resolveRoute('#/overveiw')).toBeUndefined();
    expect(resolveRoute('#/admin/overview')).toBeUndefined();
    expect(resolveRoute('#/nothing/42')).toBeUndefined();
  });

  it('carries the tail for a page addressed at one thing', () => {
    // The trace waterfall: `#/traces/<trace_id>` is the trace page pointed at one trace.
    const match = resolveRoute('#/traces/4bf92f3577b34da6a3ce929d0e0e4736');
    expect(match?.route.id).toBe('traces');
    expect(match?.tail).toBe('4bf92f3577b34da6a3ce929d0e0e4736');
    // A trailing slash is a typo, not a different trace.
    expect(resolveRoute('#/traces/abc/')?.tail).toBe('abc');
    // No tail at all is the list.
    expect(resolveRoute('#/traces')?.tail).toBeUndefined();
    expect(resolveRoute('#/traces/')?.tail).toBeUndefined();
    // The id is opaque to the router; it does not police the shape.
    expect(resolveRoute('#/traces/not-a-hex-id')?.tail).toBe('not-a-hex-id');
  });
});

describe('hashFor', () => {
  it('round-trips through the router', () => {
    for (const route of ADMIN_ROUTES) {
      expect(resolveRoute(hashFor(route.id))?.route.id).toBe(route.id);
    }
  });

  it('round-trips a tail', () => {
    expect(resolveRoute(hashFor('traces', 'abc'))?.tail).toBe('abc');
  });
});

describe('the page registry', () => {
  it('has an element for every route, and no element for anything else', () => {
    // Both halves matter. A route with no element renders "this page's element is not registered",
    // and an element with no route is dead code that a build keeps shipping.
    for (const route of ADMIN_ROUTES) {
      expect(pageElement(route.id), route.id).toBeDefined();
    }
    expect(pageElement('nope')).toBeUndefined();
    expect(registeredTags()).toHaveLength(ADMIN_ROUTES.length);
  });

  it('registers elements that are actually defined', () => {
    // `pageElement` returns a tag string; without this, a renamed tag would satisfy the check above
    // while `createElement` produced an unknown element that renders nothing at all.
    for (const tag of registeredTags()) {
      expect(customElements.get(tag), tag).toBeDefined();
    }
  });
});
