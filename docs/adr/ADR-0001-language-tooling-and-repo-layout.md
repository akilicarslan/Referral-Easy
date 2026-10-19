# ADR-0001 — Language, tooling and repository layout

- **Status:** Accepted
- **Date:** 2026-10-14
- **Phase:** 0.1
- **Supersedes:** —

## Context

`ReferralEasy_Architecture_v3.md` is the source of truth and is written in Turkish.
`IMPLEMENTATION_TODO.md` §0.1 requires `git init`, a monorepo layout "exactly as §22", a Python
toolchain for the gateway/agents/workers, and Node/TypeScript for `mock-api` and `apps/web`.

Two questions were explicitly left open by the plan and had to be settled before any interface
could be frozen:

1. Phase 0's spikes end by pinning a model ID after a **live** provider smoke test, but no provider
   credentials exist in this environment.
2. §"Open questions": whether the patient-facing UI and agents speak Turkish or English. The spec is
   Turkish and Scenario 2 is written as "Turkish + Friday + 15 miles"; the TODO is English and uses
   miles/ZIP/`America/Chicago`.

## Decisions

### D1 — English patient-facing text; English throughout

Patient-visible copy, agent responses to the patient, synthetic document fixtures and conversation
quotes are **English**. Scenario 2's "Turkish-speaking patient" becomes an ordinary preference filter
(a patient who requests a Turkish-speaking provider), not the demo's language.

All code, identifiers, comments, commit messages, ADRs and internal docs are English. The Turkish
architecture document stays at the repo root untouched as the source of truth.

*Rationale:* the portfolio is reviewed by non-Turkish readers, and the fixture setting
(miles, ZIP, `America/Chicago`) is already US-shaped. Keeping one language everywhere removes a
translation layer from evaluation datasets, prompt rubrics and agent instructions.

### D2 — Offline-first Phase 0, model pins deferred (not faked)

No provider credentials are available, so the model-selection spikes cannot complete. Rather than
invent model IDs — which §19 forbids and invariant `[!]` #13 forbids explicitly — the project runs on
the plan's own **offline-fixture profile** (§18):

- `MODEL_BACKEND` / `EMBEDDING_BACKEND` / `RERANK_BACKEND` are `offline | live` (default `offline`).
- The offline backends return canned, deterministic responses with **no network calls**.
- Every offline artefact is stamped `synthetic: true` in traces and labelled in the UI, so the
  "no fabricated demo charts" invariant (§12) cannot be violated by canned output.
- `scripts/validate_env.py` **fails loudly** if a `live` backend is selected while any `CHANGEME`
  pin remains. It never silently substitutes a default.
- Each deferred pin gets: a row in `docs/adr/` when resolved, and a runnable smoke test under
  `tests/contracts/` marked `@pytest.mark.spike` (excluded from CI until credentials exist).

**Library** versions *are* pinned now, by research rather than by live smoke test — see the S-series
spike ADRs. Those pins are `CANDIDATE` until a smoke test confirms them.

*Consequence:* Phase 0's exit criterion ("no `<pinned-…>` placeholder left in `.env.example`") is
**not met** and the phase stays open. This is recorded, not hidden.

### D3 — Toolchain

| Concern | Choice | Notes |
| --- | --- | --- |
| Python packages | `uv` workspace, Python 3.12 | ADK/A2A support on 3.14 is unconfirmed; 3.12 is the conservative pin. Local interpreter is 3.14 — `uv` fetches 3.12. |
| Node packages | `pnpm` workspace | Node 24 present locally; `engines` allows >=22. |
| Python lint/format | ruff | |
| Python types | mypy `strict` + `pydantic.mypy` plugin | |
| TS types | TypeScript `strict` | |
| Secrets gate | gitleaks pre-commit | §0.1 |

### D4 — Contract schemas are JSON Schema, authored by hand

`packages/contracts/schemas/*.json` (JSON Schema 2020-12) is the **single source** for every
cross-service type — §1.1 requires "single schema source → Pydantic (Python) + Zod/JSON Schema (TS)".

Python and TypeScript bindings are **generated** from it, never hand-maintained:

- Python → `packages/contracts/python/src/referraleasy_contracts/generated/`
- TypeScript → `packages/contracts/ts/src/generated/`

Round-trip contract tests in `tests/contracts/` assert that a value valid in Pydantic is valid in Zod
and vice versa, so drift is a test failure rather than a runtime surprise.

Version is carried by the schema `$id` (`.../schemas/v1/...`) and mirrored in `CONTRACT_VERSION`.

## Deviations from §22

§22 lists the repo layout exactly. Two directories were added and one is pending:

| Path | Why |
| --- | --- |
| `migrations/` | §7 requires PostgreSQL migrations for ten tables. §22 names no home for them; a root-level directory is where both Alembic and `psql`-based runners expect it. |
| `scripts/` | §0.1 requires the `Makefile` targets `demo`, `migrate`, `seed`, `index` to do real work. They delegate to scripts here. |
| `.github/workflows/` | §0.1 requires a CI skeleton. |

## Consequences

- The demo is runnable end to end with **zero credentials**, which is what a portfolio reviewer needs.
- Anything that would be a measurement (latency, cost, guardrail FP/FN, eval scores) is **not**
  produced by the offline profile — those metrics report "no data" until live credentials exist,
  rather than reporting canned numbers as results.
- Un-pinning a model later is a config + ADR change, not a code change, because every model call goes
  through the model factory in `packages/agent-runtime`.
