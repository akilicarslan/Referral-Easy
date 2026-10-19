import { describe, expect, it } from 'vitest';
import { canonicalRoute, normalizePath, routeMatches } from '../src/faults/routes.js';

/**
 * Route folding is why a fixture written against `GET /availability` still
 * fires on the canonical `POST /availability`, and why the notification fixture
 * reaches the outbox sender's hop instead of an address nobody calls. It is
 * small enough to be obvious and load-bearing enough to be pinned.
 */
describe('route folding', () => {
  it('folds the spellings of one endpoint onto one canonical route', () => {
    expect(canonicalRoute('GET', '/availability')).toBe('POST /availability');
    expect(canonicalRoute('POST', '/availability')).toBe('POST /availability');
    expect(canonicalRoute('GET', '/coverage')).toBe('POST /coverage/lookup');
    expect(canonicalRoute('POST', '/notifications')).toBe('POST /internal/notifications/send');
    expect(canonicalRoute('POST', '/internal/notifications/send')).toBe('POST /internal/notifications/send');

    // A query string, a trailing slash and a lower-case verb are the same address.
    expect(canonicalRoute('get', '/availability?hospital_id=20000000-0000-4000-8000-000000000001')).toBe('POST /availability');
    expect(normalizePath('/hospitals/')).toBe('/hospitals');

    // Everything else is left exactly as it was written.
    expect(canonicalRoute('POST', '/appointments')).toBe('POST /appointments');
    expect(canonicalRoute('GET', '/notifications/outbox')).toBe('GET /notifications/outbox');
  });

  it('matches a step against either spelling of the route it names', () => {
    const delivery = { match: { method: 'POST', path: '/notifications' } };
    expect(routeMatches(delivery, 'POST', '/notifications')).toBe(true);
    expect(routeMatches(delivery, 'POST', '/internal/notifications/send')).toBe(true);
    // The fold is per route, not a blanket "any notifications call".
    expect(routeMatches(delivery, 'GET', '/notifications/outbox')).toBe(false);
    expect(routeMatches(delivery, 'POST', '/notifications/drain')).toBe(false);

    const anyMethod = { match: { path: '/appointments' } };
    expect(routeMatches(anyMethod, 'GET', '/appointments')).toBe(true);
    expect(routeMatches(anyMethod, 'GET', '/hospitals')).toBe(false);
  });
});
