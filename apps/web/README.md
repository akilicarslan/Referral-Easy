# `@referraleasy/web` — the patient referral client

The ReferralEasy web client: a Vite + Lit + TypeScript single-page app that renders the
patient referral flow from the gateway's SSE stream.

The server decides what to show. It sends `SurfaceUpdate` messages containing component
instances (`HospitalPicker`, `SlotPicker`, …); this client turns each instance into a Lit
element and drops its patient action back onto the stream. The client holds presentation
state and one important rule, not the workflow.

> **The rule the whole app is built around: a blank screen is a failure.**
> Every phase — no data, no backend, a dropped stream, a payload with a missing field —
> has to produce something a patient can read and act on.

---

## Running it

Prerequisites: **Node >= 22** and **pnpm**. From the repository root:

```sh
pnpm install
pnpm --filter @referraleasy/web dev        # http://localhost:3000
```

The dev server is pinned to port **3000** with `strictPort`, so it fails loudly instead of
silently moving to 3001 and breaking whatever URL you had.

| Command | What it does |
| --- | --- |
| `pnpm --filter @referraleasy/web dev` | Vite dev server on port 3000 (HMR). |
| `pnpm --filter @referraleasy/web test` | Vitest, jsdom, single run. |
| `pnpm --filter @referraleasy/web typecheck` | `tsc --noEmit`, strict. |
| `pnpm --filter @referraleasy/web build` | Production bundle into `dist/`. |
| `pnpm --filter @referraleasy/web preview` | Serve the built bundle on 3000. |

### Three pages

- **`/index.html`** — the real client. It reads the workflow from the URL:
  `http://localhost:3000/?workflow_id=<uuid>`. Open it without that parameter to create a fresh referral using a sample patient, or expand the existing-referral form to enter a workflow ID and its matching patient reference. The creation form uses the demo-only admin seed route, then the patient session and initial message routes; a failed initial dispatch retains the workflow and command for retry.
  The demo session is kept in tab-local session storage and authenticated requests load the current
  snapshot before the stream opens.
- **`/demo.html`** — the component gallery. Every one of the eight components rendered from
  static `SurfaceUpdate` fixtures, with **no backend and no network**. This is the page to
  open first: it is what makes the UI reviewable without a running stack.

- **`/admin.html`** — the operations dashboard, reading the gateway’s admin API. Responsive cards, tables and outcome bars share the patient app’s design tokens.

The gallery is deliberately a labelled gallery rather than a replica of the app, because
several of its scenes exist to show a *negative* being handled — a booking request that
must not read as confirmed, a coverage value that is unknown rather than "no". Those are
only reviewable if the reviewer is told what to look for. Filter buttons group provider choices, review and outcomes, and recovery states without unmounting the components. Local activity is bounded to the 20 most recent entries. Each scene gets its own surface
id so the components cannot interfere with one another, and every scene is mounted through
the same adapter the real app uses, so **the gallery renders the product, not a second
implementation of it**.

The `Apply every update twice` button is an idempotency demonstration, not a reload: it
re-applies every fixture to the live surfaces and reports the instance count before and
after. If a surface update were not idempotent, the count would change and the log would
say so.

### Where the gateway URL comes from

```sh
VITE_GATEWAY_URL=http://localhost:8080 pnpm --filter @referraleasy/web dev
```

Unset — the default — the client talks to its own origin, under `/v1`. That is the right
default in both places it runs: Vite's dev server proxies it, and the container puts
nginx in front of the same origin. `VITE_GATEWAY_URL` is a **build-time** value (Vite
inlines it), so it must be set when building, not when serving — see the Docker section.

---

## Deploying it

`apps/web/Dockerfile` is a two-stage build: Vite compiles the bundle, then an nginx image
serves the static output. No Node process runs in the runtime image.

**The build context is the repository root** — compose declares that (`compose.yaml`, the
`web` service) and it has to be, because the install is driven by the root lockfile and the
client bundles `@referraleasy/contracts`, a second workspace member:

```sh
docker build -f apps/web/Dockerfile -t referraleasy/web:dev .
docker run --rm -p 3000:3000 referraleasy/web:dev
```

`docker compose --profile showcase up web` builds and runs it with the gateway reachable
under the service name `gateway`.

Two details in `nginx.conf` worth knowing before changing it:

- **The container listens on 3000, not 80.** Compose publishes `3000:3000` and probes
  `http://127.0.0.1:3000/`, so the image keeps that contract rather than publishing a
  different port and needing the compose file edited to match. The probe is written
  `curl`-shaped, and no nginx image ships curl, so the runtime stage installs it: the
  health check has to be answerable by the thing it checks.
- **`/v1/*` is proxied to `gateway:8080` through a *variable* upstream.** A literal
  `proxy_pass http://gateway:8080` is resolved when nginx loads its config, so the whole
  image would refuse to start whenever the gateway is not on the network — running this
  image on its own, or bringing up `web` without the `core` profile. With the variable the
  resolution is per request, so a missing gateway costs one 502 on an API call instead of
  the entire UI. The container serves the app either way; that is the same rule the client
  follows internally.

SSE needs the stream not to be buffered (`proxy_buffering off`, a one-hour read timeout),
because a buffered event stream is indistinguishable from a backend that has said nothing
— exactly the state this client is forbidden to confuse with "no answer".

---

## Layout

```
src/
  a2ui/adapter.ts          the ONLY module that imports @a2ui/*        ← the boundary
  app/referral-app.ts      the shell: phases, banners, diagnostics, outcome page
  components/              the eight components (+ internal/ shared bits)
  fixtures/surfaces.ts     static SurfaceUpdate fixtures for the gallery and tests
  stream/sse-client.ts     EventSource wrapper: connect, track seq, replay, reattach
  stream/surface-store.ts  what we know: instances by id, actions, status, diagnostics
  stream/command.ts        one command_id per intent, released on workflow_state_version
  styles/                  design tokens and base styles
```

### The adapter boundary

**Nothing outside `src/a2ui/adapter.ts` may import `@a2ui/*`.** The application talks to
`SurfaceRuntime`, `mountSurface` and a handful of types; the protocol library stays behind
that line. `test/adapter-boundary.test.ts` enforces this by reading the source tree — if a
future change imports `@a2ui/web_core` from a component, the suite fails and names the
file. Every specifier is also required to sit on a versioned entry point (`/v0_9/...`),
and the pinned versions are asserted against the constants in the adapter, so the test
fails if the dependency and the code disagree.

The reason this is worth a test rather than a convention is in
[`docs/adr/ADR-0027-web-client.md`](../../docs/adr/ADR-0027-web-client.md): two of the bugs
found while building this were the protocol library's own lifecycle behaviour (a
self-registered custom element tag, a render path that assigns `context` and not `node`),
and both were invisible until the boundary was small enough to read.

### The eight components

`HospitalPicker`, `DoctorPicker`, `SlotPicker`, `PatientDetailsForm`, `BookingReview`,
`AppointmentReceipt`, `DirectoryFallbackCard`, `HumanHandoffCard`.

Coverage, preference and readiness are **section and field variants of these eight**, never
new components — a coverage panel is a section of `HospitalPicker`, a preference is a field
in `PatientDetailsForm`. The order `hospitals → doctors → slots` is fixed and enforced:
`DoctorPicker` renders a guard instead of a list when no hospital has been chosen, and no
slot is ever preselected.

---

## Rules the UI follows

These are correctness requirements, not styling preferences. Each is covered by a test.

| Rule | Where it is enforced |
| --- | --- |
| A blank screen is a failure | `app-shell.test.ts`, `boot.test.ts` |
| `REQUESTED` / `PENDING_REVIEW` never look confirmed (no tick, no green, no "confirmed", no confirmation reference) | `receipt-certainty.test.ts` |
| A 2xx POST is not a confirmation — only a terminal event is an outcome | `main.ts` (`sendAction`), `app-shell.test.ts` |
| A dropped stream is "we do not know", not a result | `app-shell.test.ts` |
| `unknown` renders as unknown, with freshness — never smoothed into "no" | `receipt-certainty.test.ts`, `demo-page.test.ts` |
| Every count and chart carries its sample size | components; `demo-page.test.ts` |
| Fixture/synthetic data is labelled | components; `demo-page.test.ts` |
| Missing fields degrade to a readable state, never to nothing | `render.test.ts`, `surface-store.test.ts` |
| One `command_id` per intent; controls disabled while in flight | `command.test.ts` |
| Surface updates are idempotent by `instance_id` | `surface-store.test.ts`, `demo-page.test.ts` |
| Nothing preselected anywhere | `render.test.ts` |

Accessibility is part of the same list: every control has a label, focus is visible, the
tab order follows the visual order, streamed updates are announced through `aria-live`
regions, and status is never carried by colour alone — each semantic state pairs its colour
with a glyph and a word.

---

## Known issues

The workspace approves esbuild in its root `pnpm-workspace.yaml`.

**Rollup warns about zod 4.6.7's `@__PURE__` comments** during `build` (`v4/core/util.js`,
`v4/core/regexes.js`). The annotations sit where Rollup cannot read them, so it drops the
comments. The build succeeds; nothing in this package can change it.

## Current verification

See [the architecture review](../../docs/architecture/review-2026-10-18.md) for the repaired browser and
gateway connections, repeated end-to-end scenarios, exact checks and remaining orchestration gaps.
Set `GATEWAY_PROXY_TARGET` when the local gateway runs on a port other than 8080. The review instance
uses 3001 for Vite and 18080 for the gateway.
