"""One command_id, one outcome — with the retries owned in exactly one place (§7, §8).

Two guarantees live in this module, and they are not the same guarantee.

**Outcome dedupe (the ledger's).** The first caller to present a `command_id` and request hash gets
to do the work and write the response down; every later caller with the same pair receives that
response instead of doing it again. A *different* body under the same id is refused (`RE005` →
:class:`~referraleasy_domain.errors.CommandConflictError`), which is the tripwire against the
double-booking bug: reusing an id for a request that is not the request it was recorded with is
rejected, never coerced.

**Concurrency (the lease's).** The ledger does not lock anything: `in_progress` means "somebody took
this command", not "nobody else may". Two replicas working the same *workflow* are kept apart by the
workflow lease and generation (§7, :mod:`referraleasy_workers.ownership`), and the ledger's
contribution is that when the loser of that race tries again, it replays the winner's outcome rather
than re-issuing the write. This module therefore never treats a second `begin` as a permission slip.

**The retry curve is the agent runtime's, not ours.** `run_with_retries` is the only loop here, over
a transport whose own retries are off (`build_http_client(..., retries=0)` in the API adapters), so
three attempts mean three requests rather than three times nine. A valid empty answer — "the search
ran and matched nothing" — is returned on the first attempt instead of burned through the budget,
because asking the same question again cannot change "nothing matched".

The failure path is where a worker has to be careful, and the rules are these:

* A **domain refusal** (`RE001`, `RE003`, `RE006`, …) is *not* a command failure. It means this
  attempt may not proceed — a lost lease, a suspended workflow — and the ledger is left `in_progress`
  so that whoever does hold the workflow can still complete it. Recording it as a failure would let a
  stale worker spend another worker's command.
* **Budget exhaustion after a write attempt** is `outcome_unknown`, not `retryable_error`. A turn
  deadline that expires while a booking request is in flight does not mean "no booking exists", and
  the caller is sent to reconciliation rather than invited to retry (§7, §17).
* Everything else is stored as a `failed` command with the reason, and a replay of that command
  reports the stored failure rather than running the work again. A failed command is **spent**: a
  retry that expects to succeed is a new attempt and needs a new `command_id`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from referraleasy_agent_runtime import (
    BudgetCaps,
    BudgetExhausted,
    CallLease,
    CallOutcome,
    CapKind,
    Clock,
    TurnBudget,
    is_transient,
    run_with_retries,
)
from referraleasy_contracts.generated.common import ResultOutcome
from referraleasy_domain import (
    CommandConflictError,
    CommandDisposition,
    CommandLedger,
    CommandRecord,
    DomainError,
    canonical_request_hash,
)
from referraleasy_workers.db import ConnectionLike

__all__ = ["CommandOutcome", "run_command"]


@dataclass(frozen=True, slots=True)
class CommandOutcome[T]:
    """What happened to one command: whether it ran, what it produced, and what a retry would see."""

    command_id: str
    request_hash: str
    disposition: CommandDisposition
    executed: bool
    deduplicated: bool
    outcome: ResultOutcome
    value: T | None
    attempts: int
    stored_status: int | None
    stored_response: Mapping[str, Any] | None
    error: str | None = None

    @property
    def write_may_have_landed(self) -> bool:
        """True when the outcome is unknown *after a write attempt* (§17).

        This is the flag the caller must act on: an unknown booking outcome is resolved by looking up
        the original key, never by trying the write again.
        """
        return self.outcome is ResultOutcome.outcome_unknown

    @property
    def is_empty(self) -> bool:
        """A valid call whose answer was "nothing" — a completed call, not a failure."""
        return self.outcome is ResultOutcome.no_match


async def run_command[T](
    conn: ConnectionLike,
    *,
    command_id: str,
    request: Mapping[str, Any],
    execute: Callable[[CallLease], Awaitable[T]],
    caps: BudgetCaps,
    clock: Clock | None = None,
    site: str | None = None,
    spend: CapKind = CapKind.TOOL_CALLS,
    valid_empty: Callable[[T], bool] | None = None,
    timeout_ms: int | None = None,
    write_attempt: bool = False,
) -> CommandOutcome[T]:
    """Run ``execute`` under the ledger, at most once, with the runtime's retry policy.

    ``execute`` is called with the attempt's :class:`~referraleasy_agent_runtime.CallLease` — that is
    `run_with_retries`' convention, not this module's — and it is what lets a callable size its own
    timeout to what is left of the turn instead of to a constant it cannot see.

    ``write_attempt=True`` says this command attempts a write to the outside world, which is what
    turns an exhausted deadline into ``outcome_unknown`` instead of a retryable error.

    The ledger row is written on this connection and the caller commits it. Committing *before* the
    network call is the caller's business and matters for one case only: an uncommitted `in_progress`
    row is invisible to another replica, so a second replica would not replay it (it would race the
    insert instead). The workflow lease is what prevents that race from mattering; this note exists so
    that the commit boundary is a decision rather than an accident.
    """
    ledger = CommandLedger(conn)
    request_hash = canonical_request_hash(request)
    decision = ledger.begin(command_id, request_hash)

    if not decision.should_execute:
        replayed = decision.stored_response()
        if decision.is_replay:
            return CommandOutcome(
                command_id=command_id,
                request_hash=request_hash,
                disposition=decision.disposition,
                executed=False,
                deduplicated=True,
                outcome=ResultOutcome.completed,
                value=None,
                attempts=0,
                stored_status=replayed[0] if replayed else None,
                stored_response=replayed[1] if replayed else None,
            )
        # `failed` is terminal too: the id is spent, and what a retry replays is the stored failure.
        return CommandOutcome(
            command_id=command_id,
            request_hash=request_hash,
            disposition=decision.disposition,
            executed=False,
            deduplicated=True,
            outcome=ResultOutcome.terminal_error,
            value=None,
            attempts=0,
            stored_status=decision.record.response_status,
            stored_response=decision.record.response,
            error=decision.record.error or _stored_error(decision.record.response),
        )

    budget = TurnBudget(caps, clock=clock)
    try:
        result: CallOutcome[T] = await run_with_retries(
            execute,
            budget=budget,
            site=site or f"command:{command_id}",
            spend=spend,
            timeout_ms=timeout_ms,
            valid_empty=valid_empty,
            write_attempt=write_attempt,
        )
    except BudgetExhausted as exhausted:
        outcome = (
            ResultOutcome.outcome_unknown
            if exhausted.after_write_attempt
            else ResultOutcome.retryable_error
        )
        stored = _record_outcome(
            conn,
            ledger,
            command_id,
            request_hash,
            response_status=504,
            response={"outcome": outcome.value, "error": str(exhausted)},
            status="failed",
        )
        if not stored.accepted:
            return _decided_elsewhere(command_id, request_hash, stored.record)
        return CommandOutcome(
            command_id=command_id,
            request_hash=request_hash,
            disposition=stored.record.disposition,
            executed=True,
            deduplicated=False,
            outcome=outcome,
            value=None,
            attempts=exhausted.used,
            stored_status=stored.record.response_status,
            stored_response=stored.record.response,
            error=str(exhausted),
        )
    except DomainError:
        # A refusal is not a failure: the ledger stays `in_progress` so the worker that *does* hold
        # the workflow can complete the same command. Re-raised untouched for the caller to route.
        raise
    except Exception as error:
        if is_transient(error):
            raise
        # Anything not worth another attempt is terminal *for this command*: retrying it would spend
        # budget to receive the same refusal. `UpstreamRejected` (a well-formed 4xx) is the common
        # case, and the caller routes the workflow from the error it already has in hand.
        stored = _record_outcome(
            conn,
            ledger,
            command_id,
            request_hash,
            response_status=502,
            response={"outcome": ResultOutcome.terminal_error.value, "error": repr(error)},
            status="failed",
        )
        if not stored.accepted:
            return _decided_elsewhere(command_id, request_hash, stored.record)
        return CommandOutcome(
            command_id=command_id,
            request_hash=request_hash,
            disposition=stored.record.disposition,
            executed=True,
            deduplicated=False,
            outcome=ResultOutcome.terminal_error,
            value=None,
            attempts=0,
            stored_status=stored.record.response_status,
            stored_response=stored.record.response,
            error=repr(error),
        )

    stored_body = {"outcome": result.outcome.value, "attempts": result.attempts}
    stored = _record_outcome(
        conn,
        ledger,
        command_id,
        request_hash,
        response_status=200,
        response=stored_body,
        status="completed",
    )
    if not stored.accepted:
        return _decided_elsewhere(command_id, request_hash, stored.record)
    return CommandOutcome(
        command_id=command_id,
        request_hash=request_hash,
        disposition=stored.record.disposition,
        executed=True,
        deduplicated=False,
        outcome=result.outcome,
        value=result.value,
        attempts=result.attempts,
        stored_status=stored.record.response_status,
        stored_response=stored.record.response,
    )


@dataclass(frozen=True, slots=True)
class _Stored:
    """What came back from trying to write this command's outcome down.

    ``accepted`` is False in exactly one situation — the ledger already had an outcome for this command
    and refused this replica's — and the distinction is *not* readable from ``record``, which describes
    the row in both cases. A caller that could not tell the two apart would report its own answer as
    the recorded one.
    """

    record: CommandRecord
    accepted: bool


def _record_outcome(
    conn: ConnectionLike,
    ledger: CommandLedger,
    command_id: str,
    request_hash: str,
    *,
    response_status: int,
    response: Mapping[str, Any],
    status: str,
) -> _Stored:
    """Store an outcome, and report the ledger's instead if another replica stored one first.

    The collision is real and narrow: two replicas can both pass `begin` while a command is
    `in_progress` (the ledger does not lock), and the second one's `complete` then finds the row
    already terminal (`RE005`). The ledger's stored outcome is the authoritative one — it is the same
    command and the same request hash, or the completion would have been refused for the other reason —
    so this reads it back and reports *that*, rather than overwriting or raising past the caller.

    The write runs inside its own transaction block, which psycopg turns into a **savepoint** because
    the caller's transaction is already open (`ConnectionLike.transaction` documents precisely this
    use). That is load-bearing rather than tidiness: a refusal aborts the surrounding transaction, so a
    read issued straight after the `RE005` would itself fail with "current transaction is aborted" —
    which is what this branch did before the savepoint existed, making it dead code that no test had
    reached. See `test_the_late_replica_reports_the_winners_outcome_and_not_its_own`.
    """
    try:
        with conn.transaction():
            record = ledger.complete(
                command_id,
                request_hash,
                response_status=response_status,
                response=response,
                status=status,
            )
    except CommandConflictError:
        return _Stored(record=ledger.replay(command_id, request_hash), accepted=False)
    return _Stored(record=record, accepted=True)


def _decided_elsewhere(
    command_id: str, request_hash: str, record: CommandRecord
) -> CommandOutcome[Any]:
    """The outcome to report when the ledger, not this replica, decided the command.

    It has the shape a serialized replay has — `deduplicated=True`, the recorded response, **no local
    value** — because that is what a later replay of the same command will report, and two callers
    reading two different answers for one `command_id` is the confusion the ledger exists to remove.
    ``executed`` is still True: this replica did call the world. The recorded answer is simply not its
    to keep.

    The booking leg reads this shape as "already recorded, do not send again" and reconciles instead
    (`service.py`), which is the same branch it takes for a replay — the correct one here, since the
    winner's outcome is what the workflow's durable record must agree with.
    """
    return CommandOutcome(
        command_id=command_id,
        request_hash=request_hash,
        disposition=record.disposition,
        executed=True,
        deduplicated=True,
        outcome=(
            ResultOutcome.terminal_error if record.status == "failed" else ResultOutcome.completed
        ),
        value=None,
        attempts=0,
        stored_status=record.response_status,
        stored_response=record.response,
        error=record.error or _stored_error(record.response),
    )


def _stored_error(response: Mapping[str, Any] | None) -> str | None:
    if not response:
        return None
    error = response.get("error")
    return None if error is None else str(error)
