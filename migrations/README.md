# Migrations

Forward-only SQL, applied in order by `run.py`. **There is no ORM, and that is
deliberate**: the constraints in these files are the specification for how the
system may change state. Generated DDL would hide exactly the part a reviewer
needs to read.

PostgreSQL is the system of record for control state. OpenSearch is a search and
telemetry store and is never a transaction source; nothing here reads from it or
depends on it.

## Running

```bash
# Apply everything pending.
uv run python scripts/migrate.py --database-url postgresql://user:pw@host:5432/referraleasy

# Or via the environment variable the rest of the repo uses.
DATABASE_URL=postgresql://user:pw@host:5432/referraleasy uv run python scripts/migrate.py

uv run python scripts/migrate.py --status         # the ledger, and what is pending
uv run python scripts/migrate.py --dry-run        # what would run
uv run python scripts/migrate.py --to 7           # stop after version 007
uv run python scripts/migrate.py --validate-only  # parse the files; no database needed
```

The runner needs `psycopg` (v3). It is imported lazily, so `--validate-only` and
the unit tests work without a driver installed.

### Rules the runner enforces

* **Ordered, gapless numbering.** `NNN_lower_snake_case.sql`. A gap, a duplicate
  version, or a stray file stops the run before anything is applied.
* **One transaction per file.** A migration lands whole or not at all, and its
  row in `schema_migrations` is written in the same transaction as the DDL.
* **Forward-only.** Each applied version stores a sha256 of the file. If an
  applied file changes on disk, the runner refuses to continue rather than
  guessing which version of the truth the database is holding.

That last rule bites during development. If you edit a migration that has
already been applied to your local database, the runner will refuse to run. The
remedy is to recreate the database:

```bash
docker exec re-mig-test psql -U postgres -d postgres \
  -c 'DROP DATABASE referraleasy' -c 'CREATE DATABASE referraleasy'
```

Do this only in development. In a deployed environment, the fix is a **new**
migration, never an edit to an applied one.

## Tests

```bash
# Unit tests: no database, no driver. Always run.
uv run python -m pytest migrations -m "not integration" -v

# Everything, against a real PostgreSQL.
docker run --rm -d --name re-mig-test \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=referraleasy \
  -p 55432:5432 postgres:16-alpine

TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/referraleasy \
  uv run python -m pytest migrations -v -m integration

docker rm -f re-mig-test
```

> Run pytest as `python -m pytest` rather than bare `pytest` if `psycopg` is not
> yet in the workspace's dependency group. The bare `pytest` entry point runs
> with the project virtualenv, which will not have the driver; `uv run --with
> 'psycopg[binary]' python -m pytest` gets both from one command. Adding
> `psycopg[binary]` to the root `pyproject.toml` dev group is the cleaner fix and
> is the natural next step once the schema stops moving.

Integration tests are skipped when `TEST_DATABASE_URL` is unset, so the suite is
safe to run in an environment with no database.

## Which constraint enforces what

Every constraint below is the enforcement point for a release-blocking
invariant, and every one of them is covered by a test in `tests/`.

| Invariant | Enforced by | Tested by |
|---|---|---|
| **Stale generation cannot write** | `workflow_guard()` raises `RE001` when `generation` goes backwards; `workflow_claim` / `workflow_advance` compare the caller's generation in the `WHERE` clause | `test_workflow_invariants.py::test_stale_generation_write_is_rejected` |
| **Stale lease cannot write** | `workflow_advance` requires `lease_expires_at > now()` in the `WHERE`; `workflow_claim` lets a worker reclaim only an expired lease | `test_workflow_invariants.py::test_stale_lease_write_is_rejected` |
| **One atomic `UPDATE ... WHERE` for both** | `workflow_claim` and `workflow_advance` are single statements whose `WHERE` clause carries owner, lease, generation and state_version | `test_workflow_invariants.py::test_concurrent_claim_exactly_one_wins` |
| **Optimistic version check on every transition** | `workflow_guard()` requires `state_version` to advance by exactly one, and only with a legal edge from `workflow_transition` | `test_workflow_invariants.py::test_state_version_must_advance_by_exactly_one` |
| **Illegal transitions rejected, never coerced** | `workflow_transition` is the state machine; `workflow_guard()` raises `RE002` for any edge not present in it | `test_workflow_invariants.py::test_illegal_transition_is_rejected_not_coerced` |
| **One transition, one winner** | the guard trigger re-evaluates on the committed row; the loser's `WHERE` no longer matches | `test_workflow_invariants.py::test_concurrent_state_transition_exactly_one_wins` |
| **No long transaction spans a workflow** | every transition is one function call in one short transaction; nothing holds a workflow open across an agent turn | by construction; sessions/tasks carry no workflow state (`004`) |
| **Approval bound to patient + workflow + state_version + payload hash + expiry** | `approval_binding_key` (a UNIQUE tuple) + `approval_guard()` + `approval_is_live()` | `test_approval_booking.py::test_approval_is_bound_to_the_current_epoch` |
| **Any data change invalidates the approval** | the binding tuple *is* the key: `workflow_booking_consent_guard()` compares the workflow's current payload hash and epoch against a live approval | `test_approval_booking.py::test_changing_the_booking_payload_invalidates_the_approval` |
| **Consumed approval cannot authorise a second write** | `booking_request_one_per_approval` UNIQUE + the one-way `booking_request_consume_approval()` trigger | `test_approval_booking.py::test_a_consumed_approval_cannot_authorise_a_second_booking` |
| **Booking must carry the approved payload** | composite FK `booking_request_approval_binding_fkey` → `approval(id, workflow_id, patient_ref, booking_payload_hash)` | `test_approval_booking.py::test_a_booking_cannot_carry_a_payload_the_patient_did_not_approve` |
| **Same command, same hash → replay** | `command_begin()` / `command_replay()` return the stored response | `test_command_dedup.py::test_a_retry_replays_the_stored_response` |
| **Same command, different hash → reject** | PRIMARY KEY on `command_id` + the stored `request_hash`, compared explicitly in `command_begin()`; `command_dedup_no_rewrite()` freezes the hash | `test_command_dedup.py::test_a_different_request_under_the_same_command_id_is_rejected` |
| **Outbox rows are not handed out twice** | `outbox_claim()` uses `FOR UPDATE SKIP LOCKED` | `test_outbox.py::test_skip_locked_does_not_hand_the_same_row_to_two_workers` |
| **A permanently failing notification stops** | `outbox_settle()` moves an exhausted row to the terminal `dead` state; `outbox_claim()` never selects a terminal row | `test_outbox.py::test_a_row_dies_once_its_attempt_budget_is_spent` |
| **Human ownership suspends automation** | `workflow_guard()` raises `RE003` for any non-human writer while `human_owner_id` is set | `test_handoff.py::test_human_ownership_suspends_automation_writes` |
| **Conflicting ticket claim impossible** | `handoff_claim()` is a compare-and-set on `claim_generation`; `handoff_guard()` refuses a backwards or non-advancing generation | `test_handoff.py::test_conflicting_claim_exactly_one_wins` |
| **Resolving a ticket is not booking consent** | `handoff_resolve()`'s state allow-list excludes the booking states; `approval_guard()` refuses consent from a human actor; `workflow_booking_consent_guard()` needs a live approval | `test_handoff.py::test_resolving_a_ticket_cannot_move_a_workflow_into_booking`, `..._cannot_write_consent_by_hand` |
| **Release registry immutability** | `release_registry_no_update_trigger` (BEFORE UPDATE OR DELETE, raises `RE008`) + a TRUNCATE guard + `REVOKE`s; status is derived from the append-only `release_event` | `test_release_registry.py::test_the_release_manifest_cannot_be_updated` |
| **Audit stays in PostgreSQL** | `audit` is append-only in the system of record, hash-chained, and written by triggers on the events themselves | `test_audit.py` (whole module) |
| **Policy cannot be exceeded** | `policy_is_allowed()` is default-deny; `task_policy_gate()` raises `RE006` on every task insert | `test_policy_gate.py::test_a_tool_the_role_may_not_use_is_refused` |

### SQLSTATEs

Custom codes are raised so that a caller can branch on the reason without
string-matching a message:

| Code | Meaning |
|---|---|
| `RE001` | stale writer: generation, lease, or claim generation is not current |
| `RE002` | illegal or malformed state transition |
| `RE003` | automation tried to write a workflow a human owns |
| `RE004` | consent/approval refused: no live binding, expired, consumed, or a human tried to grant it |
| `RE005` | command dedup conflict: same `command_id`, different request hash |
| `RE006` | policy denies this role/tool/state |
| `RE007` | outbox: bad claim or settle request |
| `RE008` | append-only table refused a mutation |

## Reading order

The files are meant to be read in order; later ones build on the vocabulary
earlier ones define.

| File | What it establishes |
|---|---|
| `001_schema_migrations.sql` | the ledger itself |
| `002_actor_context.sql` | transaction-local actor identity, and the append-only guard used by several tables |
| `003_workflow.sql` | the aggregate root: state machine, owner/lease/generation, optimistic version |
| `004_session_task.sql` | sessions and tasks — bookkeeping *around* a workflow, never a second state machine |
| `005_policy.sql` | role × tool × workflow-state matrix, default deny, and the gate on task inserts |
| `006_evidence.sql` | provenance and freshness for evidence records |
| `007_approval.sql` | the consent binding, consumption, and the booking-flow guard |
| `008_booking_request.sql` | the booking, its composite FK to the approval, and one-booking-per-approval |
| `009_command_dedup.sql` | replay vs. reject |
| `010_outbox.sql` | the queue itself: leases, backoff, terminal state, `SKIP LOCKED` |
| `011_handoff.sql` | human ownership, the ticket claim CAS, and the resolution allow-list |
| `012_release_registry.sql` | append-only release manifests and derived status |
| `013_audit.sql` | the audit of record, hash-chained, plus the triggers that write it |

## Deliberate omissions

* **No foreign key from `evidence` to a workflow.** Evidence is captured before
  a workflow may exist for it, and it outlives any one referral.
* **No `deleted_at` columns.** Nothing here soft-deletes. A workflow that ends
  ends in a terminal state, and immutability is a trigger, not a convention.
* **No denormalised status columns on `task` or `booking_request` that restate a
  workflow's state.** Where two tables could disagree about the state of the
  world, only one of them is allowed to carry it.
