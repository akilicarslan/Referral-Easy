# ADR-0004 — A2UI renderer: `@a2ui/web_core@0.13.0`, protocol entry point `v0_9`

- **Status:** Accepted (renderer package and version pinned). Custom-catalog *authoring
  mechanics* remain **UNVERIFIED**.
- **Date:** 2026-10-14
- **Phase:** 0 (spike S-04)
- **Decides:** the A2UI renderer package and version, the protocol entry point to target,
  and whether a custom component catalog is available.

## Question

Which A2UI renderer/adapter version does the demo use, and does it support a custom
component catalog?

## What was checked

| # | Command / URL | Result |
|---|---|---|
| 1 | `npm view @a2ui/web_core version dist-tags` | `0.13.0` (`latest`) |
| 2 | `npm view @a2ui/web_core@0.13.0 exports --json` | subpath entry points `./v0_8`, `./v0_9`, `./v1_0`, `./rpc`, `./state`, `./data/*`, `./errors`, `./events`, `./semver` |
| 3 | `npm view @a2ui/web_core@0.13.0 dependencies` | `lit ^3.3.3`, `zod ^3.25.76`, `@lit/context ^1.1.6`, `redos-detector ^6.1.4`, `validate-color ^2.2.4`, `zod-to-json-schema ^3.25.2`, `@preact/signals-core ^1.14.2` |
| 4 | `npm search @a2ui --json` | `@a2ui/web_core 0.13.0`, `@a2ui/lit 0.13.0`, `@a2ui/react 0.13.0`, `@a2ui/angular 0.12.0` |
| 5 | `https://a2ui.org/guides/renderer-development/` | protocol versions: **v0.8 stable**, **v0.9 / v0.9.1 current**, **v1.0 candidate**; "custom catalogs" attributed to the `v0_9` export set |

## Decision

- **Pin `@a2ui/web_core@0.13.0`.** Only `latest` is published as a dist-tag; the version is
  exact-pinned in `package.json` rather than caret-ranged, because the package is
  **protocol-versioned** — a minor bump may move which wire protocol is `current`.
- **Target the `@a2ui/web_core/v0_9` entry point.** Rationale:
  - `v0_8` is *stable* but predates custom catalogs.
  - `v0_9`/`v0_9.1` is the **current** protocol and is the only line documented as adding
    "`createSurface`, **custom catalogs**, client-side functions".
  - `v1_0` is explicitly a **candidate** — not a base to build a demo on.
- **A framework renderer is a separate, optional layer.** `web_core` is Lit-based and
  framework-agnostic; the framework bindings (`@a2ui/react`, `@a2ui/lit`,
  `@a2ui/angular`) are distribution channels over the same core. `apps/web` does not exist
  yet, so **the framework renderer is deliberately not pinned here** — when the web app
  picks its framework, it adds the matching `@a2ui/*` renderer at `0.13.0`, and this ADR is
  amended with that one line. `web_core` is the invariant.
- **Architectural rule that follows from the pin:** nothing outside a single adapter module
  imports `@a2ui/*` directly. The A2UI protocol version is a wire detail; keeping it behind
  one boundary is what makes the v0.9 → v1.0 migration a one-file change.

### Custom component catalog

**Supported.** The v0.9 checklist requires binding `catalogId` when handling
`createSurface` (alongside `theme` and `sendDataModel`), and registered
catalog-defined functions participate in client-side function evaluation.

## What remains unverified

1. **The custom-catalog authoring schema.** The renderer-development guide states *that*
   catalogs are pluggable and links to a separate "Defining Your Own Catalog" page, which
   was **not read**. The shape of a catalog definition (component map, prop schemas, how
   `catalogId` is matched) is therefore unverified — do not author a catalog from this ADR.
2. **Version skew inside the `@a2ui/*` family.** `@a2ui/angular` is `0.12.0` while
   `web_core`, `react` and `lit` are `0.13.0`. Equality of the numbers I did verify is not
   evidence that a renderer and a core version are tested together.
3. **The agent-side producer.** No Python/npm package that *emits* A2UI under the v0.9
   protocol was verified to exist. The plan of record is that agents emit A2UI JSON as an
   A2A data part built against the v0.9 message schema; that hypothesis is unconfirmed and
   is part of S-04.
4. **No runtime smoke test is possible offline.** There is no credential-gated network call
   here, so a "spike" cannot be a provider call. S-04 is instead a *contract* test:
   import the pinned `v0_9` entry point, feed it a recorded `createSurface` message and
   assert the surface is constructed — which fails loudly if the entry point moves.

## Consequences

- Because the renderer is pinned exactly and imported in one place, a protocol bump is a
  reviewable, single-boundary change rather than a repo-wide hunt.
- The demo can state honestly that it targets **A2UI v0.9.1**, not "A2UI" unversioned.
