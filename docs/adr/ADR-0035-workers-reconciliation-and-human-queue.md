# ADR-0035 — Workers, reconciliation and the human queue: one key, one owner, one honest sentence

- **Status:** Accepted
- **Date:** 2026-10-15
- **Phase:** Phase 5 — worker processes, booking reconciliation, ownership/durability, HITL (§7, §11,
  §17)
- **Depends on:** §7 (workflow states, the policy table, one key per booking), §11 (escalation, the
  handoff queue, what the patient is told), §17 (proving it rather than asserting it), ADR-0030 (the
  runtime's budgets, retries and command dedupe — this phase **uses** that machinery and re-implements
  none of it), `referraleasy_contracts`, migrations 003/007/008/011/013/014

## Context

Phase 5 is the part of ReferralEasy that runs when nobody is watching: a worker claims a workflow,
sends a booking into a facility that may answer, may refuse, or may answer to nobody, reconciles
afterwards, and hands the workflow to a person when the machine cannot finish the job. Phase 5.1 —
the budget object, the retry loop, `run_command`, the command/dedupe table, the idempotency keys —
shipped inside `packages/agent-runtime` (ADR-0030, 225 tests). Nothing here re-derives any of it; the
worker package imports it.

What is new is everything that only matters because a *process* now runs it: processes are killed,
leases expire, replicas overlap, and a booking write can leave the process without an answer coming
back. Seven hazards shaped the decisions below, and each one is a plausible small shortcut:

1. **The write that left the process.** A booking POST answers `created` only when the facility says
   so. The dangerous case is the one nobody hears: the facility may have committed while our socket
   died. `outcome_unknown` is not a retry, and a system that treats it as one books twice.
2. **The late result from a replica that already lost its lease.** A worker that lost its lease and
   then writes its result must be refused — and *both* of the ways it can write (a state move and a
   checkpoint) have to be refused together, because one landing without the other is a row that
   describes a step that did not happen.
3. **The second booking key.** A reconciler that, finding the lookup ambiguous, "tries a different
   key" turns one appointment into two. The recovery from an unknown outcome is the *same* key until
   the question is answered or a person takes it.
4. **The retry that turns a degraded flow into a broken one.** A reschedule that failed transiently
   twice and then failed again must leave the flow one-way, not queue a third silent attempt.
5. **The handoff that grants consent.** Closing a ticket is not the patient approving a booking. The
   function's allow-list and the state machine's transition table are two *different* lists, and the
   gap between them is where a ticket resolution could become consent.
6. **"An agent is with you now".** There is no live channel. The honest sentence is a ticket, a
   statement that nobody has picked it up yet, and a route to a human.
7. **The model that rewords a failure.** Classifying a failure and writing a patient-facing message
   must not be a second, creative path through §11's trigger list. A failure message is assembled from
   a table of reasons, not generated.

## Decision

### The worker is a loop over the database, not a daemon with state

`services/workers` is a package of modules with no process-global state: `outbox.py` (claim, deliver,
retry, terminal), `ownership.py` (leases and generations), `checkpoint.py` (staged results and resume
plans), `booking.py` (the facility's wire vocabulary), `reconcile.py` (the reconciler),
`booking_record.py` (the durable booking row), `commands.py` (one command, one id), `recovery.py` (the
planner and escalation), `hitl.py` (the queue), `events.py` (how a call stopped being in flight),
`settings.py` (the environment). Everything a worker knows about its situation is in the row it just
read; nothing is carried between passes in memory.

The consequence is the property §7 asks for: a worker that is killed mid-pass loses nothing that the
next worker cannot read, because the only thing it would have carried is a generation number it can
re-read — and re-reading is precisely what re-claiming requires.

### Ownership is a lease, a monotonic generation, and a refusal that names the rule

`OwnedWorkflow.claim(conn, workflow_id, worker_id, expected_generation=…)` is a compare-and-set in the
row: the previous lease must have expired (or the row be unowned) **and** the generation must match.
Every advance bumps the generation, so ownership is an epoch rather than a flag, and a stale worker
cannot re-take work by re-reading the row — its tokens are fresh and its `owner_worker_id` is not.

`write_payload`'s guarded `UPDATE` matches on owner, generation, `human_owner_id IS NULL` and a live
lease. An `UPDATE` that matches nothing raises nothing, so the refusal is *produced* by re-reading the
row and saying which rule stopped the write: `HumanOwnedError` (`RE003`) when a person owns the
workflow — automation is suspended until the ticket is resolved — and `StaleOwnershipError` (`RE001`)
when the lease lapsed. The two are different answers because the caller's next move is different: a
human owner means stop and wait; a lost lease means stop, and the *next* claim is an ordinary one.

**Rejected:** a lease token in the checkpoint payload. The row is what `workflow_guard` reads; a second
place to look is a second place to forget.

### The booking leg: one key, one command id, one honest outcome

The facility is called through one key per workflow, minted once and carried on the outcome so a
caller can *assert* it never changed. Each attempt carries a deterministic `command_id` derived from
`(workflow_id, key, step)` — not from the worker id, because a second replica must dedupe onto the
same command rather than send a second one. `run_command` (ADR-0030) is the only path to the wire: it
records the attempt, dedupes on `(command_id, canonical_request_hash)`, and spends the budget.

The three answers the pipeline distinguishes are the three the database can be told about afterwards:

* **answered, created** — the workflow advances to `BOOKED`, and the `booking_request` row is the
  proof;
* **answered, already there** — a replay is not a second booking: the same row is confirmed, not
  inserted;
* **answered nowhere** — the attempt is recorded, a checkpoint is written *before* the outcome is
  reported, and the workflow advances to `BOOKING_UNVERIFIED` with what the facility answered (or the
  fact that it answered nothing).

**Rejected:** treating the third case as a failure to retry. Retrying a possibly-committed write is
how double bookings happen; §7's answer is a lookup under the same key, and that is what the resumed
replica plans.

### Checkpoints are an optimisation with a safety property

A checkpoint is `{step, state_version, generation, digest, data}` in `workflow.payload`, with the
`state_version` and generation it was written at. `read_checkpoint` verifies the digest and the
version and returns a *reason* (`verified`, `no checkpoint`, `digest mismatch`, `malformed`,
`unknown step`, `newer generation`, `last step`) rather than a bare `None`; `plan_resume` falls back to
the first step for anything it cannot prove. The rule that makes this safe is that nothing is resumed
*because* a checkpoint exists — a checkpoint only shortens the plan, and the plan is re-checked against
the policy table either way.

Resuming discovered the sharpest hole of this phase, and the fix is the interesting part:
`send_booking` must not be re-planned for a workflow in `BOOKING_UNVERIFIED`. The policy table is
default-deny, so the second attempt was refused (`RE006`) — and because the refusal happened *inside*
the plan, the recovery lookup the plan existed to reach never ran. A replica taking over an unverified
workflow now looks up, refuses to send a second time, and says so in the run's detail.

### The human queue is a separate writer, and it writes one row

`HumanQueue` writes `handoff` and nothing else. The staging record — the exact summary the patient
approved, the reason detail, the evidence lines, the deadline — is written by the *worker*, while it
still owns the workflow, because that is an automation write; the row is written by the queue. Queuing
requires consent (`ConsentRequiredError`, and consent to an empty document is not consent), so "no
consent" is a missing row rather than a flag some other query could forget to check. At most one live
ticket per workflow is the database's partial unique index, not a `SELECT`-then-`INSERT`.

Claiming is `handoff_claim`'s compare-and-set on `claim_generation`, and claiming is also what sets
`workflow.human_owner_id` — the column `workflow_guard` refuses automation writes on. An operator's
alternatives and notes are a draft in the process until resolution, because the workflow payload is
unwritable while the operator holds the ticket and a worker that wrote there anyway would be
impersonating the operator; they reach the durable record as the resolution note.

Resolution refuses the four booking-consent states and anything outside the function's allow-list with
`RE004`, **before** the database is asked, and the outcome object carries
`final_approval_still_required=True` / `booking_consent_granted=False` so no caller can infer consent
from a closed ticket. The test that pins this does it with a non-holder asking, so the refusal cannot
come from "you do not hold this ticket".

### The patient is told the truth, and the truth is a table

`OperatorResponse.live_operator_reached` is `False` by construction: there is no live channel, so
"an agent is with you" would be a lie that costs the patient a phone call. What the patient gets is a
pending ticket, the sentence that nobody has picked it up yet, and — when the directory has one — a
contact route. The message is asserted verbatim, because it is patient-facing text.

## Consequences

### What the phase found, and what keeps it found

Writing the tests found twelve real defects. Ten are in code this phase owns; the other two are in the
schema (`handoff_claim`/`handoff_release`/`handoff_resolve`, migrations 011/014), which is outside this
phase's write scope, and those two are pinned by tests and recorded here for the phase that owns that
SQL. Each of the ten is fixed, and each fix is pinned by a test that fails if it is reverted (verified
by mutation — every mutation below was applied, the named test failed, and the source was restored
byte-identical):

1. `outbox.py` read its HTTP timeout from a cap named `tool_timeout_ms`, a key that does not exist in
   the budget object: the timeout was silently `None`.
2. `run_once` crashed on `ceiling <= 0` when the default (`None`) was used — the ordinary production
   configuration.
3. `_record_outcome`'s collision branch was unreachable: it ran after the statement that had already
   aborted the transaction, with no savepoint to isolate it.
4. `BookingReconciler` annotated `clock: Clock`, promising a `now()` the protocol does not have.
5. The answered-nowhere branch could report a booked-looking run without writing the checkpoint that
   proves what the facility said.
6. `send_booking` was re-planned from `BOOKING_UNVERIFIED` and refused by the policy table before the
   lookup could run (the resume hole described above).
7. `PipelineRun.booked` required a reconciliation, so it was `False` on the ordinary direct-create
   path while the workflow row said `BOOKED` — a report contradicting the database.
8. The plan's note ("the booking write is not planned from this state") was overwritten by the last
   step's detail as the run went on, so the fact a resumed run most needs to report was the one it
   lost.
9. `HttpBookingApi.cancel` compared the facility's state string to `BookingState.cancelled` — a member
   the contract enum does not have (it is `CANCELLED`). The ordinary "the hospital confirmed the
   cancellation" answer therefore raised `AttributeError` inside the one path that tells a patient
   their appointment is gone. No test could reach it: every other test drives the booking leg through
   the scripted double, which answers in `CancelView`s and so never parses a cancellation body. mypy
   found it on its first run over this package — `"type[BookingState]" has no attribute "cancelled"
   [attr-defined]`, the only error the old line produced — and `tests/workers/test_booking_wire.py`
   now drives the real adapter over `httpx.MockTransport` with the mock API's own bodies, so the line
   has a test that fails if it is respelled.
10. `create_booking` read `booking_request_id` and `booking_state` from `booking` and from a top-level
    `detail`, and missed the third place: the outcome-unknown envelope carries both inside
    `error.detail`. That is the envelope of the case reconciliation exists for, so the view reported
    `None` for a facility that had just said "a booking may exist — here is its id and its state".
    Found by the same new test module, fixed by reading the third path.
11. `handoff_resolve` files a ticket's resolution under `actor_kind='automation'`: the handoff `UPDATE`
    (and the trigger that audits it) runs *before* the function switches the transaction's actor
    context to the operator. The operator appears in the detail, but a reader filtering
    `actor_kind='human'` cannot see that a ticket was resolved. Pinned, not papered over.
12. The same three functions (`handoff_claim`, `handoff_release`, `handoff_resolve`) save and restore
    `current_actor_kind()` and restore *only* the kind — `set_config(..., true)` is transaction-local,
    so the operator's id survives to the end of the transaction and every later audit row in it pairs
    an operator's id with somebody else's kind.

Defects 11 and 12 are in the schema (migrations 011/014) and outside this phase's write scope; they are
pinned by tests and recorded here for the phase that owns that SQL.

One thing was left deliberately unchanged and is recorded so the next reader does not hunt for it:
`HttpBookingApi.cancel` also catches `UpstreamRejected`, which `_request` cannot raise (it raises
`UpstreamTransient` for a transport failure, a 429 or a bare 5xx, and returns every other status as an
answer). The branch is unreachable rather than wrong, it is the error type a 4xx would take if that
contract ever changed, and a refusal still arrives as a `CancelView` — asserted by the wire test.

### Divergences between the schema's vocabulary and the domain's

Found while writing the tests, not fixed here (same reason): `BOOKED` has no edge to `RECOVERY`,
`BOOKING_UNVERIFIED` or `HUMAN_HANDOFF`; `workflow_advance` reports a human-owned suspension as
`RE001` rather than `RE003`, because its `WHERE` excludes human-owned rows and so cannot tell the two
apart; `handoff` has no columns for the reason class, the staging record, or an operator's drafts
(hence the payload staging and the resolution note); there is no `handoff_create()` or
`booking_request_confirm()`; `HandoffStatus.created` and `.expired` are unreachable by construction
(the schema has no `created` or `expired` state); `workflow_active_ticket_key`'s partial predicate is
written in lowercase terminal states (`'resolved'`, `'failed'`, `'cancelled'`) that the vocabulary
never uses, which makes the index global: a fresh referral for a re-opened ticket is impossible.

### The shared test database is part of the design

`TEST_DATABASE_URL` is shared with three other suites, and every one of them resets by truncating the
same twelve tables. A test's connection is deliberately not autocommit, so a running test holds the
locks on the rows it seeded; a neighbour's `TRUNCATE ... CASCADE` needs `ACCESS EXCLUSIVE` on all
twelve. The two then wait on each other across processes, and because the holding side is *idle in
transaction* — which is not a lock wait at all — PostgreSQL's deadlock detector never sees a cycle and
the run hangs until somebody kills it. That is a worse failure than the flaky one it resembles.

Three changes make the suite a good neighbour:

* `lock_timeout=15s` on every test connection: a statement that cannot get its lock fails with a
  message that says so instead of hanging.
* `idle_in_transaction_session_timeout=15s`: the server terminates an idle transaction that holds
  locks, which releases them and lets the neighbour's `TRUNCATE` finish. This is the half that fixes
  the livelock for the *other* suites too.
* Seeded workflows get a unique `ticket_ref` per test rather than the double's old fixed `T-1001`,
  because the unique index above is global: two suites seeding concurrently collided on it.

### Verification

`tests/workers` is 264 tests (69 marked `integration`, 195 pure), of which 22 are the human queue, 25
checkpoints, 17 the outbox worker, 15 reconciliation, 74 the reschedule/rebook leg, and 10 the booking
adapter's own wire vocabulary. That last module exists because of defect 9: the doubles this suite is
otherwise built on return views, so nothing but a test driving `HttpBookingApi` over
`httpx.MockTransport` can reach the lines that turn a JSON body into a contract value.
`pyproject.toml` puts `services/workers` in `[tool.ruff].src`, so `uv run ruff check` reads the same
package under test; `uv run mypy services/workers/src` is clean (16 source files), which is how defect
9 was found.

The two-replica and lease-expiry claims use two **real** database connections with distinct
`pg_backend_pid()`s (asserted by a test), commit where a replica would commit, and are the only way
the late-write, dedupe-onto-another-replica and one-holder-per-ticket claims are honest. The booking
facility and the notification sink are in-process doubles by design (no live calls, offline profile);
everything else — ownership, checkpoints, the queue, the audit trail, the state machine — is the real
database and the real SQL functions.

`services/workers` is a uv workspace member: the root `pyproject.toml` lists `services/workers` in
`[tool.uv.workspace].members`, declares `referraleasy-workers` as a project dependency and maps it in
`[tool.uv.sources]`, so `uv sync` installs the package into the shared virtualenv and
`import referraleasy_workers` works from any directory. Until those three lines were added this suite
worked around their absence with a `worker_bootstrap.py` that inserted `services/workers/src` on
`sys.path`; that module is deleted with the insert it contained, because an insert makes the suite
pass against a virtualenv where the package was never installed — a packaging break nobody sees.
`tests/workers/test_settings.py` asserts all three legs of the wiring, and asserts that no module in
the directory puts the source root on `sys.path` (the inverse of the test it replaced, with its own
planted-file proof that the scan can fail).

## Alternatives considered

**A worker that holds its lease in memory and renews it lazily.** Rejected: the lease is a column the
guard reads, and every write re-reads the row it is about to change. A cached lease is a second
opinion, and the guard does not consult it.

**A reconciler that mints a new idempotency key when the first lookup is inconclusive.** Rejected
explicitly: the second key is a second booking. The reconciler's outcome carries the one key so a
test can assert it never changed rather than trust that it did not.

**A "booking state" cache per workflow so the pipeline can avoid re-reading.** Rejected: the workflow
row *is* the state, and the resumption tests exist because a replica's only honest source of truth is
what it just read.

**Retrying a reschedule a third time when the second attempt failed transiently.** Rejected: after the
second transient failure the flow goes one-way, and the failure is visible to a person with the
evidence attached. A retry loop that keeps trying is a booking that may be made twice.

**Letting the resolution endpoint carry the operator's alternatives as a second payload write.**
Rejected: the workflow payload is unwritable during human ownership (`RE003`), and a second writer
there without ownership tokens would be automation impersonating the operator. The draft travels to
the durable record as the resolution note.

**Generating the patient-facing failure sentence with the model.** Rejected: §11's trigger list and
its reason classes are a table, and a reworded failure is a failure whose meaning depends on a model's
mood. `HITL_TRIGGERS` names every reason and the sentence each one produces.

**`SELECT`-then-`INSERT` for "one live ticket per workflow", and for the queue's claim.** Rejected:
both are races the database already refuses — a partial unique index and a compare-and-set on
`claim_generation` — and a check in Python is a check two processes can pass.
