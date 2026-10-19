"""The human side of the workflow, and the schema's shape of it (§3, §11).

HITL happens when the system has run out of honest options, and §11 is precise about how: a fixed set
of *triggers*, a summary the patient sees and consents to before anything is shared, a queue that two
operators cannot both own, and a resolution that is audited and that does **not** grant booking
consent. This module is the worker's half of that. The operator's console is elsewhere; what is here
is the machinery both halves agree on.

**Consent gates the queue, and the row proves it.** `consent_given_at` is checked before a `handoff`
row is written, and a handoff with no consent raises
:class:`~referraleasy_workers.errors.ConsentRequiredError` rather than being written in a "not yet
queued" state. The reason is that `handoff` has no such state: its ladder is `open → claimed →
resolved`, and an `open` row *is* in the queue. Writing one and relying on a flag to keep it out of
the queue would make "no consent, no share" a convention; not writing it makes it a fact.

**The columns that do not exist.** The schema has no `patient_visible_summary`, no `consent_given_at`,
no `reason_detail`, no `added_alternatives` and no `operator_notes`. This is not an oversight to work
around silently — it is the thing to be explicit about, because each piece has to live *somewhere* and
a reader is entitled to know where:

============================  ==========================================================
piece                         where it lives, and when
============================  ==========================================================
patient_visible_summary       `workflow.payload["handoff"]`, written while automation owns
consent_given_at              the workflow (:func:`~referraleasy_workers.checkpoint.write_payload`)
reason_detail, sources        — same write, and refused with `RE003` once a human owns it
ticket reason                 `handoff.reason`, the reason *class* verbatim
ticket ref                    `handoff.ticket_ref`, generated here
added_alternatives            the operator's draft, flushed into `handoff.resolution_note`
operator_notes                at resolution (the only free-text column the resolve path writes)
return_state                  an argument of `handoff_resolve`, allow-listed twice
final_approval_still_required  a constant: `True`, always (§7/§11)
============================  ==========================================================

Two consequences of that table are worth stating plainly rather than discovering later. An operator
cannot write alternatives into the payload *while they hold the ticket*, because the payload write is
the automation write and automation is suspended — that refusal (`RE003`) is the tripwire, and it is
tested as one. And the alternatives therefore reach the durable record at resolution, not before,
which is when a human stops holding the workflow and automation may write to it again.

**Nobody is pretending.** There is no live-agent channel in this system. When a ticket is opened and
the operator directory has somebody to name, the patient gets the ticket, the contact details, and a
sentence that says a person will pick it up — never a claim that one is already there.
:attr:`OperatorResponse.live_operator_reached` is `False` by construction, and the tests assert on the
sentence as well as the flag.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

from referraleasy_contracts.generated.common import HandoffReasonClass
from referraleasy_contracts.generated.patient import AlternativeOption
from referraleasy_contracts.generated.workflow import (
    Handoff,
    HandoffStatus,
    HumanQueueTicket,
    TicketStatus,
)
from referraleasy_domain import Handoff as DomainHandoff
from referraleasy_domain import HandoffStore
from referraleasy_telemetry import TelemetryEmitter
from referraleasy_workers.checkpoint import HANDOFF_KEY
from referraleasy_workers.db import ConnectionLike, fetch_one, required_row
from referraleasy_workers.errors import (
    ApprovalRefusedError,
    ConsentRequiredError,
    StaleOwnershipError,
    WorkerError,
)

__all__ = [
    "DEFAULT_CLAIM_LEASE_SECONDS",
    "HITL_TRIGGERS",
    "OPERATOR_RETURN_STATES",
    "DirectoryContact",
    "HandoffRecord",
    "HandoffRequest",
    "HandoffTrigger",
    "HumanQueue",
    "OperatorDirectory",
    "OperatorResponse",
    "ResolutionOutcome",
    "StaticDirectory",
    "TicketDraft",
    "TicketView",
    "default_ticket_ref",
]

#: The lease an operator claim carries. Long enough to work a ticket, short enough that walking away
#: does not freeze a workflow's automation for a day.
DEFAULT_CLAIM_LEASE_SECONDS: Final[int] = 900

#: The states `handoff_resolve` accepts, in the same order the SQL allow-list uses. Restated here so
#: a caller is refused *before* a statement runs, with a message that names the rule.
OPERATOR_RETURN_STATES: Final[tuple[str, ...]] = (
    "INTAKE",
    "COVERAGE_CHECK",
    "PROVIDER_MATCHING",
    "READINESS_CHECK",
    "AWAITING_SLOT_CHOICE",
    "RECOVERY",
    "FAILED",
    "CANCELLED",
    "ABANDONED",
)
#: The states that are *not* return states, and the reason the allow-list is what it is. Kept as a
#: named set because "no pre-booking state is reachable from here" is a rule about a class of states,
#: not about a list of strings.
BOOKING_CONSENT_STATES: Final[frozenset[str]] = frozenset(
    {"AWAITING_FINAL_APPROVAL", "BOOKING_IN_FLIGHT", "BOOKED", "BOOKING_UNVERIFIED"}
)


@dataclass(frozen=True, slots=True)
class HandoffTrigger:
    """One condition that starts a human handoff, exactly as §11 lists them."""

    reason_class: HandoffReasonClass
    when: str


#: §11's triggers. The list is the *whole* list: a test asserts that every reason class the system
#: can emit is here, so a new reason cannot be added to the vocabulary while being forgotten as a
#: trigger — and, just as importantly, so nothing is escalated that §11 did not ask for.
HITL_TRIGGERS: Final[tuple[HandoffTrigger, ...]] = (
    HandoffTrigger(
        HandoffReasonClass.technical_outage,
        "an upstream dependency refused or failed after the retry budget was spent (§7)",
    ),
    HandoffTrigger(
        HandoffReasonClass.no_match,
        "no provider, hospital or slot satisfies the constraints — including a slot conflict that "
        "leaves no acceptable alternative",
    ),
    HandoffTrigger(
        HandoffReasonClass.missing_document,
        "a referral requirement cannot be satisfied from what the patient has supplied",
    ),
    HandoffTrigger(
        HandoffReasonClass.conflicting_sources,
        "two sources disagree, or a reschedule left the replacement booked and the original "
        "uncancelled (§7)",
    ),
    HandoffTrigger(
        HandoffReasonClass.prolonged_unknown_booking,
        "a booking outcome stays unknown after the reconciliation budget is spent (§7)",
    ),
    HandoffTrigger(
        HandoffReasonClass.patient_requested,
        "the patient asked for a person",
    ),
    HandoffTrigger(
        HandoffReasonClass.no_reliable_recovery,
        "recovery has no action the state machine permits, or the remaining options are all unsafe",
    ),
)


def default_ticket_ref(now: datetime | None = None) -> str:
    """A short, human-quotable reference. Two tickets opened the same second still differ."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d")
    return f"HND-{stamp}-{uuid4().hex[:6].upper()}"


@dataclass(frozen=True, slots=True)
class HandoffRequest:
    """Everything §11 asks for *before* a handoff exists, whether or not it may be queued yet."""

    workflow_id: UUID
    reason_class: HandoffReasonClass
    #: The exact text the patient reviewed. Not a summary of a summary: this is the text that is
    #: shared, and a person downstream reads what the patient read.
    patient_visible_summary: str
    reason_detail: str = ""
    #: Lines of evidence a human will need: which lookups were made, which sources disagreed, which
    #: documents are missing. Free-form because the *set* of evidence depends on the trigger.
    sources: tuple[str, ...] = ()
    consent_given_at: datetime | None = None
    response_deadline_at: datetime | None = None
    ticket_ref: str | None = None

    @property
    def has_consent(self) -> bool:
        return self.consent_given_at is not None

    def require_consent(self) -> None:
        """Refuse to queue without consent — the gate, at the one place a row could be written."""
        if not self.has_consent:
            raise ConsentRequiredError(
                "refused: the patient has not consented to share this summary. "
                "The summary is shown to the patient first, and an un-consented handoff is not queued.",
            )
        if not self.patient_visible_summary.strip():
            raise ConsentRequiredError(
                "refused: there is no summary for the patient to consent to. "
                "Consent to an empty document is not consent.",
            )

    def payload(self) -> dict[str, Any]:
        """The staging record, written under `workflow.payload["handoff"]`.

        Only the fields the schema has no home for. The reason class and the ticket reference go into
        the table itself and are not duplicated here.
        """
        return {
            "reason_class": self.reason_class.value,
            "reason_detail": self.reason_detail,
            "patient_visible_summary": self.patient_visible_summary,
            "consent_given_at": None
            if self.consent_given_at is None
            else self.consent_given_at.isoformat(),
            "response_deadline_at": (
                None if self.response_deadline_at is None else self.response_deadline_at.isoformat()
            ),
            "sources": list(self.sources),
            "ticket_ref": self.ticket_ref or default_ticket_ref(),
        }

    def handoff(self, *, handoff_id: UUID, status: HandoffStatus, created_at: datetime) -> Handoff:
        """The contract object, built only from what was actually recorded."""
        return Handoff(
            handoff_id=handoff_id,
            workflow_id=self.workflow_id,
            reason_class=self.reason_class,
            reason_detail=self.reason_detail or None,
            patient_visible_summary=self.patient_visible_summary,
            consent_given_at=self.consent_given_at,
            response_deadline_at=self.response_deadline_at,
            status=status,
            created_at=created_at,
        )


#: The `handoff.state` ladder maps onto the contract's, with two of the contract's six states
#: unreachable *by construction*: `created` (a handoff exists only once it is queued, because consent
#: gates the row) and `expired` (expiry is a cancellation here — there is no expiry column).
_STATUS_BY_STATE: Final[dict[str, HandoffStatus]] = {
    "open": HandoffStatus.queued,
    "claimed": HandoffStatus.claimed,
    "resolved": HandoffStatus.resolved,
    "cancelled": HandoffStatus.withdrawn,
}


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    """One `handoff` row, with the mapping to the contract's status made explicit."""

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
    def from_row(cls, row: Mapping[str, Any]) -> HandoffRecord:
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

    @classmethod
    def from_domain(cls, handoff: DomainHandoff) -> HandoffRecord:
        """The same row through the *domain's* typed accessor, field for field.

        The domain owns `handoff_claim`, `handoff_release` and `handoff_resolve`
        (`HandoffStore`); this package's operator-side methods call it rather than issuing a second
        copy of the same three functions, and this is the one hop between the two shapes. The field
        lists are deliberately identical — a column added to one and not the other would fail here,
        loudly, which is what a mapping hop is for.
        """
        return cls(
            **{field.name: getattr(handoff, field.name) for field in dataclasses.fields(handoff)}
        )

    @property
    def status(self) -> HandoffStatus:
        return _STATUS_BY_STATE[self.state]

    @property
    def reason_class(self) -> HandoffReasonClass:
        return HandoffReasonClass(self.reason)

    @property
    def is_live(self) -> bool:
        """Whether this handoff still holds the workflow's human ownership."""
        return self.state in ("open", "claimed")

    @property
    def holds_human_ownership(self) -> bool:
        return self.state == "claimed"


@dataclass(frozen=True, slots=True)
class DirectoryContact:
    """Someone to name when no live operator is reachable. Never a promise, only a route."""

    name: str
    channel: str
    detail: str

    def line(self) -> str:
        return f"{self.name} — {self.channel} ({self.detail})"


class OperatorDirectory(Protocol):
    """Where the contact details come from. A protocol so the tests name nobody real."""

    def contact_for(
        self, *, reason_class: HandoffReasonClass, at: datetime
    ) -> DirectoryContact | None:  # pragma: no cover - protocol
        raise NotImplementedError


class StaticDirectory:
    """A directory from a mapping — the shape a configuration-driven one would have."""

    def __init__(
        self, contacts: Mapping[HandoffReasonClass | str, DirectoryContact] | None = None
    ) -> None:
        self._contacts = {str(key): value for key, value in (contacts or {}).items()}

    def contact_for(
        self, *, reason_class: HandoffReasonClass, at: datetime
    ) -> DirectoryContact | None:
        del at  # a real directory would use the hour; this one is deliberately a fixture
        return self._contacts.get(str(reason_class.value))


@dataclass(frozen=True, slots=True)
class TicketDraft:
    """What an operator has added to a ticket so far, held until resolution can store it."""

    handoff_id: UUID
    operator_id: str
    alternatives: tuple[AlternativeOption, ...] = ()
    notes: tuple[str, ...] = ()

    def to_note(self) -> str:
        """The free text flushed into `handoff.resolution_note` at resolution."""
        parts: list[str] = []
        if self.alternatives:
            parts.append(
                "alternatives: "
                + json.dumps(
                    [option.model_dump(mode="json") for option in self.alternatives], sort_keys=True
                )
            )
        if self.notes:
            parts.append("notes: " + " | ".join(self.notes))
        return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class TicketView:
    """What an operator sees when they open a ticket: the row, the evidence, their own draft."""

    ticket: HumanQueueTicket
    record: HandoffRecord
    sources: tuple[str, ...] = ()
    patient_visible_summary: str | None = None
    consent_given_at: datetime | None = None
    draft_note: str | None = None

    @property
    def addable(self) -> bool:
        """Whether *somebody* holds this ticket, and so whether additions can be accepted at all."""
        return self.record.claimed_by is not None

    def addable_by(self, operator_id: str) -> bool:
        """Whether *this* operator may add to the ticket. Held means held by one person."""
        return self.record.claimed_by == operator_id and self.record.state == "claimed"

    def source_lines(self) -> tuple[str, ...]:
        """Everything a human gets to look at. One list, so nothing is hidden behind a flag."""
        lines = list(self.sources)
        if self.patient_visible_summary:
            lines.append(f"summary shown to the patient: {self.patient_visible_summary}")
        if self.consent_given_at is not None:
            lines.append(f"consent recorded at: {self.consent_given_at.isoformat()}")
        if self.record.resolution_note:
            lines.append(f"resolution note: {self.record.resolution_note}")
        return tuple(lines)


@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    """What resolving a ticket did — and the one thing it did not do."""

    handoff_id: UUID
    workflow_id: UUID
    return_state: str
    resolution: str
    note: str | None
    final_approval_still_required: bool = True
    #: Constant. Resolution is a statement about the ticket, never an authorisation to book (§7/§11).
    booking_consent_granted: bool = False

    @property
    def patient_message(self) -> str:
        return (
            "A member of our team has finished reviewing this referral. "
            "Nothing is booked yet, and you will still be asked before anything is."
        )


@dataclass(frozen=True, slots=True)
class OperatorResponse:
    """The answer to "is a person available?" — and the honest form of "no"."""

    ticket: HumanQueueTicket
    directory_contact: DirectoryContact | None = None
    #: Always False. There is no live-agent channel in this system, and a flag that could be True
    #: would be a place for one to be pretended.
    live_operator_reached: bool = False
    patient_message: str = ""

    @property
    def is_pending(self) -> bool:
        return self.ticket.status is TicketStatus.pending


def _pending_message(contact: DirectoryContact | None) -> str:
    """The sentence a patient gets when nobody picked up. One template, no promises."""
    if contact is not None:
        return (
            "We have opened a ticket for a member of our team to look at this referral, and nobody "
            "has picked it up yet. You can also reach "
            f"{contact.name} on {contact.channel} ({contact.detail}) about this referral."
        )
    return (
        "We have opened a ticket for a member of our team to look at this referral. "
        "Nobody has picked it up yet, and we will contact you as soon as somebody has."
    )


class HumanQueue:
    """The queue: open, claim, inspect, annotate, resolve. The database owns every race in it."""

    def __init__(
        self,
        conn: ConnectionLike,
        *,
        directory: OperatorDirectory | None = None,
        telemetry: TelemetryEmitter | None = None,
    ) -> None:
        self._conn = conn
        self._directory = directory
        #: §15's HITL measurements, emitted when a ticket closes. Optional because a worker that runs
        #: with no collector must still resolve tickets — and because the two metrics describe a
        #: *human's* time, which is not the platform's (§15 keeps them out of the latency
        #: distributions the §16 alert rules watch).
        self._telemetry = telemetry
        #: An operator's own annotations, held in this process until resolution can store them. The
        #: schema has nowhere else for them (see the module docstring): `added_alternatives` and
        #: `operator_notes` are not columns, and the workflow payload is unwritable while the operator
        #: holds the ticket, because that is the automation write and automation is suspended.
        self._drafts: dict[UUID, TicketDraft] = {}

    # ---------------------------------------------------------------- patient side

    def queue(self, request: HandoffRequest) -> HandoffRecord:
        """Write the handoff row — the act that puts a workflow in the queue.

        Consent is checked first (see the module docstring). The staging record is *not* written here:
        putting it in the payload is an automation write to the workflow, which is
        :func:`~referraleasy_workers.checkpoint.write_payload`'s job while the worker still owns the
        workflow. This method's statement is the row and nothing else, so it can run in the same
        transaction as the advance that moved the workflow to `HUMAN_HANDOFF`.

        The one-live-handoff-per-workflow index refuses a second open row, which is the database's
        answer to "we already escalated this" — not something to catch and paper over.
        """
        request.require_consent()
        payload = request.payload()
        row = fetch_one(
            self._conn,
            """
            INSERT INTO handoff (workflow_id, ticket_ref, reason, state)
            VALUES (%s, %s, %s, 'open')
            RETURNING *
            """,
            (request.workflow_id, payload["ticket_ref"], request.reason_class.value),
        )
        return HandoffRecord.from_row(required_row(row, "handoff insert"))

    def withdraw(self, handoff_id: UUID | str, *, reason: str) -> HandoffRecord:
        """Close an unclaimed handoff for good (§11: withdrawn on decline)."""
        row = fetch_one(
            self._conn,
            """
            UPDATE handoff
               SET state = 'cancelled', resolution = %s, resolution_note = %s
             WHERE id = %s
               AND state = 'open'
            RETURNING *
            """,
            (reason, reason, handoff_id),
        )
        if row is None:
            raise StaleOwnershipError(
                f"handoff {handoff_id} cannot be withdrawn: it is claimed or already closed",
                sqlstate="RE001",
            )
        return HandoffRecord.from_row(row)

    def expire(self, handoff_id: UUID | str, *, now: datetime | None = None) -> HandoffRecord:
        """Close an unclaimed handoff whose response deadline has passed.

        Expiry is a cancellation: the schema has no `expired` state and no deadline column, so the
        deadline is the one staged in the payload and the clock is the caller's. Both are checked —
        expiry is a decision this method makes, not a trigger nobody can look at — and a ticket that
        has no deadline recorded, or whose deadline has not arrived, is refused rather than closed.
        """
        record = HandoffRecord.from_row(
            required_row(
                fetch_one(self._conn, "SELECT * FROM handoff WHERE id = %s", (handoff_id,)),
                "handoff read",
            )
        )
        if record.state != "open":
            raise StaleOwnershipError(
                f"handoff {handoff_id} is {record.state}; only an open ticket can expire",
                sqlstate="RE001",
            )
        staged = self._staged(record.workflow_id)
        deadline = _parse_ts(staged.get("response_deadline_at"))
        if deadline is None:
            raise WorkerError(
                f"handoff {handoff_id} has no response deadline recorded; expiry is not justified"
            )
        moment = now or datetime.now(UTC)
        if moment < deadline:
            raise WorkerError(
                f"handoff {handoff_id} is due {deadline.isoformat()} and cannot expire at {moment.isoformat()}"
            )
        return self.withdraw(
            handoff_id, reason="expired: no operator picked this up before the deadline"
        )

    def request_operator(
        self, handoff_id: UUID | str, *, at: datetime | None = None
    ) -> OperatorResponse:
        """Open the ticket for a person, and say honestly whether one is there.

        A "yes, an agent is with you now" would be a lie in a system with no live channel — and a
        costly one, because the patient would wait instead of calling the number. So the answer is
        always the pending ticket plus, when the directory has one, a contact route.
        """
        record = HandoffRecord.from_row(
            required_row(
                fetch_one(self._conn, "SELECT * FROM handoff WHERE id = %s", (handoff_id,)),
                "handoff read",
            )
        )
        moment = at or datetime.now(UTC)
        contact = (
            None
            if self._directory is None
            else self._directory.contact_for(reason_class=record.reason_class, at=moment)
        )
        ticket = HumanQueueTicket(
            ticket_id=record.id,
            handoff_id=record.id,
            workflow_id=record.workflow_id,
            status=_TICKET_STATUS_BY_STATE[record.state],
            claimed_by=record.claimed_by,
            claimed_at=record.claimed_at,
            claim_generation=record.claim_generation,
            return_state=None,
            # Two facts that travel with the ticket from the moment it exists. A ticket that could
            # arrive without them would be a ticket somebody could resolve into a booking.
            final_approval_still_required=True,
            created_at=record.created_at,
        )
        return OperatorResponse(
            ticket=ticket,
            directory_contact=contact,
            live_operator_reached=False,
            patient_message=_pending_message(contact),
        )

    # ---------------------------------------------------------------- operator side

    def claim(
        self,
        handoff_id: UUID | str,
        *,
        operator_id: str,
        expected_generation: int | None = None,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> HandoffRecord:
        """Take a ticket. A conflicting claim is refused by the compare-and-set, not by us.

        ``expected_generation=None`` reads the current generation first — the operator console's
        "claim" button, where the operator has just looked at the ticket. The read is not what makes
        the claim safe: the `UPDATE`'s `WHERE` is, and a generation that moved between the read and
        the claim loses the race exactly as a stale one should.

        ``handoff_claim`` itself is called through the domain's
        :class:`~referraleasy_domain.handoff.HandoffStore` rather than through a second copy of the
        same SQL here. Two call sites for one function is two places its argument order can drift,
        and the operator console and this worker are already the two callers.
        """
        generation = (
            expected_generation
            if expected_generation is not None
            else self._current_generation(handoff_id)
        )
        handoff = HandoffStore(self._conn).claim(
            handoff_id, operator_id, expected_generation=generation, lease_seconds=lease_seconds
        )
        return HandoffRecord.from_domain(handoff)

    def release(
        self,
        handoff_id: UUID | str,
        *,
        operator_id: str,
        expected_generation: int | None = None,
    ) -> HandoffRecord:
        """Give a ticket back without resolving it. Automation resumes with the workflow.

        ``handoff_release`` takes the generation as well as the owner — releasing is a compare-and-set
        too, so an operator whose claim was taken over cannot hand back a ticket that is no longer
        theirs. Like :meth:`claim`, ``None`` reads the current generation first; the `WHERE` clause is
        still what decides.
        """
        generation = (
            expected_generation
            if expected_generation is not None
            else self._current_generation(handoff_id)
        )
        handoff = HandoffStore(self._conn).release(
            handoff_id, operator_id, expected_generation=generation
        )
        return HandoffRecord.from_domain(handoff)

    def view(self, handoff_id: UUID | str, *, operator_id: str | None = None) -> TicketView:
        """Open a ticket: the row, the staged evidence, and the operator's own annotations."""
        record = HandoffRecord.from_row(
            required_row(
                fetch_one(self._conn, "SELECT * FROM handoff WHERE id = %s", (handoff_id,)),
                "handoff read",
            )
        )
        staged = self._staged(record.workflow_id)
        draft = self._drafts.get(record.id)
        return TicketView(
            ticket=HumanQueueTicket(
                ticket_id=record.id,
                handoff_id=record.id,
                workflow_id=record.workflow_id,
                status=_TICKET_STATUS_BY_STATE[record.state],
                claimed_by=record.claimed_by,
                claimed_at=record.claimed_at,
                claim_generation=record.claim_generation,
                added_alternatives=None if draft is None else list(draft.alternatives) or None,
                operator_notes=None
                if draft is None or not draft.notes
                else " | ".join(draft.notes),
                return_state=None,
                final_approval_still_required=True,
                created_at=record.created_at,
            ),
            record=record,
            sources=tuple(staged.get("sources") or ()),
            patient_visible_summary=staged.get("patient_visible_summary"),
            consent_given_at=_parse_ts(staged.get("consent_given_at")),
            draft_note=None if draft is None else (draft.to_note() or None),
        )

    def add_alternatives(
        self, handoff_id: UUID | str, *, operator_id: str, options: Sequence[AlternativeOption]
    ) -> TicketDraft:
        """Record alternatives the operator found. They reach the durable record at resolution.

        Not written to the workflow payload here, and the refusal is the point: while an operator
        holds the ticket, automation writes to that workflow are suspended (`RE003`), and this process
        is automation. A worker that wrote there anyway would be impersonating the operator, which is
        the same mistake as resolving a ticket into a booking, one step earlier.
        """
        record = self._require_holder(handoff_id, operator_id)
        draft = self._drafts.get(
            record.id, TicketDraft(handoff_id=record.id, operator_id=operator_id)
        )
        updated = TicketDraft(
            handoff_id=draft.handoff_id,
            operator_id=draft.operator_id,
            alternatives=(*draft.alternatives, *options),
            notes=draft.notes,
        )
        self._drafts[record.id] = updated
        return updated

    def add_notes(self, handoff_id: UUID | str, *, operator_id: str, notes: str) -> TicketDraft:
        """Append an operator's note. Same staging rule as the alternatives."""
        record = self._require_holder(handoff_id, operator_id)
        draft = self._drafts.get(
            record.id, TicketDraft(handoff_id=record.id, operator_id=operator_id)
        )
        updated = TicketDraft(
            handoff_id=draft.handoff_id,
            operator_id=draft.operator_id,
            alternatives=draft.alternatives,
            notes=(*draft.notes, notes),
        )
        self._drafts[record.id] = updated
        return updated

    def resolve(
        self,
        handoff_id: UUID | str,
        *,
        operator_id: str,
        expected_generation: int | None = None,
        resolution: str,
        return_state: str,
        note: str | None = None,
    ) -> ResolutionOutcome:
        """Close the ticket and return the workflow to an allow-listed state.

        Two refusals happen here before the database is asked anything, both of them restatements of
        rules the database also enforces — because a caller that got this wrong should learn it from
        the API it is using:

        * a **booking-consent state** is refused (`RE004`). Resolving a ticket is not consent, and the
          states where a booking could follow without the patient are not return states at all.
        * anything outside the allow-list is refused (`RE004`), matching `handoff_resolve`'s own list.
        """
        if return_state in BOOKING_CONSENT_STATES or return_state not in OPERATOR_RETURN_STATES:
            raise ApprovalRefusedError(
                f"operator resolution cannot move a workflow to {return_state}; allowed: "
                + ", ".join(OPERATOR_RETURN_STATES)
                + ". A booking still needs the patient's own approval (007).",
                sqlstate="RE004",
            )
        record = self._require_holder(handoff_id, operator_id)
        generation = (
            expected_generation
            if expected_generation is not None
            else self._current_generation(handoff_id)
        )
        draft = self._drafts.get(record.id)
        full_note = _join_notes(note, None if draft is None else draft.to_note())
        resolved = HandoffRecord.from_domain(
            HandoffStore(self._conn).resolve(
                handoff_id,
                operator_id,
                expected_generation=generation,
                resolution=resolution,
                workflow_state=return_state,
                note=full_note,
            )
        )
        self._drafts.pop(record.id, None)
        self._emit_timings(record, resolved)
        return ResolutionOutcome(
            handoff_id=resolved.id,
            workflow_id=resolved.workflow_id,
            return_state=return_state,
            resolution=resolution,
            note=full_note,
            final_approval_still_required=True,
            booking_consent_granted=False,
        )

    # ---------------------------------------------------------------- internals

    def _emit_timings(self, queued: HandoffRecord, resolved: HandoffRecord) -> None:
        """§15's two HITL measurements, from the row's own timestamps.

        `hitl_wait_ms` is how long the *workflow* waited for a human — queue to close. `hitl_resolve_ms`
        is how long the *operator* held it — claim to close. §15 asks for both and the emitter keeps
        them out of the latency percentiles §16 alerts on, so a slow morning for the humans on call
        does not look like a slow platform.

        A ticket never claimed has no pickup time, so `hitl_resolve_ms` is not emitted at all rather
        than emitted as zero: an unclaimed ticket was not resolved quickly, it was resolved without
        being claimed, and a zero would read as an impossibly fast human.
        """
        assert resolved.resolved_at is not None  # `resolve` writes it; a null means the DB refused
        wait_ms = _elapsed_ms(queued.created_at, resolved.resolved_at)
        resolve_ms = _elapsed_ms(resolved.claimed_at, resolved.resolved_at)
        if self._telemetry is None or wait_ms is None:
            return
        self._telemetry.hitl_wait(
            wait_ms=wait_ms,
            ticket_ref=resolved.ticket_ref,
            workflow_state=resolved.state,
        )
        if resolve_ms is not None:
            self._telemetry.hitl_resolve(
                resolve_ms=resolve_ms,
                resolution=resolved.resolution,
                ticket_ref=resolved.ticket_ref,
            )

    def _current_generation(self, handoff_id: UUID | str) -> int:
        return int(
            required_row(
                fetch_one(
                    self._conn, "SELECT claim_generation FROM handoff WHERE id = %s", (handoff_id,)
                ),
                "handoff read",
            )["claim_generation"]
        )

    def _require_holder(self, handoff_id: UUID | str, operator_id: str) -> HandoffRecord:
        """Refuse an operator action by an operator who does not hold the ticket."""
        record = HandoffRecord.from_row(
            required_row(
                fetch_one(self._conn, "SELECT * FROM handoff WHERE id = %s", (handoff_id,)),
                "handoff read",
            )
        )
        if record.claimed_by != operator_id or record.state != "claimed":
            raise StaleOwnershipError(
                f"handoff {handoff_id} is not held by {operator_id!r} "
                f"(state={record.state}, holder={record.claimed_by!r})",
                sqlstate="RE001",
            )
        return record

    def _staged(self, workflow_id: UUID) -> dict[str, Any]:
        """The staged handoff record for a workflow, from the payload the worker wrote."""
        row = fetch_one(self._conn, "SELECT payload FROM workflow WHERE id = %s", (workflow_id,))
        if row is None:
            return {}
        staged = (row["payload"] or {}).get(HANDOFF_KEY)
        return dict(staged) if isinstance(staged, Mapping) else {}


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _join_notes(*parts: str | None) -> str | None:
    present = [part for part in parts if part]
    return "\n".join(present) if present else None


#: The ticket status for a `cancelled` handoff is `expired`: the contract has no "withdrawn ticket",
#: and both cases are "this ticket is not going to be worked", which is what a queue view needs.
_TICKET_STATUS_BY_STATE: Final[dict[str, TicketStatus]] = {
    "open": TicketStatus.pending,
    "claimed": TicketStatus.claimed,
    "resolved": TicketStatus.resolved,
    "cancelled": TicketStatus.expired,
}


def _elapsed_ms(start: datetime | None, end: datetime | None) -> float | None:
    """Milliseconds between two instants, or `None` when either is missing.

    `None` and not `0.0`, and this is the whole reason it is a named function: a missing timestamp
    means the event did not happen, and a zero would put "resolved instantly" into a distribution
    that is supposed to describe how long people took.
    """
    if start is None or end is None:
        return None
    return (end - start).total_seconds() * 1000


def queued_handoffs(conn: ConnectionLike) -> list[HandoffRecord]:
    """The queue as an operator sees it: live tickets, oldest first.

    Delegates to `HandoffStore.open_tickets` rather than repeating the statement, because the admin
    dashboard reads the same queue (the gateway's `/v1/admin/human-queue`) and two copies of the
    query would be two answers to what §11 makes one question. The row still becomes this module's
    `HandoffRecord`, which is where the status, reason-class and ownership readings live.
    """
    return [HandoffRecord.from_row(asdict(row)) for row in HandoffStore(conn).open_tickets()]


def workflow_is_human_owned(conn: ConnectionLike, workflow_id: UUID | str) -> bool:
    """Whether automation is suspended for a workflow (§11). The same column the guard reads."""
    row = fetch_one(conn, "SELECT human_owner_id FROM workflow WHERE id = %s", (workflow_id,))
    return bool(row and row["human_owner_id"] is not None)


def deadline_from(now: datetime, *, hours: int = 24) -> datetime:
    """The default response deadline. Named rather than inlined so it is one number, in one place."""
    return now + timedelta(hours=hours)
