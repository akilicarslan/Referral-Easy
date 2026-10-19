/**
 * Which element draws which page.
 *
 * A registry rather than a switch in the shell, so the shell does not import nine page modules and a
 * page can be added by adding one line here and one module beside it. `import` for the side effect —
 * each page module defines its own custom element at module scope, guarded with
 * `customElements.get(...) === undefined` so a double import cannot redefine it.
 */

import './overview.js';
import './agents.js';
import './traces.js';
import './evaluations.js';
import './guardrails.js';
import './feedback.js';
import './scenarios.js';
import './human-queue.js';
import './alerts.js';

const TAGS: ReadonlyMap<string, string> = new Map([
  ['overview', 're-admin-overview'],
  ['agents', 're-admin-agents'],
  ['traces', 're-admin-traces'],
  ['evaluations', 're-admin-evaluations'],
  ['guardrails', 're-admin-guardrails'],
  ['feedback', 're-admin-feedback'],
  ['scenarios', 're-admin-scenarios'],
  ['human-queue', 're-admin-human-queue'],
  ['alerts', 're-admin-alerts'],
]);

/**
 * The custom element tag for a route id, or `undefined` for one that has no page.
 *
 * `undefined` is a real answer: the shell renders "this page's element is not registered" rather
 * than an empty card, because a missing registration is a build problem and should not be
 * indistinguishable from a page that legitimately has nothing to show.
 */
export function pageElement(id: string): string | undefined {
  return TAGS.get(id);
}

/** Every tag this bundle registers. Used by the test that asserts the registry matches the routes. */
export function registeredTags(): readonly string[] {
  return [...TAGS.values()];
}
