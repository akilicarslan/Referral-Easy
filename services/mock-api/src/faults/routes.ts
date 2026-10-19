import type { FaultStep } from './types.js';

/**
 * Route folding: one code path, several spellings.
 *
 * Several endpoints are reachable under more than one address. The canonical
 * address is the one the contract names (`POST /availability`,
 * `POST /coverage/lookup`, and the outbox sender's internal hop), while the
 * demo's scenario text — and therefore the fault fixtures — address the same
 * calls as `GET /availability`, `GET /coverage` and `POST /notifications`.
 *
 * They are the same call, so they must be the same *route* to the fault engine:
 * a fault armed for one spelling that never fires on the other is, in practice,
 * a fault nobody can trigger, and a fixture that silently stops firing is a
 * defect no test would notice on its own.
 *
 * The second column is the address the engine counts occurrences against, which
 * is why the counters of the two spellings are shared rather than separate: the
 * `occurrence: 2` in a fixture means "the second time this endpoint is called",
 * not "the second time it is called by this particular client".
 */
const ROUTE_FOLDS: ReadonlyArray<readonly [string, string]> = [
  ['GET /availability', 'POST /availability'],
  ['GET /coverage', 'POST /coverage/lookup'],
  ['POST /notifications', 'POST /internal/notifications/send'],
  ['POST /notifications/send', 'POST /internal/notifications/send'],
];

const ROUTE_CANONICAL = new Map<string, string>(ROUTE_FOLDS);

/** Drops the query string and a single trailing slash. */
export function normalizePath(path: string): string {
  const withoutQuery = path.split('?')[0] ?? path;
  if (withoutQuery.length > 1 && withoutQuery.endsWith('/')) return withoutQuery.slice(0, -1);
  return withoutQuery;
}

/**
 * The canonical `METHOD /path` address of a route.
 *
 * Both the requests and the fixture steps are folded with this, so every
 * comparison below is a comparison of canonical addresses and no call site has
 * to remember which spelling a fixture happened to use.
 */
export function canonicalRoute(method: string, path: string): string {
  const key = `${method.toUpperCase()} ${normalizePath(path)}`;
  return ROUTE_CANONICAL.get(key) ?? key;
}

/**
 * Whether a fixture step targets a route.
 *
 * A step may omit the method (`match: { path }`), which means "any method on
 * this path" — the one form that cannot be folded, because folding a method
 * requires a method to fold with.
 */
export function routeMatches(step: FaultStep, method: string, path: string): boolean {
  const declaredPath = step.match.path;
  const declaredMethod = step.match.method?.toUpperCase();

  if (declaredMethod === undefined) {
    // The default argument mirrors the engine's own leniency: a step that names
    // no path either is a step about every route.
    return declaredPath === undefined || canonicalRoute('*', declaredPath) === canonicalRoute('*', path);
  }
  return canonicalRoute(declaredMethod, declaredPath ?? path) === canonicalRoute(method, path);
}
