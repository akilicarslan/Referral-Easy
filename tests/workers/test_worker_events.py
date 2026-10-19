"""Cancel, abandonment and transport timeout are three events, not one (§7).

The requirement is a list of three, and the reason it is a list is that treating any two of them as
the same produces a specific, expensive bug:

* a **remote cancel** is a *request*. It can be refused, it can time out, and it can lose a race with
  a commit that was already in flight. Reading it as "nothing was booked" is how a patient ends up
  with an appointment nobody is tracking.
* **abandonment** says nothing about a request that was already on the wire, so it carries
  `write_in_flight` — and an abandonment with a write in flight is reconciled exactly like a timeout.
* a **transport timeout** is the only one of the three that is unambiguously "we do not know".

These tests are pure: the events carry what is true, and the state decision belongs to whoever reads
them. There is no database in this module because there is nothing here the database decides.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from referraleasy_workers import events
from referraleasy_workers.events import (
    ABANDONED_STATE,
    BOOKING_UNVERIFIED_STATE,
    TerminalEvent,
    TerminalEventRecord,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def test_the_three_events_are_three_distinct_values() -> None:
    """The vocabulary itself, pinned: a caller cannot compare a cancel to a timeout by accident."""
    assert len(set(TerminalEvent)) == 3
    assert TerminalEvent.REMOTE_CANCEL != TerminalEvent.USER_ABANDONED
    assert TerminalEvent.USER_ABANDONED != TerminalEvent.TRANSPORT_TIMEOUT
    assert TerminalEvent.TRANSPORT_TIMEOUT != TerminalEvent.REMOTE_CANCEL


def test_a_remote_cancel_does_not_mean_nothing_was_created() -> None:
    """§7: the cancel is best-effort, so a booking may still exist and must be reconciled.

    This is the mirror image of the double-booking bug: a workflow marked `CANCELLED` while the
    patient holds an appointment is just as expensive as two appointments, and harder to notice.
    """
    record = TerminalEventRecord.remote_cancel("wf-1", detail="cancel request sent", at=NOW)

    assert record.effect_is_best_effort is True
    assert record.booking_may_exist is True
    assert record.requires_reconciliation is True
    assert record.terminal_state is None, (
        "a cancel request may not close a workflow: CANCELLED is a fact about the facility's record"
    )


def test_abandonment_without_a_write_is_terminal_and_needs_no_reconciliation() -> None:
    """The one case where "the patient walked away" is the whole story: nothing was sent."""
    record = TerminalEventRecord.user_abandoned("wf-1", write_in_flight=False, at=NOW)

    assert record.booking_may_exist is False
    assert record.requires_reconciliation is False
    assert record.terminal_state == ABANDONED_STATE


def test_abandonment_with_a_write_in_flight_is_reconciled_like_a_timeout() -> None:
    """Abandonment says nothing about a request that was already out, so it inherits the question."""
    record = TerminalEventRecord.user_abandoned("wf-1", write_in_flight=True, at=NOW)

    assert record.booking_may_exist is True
    assert record.requires_reconciliation is True
    assert record.terminal_state == BOOKING_UNVERIFIED_STATE
    assert record.terminal_state != ABANDONED_STATE


def test_a_transport_timeout_sends_the_workflow_to_reconciliation() -> None:
    record = TerminalEventRecord.transport_timeout("wf-1", detail="connection reset", at=NOW)

    assert record.booking_may_exist is True
    assert record.requires_reconciliation is True
    assert record.effect_is_best_effort is False
    assert record.terminal_state == BOOKING_UNVERIFIED_STATE


def test_the_three_events_do_not_share_a_termination_rule() -> None:
    """The table in the module docstring, asserted as a table.

    Two events that produce the same `terminal_state` *and* the same `requires_reconciliation` would
    be one event with two names, which is the failure this module was written to prevent.
    """
    outcomes = {
        record.kind: (
            record.terminal_state,
            record.requires_reconciliation,
            record.effect_is_best_effort,
        )
        for record in (
            TerminalEventRecord.remote_cancel("wf-1", at=NOW),
            TerminalEventRecord.user_abandoned("wf-1", at=NOW),
            TerminalEventRecord.user_abandoned("wf-2", write_in_flight=True, at=NOW),
            TerminalEventRecord.transport_timeout("wf-3", at=NOW),
        )
    }

    assert outcomes[TerminalEvent.REMOTE_CANCEL] == (None, True, True)
    assert outcomes[TerminalEvent.TRANSPORT_TIMEOUT] == (BOOKING_UNVERIFIED_STATE, True, False)
    # Abandonment has two rows, and they differ — which is why `write_in_flight` exists at all.
    assert TerminalEventRecord.user_abandoned("w", at=NOW).terminal_state != (
        TerminalEventRecord.user_abandoned("w", write_in_flight=True, at=NOW).terminal_state
    )


@pytest.mark.parametrize("kind", list(TerminalEvent))
def test_no_event_claims_a_booking_exists(kind: TerminalEvent) -> None:
    """Every message is a statement about *knowledge*, never about the appointment.

    A phrase like "your appointment was cancelled" would be a claim the event cannot support, and the
    patient is the one who acts on it (by not going).
    """
    record = TerminalEventRecord(kind=kind, workflow_id="wf-1", at=NOW)
    message = record.patient_message

    assert message
    for claim in ("is cancelled", "has been cancelled", "no appointment exists", "was not booked"):
        assert claim not in message, f"{kind.value} asserts something it cannot know: {message!r}"


def test_a_cancellation_in_progress_is_not_narrated_as_a_cancellation() -> None:
    """The cancel's own sentence, read for what it does *not* say."""
    message = TerminalEventRecord.remote_cancel("wf-1", at=NOW).patient_message

    assert "not confirmed it yet" in message
    assert "not confirmation that nothing is booked" in message


def test_the_events_vocabulary_is_the_contracts() -> None:
    """`TerminalEvent` is this package's; nothing else may invent a fourth name for the same three."""
    published = {name for name in dir(events) if name.endswith("_STATE") or name == "TerminalEvent"}
    assert published == {"TerminalEvent", "BOOKING_UNVERIFIED_STATE", "ABANDONED_STATE"}
