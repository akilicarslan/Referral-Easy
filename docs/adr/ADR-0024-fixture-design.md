# ADR-0024 — Demo fixtures are validated content, indexed by scenario

- **Status:** Accepted
- **Date:** 2026-10-14
- **Phase:** 1
- **Depends on:** [ADR-0012](ADR-0012-contract-codegen.md) (the frozen schemas and `validate_def`)

## Context

The demo must show eight scenarios end to end. Their data has to come from somewhere, and the
tempting somewhere is the workflow code: a name here, a plan list there, a slot that happens to be
free. That is how a demo becomes unreproducible — the data lives in six places, no one can say
whether the failure still reproduces, and a scenario quietly stops exercising what it claims
because a provider gained an availability slot in an unrelated edit.

So fixtures are **imported content**, not a second contract. They are validated *against*
`packages/contracts/schemas/*.json` and never described alongside it: no field exists in a fixture
file that the schema does not declare, and no schema was widened to make a fixture fit.

The second half of the problem is that "validates" and "reproduces the scenario" are different
properties. A fixture set can be perfectly schema-valid while the search that is supposed to fail
succeeds. Both are asserted, in one file, with a named test per scenario.

## Decisions

### D1 — One schema definition per file, at a path that mirrors the schemas

`fixtures/directory/locations.json` is an array of `directory.Location`; `fixtures/evidence.json` is
an array of `common.Evidence`. No envelopes, no wrapper objects, no mixed files. The mapping from
file to definition is one table in the test (`FIXTURE_SPECS`), so a file that no definition covers
fails the suite rather than escaping validation.

A file per definition means the whole directory is mechanically checkable: the test asserts every
`fixtures/**/*.json` except the manifest and the fault scenarios is registered, and every registered
definition exists in its schema's `$defs` — a typo in a definition name would otherwise make the
validation vacuous.

**296 records across 25 file-specs** validate today.

### D2 — Ids are deterministic, and identity is not always the id

Ids are hand-assigned UUIDv4-shaped values with a per-type prefix (`1000…` location, `2000…`
hospital, `3000…` provider, `4000…` slot, `5000…` evidence, `6000…` plan, `7000…` network record,
`8000…` coverage result, `9000…` referral requirement, `9500…` evidence snapshot, `a000…` document,
`b000…` patient, `c000…` patient details, `d000…` referral intake, `e000…` constraint, `f000…`
workflow, `f100…` handoff, `f200…` queue ticket). A fixture reference is therefore readable at the
call site, and a diff shows which entity changed.

Uniqueness is asserted on **identity**, not on the id column. A superseded `PatientPreferences` row
keeps its `workflow_id`, and a superseded `Constraint` keeps its `constraint_id` — that is what
versioning means. The test takes an explicit identity per spec (`(patient_id, version)` for
preferences) and scopes nested definitions to their parent row, instead of weakening the check for
everything.

### D3 — `fixtures/scenarios.json` is the index, and it is not a schema

The manifest names the eight scenarios, their expected availability result kinds, the fault files
they use, the data they reference by id, and — importantly — `non_fixture_dependencies`, so a
scenario whose reproduction also depends on a code path (the version regression depends on an agent
description change) says so instead of implying the data is sufficient.

It is the only JSON under `fixtures/` that is not validated against a schema, because no schema
defines a demo scenario and inventing one would put demo-only concepts in the contract. Its
structure is asserted directly: every scenario appears exactly once, every id it references resolves
inside the fixtures, every fault file it names exists and is labelled with the same scenario id, and
every expectation it declares is present in the data.

### D4 — The acceptance criterion is reproduction, not existence

Each of the eight scenarios has a test named after it
(`test_scenario_5_timeout_after_commit_is_supported`, …) with assertion messages that name the
scenario, so a failure says which scenario stopped working. The invariants are the point:

- **Scenario 2** — no provider satisfies Spanish + Friday + within 15 miles + accepting new
  patients. The test *computes* the distances (haversine from `search_origin` to the hospital's
  `Location.geo`) rather than trusting the `distance_miles` a fixture happens to state, and it
  asserts the remaining constraints are each defeated by a *different* provider, so no single
  relaxation unlocks the search.
- **Scenario 3** — the contradiction is date-explainable (a superseded record ending 2026-06-30 and
  its replacement starting 2026-07-01) and authoritative-source-based, not merely contradictory.
  The hospital's prose blurb names a plan that the structured `accepted_plan_ids` omits.
- **Scenario 5** — the loss happens *after* the commit (30 s delay on the first POST, occurrence 1)
  and the committed/uncommitted pair is distinguishable, so "unknown" and "never happened" cannot be
  conflated.
- **Scenario 6** — `upstream_error` (authoritative: false, partial: true) exists *beside*
  `no_slots_for_constraints`, because a set that only has the empty result cannot tell a failed call
  from an empty one.
- **Scenario 8** — pinned inputs and `knowledge_version` pins, plus a declared non-fixture
  dependency, because a "regression" whose inputs drift is not a regression.

### D5 — The prompt-injection fixture is a first-class fixture, flagged and contradicted

One document (`a000…0007`) carries `contains_injection_attempt: true` and text that addresses an
assistant directly, claims the plan is in-network, and asks for the coverage check to be skipped.
Exactly one document is flagged, the flag is asserted, and the authored phrase is asserted, so the
guardrail demo cannot silently lose its input.

Critically, the *contradicting* data is also asserted: the coverage results for that patient say
`unknown`, and the authoritative evidence outranks the stale claim. An injection fixture with
nothing to be wrong against would demonstrate nothing. Every document also opens with a
`SYNTHETIC FIXTURE` marker, since these files look like medical records.

### D6 — Fault scenarios are one file per fault type, seeded

`fixtures/fault-scenarios/*.json` — 11 files, one per fault type, in the shape the mock API consumes
(`scenario_id`, `fault_seed`, `default_fault`, `steps[]` of `{match, occurrence, fault}`). The test
asserts the file stem matches the declared fault, that every declared fault type is inside the
closed set of 11, that every type is reachable, that `fault_seed` is an integer (a faulted run must
be replayable), and that each file is labelled with one of the eight scenarios.

One file per type rather than one per scenario: a scenario such as "three transient errors" composes
three files, and a new fault is one file plus one enum member, not an edit to four scenarios.

### D7 — Schema gaps are recorded, not worked around

Writing fixtures against a frozen schema surfaced four places where the contract cannot express what
the demo needs. None were worked around by adding fields, and none were widened:

1. **`ProviderCandidate.constraint_fit[].satisfied` may be `null`** (unknown). Used deliberately:
   asserting `true` for an unverified constraint would make the candidate list lie.
2. **`AlternativeOption.relaxes_constraint_id` must be `null` for hard constraints**, so an option
   that addresses a hard constraint cannot name the constraint it concerns; the linkage lives only
   in the option's `description`. The test asserts no option relaxes a hard constraint (all six are
   `null`), which is the property the workflow depends on, but the *tie* between option and
   constraint is prose and therefore not machine-checked. If §7 grows a need to group options by
   constraint, that is a schema change, and this is the record of why.
3. **`AvailabilityQuery` requires both `hospital_id` and `provider_id`**, so "no specialist exists
   anywhere" cannot be expressed as an `AvailabilityResult`. Scenario 7 is carried by the provider
   set (no provider lists the requested specialty, asserted) plus a `Handoff` with `reason_class:
   no_match` and the search evidence whose `source_id` is
   `search:specialty=functional_neurosurgery`.
4. **`NetworkRecord.provider_id` is required**, so a hospital-level coverage conflict cannot be a
   network record. Scenario 3's hospital-level case is expressed through
   `CoverageResult.conflicting_evidence_ids` and `winning_evidence_id`.

Also worth recording: `validate_def()` passes no `format_checker`, so `format: date-time`, `uuid`
and `date` are *not* enforced by the contract test. Fixture values are well-formed anyway, but the
schemas currently promise nothing about format.

## Consequences

- Fixtures are green: `94 passed, 2 skipped` in `tests/contracts/test_fixture_conformance.py`
  (the two skips are scenarios with no availability dependency — asserted as an explicit skip rather
  than an empty check). **296 records** validated, **834 references** resolved, all 11 fault types
  present, all eight scenarios asserted.
- The referential-integrity check is explicit: `REFERENCE_FIELDS` maps 23 field names to the fixture
  key their values must resolve to. Guessing from `_id` suffixes would have skipped the fields that
  matter (`invalidated_by`, `provenance`, `winning_evidence_id`), so the mapping is written out.
- Adding a scenario means adding a manifest entry and a named test; adding data means adding to an
  existing file. Nothing about the eight scenarios lives in application code.
- The dependency is one-directional: fixtures are read by tests and by the demo's import path, and
  no production module reads a fixture path directly.
