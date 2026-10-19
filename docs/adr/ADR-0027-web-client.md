# ADR-0027 — The web client renders what the server sends, and says nothing the server has not said

- **Status:** Accepted. Every claim about the protocol library below was read out of the installed
  `@a2ui` 0.13.0 sources and is listed with what it cost to learn. The two that were not verified are
  named as such at the end, not smoothed over.
- **Date:** 2026-10-14
- **Phase:** the patient web client (§12)
- **Decides:** how the browser renders a server-driven surface, where the protocol library is allowed
  to be touched, and which UI rules are correctness requirements rather than styling.

## Question

The server sends `SurfaceUpdate` messages describing component instances. The browser has to turn
each one into a rendered control, send the patient's answer back, and stay honest about the state of
the workflow while doing it — including when the backend is down, when a payload is missing a field,
and when a booking request has been sent but not yet confirmed.

How does a Lit application do that without restating the workflow, and without inventing the
protocol?

## Context

**The client is a renderer, not a second workflow.** The states, the transitions, the ordering
(`hospitals → doctors → slots`), the legality of an action — all of that is the gateway's and the
database's. A browser that re-derives any of it produces a second opinion about what is true, and
between the two, the browser's is the one the patient sees first.

**The client is also the only thing the patient can see.** It cannot fall back on "ask the server" at
the moment the server is not answering. So the failure modes are not exceptions to the design; they
are the design. Three of them are load-bearing:

1. **The stream may say nothing.** Empty is a state to render, not a blank page.
2. **Research findings must survive a missing field.** Every rule about a package's shortcomings has
   to degrade to a readable screen, not a stack trace.
3. **A request is not a booking.** `REQUESTED` and `PENDING_REVIEW` are where the patient is most
   likely to be misled — a tick here is a factual claim about a hospital's behaviour, and a false one.

**The protocol library is a dependency with a lifecycle, and that lifecycle is not documented in one
place.** Most of the cost of this work was in that sentence. It is why the boundary is one file.

## What was checked

| # | Command | Result |
|---|---|---|
| 1 | `pnpm install` | `ERR_PNPM_IGNORED_BUILDS` for `esbuild@0.25.12` and `esbuild@0.28.2`; non-zero exit, esbuild works at runtime, and the fix belongs to the root workspace (see Consequences) |
| 2 | `pnpm --filter @referraleasy/web typecheck` | exit 0 (`tsc -p tsconfig.json --noEmit`, strict + `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes`) |
| 3 | `pnpm --filter @referraleasy/web test` | `Test Files 8 passed (8)`, `Tests 70 passed (70)` |
| 4 | `pnpm --filter @referraleasy/web build` | 378 modules; `dist/index.html` 3.28 kB, `dist/demo.html` 3.17 kB, `main-*.js` 224.42 kB (gzip 50.21), `adapter-*.js` 475.56 kB (gzip 129.73), `adapter-*.css` 6.90 kB |
| 5 | `docker build -f apps/web/Dockerfile -t referraleasy/web:dev .` | built; image `sha256:2e7d2150…`, runtime `nginx/1.27.5` |
| 6 | `docker run -p 3311:3000` + `curl -D-` | `/` → 200, `Cache-Control: no-store`; `/assets/main-*.js` → 200, `Cache-Control: public, max-age=31536000, immutable`; `/demo.html` → 200, 3171 bytes; `/whatever/deep/path` → 200 (app shell) |
| 7 | `curl http://127.0.0.1:3311/v1/workflows/health` (no gateway on the network) | **502, container still `running health=healthy`** — the design goal, verified rather than assumed |
| 8 | `pnpm ls @a2ui/web_core` in the lit package's own resolution | a single `0.13.0` instance, deduped and shared by `@a2ui/lit` and this application |

Findings 9–12 are the ones that changed the code.

## Decisions

### D1 — The surface is a component-instantiation problem, so the component model is Lit's

The renders are described by the server as an instance list, keyed by `instance_id`, replaced in
place on update. That is not a component tree to be reconciled; it is a set of independent custom
elements whose props change. Lit's model — a class with reactive properties, custom-element
registration, shadow DOM — is a direct match, and the eight components are ordinary elements that can
be tested in isolation under jsdom, which is how rule coverage in the table below is achieved.

The measured cost of the choice is in row 4: the protocol library is the largest part of the bundle
(475.56 kB raw, 129.73 kB gzip) and it is loaded once, not per component. The application's own code
is the smaller `main-*.js` chunk. Nothing here is React, and this ADR does not relitigate that.

### D2 — One module may import `@a2ui/*`, and a test enforces it

`src/a2ui/adapter.ts` is the only file that imports the protocol library. Everything else in the app
talks to `SurfaceRuntime`, `mountSurface` and a few types.

This is enforced, not documented: `test/adapter-boundary.test.ts` reads the source tree and fails,
naming the offending file, if any other module imports `@a2ui/*`. The same test asserts that every
specifier sits on a versioned entry point (`/v0_9/…`) rather than a bare package name, and that the
pinned versions in `package.json` still match the constants the adapter is written against — so the
test fails if the code and the dependency drift apart.

The reason this earns a test is D3 and D4. Both bugs were the library's lifecycle behaviour, both
produced identical symptoms (a blank page), and neither was findable by reading the application.

### D3 — An error from the protocol library is never swallowed

**Verified against the installed sources:** `@a2ui/lit` registers its surface element itself, at
import time, under the tag **`a2ui-surface`**. And a custom element constructor owns exactly one tag:
`customElements.define` throws `NotSupportedError` for a second one.

The first adapter defined its own tag, `re-a2ui-surface`, for the same class inside a `try/catch` that
logged nothing. The definition threw, the catch ate it, and `document.createElement('re-a2ui-surface')`
produced an inert unknown element with no shadow root. Every component rendered into nothing. **The
blank screen was caused entirely by the adapter's own defensive coding** — the failure was visible and
being hidden.

The adapter now uses upstream's tag, early-returns if it is already defined, and reports any
definition failure through `console.error`. The general rule this bought: in this boundary, an
unexpected error is data to display, never a condition to absorb.

### D4 — Component props come from `context.componentModel.properties`

**Verified against the installed sources:** `renderA2uiNode` has two render paths. The
`ComponentNode` path (a three-argument call) assigns `node` *and* `context`. The `ComponentContext`
path (a two-argument call) assigns **only `context`**. `A2uiSurface.render()` uses the context path.

So a component that reads `this.node.props` works everywhere except under the surface it is actually
rendered by — the failure mode is a component that is present in the DOM, styled, and empty. The base
class reads `context.componentModel.properties` first and falls back to the resolved `node.props`
value, and `instanceId` resolves the same way.

Also verified: `ComponentModel.properties` is the **raw payload**. Nothing coerces or defaults it, so
every field read is an `unknown` that the component has to narrow itself. That is why every component
is written against a missing-field state rather than assuming its payload.

### D5 — Two zod majors coexist through an alias that never leaves the adapter

`@a2ui/lit` and `@a2ui/web_core` 0.13.0 depend on `zod ^3.25.76`. `@referraleasy/contracts` uses
`zod ^4.0.5`. One `node_modules` cannot hold both under one name.

The resolution is an alias dependency in `apps/web/package.json`:

```json
"zod-v3": "npm:zod@^3.25.76"
```

imported only inside the adapter. The application and the contracts package keep zod 4. The boundary
test asserts the alias exists and stays in step with the adapter's constants, so the workaround cannot
quietly rot into a mystery.

**Not verified:** whether `web_core` actually tolerates zod 4 — the alias was chosen instead of
testing forward compatibility with a library that declares the older range.

### D6 — The honesty rules are behaviour, with tests naming each one

"Don't overstate what happened" is not a style guide; it is a set of assertions:

| Rule | Test |
|---|---|
| A blank screen is a failure — every phase renders something readable | `app-shell.test.ts` (10), `boot.test.ts` (3) |
| `REQUESTED` / `PENDING_REVIEW` never read as confirmed: no tick, no green, no "confirmed", no confirmation reference | `receipt-certainty.test.ts` (15) |
| A dropped stream is "we do not know", not a failure and not an outcome | `boot.test.ts`, `app-shell.test.ts` |
| `unknown` renders as unknown with its freshness — never smoothed into "no" | `demo-page.test.ts`, `receipt-certainty.test.ts` |
| One `command_id` per intent, minted once; controls disabled in flight; a double click is one submission | `command.test.ts` (10) |
| Surface updates are idempotent by `instance_id`; a replay changes nothing | `surface-store.test.ts` (16) |
| Missing fields degrade to a readable state | `render.test.ts` (7) |
| Nothing is ever preselected, and no doctor appears before a hospital | `render.test.ts` |
| All eight components render through the real pipeline | `render.test.ts`, `demo-page.test.ts` (4) |

Three of these were bugs found by writing the test, not by reading the code, and each was a case of
the *success* path telling the patient something false:

- A booking that succeeded carried reason code `ok` — a real member of `ReasonCode` meaning "nothing
  to explain" — and the fallback sentence for unhandled codes told the patient the step could not be
  completed. On a confirmed booking. `ok` now means silence.
- `BOOKING_UNVERIFIED` was rendered with the confirmed tone and a `✓`. It now renders `?`, and the
  readout comes from the shared classifier rather than from the local branch.
- `degraded: 'full'` — the healthy value — put every normal run into the degraded phase, because the
  code tested for absence of `'full'` instead of for a degraded value.

The lesson is the same in all three: the failure path is the one people write carefully, and the
success path is where the client lies.

### D7 — The client is offline-first, and so is its container

The client defaults to its own origin, under `/v1`, rather than to a baked-in absolute URL, and
`nginx.conf` proxies that prefix to `gateway:8080`.

The upstream is a **variable**, which makes nginx resolve it per request instead of at start-up. A
literal `proxy_pass http://gateway:8080` is resolved when the config loads, so nginx refuses to start
whenever the gateway is not on the network — running the image alone, or bringing up `web` without the
`core` profile — and the entire UI is dead for a reason unrelated to the UI. With the variable, a
missing gateway costs one 502 on an API call (row 7) while the app keeps being served.

This is the same rule as D6, applied one layer down: the screen renders, and says what it does and
does not know. SSE is proxied unbuffered with a one-hour read timeout for the same reason — a buffered
event stream is indistinguishable from a backend that has said nothing, which is precisely the state
the client must not confuse with "no answer".

### D8 — The gallery is not a second implementation

`demo.html` renders every component from static `SurfaceUpdate` fixtures through the same adapter and
the same store the real app uses, with no backend. It is a labelled gallery rather than a mock of the
app, because half its value is in showing negatives being handled — a request that must not look
confirmed, a coverage value that is unknown rather than "no", a payload with almost nothing in it.

Its `Apply every update twice` control is a live idempotency check: it re-applies every fixture and
reports the instance count before and after. If a surface update stopped being idempotent the count
would change and the page would say so.

The consequence is that the gallery and the product cannot disagree, because there is only one
renderer.

## Consequences

**Enforced, cheap to keep:** the adapter boundary test, the honesty tests, and the Dockerfile's
versioned entry points mean the failure modes above cannot return silently. Adding a component means
adding it to the catalog and to the fixture set; it does not mean touching the protocol layer.

**Accepted costs.** The protocol chunk is 475.56 kB raw (129.73 kB gzip) and dominates the bundle.
`@a2ui/lit` declares an **unmet peer dependency** on `@a2ui/markdown-it ^0.2.1`, which is not
installed: harmless while the catalog is ours, and a runtime failure the moment someone registers
upstream's built-in catalog. Both are recorded here rather than solved, because the client does not
need upstream's catalog and the bundle is loaded once.

**Outside this change, and it blocks a clean install:** `pnpm install` exits non-zero with
`ERR_PNPM_IGNORED_BUILDS` naming `esbuild@0.25.12` and `esbuild@0.28.2`. esbuild works without its
install script, so nothing this package does is affected, but the fix (`onlyBuiltDependencies` in the
root `pnpm-workspace.yaml` or `package.json`) is the repository root's file and is not changed here.
Until then, an install step that treats a non-zero exit as failure fails on install rather than on a
result.

**`v0_9` is the entry point that matters.** It is the line that added `createSurface` and custom
catalogs; `@a2ui/lit` also exports `0.8`/`v0_8`/`ui`, and importing through a bare package name would
give whichever of those the resolver picked. The boundary test requires the versioned path.

## Open questions / follow-ups

1. **No upstream test covers `@a2ui/lit` + `@a2ui/web_core` 0.13.0 as a compatible pair.** Each is
   tested in isolation (`custom-catalogs.test.js`, `a2ui-surface.test.js` in `@a2ui/lit`;
   `render-a2ui-node.test.js`, `register_universal_element.test.js`, `a2ui-controller.test.js` in
   `web_core`) and pnpm resolves one shared `web_core` instance for both (row 8). The pair works here
   because the tests exercise the real path — not because anything upstream asserts the combination.
   A minor bump should be treated as unverified until the suite says otherwise.
2. **Whether `MessageProcessor.getSurface()` returns a stable object identity across updates was not
   verified.** The store therefore keys on `instance_id` itself and never on object identity, so the
   question cannot become a bug. If a future change wants to hold a surface object for a long time,
   this needs answering first.
3. **The agent-side v0.9 producer is still unverified** (see ADR-0004). The client is written against
   the `sse$*` schemas in `packages/contracts` and validated at the boundary; whether the upstream
   producer emits exactly those shapes has not been demonstrated end to end, because no gateway
   produced them.
4. **Nothing here has been seen in a real browser.** Rendering is verified under jsdom — the demo
   page's four tests assert the components, their copy and the idempotency control — and the served
   HTML and assets were verified over HTTP (rows 6–7). Visual correctness, layout at real viewport
   sizes, and the behaviour of the SSE client against a real `EventSource` are all still unverified;
   jsdom has no `EventSource`, so the transport's happy path is not covered by a test at all.
