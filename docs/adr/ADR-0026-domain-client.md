# ADR-0026 — The typed client calls the database's rules; it does not restate them

- **Status:** Accepted. Every claim below was checked against a real PostgreSQL 16 with all 14
  migrations applied, and every invariant is covered by a test that asserts the refusal and not a
  message string. Nothing here is aspirational.
- **Date:** 2026-10-14
- **Phase:** 1 (a typed client over the control-state schema)
- **Decides:** what `referraleasy_domain`'s stores are allowed to decide for themselves, what they
  must delegate to PostgreSQL, and how a refusal reaches the caller.

## Question

ADR-0022 placed the control-state invariants in constraints, triggers and single-statement
functions. What is left for the Python that calls them?

The question is not rhetorical. A friendly client *wants* to help: it wants to check
`human_owner_id` before writing, cache the operator resolution allow-list, validate a payload hash
before sending it, and refuse a booking when consent has lapsed. Every one of those is a second copy
of a rule that already exists — and between Python and PostgreSQL, the copy that runs first is
always Python.

## Context

**The schema is the only authority on legality.** `workflow_advance` compares ownership, lease,
generation and state_version in one `UPDATE`; `approval_is_live` decides what a live approval is;
`policy_is_allowed` is default-deny; `command_begin` is what makes a retry a retry. None of these is
a helper the application could have written for itself, because each of them has to be atomic with
the write it guards. A Python check is not atomic with anything.

**There is no second state machine, and there must not be one.** The transition graph is
`workflow_transition` rows. Reproducing it in Python would produce a client that believed a
transition was legal when the database had been migrated to think otherwise — and the disagreement
would surface as a confusing runtime failure rather than as a refused call.

**The client's real jobs are the ones the database cannot do.** Encoding a canonical payload hash
so that "the same request, retried" is recognisable across a network boundary. Turning a SQLSTATE
into a Python type a caller can branch on. Decoding a row without demanding a particular row factory.
Claiming a batch of messages and running the delivery loop around them.

**The pressure to duplicate is concentrated in exactly two places,** and both are handled by naming
them: the allow-list of states an operator may resolve a ticket to, and the shape of a request hash.
Where the rule is enforced by a `CHECK`, the client's contribution is to produce a value that
satisfies it, not to re-derive the constraint.

## What was checked

| # | Command | Result |
|---|---|---|
| 1 | `docker run --rm -d --name re-domain -e POSTGRES_PASSWORD=test -e POSTGRES_DB=referraleasy -p 55450:5432 postgres:16-alpine` | container up, port 55450 published |
| 2 | `DATABASE_URL=postgresql://postgres:test@localhost:55450/referraleasy uv run python scripts/migrate.py --database-url ...` | 14 migration(s) applied (001–014, no errors) |
| 3 | `... uv run python scripts/migrate.py --status` | `14 applied, 0 pending` |
| 4 | `TEST_DATABASE_URL=... uv run pytest tests/domain` | `176 passed in 7.86s` |
| 5 | `uv run pytest tests/domain` (no `TEST_DATABASE_URL`) | `84 passed, 92 skipped in 0.33s` |
| 6 | `uv run ruff check packages/domain tests/domain` | `All checks passed!` |
| 7 | `uv run mypy packages/domain` | `Success: no issues found in 8 source files` |

Three findings changed the code rather than merely confirming it.

**The contracts package has no `py.typed`,** and the repository-level mypy configuration that would
silence it is outside this change. The client had been importing `WorkflowState` from
`referraleasy_contracts.generated.common`, which made `mypy --strict` fail in a file it does not own.
The imports were removed: the contract's `WorkflowState` is a `StrEnum`, so it already *is* a `str`
and arrives as a plain string argument, and the one genuinely useful shape — anything with a `.root`
attribute, as a contract action reference has — became a two-line structural Protocol. The lesson is
the ADR's own subject: a dependency whose only contribution is a type name was a second definition
waiting to disagree.

**`approval_no_rebinding` makes `expires_at` immutable, and `approval_expiry_after_approval`
requires `expires_at > approved_at`.** So a lapsed approval cannot be fabricated, at insert or after.
That is the right rule — consent cannot be back-dated into still being valid — but it means the only
controllable clock is `approval_is_live`'s `at` argument, and the gate-level expiry test has to cross
a real transaction boundary, because `now()` is the transaction timestamp. Both forms are now in the
booking tests, and the seeder that used to fabricate lapsed consent with a negative interval is gone.

**A refused `outbox_settle` poisons the transaction it ran in,** which a test caught: `drain` was
catching `OutboxClaimError` and continuing, and the next statement failed with
`current transaction is aborted`. The `lost` counter was therefore unreachable on any connection
that was not autocommit. Each settle now runs in its own block, which psycopg makes a savepoint when
a transaction is already open. This is recorded because it is the class of bug the whole design is
aimed at: the rule was in the database and correct, and the client's *claim about the rule* was
wrong.

## Decision

### 1. The client asks; the database judges

`PolicyTable.is_allowed` calls `policy_is_allowed`. `WorkflowStore.has_live_approval` calls
`approval_is_live`. `CommandLedger.begin` calls `command_begin`. Every one of these is an optional
convenience over a decision the database makes anyway — the write that follows re-checks it, because
the function it calls does. Nothing here is a gate; the gate is the `UPDATE`'s `WHERE` clause.

The corollary is what is deliberately *absent*. There is no `can_transition`, no
`is_booking_allowed`, no Python list of legal states, and no `if workflow.is_human_owned: raise`
before a write — that last one would be a check that could pass moments before the database refused
the write anyway, which is strictly worse than no check, because it teaches the caller to trust it.

### 2. A refusal is a value, with a type

`errors.translate` maps eight SQLSTATEs (`RE001`–`RE008`) to exceptions and everything else to a
plain `DomainError` that still carries its code. The stores do not raise their own domain errors;
they translate, in one place, in `_translated()`. The distinction is visible to the caller and
testable: a stale write is `StaleOwnershipError` and its `sqlstate` is `RE001`; a unique-index
violation is a `DomainError` whose `sqlstate` is `23505`, because no rule of the domain was broken —
the row simply already existed.

The translation is duck-typed on `sqlstate` (psycopg) or `pgcode` (DBAPI 2.0) rather than
`except psycopg.Error`, so the package imports no driver. It is also *narrow*: an exception with no
SQLSTATE is not a refusal, so a `TypeError` from a typo stays a `TypeError`. A bug must never
present itself to a caller as "the database refused your transition".

### 3. The caller supplies the expectation; the database supplies the decision

Every write takes `expected_generation` and, where the state machine is involved,
`expected_state_version`, and the "expected" is the point: the caller passes what it read, and a
mismatch means someone else moved the row. The alternative — the client fetching the current values
and passing those back — would silently convert every stale write into a successful one, and would
quietly make the compare-and-set meaningless.

For the same reason no store method holds a transaction open across a workflow's life. One call is
one statement, and the ownership that outlives the transaction is a lease, because a lease can
expire without anyone having to remember to clean it up.

### 4. The client owns the loop and the encoding, and nothing else

`OutboxStore.drain` is the one method with control flow, and it is control flow over *other people's
calls*: claim a batch, run the handler, settle, count. It does not decide whether a message should
be retried — `outbox_settle` does that, including the terminal `dead` state — it only reports the
outcome and contains the handler's exception so a failing notification cannot reach a caller who is
holding the booking's transaction open.

`canonical_request_hash` is the other thing that has to live in Python, because it is an encoding
shared with the outside world: the same JSON bytes have to hash the same way on both sides of a
retry. It is not reimplemented — it imports `_canonical_bytes` from `approval.py` (private, and
imported anyway) so that the booking-payload hash and the request hash are one encoder under two
names. A second encoder would be a second chance to disagree, and the disagreement would appear as a
false conflict on an identical retry.

### 5. Where a rule *is* restated, it is restated as an omission

`HandoffStore.resolve` passes `workflow_state` straight through. The allow-list that bounds it is
not reproduced in Python, and that is a decision rather than an omission: the list is a security
property of the operator queue, and a copy in Python would be the copy that got it wrong. The
refusal arrives as `ApprovalRefusedError` (`RE004`) with the database's own hint attached, which
tells the operator how to book *properly* — and the caller cannot be talked out of it, because
nothing in this package knows which states are forbidden.

## Consequences

**The client is untestable without PostgreSQL, and that is accepted.** The tests are split along the
same line as the design: 84 unit tests need neither a database nor a driver, and 92 integration tests
skip unless `TEST_DATABASE_URL` is set. The skips are a real risk — a CI that forgets to set the
variable would run the suite, see it pass, and cover nothing that matters. The mitigation is that the
skip is not silent: each skipped test reports `TEST_DATABASE_URL is not set; integration tests need a
database`, and the driver check fails with the command to install one.

**Two refusals have no client-reachable path, and the tests say so.** `referraleasy_domain` offers no
way to record an approval: consent is patient-side, and a store method that wrote one would be a way
to forge it. So the approval-binding refusals are asserted in `migrations/tests` against a direct
`INSERT`, and the booking tests cover the client's own path instead. Likewise `HumanOwnedError`
(`RE003`) is unreachable through this package, because `workflow_claim` and `workflow_advance` both
require `human_owner_id IS NULL` *inside* the compare-and-set — a human-owned workflow never reaches
the guard trigger and refuses as `StaleOwnershipError` (`RE001`). The guard is still shown to be live
by a test that writes the way a future caller might, and the mapping stays unit-tested, but a caller
should read the two codes as the same answer: the write did not happen.

**`referraleasy_domain` is not installed into the workspace virtualenv.** The root project declares
`package = false` and does not depend on the workspace members, so `uv sync` never installs them and
`pytest tests/domain` cannot import the package by itself. The documented workaround in this repo is
`PYTHONPATH=packages/domain/src`. Fixing it properly means editing the root `pyproject.toml`
(install the workspace members, or add `packages/domain/src` to ruff's `src` list, which is also why
`referraleasy_domain` currently sorts as a third-party import) — both are outside this change, and
both will be felt by every later phase that imports this package.
