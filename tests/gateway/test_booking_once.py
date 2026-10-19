"""Exactly one booking, however many times the request arrives (§7, §9, §12).

§12's line is "tekrar render tekrar booking yaratmaz" — re-rendering does not create a second booking.
It is easy to write and easy to believe, so this module tests it the only way it can be tested: **by
counting rows**, from three different directions, against the profile where the write actually happens.

Three ways a booking can be asked for twice, all three in one place:

1. **The client retries the command.** Same `command_id`, same body: the ledger's stored response
   comes back, with the first booking's id in it, and nothing is written.
2. **The client asks again properly.** A new `command_id` with the same approval: the approval was
   consumed by the first booking, so this is refused (`RE004`) — one approval authorises one booking.
3. **The stream is replayed.** A reconnect re-emits events that were already produced; a cursor is not
   a command, so no code path through the stream can write anything. Asserted as a row count *and* as
   an unchanged `state_version`, because the interesting failure is a replay that quietly advanced the
   workflow a second time.

The shipped profile refuses the write outright, so every test here uses `configured_demo` — the app
built with real layers behind the guardrail seam. `test_guardrails.py` holds the other half: with that
seam unconfigured, the same request writes nothing and says why.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from gateway_fixtures import BOOKING_PAYLOAD, Demo, hospital_ref

from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_domain.approval import PAYLOAD_HASH_KEY, canonical_booking_payload_hash
from referraleasy_gateway.commands import BOOKING_OUTCOME_UNVERIFIED


def booked(demo: Demo, *, outcome: str = "confirmed") -> tuple[str, dict[str, Any]]:
    """Take a workflow through consent and the booking write. Returns (workflow_id, response body)."""
    workflow_id = demo.seed()
    approval_id = demo.in_booking_flight(workflow_id)
    response = demo.booking(workflow_id, approval_id=approval_id, version=7, outcome=outcome)
    assert response.status_code == 200, response.text
    return workflow_id, response.json()


# ---------------------------------------------------------------------------
# The write itself
# ---------------------------------------------------------------------------


def test_a_confirmed_booking_writes_one_row_and_moves_the_workflow_once(
    configured_demo: Demo,
) -> None:
    """The happy path, asserted on the row and on the state — not only on the response.

    A 200 that wrote nothing is exactly the failure a response-only test would miss, so the row, its
    binding and the workflow's state are each checked — including the two facts that make `BOOKED`
    *legal*: the row is `CONFIRMED`, and it carries the hospital's own reference. §7's
    `workflow_booking_consent_guard` refuses `BOOKED` without exactly that, so a test that only
    asserted the response would not be able to tell a worked example of the rule from a bypass of it.

    Both profiles. The row used to read `REQUESTED` here and the workflow could only reach `BOOKED`
    in the memory profile, because the port had no way to say "the hospital confirmed it". It does
    now, and this is the assertion that changed with it.
    """
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)
    response = configured_demo.booking(workflow_id, approval_id=approval_id, version=7)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["replayed"] is False
    assert body["state"] == "BOOKED"
    assert body["state_version"] == 8
    assert body["approval_id"] == approval_id

    rows = configured_demo.bookings(workflow_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["booking_id"] == body["booking"]["booking_id"]
    assert row["approval_id"] == approval_id
    assert row["payload_hash"] == canonical_booking_payload_hash(BOOKING_PAYLOAD)
    assert row["state"] == "CONFIRMED"
    # The hospital's reference, recorded as it arrived: the scheduler is the only party that can
    # issue one, and the row is evidence of what it issued rather than of what the gateway believed.
    assert row["external_ref"] == body["booking"]["external_ref"]
    assert row["external_ref"]

    # The workflow has nothing left to ask, so the snapshot's list is empty — a receipt is issued
    # *to* the patient, not *by* them. It travels on the stream instead, which is asserted on its own
    # in `test_the_receipt_arrives_on_the_stream_while_the_snapshot_reports_no_open_question`.
    snapshot = configured_demo.snapshot(workflow_id).json()
    assert [card["component"] for card in snapshot["components"]] == ["AppointmentReceipt"]
    assert snapshot["components"][0]["action"] is None
    assert snapshot["state"] == "BOOKED"


def test_the_booking_is_bound_to_the_payload_the_patient_approved(configured_demo: Demo) -> None:
    """A payload the patient did not consent to is refused *before* the write, not after it."""
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)
    changed = {**BOOKING_PAYLOAD, "slot_id": "slot-2"}
    response = configured_demo.booking(
        workflow_id, approval_id=approval_id, version=7, payload=changed
    )
    assert response.status_code == 422
    message = response.json()["error"]["message"]
    assert "not the one this workflow holds" in message
    assert configured_demo.bookings(workflow_id) == []
    assert configured_demo.snapshot(workflow_id).json()["state"] == "BOOKING_IN_FLIGHT"


def test_an_unverified_outcome_is_recorded_as_the_database_would_record_it(
    configured_demo: Demo,
) -> None:
    """§7 resolves an unknown result by reconciliation, never by guessing — and the state says which.

    `BOOKING_UNVERIFIED` is the honest outcome when the hospital has not confirmed, and the row is
    still written: a booking that may exist is a fact that has to be stored before it can be chased.

    The row reads `REQUESTED`, which is the *same* state the postgres profile stores — there it is the
    column `pending`, and `control._BOOKING_COLUMN` is the one place the two spellings meet. Asserted
    because the two profiles read that row back through different code: the memory profile keeps the
    contract's word and the postgres profile translates the column into it, and a mapping that drifted
    would show up here rather than in whichever profile a test happened to be written against.
    """
    workflow_id, body = booked(configured_demo, outcome=BOOKING_OUTCOME_UNVERIFIED)
    assert body["state"] == "BOOKING_UNVERIFIED"
    rows = configured_demo.bookings(workflow_id)
    assert len(rows) == 1
    assert rows[0]["state"] == "REQUESTED"
    fallback = configured_demo.snapshot(workflow_id).json()["components"][0]
    assert fallback["component"] == "DirectoryFallbackCard"
    assert fallback["props"]["notice"] == "The hospital has not confirmed this booking yet."
    # The action on the fallback card is the reconciliation lookup, not a second booking.
    assert fallback["action"] == "scheduling:lookup_request"


def test_a_confirmation_with_no_hospital_reference_is_refused(configured_demo: Demo) -> None:
    """The other half of the confirmed outcome: it is only a confirmation if the hospital issued one.

    `external_ref` is the one fact in the request that only the other side of the wire can supply, so a
    `confirmed` outcome without it is a guess wearing the word, and 008's
    `booking_request_confirmed_has_ref` refuses to store it. The port refuses the same request earlier,
    naming that constraint, so the caller gets a contract violation rather than a driver error out of
    an INSERT — and the memory profile, which has no CHECK to fail on, refuses exactly what the
    database refuses.

    Nothing is written, asserted on the rows rather than only on the code: the refusal happens before
    the write, which is what keeps a failed confirmation from leaving a `pending` booking the
    reconciliation path would later have to explain.
    """
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)
    response = configured_demo.booking(
        workflow_id, approval_id=approval_id, version=7, external_ref=""
    )
    assert response.status_code == 422
    assert "booking_request_confirmed_has_ref" in response.json()["error"]["message"]
    assert configured_demo.bookings(workflow_id) == []
    unchanged = configured_demo.snapshot(workflow_id).json()
    assert (unchanged["state"], unchanged["state_version"]) == ("BOOKING_IN_FLIGHT", 7)


def test_a_reference_with_no_outcome_is_a_confirmation(configured_demo: Demo) -> None:
    """The derived branch that reaches `BOOKED`: the reference is the claim, and the outcome follows.

    `outcome` may be left out of the request, and what it means is then decided by whether the
    hospital's own reference came with it — a caller that has the reference has a confirmation, and
    there is nothing left for it to assert that the reference does not already say. Asserted through
    to the row and the state rather than on the response alone: 014's guard refuses `BOOKED` unless
    the row is `confirmed` *and* carries a reference, so a test that only read the response could not
    tell this apart from a bypass.

    The last assertion is the other half of deriving *before* the request is hashed: a retry that
    states the outcome the first attempt left implicit is the same command, answered from the ledger,
    rather than a `RE005` conflict about two bodies that mean one thing.
    """
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)
    reference = hospital_ref()
    response = configured_demo.booking(
        workflow_id,
        approval_id=approval_id,
        version=7,
        outcome=None,
        external_ref=reference,
        command_id="derived-outcome",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "BOOKED"
    assert body["booking"]["state"] == "CONFIRMED"
    assert body["booking"]["external_ref"] == reference
    rows = configured_demo.bookings(workflow_id)
    assert [(row["state"], row["external_ref"]) for row in rows] == [("CONFIRMED", reference)]

    restated = configured_demo.booking(
        workflow_id,
        approval_id=approval_id,
        version=7,
        outcome="confirmed",
        external_ref=reference,
        command_id="derived-outcome",
    )
    assert restated.status_code == 200, restated.text
    assert restated.json()["replayed"] is True
    assert restated.json()["booking"] == body["booking"]
    assert len(configured_demo.bookings(workflow_id)) == 1


def test_a_booking_with_no_outcome_and_no_reference_is_recorded_unverified(
    configured_demo: Demo,
) -> None:
    """The other derived branch: silence about the hospital is not a claim about the hospital.

    This is the bare POST — a caller saying "record this booking" and nothing about what the hospital
    answered. Reading it as `confirmed` would be inventing the hospital's word, and it is the reading
    the database refuses; `unverified` is the truthful one, which §7 resolves by reconciliation. The
    row is `REQUESTED` with no reference at all, and the workflow lands in `BOOKING_UNVERIFIED` with
    the reconciliation card on it, so the branch is asserted where it can be acted on rather than only
    as a status code.
    """
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)
    response = configured_demo.booking(
        workflow_id, approval_id=approval_id, version=7, outcome=None
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "BOOKING_UNVERIFIED"
    assert body["booking"]["state"] == "REQUESTED"
    assert body["booking"]["external_ref"] is None
    rows = configured_demo.bookings(workflow_id)
    assert [(row["state"], row["external_ref"]) for row in rows] == [("REQUESTED", None)]
    card = configured_demo.snapshot(workflow_id).json()["components"][0]
    assert card["action"] == "scheduling:lookup_request"


def test_an_outcome_the_contract_does_not_have_is_refused(configured_demo: Demo) -> None:
    """`outcome` is not free text: there are two, and a third would be a guess the gateway records."""
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)
    response = configured_demo.booking(
        workflow_id, approval_id=approval_id, version=7, outcome="probably_fine"
    )
    assert response.status_code == 422
    assert "never by guessing" in response.json()["error"]["message"]
    assert configured_demo.bookings(workflow_id) == []


# ---------------------------------------------------------------------------
# Asked once
# ---------------------------------------------------------------------------


def test_retrying_the_booking_command_returns_the_first_booking(configured_demo: Demo) -> None:
    """The client's retry, byte for byte, with one row at the end.

    The retry presents the version it originally read (7), which is stale by now — the workflow is at
    8 — and it is answered *anyway*, because the ledger is consulted before the version: a completed
    command is a fact about a request that already happened. Answering a stale retry with a conflict
    would tell the client its booking had not been made.
    """
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)
    first = configured_demo.booking(
        workflow_id, approval_id=approval_id, version=7, command_id="booking-once"
    )
    assert first.status_code == 200
    retry = configured_demo.booking(
        workflow_id, approval_id=approval_id, version=7, command_id="booking-once"
    )
    assert retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert retry.json()["booking"] == first.json()["booking"]
    assert len(configured_demo.bookings(workflow_id)) == 1
    assert configured_demo.snapshot(workflow_id).json()["state_version"] == 8


@pytest.mark.memory_only  # 014 refuses the move back; see test_postgres_control_state.py
def test_a_second_booking_with_the_same_approval_is_refused_as_re004(configured_demo: Demo) -> None:
    """One approval authorises exactly one booking — even with a fresh command id and a legal state.

    The route back to `BOOKING_IN_FLIGHT` exists only from `BOOKING_UNVERIFIED` (§7's reconciliation
    path), so that is how a second attempt can be made at all: the state machine itself is what stops
    the obvious version of this. The refusal is `RE004`, the approval's rule, and it is *not* the
    policy table's — the table permits this action in this state, which is what makes the approval gate
    the thing being tested.

    Memory profile only, for a reason that is not about the booking write: the route back to
    `BOOKING_IN_FLIGHT` is itself refused in the postgres profile, by 014's consent guard, because
    the approval the first booking spent is not live for the version the workflow is now on. The
    schema is right — consent was given for the state the first attempt was made in — and the memory
    profile has no liveness check at that transition to disagree with;
    `test_postgres_control_state.py::test_reconciliation_needs_a_live_approval_and_a_spent_one_is_not_it`
    records that from the database's side. The refusal the *database* makes for a second booking on
    one approval is asserted separately, in
    `test_one_approval_authorises_one_booking_and_the_database_is_what_says_so`.
    """
    workflow_id, body = booked(configured_demo, outcome=BOOKING_OUTCOME_UNVERIFIED)
    approval_id = body["approval_id"]
    moved = configured_demo.move(workflow_id, version=8, to_state=WorkflowState.BOOKING_IN_FLIGHT)
    assert moved.status_code == 200, moved.text

    second = configured_demo.booking(
        workflow_id, approval_id=approval_id, version=9, command_id="booking-again"
    )
    assert second.status_code == 403
    error = second.json()["error"]
    assert error["sqlstate"] == "RE004"
    assert "one approval authorises exactly one booking" in error["message"]
    assert len(configured_demo.bookings(workflow_id)) == 1
    # Nothing was half-done: the refusal came before the state advance, so the workflow is still
    # where the reconciliation left it.
    assert configured_demo.snapshot(workflow_id).json()["state"] == "BOOKING_IN_FLIGHT"


def test_replaying_the_stream_creates_no_second_booking(configured_demo: Demo) -> None:
    """The claim this phase is measured on, asserted on the row count.

    A cursor selects events that were *already produced*: the stream is a reader over the log, and the
    log holds no commands. So replaying it, from an id, from the beginning, and twice in a row, must
    leave the row count at one — and leave `state_version` where the booking put it, which is the part
    a row count alone would not catch if a replay re-ran a transition.
    """
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)
    assert (
        configured_demo.booking(workflow_id, approval_id=approval_id, version=7).status_code == 200
    )
    after_booking = configured_demo.snapshot(workflow_id).json()
    rows_after_booking = configured_demo.bookings(workflow_id)
    assert (len(rows_after_booking), after_booking["state_version"]) == (1, 8)

    original = configured_demo.events(workflow_id)
    assert original.status_code == 200
    assert [event for event in original.text.split("\n\n") if event.strip()]

    # Replay from the last id, from the first id, from an id that does not exist, and from the top.
    first_id = next(
        line[len("id: ") :] for line in original.text.splitlines() if line.startswith("id: ")
    )
    for cursor in (first_id, "0" * 32, None):
        replayed = configured_demo.events(workflow_id, last_event_id=cursor)
        assert replayed.status_code == 200
    assert configured_demo.events(workflow_id).text == original.text

    assert len(configured_demo.bookings(workflow_id)) == 1
    after_replays = configured_demo.snapshot(workflow_id).json()
    assert after_replays["state_version"] == after_booking["state_version"]
    assert after_replays["state"] == "BOOKED"


def test_the_receipt_arrives_on_the_stream_while_the_snapshot_reports_no_open_question(
    configured_demo: Demo,
) -> None:
    """Where each surface keeps what, asserted so the two cannot drift into disagreement.

    The snapshot includes the display-only receipt, as does the stream. Reconnecting from the
    snapshot cursor must not remove the only evidence the patient has of a confirmed booking.
    """
    workflow_id, body = booked(configured_demo)
    # A BOOKED workflow has a display-only receipt in its current surface.
    snapshot = configured_demo.snapshot(workflow_id).json()
    assert [card["component"] for card in snapshot["components"]] == ["AppointmentReceipt"]
    assert snapshot["components"][0]["action"] is None

    response = configured_demo.events(workflow_id)
    assert response.status_code == 200
    events = [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    receipts = [
        component
        for event in events
        if event["type"] == "surface_update"
        for component in event["payload"]["components"]
        if component["component"] == "AppointmentReceipt"
    ]
    assert len(receipts) == 1
    assert receipts[0]["props"]["appointment"]["booking_id"] == body["booking"]["booking_id"]


def test_the_workflow_holds_the_approved_payload_and_its_hash(configured_demo: Demo) -> None:
    """The row's `payload_hash` and the workflow's own must be the same value from the same encoder."""
    workflow_id, body = booked(configured_demo)
    payload = configured_demo.admin_view(workflow_id).json()["workflow"]["payload"]
    assert payload["booking_payload"] == BOOKING_PAYLOAD
    assert payload[PAYLOAD_HASH_KEY] == body["booking"]["payload_hash"]
    assert payload[PAYLOAD_HASH_KEY] == canonical_booking_payload_hash(BOOKING_PAYLOAD)
