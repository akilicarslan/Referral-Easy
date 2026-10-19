"""One `command_id`, one outcome, and exactly one owner of the retries (§7, §8).

Two guarantees that look like one:

* **outcome dedupe** — the first caller to present a `command_id` *and* its request hash gets to do
  the work and write the response down; every later caller with the same pair receives that response
  instead of doing it again, and a different body under the same id is refused (`RE005`).
* **the retry curve** — one loop, `run_with_retries`, over a transport whose own retries are off, so
  three attempts mean three requests rather than three times nine.

The tests below keep them apart, because the ledger's guarantee is about *outcomes* while the retry
owner's is about *attempts*. The interleaving the ledger deliberately does **not** prevent — one
replica's `in_progress` row committed while another replica finishes the same command — is pinned as a
test rather than left as a comment: the workflow lease is what serialises execution (§7,
`test_ownership.py`), and the ledger's contribution is that the late replica reports the winner's
outcome instead of overwriting it.

The workers are async because their transports are; the ledger is not. `run_any` is the one place a
test enters an event loop, and it is a function rather than a fixture so that a test with two replicas
can drive the second one from *inside* the first one's call.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from worker_doubles import expect_refusal

from referraleasy_agent_runtime import (
    BudgetCaps,
    CallOutcome,
    UpstreamRejected,
    UpstreamTransient,
    is_transient,
)
from referraleasy_contracts.generated.common import ResultOutcome
from referraleasy_domain import CommandLedger, canonical_request_hash
from referraleasy_domain.errors import CommandConflictError, StaleOwnershipError
from referraleasy_workers import commands as commands_module
from referraleasy_workers.booking import HttpBookingApi
from referraleasy_workers.commands import CommandOutcome, run_command
from referraleasy_workers.outbox import HttpNotificationSender

#: The spec's numbers. Stated rather than imported from the conftest so a change in the fixture shows
#: up as a diff in two places instead of quietly redefining what "three attempts" means here.
CAPS = BudgetCaps(
    attempt_timeout_ms=3000, max_attempts=3, turn_deadline_ms=30000, http_tool_timeout_ms=1500
)

COMMAND = "cmd-booking-1"
SITE = "scheduling:create_booking"


class Recorder:
    """A callable that answers the same thing every time and remembers that it was asked."""

    def __init__(self, answer: Any) -> None:
        self._answer = answer
        self.calls: list[Any] = []

    async def __call__(self, lease: Any) -> Any:
        self.calls.append(lease)
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer

    @property
    def attempts(self) -> int:
        return len(self.calls)


def request(body: str = "S-1") -> dict[str, str]:
    return {"idempotency_key": "idem-1", "slot_id": body}


def run(conn: Any, execute: Any, *, body: dict[str, str] | None = None, **kwargs: Any) -> Any:
    """One `run_command` call, with the command id and site the booking leg uses."""
    return run_command(
        conn,
        command_id=COMMAND,
        request=body or request(),
        execute=execute,
        caps=CAPS,
        site=SITE,
        **kwargs,
    )


def run_any(coro: Any) -> Any:
    return asyncio.run(coro)


def ledger_row(conn: Any, command_id: str = COMMAND) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM command_dedup WHERE command_id = %s", (command_id,))
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Who owns the retries (§8)
# ---------------------------------------------------------------------------


def test_every_http_adapter_this_package_builds_has_transport_retries_off() -> None:
    """The "one retry owner" claim, inspectable at the one place this package opens a client.

    An adapter that built its own `httpx.AsyncClient` with retries on would turn one attempt into
    several requests while the budget counted one — the multiplication the runtime's own retry tests
    exist to prevent, reintroduced from the worker side. Two adapters exist, both are asserted, and the
    assertion reads the *profile* rather than the code.
    """
    booking = HttpBookingApi("http://booking.invalid", caps=CAPS)
    notifications = HttpNotificationSender("http://notify.invalid", caps=CAPS)

    assert booking.profile is not None
    assert notifications.profile is not None
    assert booking.profile.retries == 0
    assert notifications.profile.retries == 0
    assert booking.profile.follow_redirects is False


@pytest.mark.integration
def test_the_retry_curve_is_the_runtimes_and_the_knobs_are_passed_through(
    db: Any, monkeypatch: Any
) -> None:
    """`run_command` delegates to the runtime rather than looping itself, and passes the knobs on.

    A worker-side loop around `run_with_retries` is the other way to multiply a budget, and it is
    invisible to the runtime's own tests. This asserts the delegation structurally — the imported
    retry function is called exactly once, with the site, the empty-answer predicate and the
    write-attempt flag — and then checks that the outcome it returns is what gets stored, so the
    delegation is not merely present but load-bearing.
    """
    seen: list[dict[str, Any]] = []
    marker = lambda value: value == {}  # noqa: E731 - the predicate's identity is the assertion

    async def fake_run_with_retries(call: Any, **kwargs: Any) -> Any:
        seen.append({"call": call, **kwargs})
        return CallOutcome(
            value={"booking_request_id": "B-1"},
            attempts=1,
            outcome=ResultOutcome.completed,
            retry_after_s=None,
            elapsed_ms=1,
        )

    monkeypatch.setattr(commands_module, "run_with_retries", fake_run_with_retries)

    outcome = run_any(run(db, Recorder({"ignored": True}), valid_empty=marker, write_attempt=True))

    assert len(seen) == 1
    assert seen[0]["site"] == SITE
    assert seen[0]["valid_empty"] is marker
    assert seen[0]["write_attempt"] is True
    assert seen[0]["budget"].caps is CAPS
    assert outcome.value == {"booking_request_id": "B-1"}
    assert outcome.stored_response == {"outcome": "completed", "attempts": 1}


def test_a_transient_error_is_retried_and_a_refusal_is_not() -> None:
    """Two exceptions, one rule each, and the rule is the runtime's (`is_transient`).

    This package's contribution is that it never second-guesses the classification — the retry curve
    decides what is another attempt, and the worker only decides what to *store* afterwards.
    """
    assert is_transient(UpstreamTransient("the facility is down")) is True
    assert is_transient(UpstreamRejected("400 from the facility", status_code=400)) is False


# ---------------------------------------------------------------------------
# The outcome, as a value
# ---------------------------------------------------------------------------


def test_the_two_derived_flags_a_caller_acts_on() -> None:
    """`write_may_have_landed` and `is_empty` are derived rather than stored, so a stored outcome and a
    derived one cannot disagree about whether a write might have landed."""
    base: dict[str, Any] = {
        "command_id": COMMAND,
        "request_hash": "0" * 64,
        "disposition": None,
        "executed": True,
        "deduplicated": False,
        "value": None,
        "attempts": 1,
        "stored_status": 200,
        "stored_response": None,
    }
    unknown = CommandOutcome(**{**base, "outcome": ResultOutcome.outcome_unknown})
    empty = CommandOutcome(**{**base, "outcome": ResultOutcome.no_match})
    done = CommandOutcome(**{**base, "outcome": ResultOutcome.completed})

    assert unknown.write_may_have_landed is True
    assert empty.write_may_have_landed is False
    assert empty.is_empty is True
    assert done.is_empty is False
    assert done.write_may_have_landed is False


# ---------------------------------------------------------------------------
# A valid empty answer
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_valid_empty_result_is_completed_on_the_first_attempt(db: Any) -> None:
    """§8: "the call ran and matched nothing" is an *answer*, not a failure to answer.

    Two things asserted together because they are the same claim from both ends: the call is not
    repeated (one attempt, not three), and the ledger records it as `completed` — so a later caller
    replays "nothing matched" instead of spending another three attempts to hear it again.

    The replay reports `completed` with no value and the stored response beside it, which is the shape
    a caller must read: the disposition says "replayed", the response says what the answer was, and the
    *value* is only ever this process's own call result.
    """
    empty = lambda value: not value["matches"]  # noqa: E731 - see above
    execute = Recorder({"matches": []})

    outcome = run_any(run(db, execute, valid_empty=empty))

    assert outcome.executed is True
    assert outcome.deduplicated is False
    assert outcome.outcome is ResultOutcome.no_match
    assert outcome.is_empty is True
    assert outcome.attempts == 1, "an empty answer is final; asking again cannot change it"
    assert execute.attempts == 1
    assert ledger_row(db)["status"] == "completed"
    assert ledger_row(db)["response"]["outcome"] == "no_match"

    replay = run_any(run(db, execute, valid_empty=empty))

    assert replay.deduplicated is True
    assert replay.executed is False
    assert execute.attempts == 1, "the ledger means a second caller never touches the world"
    assert replay.outcome is ResultOutcome.completed
    assert replay.stored_response == {"outcome": "no_match", "attempts": 1}


# ---------------------------------------------------------------------------
# Two replicas, one command
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_two_replicas_sending_the_same_command_dedupe_on_the_command_id(db: Any, db2: Any) -> None:
    """The §7 dedupe, with two real connections and a real commit between them.

    Replica A sends the booking under the command id and commits the outcome; replica B, working the
    same workflow after taking over, presents the same id and the same body. B does not call the
    facility again, gets the stored response back, and is told it was a replay. The key is the command
    id *and* the body hash: the id alone would let a rewritten request pass as a retry.
    """
    first = Recorder({"booking_request_id": "B-1", "result": "created"})
    second = Recorder({"booking_request_id": "B-2", "result": "created"})

    a = run_any(run(db, first))
    db.commit()
    b = run_any(run(db2, second))

    assert a.executed is True
    assert a.deduplicated is False
    assert b.executed is False
    assert b.deduplicated is True
    assert second.attempts == 0, "the second replica never touched the facility"
    assert b.attempts == 0
    assert b.stored_response == a.stored_response
    assert b.disposition.value == "replayed"
    assert a.request_hash == canonical_request_hash(request())


@pytest.mark.integration
def test_the_same_id_with_a_different_body_is_refused_not_coerced(db: Any, db2: Any) -> None:
    """`RE005` is the double-booking tripwire: an id records *one* request, forever.

    The refusal is the database's (`command_begin` compares the request hash) and it is raised before
    anything is executed — a worker that reused an id for a rewritten request would otherwise be
    sending a body the facility has no record of under that key.
    """
    run_any(run(db, Recorder({"result": "created"})))
    db.commit()

    with expect_refusal(db2, CommandConflictError):
        run_any(run(db2, Recorder({"result": "created"}), body=request("S-2")))

    row = ledger_row(db2)
    assert row is not None
    assert row["request_hash"] == canonical_request_hash(request())


@pytest.mark.integration
def test_the_late_replica_reports_the_winners_outcome_and_not_its_own(db: Any, db2: Any) -> None:
    """The interleaving the ledger does not prevent, and what it does about it.

    A begins, commits its `in_progress` row — the "commit before the network call" pattern
    `commands.py` documents — and then stalls. Replica B, which sees the committed row, does the work
    and completes the command. When A finishes and writes its outcome, the row is terminal and
    `command_begin`/`command_complete` refuse the overwrite (`RE005`); the worker then reads the
    *winner's* response back and reports it, rather than raising or writing over a decided outcome.

    Two things are pinned as a consequence: one command id has one recorded outcome, and A reports the
    recorded one — `deduplicated=True`, the winner's stored response, and **no local value** — because
    a later replay of the same command will report the winner's answer too, and two answers for one id
    is the confusion the ledger exists to remove. A's own call result is discarded, not returned.

    This is also the test that found the savepoint: without it the replay after `RE005` failed with
    "current transaction is aborted" (the refusal aborts the transaction it lands in), so this branch
    was unreachable in practice, and the booking leg would have been handed the *loser's* answer.
    """
    winner: list[Any] = []

    async def stalled(lease: Any) -> Any:
        db.commit()  # A's in_progress row becomes visible to the other replica
        winner.append(await run(db2, Recorder({"booking_request_id": "B-2"})))
        db2.commit()
        return {"booking_request_id": "B-1"}

    a = run_any(run(db, stalled))
    db.commit()

    assert winner[0].executed is True and winner[0].deduplicated is False
    assert a.executed is True, "A did call the world; the ledger simply did not keep its answer"
    assert a.deduplicated is True
    assert a.value is None, "the loser's answer is not the recorded answer"
    assert a.outcome is ResultOutcome.completed
    assert a.disposition.value == "replayed"
    assert a.stored_response == winner[0].stored_response
    assert "B-1" not in str(a.stored_response), "A's own booking id never reached the ledger"
    assert ledger_row(db)["response"] == winner[0].stored_response
    assert ledger_row(db)["status"] == "completed"


@pytest.mark.integration
def test_a_failed_command_is_spent(db: Any, db2: Any) -> None:
    """A terminal failure is stored, replayed, and never silently retried into a success.

    The sharp edge of "one outcome": the id is spent, so a retry that expects to succeed is a new
    attempt with a new command id. The stored failure is what a replay reports — including the error
    text, so an operator reading the ledger sees why and not only that something went wrong.
    """
    execute = Recorder(UpstreamRejected("400 from the facility", status_code=400))

    a = run_any(run(db, execute))
    db.commit()

    assert a.executed is True
    assert a.outcome is ResultOutcome.terminal_error
    assert a.error is not None and "400" in a.error
    assert ledger_row(db)["status"] == "failed"
    assert ledger_row(db)["response"]["outcome"] == "terminal_error"

    replay_execute = Recorder({"result": "created"})
    b = run_any(run(db2, replay_execute))

    assert b.deduplicated is True
    assert b.outcome is ResultOutcome.terminal_error
    assert replay_execute.attempts == 0, "a spent id may not be turned into a fresh attempt"
    assert b.error is not None and "400" in b.error
    assert b.stored_status == 502


@pytest.mark.integration
def test_a_domain_refusal_does_not_spend_the_command(db: Any, db2: Any) -> None:
    """A refusal is not a failure: the command stays open for whoever holds the workflow.

    `RE001`/`RE003` mean "this attempt may not proceed", not "the command failed". Recording it as a
    failure would let a stale worker spend another worker's command — the id would be terminal and the
    replica that legitimately took over could not complete it. The refusal propagates to the caller
    (which routes it) and the row is left `in_progress`.
    """
    with pytest.raises(StaleOwnershipError):
        run_any(run(db, Recorder(StaleOwnershipError("lost the lease", sqlstate="RE001"))))
    db.commit()

    row = ledger_row(db)
    assert row is not None
    assert row["status"] == "in_progress", "a refusal must not spend the id"
    assert row["response"] is None

    # The replica that does hold the workflow completes the very same command.
    done = run_any(run(db2, Recorder({"result": "created"})))
    db2.commit()

    assert done.executed is True
    assert done.deduplicated is False
    assert ledger_row(db2)["status"] == "completed"


@pytest.mark.integration
@pytest.mark.parametrize("write_attempt", [True, False])
def test_exhaustion_after_a_write_attempt_is_unknown_not_retryable(
    db: Any, write_attempt: bool
) -> None:
    """§7/§17: a deadline that expires with a booking request in flight means "we do not know".

    The same exhausted budget is `outcome_unknown` when the call was a write and `retryable_error`
    when it was not, because "no booking exists" is exactly what a timeout during a write cannot
    support. The stored response carries the distinction too, so a replay does not have to re-derive it
    from the cap that happened to be hit.
    """
    execute = Recorder(UpstreamTransient("the facility is not answering"))

    outcome = run_any(run(db, execute, write_attempt=write_attempt))

    expected = ResultOutcome.outcome_unknown if write_attempt else ResultOutcome.retryable_error
    assert outcome.outcome is expected
    assert outcome.write_may_have_landed is write_attempt
    assert outcome.attempts == CAPS.max_attempts, "three attempts, and the third is the last"
    assert execute.attempts == CAPS.max_attempts
    assert ledger_row(db)["status"] == "failed"
    assert ledger_row(db)["response"]["outcome"] == expected.value


@pytest.mark.integration
def test_a_replay_is_readable_without_claiming_the_command(db: Any) -> None:
    """`CommandLedger.replay` exists so reconciliation can read the original answer (§7, §17).

    A lookup with the same key must be able to see what the create command recorded without opening a
    new command, and asking under a different hash is a refusal rather than a second look — which is
    what keeps "reconcile with the same key" from degrading into "book again".
    """
    original = run_any(run(db, Recorder({"result": "created"})))
    db.commit()

    ledger = CommandLedger(db)
    record = ledger.replay(COMMAND, original.request_hash)

    assert record.status == "completed"
    assert record.response == original.stored_response

    with expect_refusal(db, CommandConflictError):
        ledger.replay(COMMAND, canonical_request_hash(request("S-9")))
