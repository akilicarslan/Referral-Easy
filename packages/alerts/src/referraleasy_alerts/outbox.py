"""The mock email outbox: what a notification would say, and what happened to it.

    The demo email screen displays content and delivery state; it does not send real email.

The last clause is the whole design. Nothing in this module sends anything: a message is *composed*,
written to the outbox with a delivery state, and delivered by a double that records the attempt. The
admin page reads the outbox, so the demo shows a real message body and a real state history rather
than a claim that mail was sent.

**A notification failure does not open a loop.** §16 says so explicitly, and it is the failure mode
an outbox exists to prevent: an alert whose delivery failed must not re-trigger the alert that
produced it, because the second alert's delivery would fail the same way. So the outbox's retry
policy is bounded and *terminal* — after `max_attempts` the message is `dead` rather than pending —
and nothing in this module can create a new alert. The feedback path from delivery back into
evaluation does not exist, which is the structural reason it cannot loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from referraleasy_alerts.models import AlertEvent

__all__ = [
    "ComposedEmail",
    "DeliveryState",
    "Message",
    "Outbox",
    "RecordingSender",
    "Sender",
    "compose",
]


class DeliveryState(StrEnum):
    """Where one message is. `DEAD` is terminal and is not `PENDING`."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class ComposedEmail:
    """A message body, ready to store. No transport is involved in composing one."""

    subject: str
    body: str

    def as_dict(self) -> dict[str, Any]:
        return {"subject": self.subject, "body": self.body}


def compose(event: AlertEvent, *, link: str | None = None) -> ComposedEmail:
    """The message for one alert event.

    Deliberately plain text with the measurement in it: the rule, the threshold, what was observed,
    and the sample it came from. A notification that says only "latency is high" makes its reader go
    and look up the number it used — and an alert that cannot be acted on from the message itself is
    the kind that gets filtered into a folder.
    """
    direction = "firing" if event.opened else "resolved"
    evaluation = event.evaluation
    lines = [
        f"{event.rule} is {direction}.",
        "",
        f"Observed:  {evaluation.observed}",
        f"Threshold: {evaluation.threshold}",
        f"Sample:    {evaluation.sample.count} event(s), {evaluation.sample.documents} document(s)",
        "",
        evaluation.detail,
    ]
    if link is not None:
        lines += ["", f"Dashboard: {link}"]
    lines += [
        "",
        "— ReferralEasy, demo alerting. This message was composed by the mock outbox and was not "
        "sent to anyone.",
    ]
    return ComposedEmail(
        subject=f"[{event.severity}] {event.rule} {direction}", body="\n".join(lines)
    )


class Sender(Protocol):
    """The transport seam. The only thing that could ever send."""

    def send(self, message: Message) -> bool:
        """True when the message was delivered. Raising is a failure like any other."""
        ...


@dataclass
class Message:
    """One outbox row: the content, its state, and how many times it was tried."""

    id: str
    dedupe_key: str
    to: str
    email: ComposedEmail
    created_at: datetime
    state: DeliveryState = DeliveryState.PENDING
    attempt_count: int = 0
    last_error: str | None = None
    delivered_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dedupe_key": self.dedupe_key,
            "to": self.to,
            "subject": self.email.subject,
            "body": self.email.body,
            "created_at": self.created_at.isoformat(),
            "state": str(self.state),
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
        }


@dataclass
class RecordingSender:
    """A sender that records what it was asked to send and fails when told to.

    The double the demo uses, and the one the tests use. It does not touch a network, does not need
    credentials, and cannot accidentally mail a real address — which is the point of §16's "does not
    send real email".
    """

    fail_next: int = 0
    sent: list[str] = field(default_factory=list)
    error: str = "the demo sender refuses to deliver"

    def send(self, message: Message) -> bool:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise OSError(self.error)
        self.sent.append(message.id)
        return True


class Outbox:
    """Composes, stores and delivers messages through a `Sender`.

    In-memory here, and the methods are the ones a durable store would need — `add`, `deliver`,
    `pending`, `items` — so the Postgres implementation is a different class with the same surface
    rather than a different design.
    """

    def __init__(self, sender: Sender, *, max_attempts: int = 5) -> None:
        self._sender = sender
        self._max_attempts = max_attempts
        self._messages: dict[str, Message] = {}
        self._counter = 0

    def add(
        self,
        event: AlertEvent,
        *,
        to: str,
        now: datetime,
        link: str | None = None,
        series: str = "",
    ) -> Message | None:
        """Queue one notification, unless the same transition is already queued or delivered.

        **Deduplicated on the alert's key *and* its direction**, which is what stops an at-least-once
        delivery path from becoming an at-least-twice inbox: a retry that re-reaches `add` finds the
        key and returns `None` rather than composing a second message. That is the loop §16 forbids,
        closed here rather than hoped for.

        The direction is in the key because an opening and its resolution are different messages about
        the same alert. Keying on the rule alone would swallow the resolve as a duplicate of the alert
        it closes — the operator told that something fired, and never told that it stopped.
        """
        key = f"{event.dedupe_key(series)}:{event.kind}"
        if any(message.dedupe_key == key for message in self._messages.values()):
            return None
        self._counter += 1
        message = Message(
            id=f"msg-{self._counter:04d}",
            dedupe_key=key,
            to=to,
            email=compose(event, link=link),
            created_at=now,
        )
        self._messages[message.id] = message
        return message

    def deliver(self, message: Message, *, now: datetime) -> Message:
        """Attempt delivery once, and move the message to a state that says what happened.

        The terminal transition is the important half: after `max_attempts` the message is `DEAD` and
        stops being retried. An outbox that retried forever would be the infinite loop §16 names, one
        layer below the alerts.
        """
        if message.state is DeliveryState.DELIVERED:
            return message
        message.attempt_count += 1
        try:
            if self._sender.send(message):
                message.state = DeliveryState.DELIVERED
                message.delivered_at = now
                message.last_error = None
                return message
            message.last_error = "the sender reported failure without raising"
        except Exception as problem:
            message.last_error = f"{type(problem).__name__}: {problem}"

        message.state = (
            DeliveryState.DEAD
            if message.attempt_count >= self._max_attempts
            else DeliveryState.FAILED
        )
        return message

    def deliver_pending(self, *, now: datetime) -> list[Message]:
        """One pass over everything not yet delivered. Returns what it touched."""
        touched: list[Message] = []
        for message in list(self._messages.values()):
            if message.state in (DeliveryState.DELIVERED, DeliveryState.DEAD):
                continue
            touched.append(self.deliver(message, now=now))
        return touched

    def pending(self) -> tuple[Message, ...]:
        return tuple(
            message
            for message in self._messages.values()
            if message.state in (DeliveryState.PENDING, DeliveryState.FAILED)
        )

    def items(self) -> tuple[Message, ...]:
        """Everything, newest first, for the admin page."""
        return tuple(reversed(tuple(self._messages.values())))

    def counts(self) -> dict[str, int]:
        """The delivery state tally. Every state is present, including the zero ones, so a missing
        state reads as zero rather than as absent."""
        counts = {str(state): 0 for state in DeliveryState}
        for message in self._messages.values():
            counts[str(message.state)] += 1
        return counts
