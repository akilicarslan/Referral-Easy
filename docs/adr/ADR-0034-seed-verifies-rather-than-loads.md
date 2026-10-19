# ADR-0034 — `make seed` verifies the seed state; it does not load anything

- **Status:** Accepted
- **Date:** 2026-10-15
- **Phase:** Phase 0 / the demo flow
- **Depends on:** ADR-0022 (the PostgreSQL schema), ADR-0023 (compose topology), ADR-0024 (fixture
  design), ADR-0025 (aligning the domain vocabulary)

## Context

`make seed` was a Phase 0 stub that listed `fixtures/` and exited 3, and the CI job that guards the
stubs (`phase0-stubs`) forces the question `make seed` was always going to ask: *what belongs in
this script?* The stub's premise — that there is a dataset sitting somewhere that needs loading into
something — was never checked. It was an assumption inherited from the name.

Two loaders already exist, and neither is `make seed`:

1. **The migrations own PostgreSQL.** `migrations/003` and `005` create and populate the reference
   tables; `014_align_domain_vocabulary.sql` re-derives `workflow_state`, `workflow_transition`,
   `agent_role`, `tool_registry` and `policy_rule` from the frozen contract
   (`packages/contracts/schemas/common.json#/$defs/WorkflowState`) and prunes what no longer belongs.
2. **The mock hospital API owns the external systems.** `services/mock-api` reads `fixtures/` at
   startup (`src/app.ts` → `domain/seeding.ts:seedStore` → `domain/fixture-seed.ts:seedFromFixtures`)
   and holds the corpus in its store; there is no `hospital`, `slot`, `plan`, `patient` or
   `coverage_result` table in PostgreSQL, deliberately, because those are systems the demo does not
   own (ADR-0022, ADR-0024).

What is left for `make seed` is therefore not a third copy of the data. It is the thing the two
loaders cannot do for themselves: **say whether they are actually in the state everything
downstream assumes.** The distinction matters because every failure mode here is silent. A database
one `policy_rule` row short still answers every query — with a default-deny at the policy gate. A
mock API started with `MOCK_API_SEED=builtin` serves a complete, self-consistent dataset that shares
no id with `fixtures/scenarios.json`, so the demo runs end to end while every scenario names a
hospital that does not exist. Neither failure raises an error anywhere. The demo's numbers are
computed from this data, which is why a wrong dataset is worse than a missing one.

## Decision

**`make seed` writes nothing, and that is the finding.** It reads each dataset's source, reads the
state produced from that source, and fails when the two disagree. It is a verifier.

### The verdict is derived, not restated

Nothing in the script is a hardcoded expectation, because a hardcoded expectation is a second copy
of the data that can drift from the first — the same thing ADR-0024 refuses for fixtures. The
reference-table counts come from parsing the `INSERT … VALUES` blocks of
`014_align_domain_vocabulary.sql`; the contract comparison comes from loading
`common.json#/$defs/WorkflowState` through `referraleasy_contracts.schemas.load_schema`; the
migration ledger is compared against the sha256 of each file's UTF-8 text, computed the way
`migrations/run.py` computes the checksum it stored; the fixture counts come from the fixture files.
The script prints what it derived before it checks anything, so a wrong expectation is visible rather
than buried — and `tests/scripts/test_seed_database.py` asserts the parse and PostgreSQL's own
reading of the same migration agree, which is what makes "derived" a claim rather than a hope.

### The ten checks

| Section | Check | Fails when |
| --- | --- | --- |
| PostgreSQL | `database reachable` | `DATABASE_URL` is unset or nothing answers |
| PostgreSQL | `migrations applied` | a migration on disk is unapplied, or a file changed after being applied |
| PostgreSQL | `workflow_state == contract` | the vocabulary and the frozen contract are not the same set of codes |
| PostgreSQL | `reference row counts` | a table does not have the rows its migration declares (16 / 74 / 9 / 22 / 47) |
| PostgreSQL | `state machine is whole` | a state has no outgoing transition, or is unreachable from `INTAKE` |
| Mock API | `mock API reachable` | the service is not answering — the corpus is loaded at boot, so a service that is down is a corpus that is not loaded |
| Mock API | `mock API serves the fixtures` | `seed.mode` is not `fixtures`, or `seed.source_directory` is not this repo's `fixtures/` |
| Mock API | `seed report == fixtures` | any of the ten counts the loader reports disagrees with the file it read |
| Mock API | `store == fixtures` | the store's id set is not the fixture file's id set, in either direction |
| Mock API | `corpus coherent` | the service reports a fixture load error, or a scenario manifest that is incomplete or has unreferenced files |

The store comparison is the one the loader's own report cannot make. `seedFromFixtures` returns the
counts it *read*, and it writes slots with `INSERT OR IGNORE` so a restart cannot resurrect a slot a
live booking holds. A slot deleted from the fixtures therefore stays in the store, and a store one
record short is a demo whose numbers are wrong with nothing detecting it. The comparison also runs in
the other direction: a record the fixtures no longer describe is drift too.

`GET /documents` refuses an unfiltered request, so the ids are grouped by the `patient_id` values the
fixture file itself contains rather than by a list in the script.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | every dataset is loaded and matches the source it is derived from |
| 1 | the environment is not runnable — reported by `scripts/validate_env.py`, which runs first because no dataset state means anything in an environment that cannot start |
| 2 | the environment is fine, but a dataset is missing, partial or does not match its source — including the case where a service is not running |

This is `scripts/health.py`'s vocabulary (0 fine, 1 invalid environment, 2 not ready) — the same
three states, so `make seed` and `make health` can be read the same way. 3 means "still a stub" and no
longer applies: the script checks something real now.

### Where the data actually comes from

| Data | Source of truth | Loaded by | `make seed` checks |
| --- | --- | --- | --- |
| `workflow_state` (16), `workflow_transition` (74), `agent_role` (9), `tool_registry` (22), `policy_rule` (47) | `migrations/014_align_domain_vocabulary.sql` | `make migrate` | counts, contract equality, state-machine wholeness, ledger checksums |
| `directory/{locations,hospitals,providers,slots}.json`, `coverage/{plans,network_records,referral_requirements,evidence_snapshots}.json`, `documents/documents.json`, `evidence.json`, `patients/patient_details.json` | `fixtures/` | `services/mock-api` at startup | reported count vs file, store id set vs file id set |
| `scenarios.json`, `fault-scenarios/*`, the `patients/**` and `workflow/**` contract fixtures, `coverage/coverage_results.json`, `directory/{availability_results,provider_candidates}.json` | `fixtures/` | nobody — see Gaps | the service's coherence report (manifest completeness, unreferenced files) |

## Consequences

* **`make seed` cannot damage a running stack.** It opens one connection with `autocommit=True` and
  issues only `SELECT`s. There is no `TRUNCATE … CASCADE`, no delete, and no flag to enable one:
  the destructive path a loader needs does not exist here, which is why the script's docstring does
  not have to explain what a second run does to the first run's rows.
* **It is safe to run twice, and the test proves it rather than asserting it.**
  `tests/scripts/test_seed_idempotent.py` counts every base table in the public schema before a run,
  after it, and after a second run, and requires all three to be equal — every table, not a chosen
  list, because a list is where the one table that changed would hide. It also requires both runs to
  exit 0 and to print identical output, so a check that quietly stopped running cannot pass by
  changing nothing.
* **A failure names the fix.** "`make seed` failed" is not actionable at 2am; `make migrate`,
  `make up` and "run `make migrate` first" are. An unmigrated database produces that message rather
  than a `relation "schema_migrations" does not exist` traceback, which is a state a fresh clone
  meets on its first command.
* **The check can be wrong in a way that is reported, not swallowed.** Anything the checks raise —
  a missing fixture file, a record with no id, a duplicated id, an unparseable `VALUES` block — is a
  failure with the reason attached. A check that cannot compute its expectation must not fall through
  to "pass", so `_count_values_rows` raises rather than returning 0, and `fixture_ids` refuses a
  record with no id instead of comparing a smaller set.
* **`tests/scripts` splits, and the split is required by CI.** The mock-API checks are pure functions
  over a URL and a payload, so they run hermetically against a stub that serves the real fixture files
  — no service, no database, no `integration` marker. The database checks are marked `integration`
  and gate on `TEST_DATABASE_URL` exactly as `tests/domain/conftest.py` does. That is not cosmetic:
  CI's integration job provides PostgreSQL and nothing else, and it fails the run if any integration
  test skips, so an integration test that needed the mock API service could never legitimately run
  there.
* **The stub proves the check, not the service.** `tests/scripts` asserts the comparison is right
  against fixture content; that the real service answers as the stub does is the live run's evidence
  and `services/mock-api/test/fixtures-integration.test.ts`'s business. Said plainly because a test
  that reads its expectations from the same files the script reads can look stronger than it is.
* **`phase0-stubs` in CI still lists `seed`.** That job accepts only exit 3 or 1 from
  `scripts/demo.py` and `scripts/seed.py`, and `seed` now exits 2 when a service is down — which is
  what it truthfully does in a job that runs no services. It needs the treatment `scripts/health.py`
  already has there: its own step, pinning exit 2 and asserting no `PASS` row is printed. That edit is
  outside this change's scope and is recorded here rather than made silently.
* **`make seed` is a precondition check, not a substitute for one.** It does not start services, apply
  migrations, or make anything true. Run it after `make up` and `make migrate`; a green run means the
  state downstream expects is the state that exists.
* **The Makefile's one-line description of this target is now wrong.** It reads "Load synthetic
  fixtures into PostgreSQL and the mock API", which is the assumption this ADR rejects. It should say
  that the target verifies both loaders rather than performing either. Recorded here because
  `Makefile` is outside this change's scope.

## Alternatives considered

* **Insert the fixtures into PostgreSQL.** Rejected: it would create the tables ADR-0022 deliberately
  does not have, putting the demo's copy of a hospital next to the mock API's copy — two authorities
  for the same fact, which is the failure ADR-0024 exists to prevent. It would also mean writing rows
  no production module reads, and a loader whose second run needs an upsert strategy is a loader whose
  first run needed a reason.
* **Load the migrations' reference rows from a fixture file inside `scripts/seed.py`.** Rejected:
  `fixtures/` contains no vocabulary file, the tables' authority is the migration that creates them,
  and a script that re-inserted them would be a second writer racing `make migrate` — with the
  contract as a third opinion. The check compares the migration's output to the contract instead.
* **Exit 3 ("not implemented") when a service is down.** Rejected: 3 means the script did not do its
  job. Here the script did its job and the answer is "not seeded". Conflating the two is how a stub
  stays a stub with a green build.
* **Trust the loader's `seed.mode` and skip the store comparison.** Rejected: `mode: "fixtures"` says
  which files were read, not what the store now holds, and `INSERT OR IGNORE` means those two can
  differ. It is the cheaper check that would have passed on every failure this script exists for.
* **Make the mock-API checks part of CI's integration job.** Rejected: that job has no mock API
  service, and an integration test that skips there is a failed build by that job's own rule. The stub
  gives the comparison the same coverage with no service to start.
* **Have `make seed` create the scratch database the tests use.** Rejected: tests create what they
  need and drop it afterwards (`tests/scripts/scripts_fixtures.py`), so a failing run cannot leave a
  database behind that the next run silently reuses.

## Gaps, stated

* **Three files are verified in count only, because their routes cannot enumerate them.**
  `directory/slots.json` (35) has **no list route at all** — slots are reached through
  `/availability` and the booking routes, so there is no endpoint whose response could be compared
  against the file. `GET /network-records` (17) requires both a `plan_id` and a `provider_id` before
  it answers anything, and a network record's identity is that pair plus a hospital rather than a
  single id. `GET /referral-requirements` (10) requires a `hospital_id` and a `visit_type`, and
  returns the rules that *apply* to that combination rather than every rule in the file. All three
  are checked as counts in the loader's report; the other seven listable datasets are compared id by
  id. Making them comparable would mean either a new route (a service change, not a seeding one) or
  grouping by a cross-product of fixture values, which would compare the script's enumeration
  against itself as much as against the store.
* **Field-level drift at an unchanged id is not detected.** A fixture record whose `distance_miles`
  changed, or whose coverage status flipped, keeps its id, and an id-set comparison cannot see it. The
  defence is that the checks compare each dataset against the *file*, so the file changing without the
  service restarting is caught by the count only if the count changes. A service restarted onto new
  fixtures is in sync by construction.
* **Not everything in `fixtures/` is loaded by anything, deliberately.** `coverage/coverage_results.json`
  is the demo's *expectation* of what Coverage Verification should produce. It is reachable through
  `referraleasy_rag.authority.CoverageLookup.from_fixtures`, which takes a directory rather than a
  path (the shape ADR-0024 requires, so nothing hardcodes a fixture location), and it is read by
  `tests/rag/test_authority.py` and `services/mock-api`'s coverage test. `directory/availability_results.json`
  and `directory/provider_candidates.json` are expectations for Availability and Provider Matching,
  read as test inputs by `tests/agents/test_matching_subagents.py`; `patients/**` and `workflow/**` are
  contract fixtures validated against schemas by `tests/contracts/test_fixture_conformance.py`;
  `patients/patient_details.json` is loaded by the mock API as identity only. Loading the expectation
  files into a service would make the demo's expected answer indistinguishable from its actual one —
  which is why they are left alone, and why `make seed` does not claim to have loaded them.
* **`registry/` is empty and nothing seeds it.** `registry/` holds empty `agents/` and `prompts/`
  directories and no `releases/` at all, while `make eval` references
  `registry/releases/latest.json`. `release_registry` has no rows, no fixture source and no writer.
  Out of this script's scope and not something a verifier should paper over — noted here so the next
  reader does not go looking for a `make seed` flag that would fix it.
* **This script does not verify the services end to end.** It checks that the datasets exist and match
  their sources. Whether the flow runs against them is `tests/e2e`'s and the demo script's question.
