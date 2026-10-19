# ADR-0012 — Contract bindings are generated from JSON Schema

- **Status:** Accepted
- **Date:** 2026-10-14
- **Phase:** 1.1
- **Depends on:** [ADR-0001](ADR-0001-language-tooling-and-repo-layout.md) D4

## Context

§1.1 requires a **single schema source** producing both Pydantic (Python) and Zod/JSON Schema (TS)
bindings. Two hand-maintained copies would drift, and the drift would surface as a runtime type
error between the gateway and an agent rather than as a test failure.

The source is `packages/contracts/schemas/*.json` — 8 hand-authored JSON Schema (draft 2020-12)
files, 96 `$defs`, all cross-file `$ref`s verified to resolve.

## Decisions

### D1 — Python bindings are generated, never hand-written

`datamodel-code-generator` (0.83.0) produces Pydantic v2 models into
`packages/contracts/python/src/referraleasy_contracts/generated/`. Current output: **102 models,
44 enums**. Hand-written modules are limited to `schemas`, `a2a` and `constraints` — each with a
stated reason to exist.

### D2 — `--use-title-as-name` is load-bearing

Without it the generator names inline enums after the *field*, so two different `status` fields in
one file become `Status` and `Status1`, and `AgentError.class` becomes `Class`. Those names read as
interchangeable when they mean different things — confusing `ReconciliationStatus` with
`OutboxStatus` would be a real bug.

Two changes in the schemas make the flag work:

- Every inline enum carries a `title`. All 44 enums now have a meaningful name.
- Named enums that were duplicated inline now `$ref` the shared definition — notably
  `NetworkRecord.status` now refs `common.CoverageStatus` instead of restating the same three
  values, which had produced two distinct Python enums for one concept.

### D3 — Drift is a test failure, not a convention

`packages/contracts/generate.sh --check` regenerates into a temp directory and diffs against the
committed output. CI runs it. Changing a schema without regenerating fails the build.

### D4 — Two divergences between JSON Schema and Pydantic are *recorded*, not hidden

`datamodel-code-generator` cannot translate everything, and between the two layers the JSON Schema
is the contract (§1.1), so the schema is never weakened to make the layers agree:

1. **`allOf` + `if`/`then` is not translated.** `ResultEnvelope` forbids an error outcome with no
   `error` object; the generated model alone accepts it. Compensated by
   `constraints.validate_result_envelope`, with a test asserting *both* that the schema rejects the
   instance and that the compensating check also rejects it.
2. **Pydantic coerces; JSON Schema does not.** `"0"` validates as an integer field through the
   model but not through the schema. **Consequence for callers: validate untrusted input with
   `validate_def()`, not by constructing a model.** Model construction is a convenience, not the
   contract.

Both divergences have named tests in `tests/contracts/test_schema_bindings.py` so they are
discovered by reading the suite rather than by debugging production.

### D5 — The A2A adapter is hand-written, deliberately

`a2a.py` implements the mapping table declared in the `$comment` on `A2ATaskState`. It is not
generated because it encodes a rule the generator cannot express: **the protocol state may never be
read without the application outcome beside it.**

`outcome_unknown` projects to `failed` — terminal at the protocol level — while a booking may exist
and must be reconciled with the same idempotency key (§7). A caller reading only the protocol state
concludes "no booking exists" and may double-book. So the only way to obtain an `A2ATaskState` is
`AgentTaskResult`, which carries the outcome alongside and exposes `requires_reconciliation`.

## Bugs this work found in the schemas

- **Five schemas placed `$comment` inside a `properties` object.** Property values must be schemas,
  and a string is not one, so the schemas were invalid and every generator failed on them. Fixed by
  moving each comment up to the object level. A test now rejects any reserved keyword used as a name
  inside `properties`/`$defs`.
- **`coverage.json` restated `CoverageStatus` inline**, producing a second enum class for one
  concept. Now a `$ref`.
- **Three enums encoded `null` as a member value** (`"enum": [..., null]`), which generated
  `NoneType_None = None`. All three fields are already optional, so the `null` member was redundant;
  the schema now uses a plain string enum and nullability comes from `required`.

### D6 — TypeScript: every `$ref` is dereferenced before generation

`json-schema-to-zod` (2.6.1) **does not resolve `$ref` at all.** Verified directly: given a `$ref`
to a sibling `$defs` entry, a root `$ref`, or a `definitions` map supplied alongside, it emits
`z.any()`. A Zod schema full of `z.any()` type-checks, runs, and validates **nothing** — the failure
is a boundary that silently accepts anything, invisible until something invalid crosses it.

So `gen-zod.mjs` dereferences every `$ref` before handing anything to the generator, merging sibling
keywords (`{"$ref": ..., "description": ...}`) rather than dropping them, and raising a clear error
on a cycle rather than emitting a silent `z.any()`. `tests/generated.test.ts` asserts the generated
output contains no `z.any()`, and a second test proves a cross-file ref became a real constraint by
rejecting a non-uuid `workflow_id`.

### D7 — TypeScript: exports are namespaced, definitions are emitted once

Definition names are only unique *within* a file — `AdapterType` exists in both `booking.json` and
`directory.json` meaning different things. Exports are therefore `origin$Name`
(`booking$BookingRequest`), and each definition is emitted exactly once in its home module so that
`export *` across eight modules is unambiguous.

### D8 — `z.any()` is rewritten to `z.unknown()`

The generator emits `z.any()` for `oneOf` branches, paired with a genuine `superRefine`, so runtime
validation there is correct. But `z.infer` of `z.any()` is `any`, which switches off type-checking
for every field that touches it. The two are value-identical; `z.unknown()` keeps the inferred type
honest and forces callers to narrow. Verified: a malformed timestamp **is** rejected, so the
`superRefine` is real validation and not a passthrough.

## Consequences

- Both halves of §1.1 are now real. `pnpm gen:contracts` regenerates **102 Pydantic models / 44
  enums** in Python and **96 Zod schemas** in TypeScript from the same 8 schema files.
- `generate.sh --check` diffs *both* languages against a fresh generation, so schema/binding drift
  fails CI in either direction.
- Green: 141 Python contract tests, 33 TypeScript contract tests, mypy strict, ruff, tsc strict.
- **Recorded cost:** the TypeScript and Python surfaces are structurally parallel but separately
  implemented — a rule expressed in one adapter (e.g. the A2A projection) is not automatically
  expressed in the other. `tests/contracts/test_a2a_adapter.py` and `ts/tests/a2a.test.ts` assert
  the same table from both sides; they are duplicated on purpose, because that duplication is what
  catches divergence.
- Fixture conformance runs with **format checking enabled** (`format: uuid` / `date-time` / `date`
  are enforced, not annotations), so the 296 fixture records are validated structurally *and*
  format-wise. `validate_def(..., check_formats=False)` remains available for structural-only use.
