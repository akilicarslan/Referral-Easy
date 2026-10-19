/**
 * The admin app's router: nine pages, addressed by hash.
 *
 * **Hash routing, deliberately.** `apps/web` is served by nginx with
 * `try_files $uri $uri/ /index.html`, so a deep path like `/admin/traces` would answer with the
 * *patient* shell — the fallback is the file that exists, and it is the wrong one. Hash routes stay
 * inside `admin.html`, which is a real file at a real path, so the same URL means the same page
 * under the dev server and under nginx, and neither needs a rewrite rule. `nginx.conf`'s comment
 * claims the client "otherwise routes in-page"; until this file, it did not.
 *
 * **Unknown is a page, not a blank.** Every lookup is total: `resolveRoute` answers with a route for
 * a known id and with `undefined` for anything else, and the shell renders the list of pages it does
 * have rather than an empty outlet. A blank screen says "that page has nothing on it"; this one says
 * "that page is not one of mine", which is a different fact.
 *
 * No dependency: a router that pulls a library for nine static routes adds a lifecycle to reason
 * about that the routes do not need.
 */

/** One page. `id` is what appears in the hash and what a page element is tagged with. */
export interface AdminRoute {
  readonly id: string;
  /** Nav label, and the page heading. */
  readonly title: string;
  /** One line under the heading: what this page answers. */
  readonly summary: string;
}

/**
 * §12's nine pages, in the order §12 lists them.
 *
 * The order is the spec's and not by importance, so a reader comparing this list with the spec finds
 * them in the same sequence.
 */
export const ADMIN_ROUTES: readonly AdminRoute[] = [
  {
    id: 'overview',
    title: 'Overview',
    summary: 'What the stack recorded in the selected window, and how much of it there was.',
  },
  {
    id: 'agents',
    title: 'Agents & Versions',
    summary: 'Which agent, prompt, skill and model versions produced the traffic.',
  },
  {
    id: 'traces',
    title: 'Trace Explorer',
    summary: 'One trace, end to end: the spans and the attributes each one carried.',
  },
  {
    id: 'evaluations',
    title: 'Evaluations',
    summary: '§14 scores per run and metric, never merged across judges or rubrics.',
  },
  {
    id: 'guardrails',
    title: 'Guardrails',
    summary: 'What each §9 layer decided, counted per layer and decision.',
  },
  {
    id: 'feedback',
    title: 'Feedback',
    summary: 'Raw count, like rate and participation rate, kept apart on purpose.',
  },
  {
    id: 'scenarios',
    title: 'Fault Scenarios',
    summary: 'The eight demo scenarios, their fault step, and how to replay one.',
  },
  {
    id: 'human-queue',
    title: 'Human Queue',
    summary: 'Handoffs waiting for an operator, and what each one is waiting for.',
  },
  {
    id: 'alerts',
    title: 'Alerts',
    summary: 'Scheduler rules that fired, their dedup window, and the mock email outbox.',
  },
];

/** The page every visitor lands on when the hash names nothing. */
export const DEFAULT_ROUTE_ID = 'overview';

/**
 * A resolved hash: the page, and whatever followed it.
 *
 * The tail is how a page addresses *one thing* — `#/traces/<trace_id>` — without a route table that
 * enumerates what a page might want to point at. It is a single opaque segment rather than a parsed
 * path: the router has no business knowing that a trace id is a hex string, and a page that needs to
 * reject a malformed tail can do it where the meaning lives.
 */
export interface RouteMatch {
  readonly route: AdminRoute;
  /** Everything after the route id, or `undefined` when the hash named only a page. */
  readonly tail: string | undefined;
}

/** Split a hash into its route id and tail. */
function segments(hash: string): { id: string | undefined; tail: string | undefined } {
  const trimmed = hash
    .replace(/^#/, '')
    .replace(/^\/+/, '')
    .replace(/\/+$/, '');
  if (trimmed.length === 0) {
    return { id: undefined, tail: undefined };
  }
  const slash = trimmed.indexOf('/');
  if (slash === -1) {
    return { id: trimmed, tail: undefined };
  }
  const tail = trimmed.slice(slash + 1).replace(/\/+$/, '');
  return { id: trimmed.slice(0, slash), tail: tail.length > 0 ? tail : undefined };
}

/**
 * The route id a hash names, or `undefined`.
 *
 * Accepts `#/overview`, `#overview`, `overview` and the trailing-slash forms, because all four are
 * things a person can type and none of them is wrong enough to deserve a 404 screen. An empty hash
 * is `undefined` and not the default: the caller decides what "no route named" means.
 */
export function parseHash(hash: string): string | undefined {
  return segments(hash).id;
}

/**
 * Resolve a hash, defaulting only when nothing was named.
 *
 * The two cases are separate because they mean different things: no hash is "wherever we start", and
 * an *unknown* hash is a wrong address that must not silently become the overview page — that is how
 * a stale bookmark reports success for a page nobody built.
 *
 * A tail is only carried for a route that exists: `#/traces/abc` matches the trace page with
 * `tail: "abc"`, while `#/nope/abc` matches nothing at all.
 */
export function resolveRoute(hash: string): RouteMatch | undefined {
  const { id, tail } = segments(hash);
  if (id === undefined) {
    const route = ADMIN_ROUTES.find((candidate) => candidate.id === DEFAULT_ROUTE_ID);
    return route === undefined ? undefined : { route, tail: undefined };
  }
  const route = ADMIN_ROUTES.find((candidate) => candidate.id === id);
  return route === undefined ? undefined : { route, tail };
}

/** The hash for a route id. Used by the nav, which must not spell `#/` anywhere else. */
export function hashFor(id: string, tail?: string): string {
  return tail === undefined ? `#/${id}` : `#/${id}/${tail}`;
}

/**
 * Watch `window.location.hash` and report the current match.
 *
 * `onChange` fires once immediately with the match the page loaded on, so a caller never has to
 * render before subscribing and then reconcile. Returns an unsubscribe function; a test installs a
 * listener, drives `hashchange`, and removes it, without a real browser hash ever moving.
 */
export function watchRoute(onChange: (match: RouteMatch | undefined, hash: string) => void): () => void {
  const report = (): void => {
    onChange(resolveRoute(window.location.hash), window.location.hash);
  };
  window.addEventListener('hashchange', report);
  report();
  return () => {
    window.removeEventListener('hashchange', report);
  };
}
