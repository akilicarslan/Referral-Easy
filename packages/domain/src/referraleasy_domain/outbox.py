"""Outbox delivery (§7): the queue is a table, and a failing notification has an end.

There is no message broker. `outbox` *is* the transport, so the properties a broker would provide
are properties of the SQL instead: `outbox_claim` hands a batch to exactly one worker using
`FOR UPDATE SKIP LOCKED`, so two workers cannot receive the same row and a slow batch does not block
a fast one behind it.

Two invariants shape this module.

**Notification failure never rolls back a booking.** The row is enqueued in the booking's
transaction; delivery happens in a later transaction, in a different process. A handler that raises
is therefore captured and settled as a failure — never propagated — because there is nothing useful
a raise could do except take down a worker that is otherwise making progress, and because the
temptation to treat "the notification failed" as "the booking failed" is exactly the bug this
arrangement removes.

**A permanently failing notification stops rather than loops.** The attempt budget is on the row:
`outbox_settle` returns a failed row to `pending` with exponential backoff, and once
`attempt_count` reaches `max_attempts` it moves the row to the terminal `dead` state instead. A
`dead` row is never claimable again, so the loop terminates on its own — it does not need a retry
policy in Python that could drift from the budget the database is already enforcing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from referraleasy_domain.errors import OutboxClaimError
from referraleasy_domain.workflow import (
    ConnectionLike,
    fetch_all,
    fetch_one,
    required_row,
)

__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "DEFAULT_CLAIM_LEASE_SECONDS",
    "DEFAULT_CLAIM_LIMIT",
    "DrainReport",
    "OutboxMessage",
    "OutboxStatus",
    "OutboxStore",
]

#: The defaults the SQL functions carry, restated so a caller can see them without reading SQL.
DEFAULT_CLAIM_LIMIT: Final[int] = 10
DEFAULT_CLAIM_LEASE_SECONDS: Final[int] = 60
DEFAULT_BACKOFF_BASE_SECONDS: Final[int] = 5
DEFAULT_BACKOFF_CAP_SECONDS: Final[int] = 3600


class OutboxStatus(StrEnum):
    """The lifecycle of one queued delivery; `outbox_status_known` allows exactly these."""

    PENDING = "pending"
    CLAIMED = "claimed"
    DELIVERED = "delivered"
    #: Attempt budget spent. Terminal: never claimed again.
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """One `outbox` row, including the delivery budget spent on it so far."""

    id: UUID
    dedupe_key: str
    kind: str
    payload: Mapping[str, Any]
    status: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime
    claimed_by: str | None
    claimed_until: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None
    terminal_at: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> OutboxMessage:
        return cls(
            id=row["id"],
            dedupe_key=row["dedupe_key"],
            kind=row["kind"],
            payload=row["payload"] or {},
            status=row["status"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            next_attempt_at=row["next_attempt_at"],
            claimed_by=row["claimed_by"],
            claimed_until=row["claimed_until"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            delivered_at=row["delivered_at"],
            terminal_at=row["terminal_at"],
        )

    @property
    def is_terminal(self) -> bool:
        """Whether this row has stopped: delivered, or buried as dead."""
        return self.status in (OutboxStatus.DELIVERED, OutboxStatus.DEAD)

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempt_count)


@dataclass(frozen=True, slots=True)
class DrainReport:
    """What one `drain` did. Returned rather than logged so a caller can assert on it."""

    batches: int = 0
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    dead: int = 0
    #: Rows whose lease expired mid-delivery, so another worker owns them now.
    lost: int = 0

    @property
    def settled(self) -> int:
        return self.delivered + self.retried + self.dead


class OutboxStore:
    """Typed access to `outbox_claim` / `outbox_settle` over one connection."""

    def __init__(self, conn: ConnectionLike) -> None:
        self._conn = conn

    def claim(
        self,
        worker_id: str,
        *,
        limit: int = DEFAULT_CLAIM_LIMIT,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> list[OutboxMessage]:
        """Lease up to `limit` due rows to this worker.

        Rows already leased to a live worker are skipped rather than waited for, and rows that have
        spent their attempt budget are never returned at all — that is what stops a dead
        notification from being retried forever.

        A non-positive `limit` is refused with
        :class:`~referraleasy_domain.errors.OutboxClaimError` (RE007).
        """
        rows = fetch_all(
            self._conn,
            "SELECT * FROM outbox_claim(%s, %s, %s)",
            (worker_id, limit, lease_seconds),
        )
        return [OutboxMessage.from_row(row) for row in rows]

    def settle(
        self,
        outbox_id: UUID | str,
        worker_id: str,
        *,
        ok: bool,
        error: str | None = None,
        backoff_base: int = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_cap: int = DEFAULT_BACKOFF_CAP_SECONDS,
    ) -> OutboxMessage:
        """Report the outcome of a claimed delivery.

        Success is terminal. Failure backs off exponentially up to `backoff_cap`, and once the
        budget is spent the row becomes `dead` — terminal, and the terminal state is reached here
        rather than by a separate "give up" call.

        Settling a row this worker does not hold (typically because its lease expired) raises
        :class:`~referraleasy_domain.errors.OutboxClaimError` (RE007): the correct response is to
        stop touching it, not to insist.
        """
        row = fetch_one(
            self._conn,
            "SELECT * FROM outbox_settle(%s, %s, %s, %s, %s, %s)",
            (outbox_id, worker_id, ok, error, backoff_base, backoff_cap),
        )
        return OutboxMessage.from_row(required_row(row, "outbox_settle"))

    def drain(
        self,
        worker_id: str,
        deliver: Callable[[OutboxMessage], None],
        *,
        limit: int = DEFAULT_CLAIM_LIMIT,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        max_batches: int | None = None,
    ) -> DrainReport:
        """Claim, deliver and settle until the queue is empty.

        `deliver` is the actual side effect (an email, a webhook, a card). If it raises, the row is
        settled as a failure and the loop continues: the exception is captured and never re-raised,
        so a bad notification cannot abort the batch, and cannot reach a caller who might be holding
        the booking's transaction open.

        The loop ends on its own: batches are claimed by `next_attempt_at` order, a failed row is
        pushed into the future by the backoff, and an exhausted row becomes terminal. `max_batches`
        is a belt-and-braces ceiling for a caller that wants a bounded unit of work.

        Run this in its own transaction, outside the transaction that recorded the booking. The
        guarantee that a notification failure cannot roll back a booking comes from that
        separation — this method only guarantees it will not hand you the failure to roll back with.
        """
        batches = claimed = delivered = retried = dead = lost = 0

        while max_batches is None or batches < max_batches:
            batch = self.claim(worker_id, limit=limit, lease_seconds=lease_seconds)
            if not batch:
                break
            batches += 1

            for message in batch:
                claimed += 1
                ok, error = _attempt(deliver, message)
                try:
                    # The settle runs in its own block, which is a savepoint while the caller already
                    # has a transaction open. That matters: a refused settle poisons the enclosing
                    # transaction, so without it a lost lease could be reported but not recovered
                    # from -- the next statement would fail for a reason that has nothing to do with
                    # the queue.
                    with self._conn.transaction():
                        settled = self.settle(message.id, worker_id, ok=ok, error=error)
                except OutboxClaimError:
                    # The lease expired while the handler ran and someone else owns the row now.
                    # Its outcome is theirs to record; ours is stale.
                    lost += 1
                    continue

                if settled.status == OutboxStatus.DELIVERED:
                    delivered += 1
                elif settled.status == OutboxStatus.DEAD:
                    dead += 1
                else:
                    retried += 1

        return DrainReport(
            batches=batches,
            claimed=claimed,
            delivered=delivered,
            retried=retried,
            dead=dead,
            lost=lost,
        )


def _attempt(
    deliver: Callable[[OutboxMessage], None], message: OutboxMessage
) -> tuple[bool, str | None]:
    """Run one delivery, converting its exception into a settle argument.

    Catching broadly is the point here, not laziness: `deliver` is arbitrary caller code (an HTTP
    call, a template render), and every way it can fail means the same thing to the queue — this
    attempt did not work, record why and try again later.
    """
    try:
        deliver(message)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, None
