"""The durable booking record, before a workflow may claim to be `BOOKED` (§7, §9).

`workflow_booking_consent_guard` — 007's, restated over the new vocabulary in 014 — refuses to move
a workflow into `BOOKED` unless a `booking_request` row for that workflow is `confirmed`. The record
is therefore a *precondition* of the state rather than a report about it, and the step that advances
to `BOOKED` has to be the step that produces it. Without that, the booked branch is a branch that
cannot run: the facility says the appointment exists, and the workflow is stuck one state short of
saying so.

Two facts about the schema shape what is here.

* **The row is created by the database, from an approval.** `booking_request_create(approval_id,
  requested_by, external_ref, payload)` copies the binding columns — workflow, patient, payload hash
  — out of the approval itself, so this module never passes a hash and cannot disagree with the
  consent it is spending. Consuming the approval is part of that insert
  (`booking_request_consume_approval`), which is what makes "one booking per approval, ever" true
  even across replicas.
* **There is no `booking_request_confirm()`.** `pending → confirmed` is a plain statement in the
  schema, guarded by `booking_request_guard` (a legal edge, a `confirmed_at`, a non-empty
  `external_ref`). This module writes that one statement, in one place, and says so out loud rather
  than burying it: it is the only raw `booking_request` statement in this package, and the
  alternative — advancing with no record — is refused by the database with `RE004`.

The confirm is a compare-and-set on `state = 'pending'`, so two replicas confirming the same booking
converge instead of racing: the second one matches no row, and :func:`ensure_confirmed_booking` then
re-reads and reports what the row actually says. Confirming a booking twice is not a second booking;
the table's own `pending → confirmed` edge is terminal and `UNIQUE(approval_id)` is what makes a
second one impossible.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from referraleasy_domain import Workflow, WorkflowStore
from referraleasy_domain.errors import ApprovalRefusedError
from referraleasy_workers.db import ConnectionLike, fetch_one

__all__ = [
    "BOOKING_REQUEST_CONFIRM_STATEMENT",
    "BookingRecordOutcome",
    "ensure_confirmed_booking",
    "existing_booking_request",
    "live_approval_id",
]

#: The statement, named once. It is the schema's `pending -> confirmed` edge, written out, because
#: the schema exposes no function for it; `booking_request_guard` still has the final say.
BOOKING_REQUEST_CONFIRM_STATEMENT = """
        UPDATE booking_request
           SET state = 'confirmed', confirmed_at = now(), external_ref = %s
         WHERE id = %s
           AND workflow_id = %s
           AND state = 'pending'
        RETURNING *
"""


@dataclass(frozen=True, slots=True)
class BookingRecordOutcome:
    """What the durable record says now, and whether the workflow may be called booked."""

    booking_request_id: UUID | None
    #: True when this call created the row (as opposed to finding one already there).
    recorded: bool
    #: True when the row is `confirmed` — the only state `BOOKED` may be entered from.
    confirmed: bool
    state: str | None = None
    detail: str = ""

    @property
    def agrees(self) -> bool:
        """Whether the record and the facility's answer tell the same story."""
        return self.confirmed

    @property
    def patient_message(self) -> str:
        if self.confirmed:
            return "Your appointment with the hospital is confirmed."
        return (
            "The hospital reports an appointment for you, and our own record of it does not agree. "
            "A person on our team is checking, and we will tell you what we find."
        )


def live_approval_id(conn: ConnectionLike, workflow_id: UUID | str) -> UUID | None:
    """The approval this workflow may still spend, or ``None`` when there is not one.

    Unconsumed and unexpired, most recent first. The *database* is what decides whether an approval is
    live at insert time (`booking_request_consume_approval` reads the same two columns); this read
    exists so the caller can say "there is no consent to spend" instead of receiving a foreign-key
    failure from a statement that had already chosen an approval.
    """
    row = fetch_one(
        conn,
        """
        SELECT id
          FROM approval
         WHERE workflow_id = %s
           AND consumed_at IS NULL
           AND expires_at > now()
         ORDER BY approved_at DESC
         LIMIT 1
        """,
        (workflow_id,),
    )
    return None if row is None else row["id"]


def existing_booking_request(
    conn: ConnectionLike, workflow_id: UUID | str
) -> Mapping[str, Any] | None:
    """The workflow's booking_request row, if it has one. One per workflow in practice."""
    return fetch_one(
        conn,
        "SELECT * FROM booking_request WHERE workflow_id = %s ORDER BY requested_at DESC LIMIT 1",
        (workflow_id,),
    )


def ensure_confirmed_booking(
    conn: ConnectionLike,
    *,
    workflow: Workflow,
    external_ref: str | None,
    requested_by: str,
) -> BookingRecordOutcome:
    """Record the booking against the live approval and confirm it, idempotently.

    Called only where the facility has answered that an appointment exists. The three cases it
    handles are all real: no row yet (this replica confirms it), a `pending` row (another replica
    recorded it and did not get to confirm), and an already `confirmed` row (nothing to do, and the
    caller may advance). A `failed` or `cancelled` row is reported rather than coerced — there is no
    edge from either of those to `confirmed`, and inventing one by writing the row directly would be
    the double-booking bug through a side door.
    """
    row = existing_booking_request(conn, workflow.id)
    recorded = False
    if row is None:
        approval_id = live_approval_id(conn, workflow.id)
        if approval_id is None:
            raise ApprovalRefusedError(
                f"booking for workflow {workflow.id} cannot be recorded: no unconsumed, unexpired "
                "approval is bound to it. Consent is the only thing that authorises this write "
                "(007), and it is not this module's to assume.",
                sqlstate="RE004",
            )
        created = WorkflowStore(conn).record_booking(
            approval_id,
            requested_by,
            external_ref=external_ref,
            payload={"facility_booking_request_id": external_ref} if external_ref else None,
        )
        row = {
            "id": created.id,
            "state": created.state,
            "external_ref": created.external_ref,
        }
        recorded = True

    if row["state"] == "confirmed":
        return BookingRecordOutcome(
            booking_request_id=row["id"],
            recorded=recorded,
            confirmed=True,
            state="confirmed",
            detail="the booking is recorded and confirmed",
        )
    if row["state"] != "pending":
        return BookingRecordOutcome(
            booking_request_id=row["id"],
            recorded=recorded,
            confirmed=False,
            state=str(row["state"]),
            detail=(
                f"booking_request {row['id']} is {row['state']}; a confirmed booking cannot be "
                "claimed from it and the row may not be re-pointed (booking_request_guard)"
            ),
        )

    confirmed = fetch_one(
        conn,
        BOOKING_REQUEST_CONFIRM_STATEMENT,
        (external_ref or str(row["id"]), row["id"], workflow.id),
    )
    if confirmed is not None:
        return BookingRecordOutcome(
            booking_request_id=confirmed["id"],
            recorded=recorded,
            confirmed=True,
            state=confirmed["state"],
            detail="the booking was recorded and confirmed",
        )

    # No row matched: another writer got there first. Re-read rather than assume, because the answer
    # decides whether the caller may advance.
    current = fetch_one(conn, "SELECT * FROM booking_request WHERE id = %s", (row["id"],))
    state = None if current is None else str(current["state"])
    return BookingRecordOutcome(
        booking_request_id=row["id"],
        recorded=recorded,
        confirmed=state == "confirmed",
        state=state,
        detail=f"the booking record is {state} after a concurrent confirmation",
    )
