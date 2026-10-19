# ADR-0022 — PostgreSQL control-state schema: constraints as the enforcement point

- **Status:** Accepted. Every constraint below is applied to a real PostgreSQL 16 and
  covered by a passing test; nothing in this ADR is aspirational.
- **Date:** 2026-10-14
- **Phase:** 1 (referral workflow, schema and migration runner)
- **Decides:** how the control state of a referral is modelled, where each release-blocking
  invariant is enforced, and how the schema is migrated.

## Question

The workflow spans a coordinator, several agents, a human operator queue and a booking
step. Which of the guarantees that keep that safe — stale workers, double booking, replayed
commands, re-sent notifications, an operator acting beyond their remit — are enforced by
the schema, and which are left to the application to remember?

## Context

Two facts about the system drive everything below.

**There is no message broker.** The outbox that carries notifications is the transport, not
a staging area in front of one. Anything a broker would normally provide has to be rows and
`SKIP LOCKED`.

**Agents are not trusted to be careful.** A prompt is not a boundary, and an agent that
believes it is in `awaiting_approval` may in fact be working from a state that moved two
minutes ago. So every rule that matters is placed where it cannot be skipped: in a
constraint, a trigger or a single-statement function.

PostgreSQL is the system of record. OpenSearch holds telemetry and search, is sampled and
best-effort, and is never a transaction source — including for audit, which stays in
PostgreSQL precisely because a reindexable index cannot be a record of what happened.

## What was checked

| # | Command | Result |
|---|---|---|
| 1 | `docker run --rm -d --name re-mig-test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=referraleasy -p 55432:5432 postgres:16-alpine` | container up, port 55432 published |
| 2 | `uv run python scripts/migrate.py --validate-only` | `13 migration file(s) valid: 001-013` |
| 3 | `uv run python scripts/migrate.py --database-url postgresql://postgres:test@localhost:55432/referraleasy` | `13 migration(s) applied` (001–013, no errors) |
| 4 | `TEST_DATABASE_URL=... uv run --with 'psycopg[binary]' python -m pytest migrations -m integration` | `104 passed, 20 deselected` |
| 5 | `uv run --with 'psycopg[binary]' python -m pytest migrations -m "not integration"` | `20 passed, 104 deselected` |
| 6 | `uv run python scripts/migrate.py --status` | 13 applied, 0 pending, checksums match |

One finding during (3) is worth recording, because it changed the schema: a
`GENERATED ALWAYS AS (observed_at + fresh_for) STORED` column was refused with
`generation expression is not immutable`. `timestamptz + interval` is `STABLE`, not
`IMMUTABLE`, because the result genuinely depends on the session `TimeZone` across a DST
boundary. Freshness is therefore stamped by a `BEFORE INSERT` trigger that overwrites
whatever the caller supplied (`006_evidence.sql`).

## Decision

### 1. Owner and lease are two independent staleness checks, in one statement

`workflow` carries `owner_worker_id`, `lease_expires_at` and a monotonic `generation`.
`workflow_claim` and `workflow_advance` are single `UPDATE ... WHERE` statements whose
`WHERE` clauses require the caller's generation, the live lease and the expected
`state_version` to all still hold. A worker whose lease lapsed, or whose generation has
been superseded, matches no row and writes nothing.

There is no separate "check then act" step, so there is no window between the two.

### 2. Every transition is optimistic and short

`workflow_transition` holds the legal edges; an edge that is absent is not a transition,
whatever the caller believes. `workflow_guard()` requires `state_version` to advance by
exactly one alongside a legal edge, and raises `RE002` otherwise. Nothing holds a workflow
open across an agent turn — a `session` may span many short transitions, but it carries no
workflow state itself (`004_session_task.sql`).

### 3. The approval binding is a key, not a convention

An approval is bound to **patient + workflow + state_version + booking payload hash +
expiry**. That tuple is a `UNIQUE` constraint (`approval_binding_key`), and
`booking_request` reproduces it and points at it with a composite foreign key.

This is the whole mechanism: a booking row that disagrees with its approval on workflow,
patient or payload hash has **no parent row to point at**, so the insert cannot succeed.
The database compares the hashes; the handler does not get the opportunity to forget.

"Any data change invalidates the approval" follows from the same key:

* change the booking payload → the hash moves → no live approval matches;
* move the workflow → the epoch moves → no live approval matches;
* let the approval expire → it is not live.

`workflow_booking_consent_guard()` refuses entry into `booking_pending` without a live,
unconsumed, unexpired approval bound to the *current* epoch and payload hash, and refuses
`booked` without a confirmed `booking_request`.

### 4. A consumed approval is spent, permanently

`booking_request_one_per_approval` is a plain `UNIQUE (approval_id)` — one booking per
approval for all time, not "one live booking". A cancelled booking does not return the
capability. Independently, `booking_request_consume_approval()` spends the approval as
part of the insert and refuses an insert against an already-consumed or expired approval.
Either guard alone would do; both together mean a retry, a race or a replayed command
cannot produce a second booking.

### 5. Reused command ids are rejected, not coerced

The tempting implementation is `INSERT ... ON CONFLICT DO NOTHING` plus a read-back, which
quietly returns the first response for a second, different request. Instead,
`command_begin()` compares the stored `request_hash` against the supplied one and raises
`RE005` on a mismatch. The stored hash is immutable by trigger.

### 6. The outbox is the queue, and it has an ending

`outbox_claim()` leases due rows with `FOR UPDATE SKIP LOCKED`, so a row is handed to one
worker and no worker blocks behind another's lease. `next_attempt_at` carries the backoff
as row state rather than a sleeping worker. `outbox_settle()` moves an exhausted row to the
terminal `dead` state, and `outbox_claim()` never selects a terminal row: a notification
that will never succeed stops instead of looping.

### 7. Human ownership suspends automation, structurally

`workflow.human_owner_id` is checked by `workflow_guard()` on every update: while a human
owns a workflow, a non-human writer is refused with `RE003`. Automation does not need to be
told to stand down; the write does not land.

Ticket claims are compare-and-set on `handoff.claim_generation`, so two operators reading
generation N cannot both win. `handoff.claim_generation` may only advance by one, and never
backwards.

**Resolving a ticket is not booking consent**, and that is enforced three times over:

1. `handoff_resolve()`'s state allow-list is `triaged | resolved | failed | cancelled` —
   the booking states are not reachable from it;
2. `approval_guard()` refuses an approval created by a human actor, so an operator cannot
   mint consent even by writing the insert directly;
3. `workflow_booking_consent_guard()` needs a real, live approval regardless of who is
   asking.

### 8. Immutability is a trigger, not a grant

`release_registry` has no UPDATE path: a `BEFORE UPDATE OR DELETE` trigger raises `RE008`
for every writer, including the table owner, and `TRUNCATE` has its own statement-level
guard because a row-level trigger never sees it. `REVOKE`s are applied as well, but they
are defence in depth — the owner bypasses them, and a trigger is what actually holds.

Release status is therefore not a column. It is derived from the append-only
`release_event` log by the `release_status` view, so it cannot drift from recorded history.

The same append-only guard covers `audit` and `evidence`.

### 9. Audit lives in PostgreSQL

`audit` is append-only and hash-chained: each entry stores the previous entry's hash, and
`audit_verify_chain()` reports the first broken link. Entries for workflow transitions,
approval consumption, bookings and handoff changes are written by triggers on those events,
so the audit records what the database did rather than what a handler remembered to log.

A hash chain is **tamper-evident, not tamper-proof**: it detects edits, deletions and
reordering, and it cannot detect a well-formed append by someone with write access. The
ADR says so rather than implying a guarantee that does not exist.

### 10. Policy is data, and it is default-deny

`policy_rule` is a role × tool × workflow-state matrix. `policy_is_allowed()` returns
`false` when no row exists — bad news travels as default, not as `NULL`. `task_policy_gate()`
consults it on every task insert and raises `RE006`, so the coordinator cannot exceed the
matrix no matter what it decides.

### 11. Migrations are numbered SQL applied by a small runner

Forward-only, one transaction per file, checksummed in `schema_migrations`. No ORM: the DDL
is meant to be read, and the constraints above *are* the specification. The runner splits
statements with a quote-aware lexer so that a `;` inside a `$$`-quoted function body does
not end a statement, and that same lexer backs a well-formedness check that needs no
database.

## Consequences

**What this buys.** The eight failure modes that would make the demo unsafe —
stale writer, race on a transition, double booking, approval drift, replayed command,
re-sent notification, operator overreach, rewritten release manifest — are unavailable by
construction, not by discipline. Each one fails loudly with a distinct SQLSTATE a caller
can branch on.

**What it costs.**

* **Every write to `workflow` goes through the guard trigger.** That is one extra function
  call per transition, and it is the point. A bulk data migration that wants to bypass it
  must disable the trigger deliberately, which is visible in review.
* **Errors are exceptions, so a refused write aborts its transaction.** Denials therefore
  cannot be logged in the same transaction that caused them; a gateway that wants them in
  the audit trail writes `outcome='deny'` in a separate transaction after catching. The
  schema supports it and does not pretend to do it automatically.
* **Changing a migration that has already been applied is refused.** During development
  this means recreating the local database; in a deployed environment it means a new
  migration. This is the intended friction.
* **`audit_link()` takes a transaction-scoped advisory lock**, serialising appenders to
  keep one chain tail. Audit writes are therefore not free under high concurrency. At this
  scale that is irrelevant; at a larger one, the chain would be per-stream.

**Not decided here.** Connection pooling, partitioning of `audit` and `outbox` by time,
replication topology, and the retention job that prunes `command_dedup` past `expires_at`.
None of these change the constraints above.
