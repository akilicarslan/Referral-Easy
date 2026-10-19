# ruff: noqa: RUF002  # §11 is quoted in Turkish, verbatim from the spec
"""The operator queue, as §12's Human Queue page reads it (§11).

    Ticket admin Human Queue'ya düşer. Operatör claim eder, kaynakları görür, alternatif veya eksik
    belge açıklaması ekler. Çakışan claim engellenir.

**The read is the domain's, the wording here is the page's.** `HandoffStore.open_tickets` is the one
statement for "what is in the queue", and it has two callers — this and the workers'
`queued_handoffs`, which is the same query seen by the CLI an operator would use. A second copy here
would be a second answer to a question §11 makes one.

**The queue is only readable in the postgres profile**, and that is not a limitation to work around:
a handoff is a row, and the in-memory profile has no rows. The route says which profile it is on
rather than answering an empty list, because "no tickets" and "no ticket store" are different facts
and an operator must not have to guess which one they are looking at.

**Claim expiry is reported, not applied.** A ticket whose lease has lapsed still reads `claimed` in
the row until something reclaims it, so this page shows the row and the expiry beside it. A queue that
hid an expired claim would disagree with the claim that is about to succeed — the one race §11 names.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from referraleasy_domain import HandoffStore

from .admin import unavailable
from .psql import session

__all__ = ["open_tickets"]

#: A ceiling on one page of the queue. The operator's list is short by construction — a queue nobody
#: can work through is a different problem — and a full page is reported as full rather than silently
#: truncated.
QUEUE_LIMIT: Final = 200


def open_tickets(dsn: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Every live ticket, oldest first, with how long each has waited.

    The wait is computed here rather than stored: it is a difference between two timestamps, and a
    column would be a fourth thing to keep in step with the clock. `claimed_expired` is derived the
    same way and for the same reason — it is a comparison against *now*, not a state the row has.
    """
    if not dsn.strip():
        return unavailable(
            "GATEWAY_CONTROL_BACKEND is not postgres, so there is no handoff table to read. A "
            "handoff is a row (§11); the in-memory profile has none, and an empty queue would look "
            "like an idle operator rather than a profile without the store."
        )
    moment = now or datetime.now(UTC)
    with session(dsn) as connection:
        rows = HandoffStore(connection).open_tickets(limit=QUEUE_LIMIT)
    tickets = [_ticket(row, moment) for row in rows]
    return {
        "available": True,
        "tickets": tickets,
        "counts": {
            "open": sum(1 for ticket in tickets if ticket["state"] == "open"),
            "claimed": sum(1 for ticket in tickets if ticket["state"] == "claimed"),
            "claimed_expired": sum(1 for ticket in tickets if ticket["claimed_expired"]),
        },
        "limit": QUEUE_LIMIT,
        "truncated": len(tickets) >= QUEUE_LIMIT,
        "note": (
            "Claiming, releasing and resolving happen in the workers service — "
            "`python -m referraleasy_workers handoff ...` — because that is where §11's queue lives "
            "(ADR-0035). This page reads the same table and takes no action on it; a second "
            "implementation of the claim rules is the last thing a race needs."
        ),
    }


def _ticket(row: Any, now: datetime) -> dict[str, Any]:
    """One row, as the page needs it. Times are ISO-8601 strings, durations are milliseconds."""
    expires = row.claim_expires_at
    return {
        "handoff_id": str(row.id),
        "workflow_id": str(row.workflow_id),
        "ticket_ref": row.ticket_ref,
        "reason": row.reason,
        "state": row.state,
        "claim_generation": row.claim_generation,
        "claimed_by": row.claimed_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        "claim_expires_at": expires.isoformat() if expires else None,
        # Derived, not stored: "is this claim still live" is a question about the clock. A `claimed`
        # row whose lease has lapsed is reclaimable, and the page has to be able to say so.
        "claimed_expired": bool(expires is not None and expires <= now),
        "wait_ms": (
            (now - row.created_at).total_seconds() * 1000 if row.created_at is not None else None
        ),
    }
