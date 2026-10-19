"""Three events that look alike and mean different things (§7).

A booking can end without a booking, and it can end without *knowing* whether there is one. The
system has three ways for a call to stop being in flight, and the whole point of this module is that
they are three values rather than one string:

===================  =====================  ==========================  ====================
event                may a booking exist?  does it need reconciling?    is the effect best-effort?
===================  =====================  ==========================  ====================
`REMOTE_CANCEL`      yes                    yes                         yes
`USER_ABANDONED`     only if a write ran    only if a write ran         no
`TRANSPORT_TIMEOUT`  yes                    yes                         no
===================  =====================  ==========================  ====================

The remote cancel is the one that is routinely got wrong. Sending a cancel to a facility is a
*request to cancel*, not proof that nothing was created: the cancel can lose a race with a commit
that was already in flight, it can be refused, and it can time out — and none of those outcomes
means "no appointment exists". A worker that treated a best-effort cancel as a terminal fact would
mark a workflow `CANCELLED` while the patient holds an appointment, which is the mirror image of the
double-booking bug and just as expensive.

Abandonment is the one that is *not* a lie only when nothing was sent. "The patient walked away" says
nothing about a request that was already on the wire, so the event carries whether one was — and an
abandonment with a write in flight goes to reconciliation like a timeout would.

Nothing here advances a workflow: the events carry what is true, and the state decision belongs to
the caller that knows whether a human or a recovery path is next.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

__all__ = [
    "ABANDONED_STATE",
    "BOOKING_UNVERIFIED_STATE",
    "TerminalEvent",
    "TerminalEventRecord",
]


class TerminalEvent(StrEnum):
    """How a call stopped being in flight."""

    #: A cancellation was *sent*. Best-effort by nature: the facility may have committed a booking
    #: before it, and a cancel that is refused or lost changes nothing about that.
    REMOTE_CANCEL = "remote_cancel"
    #: The patient walked away. Says nothing about a request that was already running.
    USER_ABANDONED = "user_abandoned"
    #: Contact was lost while the request was out. Either timeout flavour — before or after the
    #: facility's commit — leaves the same open question, because only the facility can answer it.
    TRANSPORT_TIMEOUT = "transport_timeout"


#: Where a workflow goes when the outcome is genuinely unknown. Named once, here, because it is the
#: state every "we do not know" path converges on (see §7: reconcile with the SAME key).
BOOKING_UNVERIFIED_STATE: Final[str] = "BOOKING_UNVERIFIED"
#: Where a workflow goes when the patient has stopped. Terminal, and not a statement about a booking.
ABANDONED_STATE: Final[str] = "ABANDONED"

_MESSAGES: Final[dict[TerminalEvent, str]] = {
    TerminalEvent.REMOTE_CANCEL: (
        "We asked the hospital to cancel this appointment and have not confirmed it yet. "
        "We are checking. A cancellation request is not confirmation that nothing is booked."
    ),
    TerminalEvent.USER_ABANDONED: (
        "You have not continued with this referral, so we are not doing anything further with it."
    ),
    TerminalEvent.TRANSPORT_TIMEOUT: (
        "We lost contact with the hospital's scheduling system while this request was in flight. "
        "We do not yet know whether an appointment was made, and we are checking."
    ),
}


@dataclass(frozen=True, slots=True)
class TerminalEventRecord:
    """One occurrence of one of the three events, with what it does and does not imply."""

    kind: TerminalEvent
    workflow_id: str
    at: datetime
    detail: str = ""
    #: True when a write had already left this process when the event happened. Only meaningful for
    #: abandonment, where it is the difference between "nothing happened" and "something might have".
    write_in_flight: bool = False

    @property
    def booking_may_exist(self) -> bool:
        """Whether an appointment may exist at the facility, as far as this event can tell."""
        if self.kind is TerminalEvent.USER_ABANDONED:
            return self.write_in_flight
        # A timeout and a cancel both leave the facility's record in question. For a cancel that is
        # the uncomfortable answer, and it is the one the evidence supports.
        return True

    @property
    def requires_reconciliation(self) -> bool:
        """Whether the caller must resolve the outcome with the original key before moving on."""
        return self.booking_may_exist

    @property
    def effect_is_best_effort(self) -> bool:
        """Whether the action taken was a request whose success was never confirmed."""
        return self.kind is TerminalEvent.REMOTE_CANCEL

    @property
    def terminal_state(self) -> str | None:
        """The state this event can take a workflow to on its own, if any.

        A remote cancel has none: `CANCELLED` is a fact about the facility's record, and a
        best-effort request is not that fact. Its workflow stays in `BOOKING_UNVERIFIED` until a
        lookup against the same key says otherwise.
        """
        if self.kind is TerminalEvent.TRANSPORT_TIMEOUT:
            return BOOKING_UNVERIFIED_STATE
        if self.kind is TerminalEvent.USER_ABANDONED:
            return ABANDONED_STATE if not self.write_in_flight else BOOKING_UNVERIFIED_STATE
        # A remote cancel has none.
        return None

    @property
    def patient_message(self) -> str:
        """What can be said to the patient without asserting anything unsupported."""
        return _MESSAGES[self.kind]

    @classmethod
    def remote_cancel(
        cls, workflow_id: str, *, detail: str = "", at: datetime | None = None
    ) -> TerminalEventRecord:
        """A cancel request that was sent — and therefore may or may not have cancelled anything."""
        return cls(
            kind=TerminalEvent.REMOTE_CANCEL,
            workflow_id=workflow_id,
            at=at or datetime.now(UTC),
            detail=detail,
            write_in_flight=True,
        )

    @classmethod
    def user_abandoned(
        cls,
        workflow_id: str,
        *,
        write_in_flight: bool = False,
        detail: str = "",
        at: datetime | None = None,
    ) -> TerminalEventRecord:
        """The patient stopped. Pass ``write_in_flight`` when a request was already out."""
        return cls(
            kind=TerminalEvent.USER_ABANDONED,
            workflow_id=workflow_id,
            at=at or datetime.now(UTC),
            detail=detail,
            write_in_flight=write_in_flight,
        )

    @classmethod
    def transport_timeout(
        cls, workflow_id: str, *, detail: str = "", at: datetime | None = None
    ) -> TerminalEventRecord:
        """Contact was lost with the request in flight. Unknown, and reconciled as unknown."""
        return cls(
            kind=TerminalEvent.TRANSPORT_TIMEOUT,
            workflow_id=workflow_id,
            at=at or datetime.now(UTC),
            detail=detail,
            write_in_flight=True,
        )
