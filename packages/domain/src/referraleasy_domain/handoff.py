"""Human handoff (§11): an operator takes a ticket, and automation stands down while they hold it.

The interesting part of this module is what it refuses to do. `handoff_resolve` takes the state the
workflow is returned to, and that state is checked against an allow-list **in the database** — the
pre-booking states are absent from it, so an operator can send a workflow back to search, readiness
or recovery but cannot park it on the threshold of a booking.

Resolving a ticket is not booking consent. That is the whole reason the list exists, and it is
enforced where it cannot be argued with. So there is deliberately no copy of the list here, and no
local validation of `workflow_state`: a list in Python would be a second place to update, and the
day it drifts it would permit a transition the database would also have permitted — which is
precisely the failure mode the allow-list was written to prevent. A refused state surfaces as
:class:`~referraleasy_domain.errors.ApprovalRefusedError` (RE004), the same class as any other
consent-boundary refusal, because that is what it is.

Holding a ticket (#11, §3) also suspends automation: `handoff_claim` sets `human_owner_id` on the
workflow, and from that moment the workflow guard refuses every non-human writer. The two can never
write concurrently, and nothing in the worker code has to remember to check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from referraleasy_domain.workflow import (
    ConnectionLike,
    fetch_all,
    fetch_one,
    required_row,
    to_text,
)

__all__ = [
    "DEFAULT_CLAIM_LEASE_SECONDS",
    "DEFAULT_RESOLUTION_STATE",
    "Handoff",
    "HandoffStore",
]

#: `handoff_claim`'s own default. A ticket is held for fifteen minutes, not fifteen seconds: the
#: operator is a human reading a record, not a worker doing a unit of work.
DEFAULT_CLAIM_LEASE_SECONDS: Final[int] = 900

#: `handoff_resolve`'s own default, and the safe place to resume from.
DEFAULT_RESOLUTION_STATE: Final[str] = "PROVIDER_MATCHING"


@dataclass(frozen=True, slots=True)
class Handoff:
    """One `handoff` row — a ticket, and who currently holds it."""

    id: UUID
    workflow_id: UUID
    ticket_ref: str
    reason: str
    state: str
    claim_generation: int
    claimed_by: str | None
    claimed_at: datetime | None
    claim_expires_at: datetime | None
    resolved_at: datetime | None
    resolution: str | None
    resolution_note: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Handoff:
        return cls(
            id=row["id"],
            workflow_id=row["workflow_id"],
            ticket_ref=row["ticket_ref"],
            reason=row["reason"],
            state=row["state"],
            claim_generation=row["claim_generation"],
            claimed_by=row["claimed_by"],
            claimed_at=row["claimed_at"],
            claim_expires_at=row["claim_expires_at"],
            resolved_at=row["resolved_at"],
            resolution=row["resolution"],
            resolution_note=row["resolution_note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @property
    def is_open(self) -> bool:
        """Whether the ticket is still claimable. Informational, like `Workflow.is_human_owned`."""
        return self.state in ("open", "claimed")


class HandoffStore:
    """Typed access to `handoff_claim` / `handoff_release` / `handoff_resolve` over one connection."""

    def __init__(self, conn: ConnectionLike) -> None:
        self._conn = conn

    def open_tickets(self, *, limit: int = 200) -> list[Handoff]:
        """The operator queue: tickets still open or claimed, oldest first.

        **A plain SELECT, and the only read in this class.** Everything else goes through a database
        function, because everything else *changes* something and the rules live in SQL. This one
        takes no decision, so there is no rule for a function to hold.

        Oldest first because the operator's question is which ticket has waited longest; `limit` is a
        ceiling rather than a page size, and a full result means there may be more.

        **Claim expiry is not applied here.** A ticket whose lease has lapsed is still `claimed` until
        something reclaims it, and this read reports the row rather than computing a different truth
        from `claim_expires_at` — a queue that hid an expiry would disagree with the claim that is
        about to succeed. `claim_expires_at` travels instead, so a caller can show it.

        Two callers share this and neither owns it: the workers' `queued_handoffs` (the queue as an
        operator sees it) and the gateway's admin read (the same queue on a dashboard). Two copies of
        this statement would be two answers to "what is in the queue", which §11 makes a single
        question.
        """
        rows = fetch_all(
            self._conn,
            "SELECT * FROM handoff WHERE state IN ('open', 'claimed') "
            "ORDER BY created_at ASC LIMIT %s",
            (limit,),
        )
        return [Handoff.from_row(row) for row in rows]

    def claim(
        self,
        handoff_id: UUID | str,
        operator_id: str,
        *,
        expected_generation: int,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> Handoff:
        """Take a ticket, and with it human ownership of the workflow.

        `expected_generation` is the `claim_generation` the operator read off the ticket. Two
        operators claiming the same generation: exactly one `UPDATE` matches, and the other is
        refused with :class:`~referraleasy_domain.errors.StaleOwnershipError` (RE001). An expired
        claim is reclaimable; a live one is not, unless it is the same operator's.
        """
        row = fetch_one(
            self._conn,
            "SELECT * FROM handoff_claim(%s, %s, %s, %s)",
            (handoff_id, operator_id, expected_generation, lease_seconds),
        )
        return Handoff.from_row(required_row(row, "handoff_claim"))

    def release(
        self,
        handoff_id: UUID | str,
        operator_id: str,
        *,
        expected_generation: int,
    ) -> Handoff:
        """Put the ticket back and clear human ownership, so automation resumes.

        Only the operator holding the claim can release it, and only at the generation they hold;
        anything else is :class:`~referraleasy_domain.errors.StaleOwnershipError` (RE001).
        """
        row = fetch_one(
            self._conn,
            "SELECT * FROM handoff_release(%s, %s, %s)",
            (handoff_id, operator_id, expected_generation),
        )
        return Handoff.from_row(required_row(row, "handoff_release"))

    def resolve(
        self,
        handoff_id: UUID | str,
        operator_id: str,
        *,
        expected_generation: int,
        resolution: str,
        workflow_state: str = DEFAULT_RESOLUTION_STATE,
        note: str | None = None,
    ) -> Handoff:
        """Close the ticket, release human ownership, and return the workflow to automation.

        `workflow_state` is bounded by the database's allow-list. Passing a state the operator may
        not produce — any pre-booking state, and in particular anything that would amount to
        consent — raises :class:`~referraleasy_domain.errors.ApprovalRefusedError` (RE004). A
        workflow that has moved on, or a claim held by someone else, raises
        :class:`~referraleasy_domain.errors.StaleOwnershipError` (RE001).

        A contract `WorkflowState` may be passed directly: it is a `StrEnum`, so it *is* a `str`.

        Note what this method cannot do: there is no argument here that creates, satisfies or
        bypasses an approval. Resolving a ticket and granting booking consent are different acts by
        different parties, and the second one is only reachable through the patient-side approval.
        """
        row = fetch_one(
            self._conn,
            "SELECT * FROM handoff_resolve(%s, %s, %s, %s, %s, %s)",
            (
                handoff_id,
                operator_id,
                expected_generation,
                resolution,
                to_text(workflow_state),
                note,
            ),
        )
        return Handoff.from_row(required_row(row, "handoff_resolve"))
