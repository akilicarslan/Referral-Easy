"""Integration tests for the command ledger (§8).

The rule under test: **one `command_id` has one outcome.** A retry of the same request is answered
from the ledger; a *different* request reusing the id is a conflict, not a second execution. Getting
these two apart is the whole job — treating a replay as a conflict breaks every retry, and treating
a conflict as a replay silently executes somebody else's request.

The distinction the client must expose is therefore three-way, not two-way: run it, replay it, or
stop. `failed` is terminal too, and the id is spent: the failure is the recorded outcome, so
retrying under the same id would be a second attempt at a request the system already answered.

This is also where the reconciliation invariant is kept: an unknown booking outcome is resolved with
the **same** key it already has. A caller that mints a new id to "try again" is refused here rather
than at the booking table, because the payload it would carry does not match the stored one.
"""

from __future__ import annotations

import pytest

from referraleasy_domain import (
    CommandConflictError,
    CommandDisposition,
    CommandLedger,
    DomainError,
    canonical_booking_payload_hash,
    canonical_request_hash,
)

pytestmark = pytest.mark.integration

BOOKING = {"slot_id": "S-1", "doctor_id": "D-9"}


@pytest.fixture
def ledger(db) -> CommandLedger:
    return CommandLedger(db)


def test_a_command_that_has_never_run_must_be_executed(ledger) -> None:
    decision = ledger.begin("cmd-1", canonical_request_hash(BOOKING))

    assert decision.command_id == "cmd-1"
    assert decision.disposition is CommandDisposition.IN_FLIGHT
    assert decision.should_execute
    assert not decision.is_replay
    # Nothing to return yet: the work has not happened.
    assert decision.stored_response() is None
    assert decision.record.is_terminal is False


def test_completing_a_command_makes_the_next_begin_a_replay(ledger) -> None:
    request_hash = canonical_request_hash(BOOKING)
    ledger.begin("cmd-1", request_hash)
    ledger.complete(
        "cmd-1",
        request_hash,
        response_status=201,
        response={"booking_ref": "PROVIDER-REF-1"},
    )

    decision = ledger.begin("cmd-1", request_hash)

    assert decision.disposition is CommandDisposition.REPLAYED
    assert decision.is_replay
    assert not decision.should_execute
    # The stored response is what the retry returns: the same answer as the first call, without
    # touching the provider again.
    assert decision.stored_response() == (201, {"booking_ref": "PROVIDER-REF-1"})


def test_the_same_horse_before_it_finishes_is_still_not_a_conflict(ledger) -> None:
    """In-flight is a legitimate answer for a duplicate: the outcome is simply not known yet.

    A crash between `begin` and `complete` leaves exactly this row, and a retry has to be able to
    drive it to completion. Refusing the second call would strand the command.
    """
    request_hash = canonical_request_hash(BOOKING)
    first = ledger.begin("cmd-1", request_hash)
    second = ledger.begin("cmd-1", request_hash)

    assert first.should_execute and second.should_execute
    assert second.disposition is CommandDisposition.IN_FLIGHT


# ---------------------------------------------------------------------------
# Replay vs conflict: the distinction the ledger exists for
# ---------------------------------------------------------------------------


def test_the_same_id_with_a_different_request_is_a_conflict(ledger) -> None:
    """A retried command is the same command. A changed payload under the same id is not.

    This is the refusal that stops a "retry" from becoming a second booking attempt with different
    data: the id is taken, and the ledger will not let it mean something new.
    """
    ledger.begin("cmd-1", canonical_request_hash(BOOKING))

    with pytest.raises(CommandConflictError) as excinfo:
        ledger.begin("cmd-1", canonical_request_hash({**BOOKING, "slot_id": "S-2"}))

    assert excinfo.value.sqlstate == "RE005"
    assert "request hash" in str(excinfo.value)


def test_completing_with_a_hash_that_does_not_match_is_a_conflict(ledger) -> None:
    ledger.begin("cmd-1", canonical_request_hash(BOOKING))

    with pytest.raises(CommandConflictError):
        ledger.complete(
            "cmd-1",
            canonical_request_hash({**BOOKING, "slot_id": "S-2"}),
            response_status=201,
        )


def test_a_command_cannot_be_completed_twice(ledger) -> None:
    """Once the row is terminal the outcome is fixed; a second completion could only contradict it."""
    request_hash = canonical_request_hash(BOOKING)
    ledger.begin("cmd-1", request_hash)
    ledger.complete("cmd-1", request_hash, response_status=201)

    with pytest.raises(CommandConflictError) as excinfo:
        ledger.complete("cmd-1", request_hash, response_status=503)

    assert "already terminal" in str(excinfo.value)


def test_completing_a_command_that_was_never_started_is_a_conflict(ledger) -> None:
    with pytest.raises(CommandConflictError):
        ledger.complete("cmd-ghost", canonical_request_hash(BOOKING), response_status=201)


# ---------------------------------------------------------------------------
# Failure is an outcome, not an absence of one
# ---------------------------------------------------------------------------


def test_a_failed_command_is_terminal_and_does_not_masquerade_as_a_replay(ledger) -> None:
    request_hash = canonical_request_hash(BOOKING)
    ledger.begin("cmd-1", request_hash)
    ledger.complete(
        "cmd-1",
        request_hash,
        response_status=502,
        status="failed",
    )

    decision = ledger.begin("cmd-1", request_hash)

    assert decision.disposition is CommandDisposition.FAILED
    assert not decision.should_execute
    assert not decision.is_replay
    # A replay means "here is the successful response". There is none, and inventing one would be
    # the worst possible answer.
    assert decision.stored_response() is None
    assert decision.record.is_terminal


def test_the_id_of_a_failed_command_is_spent(ledger) -> None:
    """Retrying under a spent id is refused, so a failure cannot be papered over.

    The caller has to choose a new id, which makes "we tried this and it failed, and now we are
    doing something different" an explicit act rather than an accident.
    """
    request_hash = canonical_request_hash(BOOKING)
    ledger.begin("cmd-1", request_hash)
    ledger.complete("cmd-1", request_hash, response_status=502, status="failed")

    with pytest.raises(CommandConflictError):
        ledger.complete("cmd-1", request_hash, response_status=201)


def test_a_status_outside_the_two_known_outcomes_is_refused(ledger) -> None:
    """The client does not get to invent an outcome; the database allows completed or failed."""
    request_hash = canonical_request_hash(BOOKING)
    ledger.begin("cmd-1", request_hash)

    with pytest.raises(CommandConflictError) as excinfo:
        ledger.complete("cmd-1", request_hash, response_status=201, status="cancelled")

    assert "must be completed or failed" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Reconciliation: the same key, never a second one
# ---------------------------------------------------------------------------


def test_a_reconciler_reads_the_stored_outcome_without_executing_anything(ledger) -> None:
    """The shape an unknown booking outcome is resolved in.

    `replay` is the read-only path a reconciler uses: it returns the answer the first attempt
    stored, and performs no work. The key is the one the attempt already used -- which is the whole
    point, since a second key would be a second booking.
    """
    request_hash = canonical_request_hash(BOOKING)
    ledger.begin("booking:cmd-1", request_hash)
    ledger.complete(
        "booking:cmd-1",
        request_hash,
        response_status=201,
        response={"booking_ref": "PROVIDER-REF-1"},
    )

    record = ledger.replay("booking:cmd-1", request_hash)

    assert record.response_status == 201
    assert record.response == {"booking_ref": "PROVIDER-REF-1"}
    assert record.disposition is CommandDisposition.REPLAYED


def test_reconciling_with_a_different_payload_is_refused_rather_than_answered(ledger) -> None:
    request_hash = canonical_request_hash(BOOKING)
    ledger.begin("booking:cmd-1", request_hash)
    ledger.complete("booking:cmd-1", request_hash, response_status=201)

    with pytest.raises(CommandConflictError):
        ledger.replay("booking:cmd-1", canonical_request_hash({**BOOKING, "slot_id": "S-2"}))


def test_reconciling_an_unknown_key_is_a_conflict_not_an_empty_answer(ledger) -> None:
    """A key the ledger has never seen means nothing was attempted under it.

    That is not the same as "the attempt failed", so it must not return a shape a caller could read
    as an outcome. It is refused, and the caller learns it used the wrong key.
    """
    with pytest.raises(CommandConflictError) as excinfo:
        ledger.replay("booking:cmd-2", canonical_request_hash(BOOKING))

    assert "not found" in str(excinfo.value)


def test_the_booking_payload_hash_is_the_same_kind_of_key(ledger) -> None:
    """The consent hash and the dedup hash are one encoding, so a caller cannot satisfy one by
    hashing the other's way."""
    assert canonical_booking_payload_hash(BOOKING) == canonical_request_hash(BOOKING)


# ---------------------------------------------------------------------------
# The shape the column enforces
# ---------------------------------------------------------------------------


def test_a_hash_the_column_cannot_accept_is_refused_by_the_column(ledger) -> None:
    """The hex-64 CHECK is the contract, and the client produces hashes that satisfy it.

    A malformed hash is a constraint violation (23514), not an RE00x refusal: no rule of the domain
    was broken, the row simply does not have the shape the schema requires. It arrives as a
    `DomainError` carrying the code, and the constraint's name is in the message so the caller can
    see which shape was wrong.
    """
    with pytest.raises(DomainError) as excinfo:
        ledger.begin("cmd-1", "not-a-hash")

    assert excinfo.value.sqlstate == "23514"
    assert "command_dedup_request_hash_hex" in str(excinfo.value)
