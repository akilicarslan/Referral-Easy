"""Command dedup (§7): one `command_id`, one outcome — replay is a row, not a retry policy.

A command that reached the database may or may not have reached the outside world. The ledger turns
that ambiguity into a stored fact: the first caller to present a `command_id` and request hash gets
to do the work and write the response down, and every later caller with the *same* pair receives
that response instead of doing it again.

Two refusals matter, and the database makes both of them:

* the same `command_id` with a **different** request hash is refused (`RE005` →
  :class:`~referraleasy_domain.errors.CommandConflictError`). It is not coerced, not overwritten and
  not "taken as the newer intent" — reusing an id for a different body is exactly the double-booking
  bug the ledger exists to prevent.
* a command that is already terminal cannot be completed again. This is the sharp edge of "one
  outcome": a `failed` command is **spent**. Its stored failure is what a retry replays, and
  recording a success under that id is refused. A retry that expects to succeed is a new attempt and
  therefore a new `command_id`.

Reconciliation obeys the same rule, which is why `replay` exists: a booking whose outcome is unknown
is looked up with the **same** key it was created with, never with a freshly minted one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from referraleasy_domain.approval import _canonical_bytes
from referraleasy_domain.workflow import ConnectionLike, fetch_one, required_row, to_text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pydantic import BaseModel

__all__ = [
    "REQUEST_HASH_PATTERN",
    "CommandDecision",
    "CommandDisposition",
    "CommandLedger",
    "CommandRecord",
    "canonical_request_hash",
]

#: The shape `command_dedup_request_hash_hex` enforces on the column.
REQUEST_HASH_PATTERN: Final[str] = r"^[0-9a-f]{64}$"


def canonical_request_hash(payload: BaseModel | Mapping[str, Any]) -> str:
    """Return the request hash for a command body.

    Deliberately the *same encoder* as the booking-payload hash — this imports `_canonical_bytes`
    from :mod:`referraleasy_domain.approval` rather than writing a second canonical-JSON routine.
    Two encoders would be two chances to disagree, and a disagreement here shows up as a false
    "same command, different body" conflict on a retry that was in fact identical.

    The two functions are separate names because they are separate *values* with separate meanings
    (one binds patient consent, one identifies a command); that they compute the same way for the
    same input is the point, not an accident.
    """
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


class CommandDisposition(StrEnum):
    """What the ledger says the caller should do with a command.

    `REPLAYED` and `FAILED` are both terminal and both mean "do not run the work again" — the
    difference is whether there is a successful response to return or a failure to report.
    """

    #: No stored outcome yet. Execute the work, then `complete` it.
    IN_FLIGHT = "in_flight"
    #: Already completed. Return the stored response; do not execute.
    REPLAYED = "replayed"
    #: Already failed, terminally. The id is spent; report the stored failure.
    FAILED = "failed"


#: `command_dedup_status_known` allows exactly these three. Indexing rather than `.get()` is
#: intentional: a status the database cannot produce should be loud, not silently treated as
#: "go ahead and execute".
_DISPOSITION_BY_STATUS: Final[dict[str, CommandDisposition]] = {
    "in_progress": CommandDisposition.IN_FLIGHT,
    "completed": CommandDisposition.REPLAYED,
    "failed": CommandDisposition.FAILED,
}


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """One `command_dedup` row — the ledger entry itself."""

    command_id: str
    request_hash: str
    status: str
    response_status: int | None
    response: Mapping[str, Any] | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> CommandRecord:
        return cls(
            command_id=row["command_id"],
            request_hash=row["request_hash"],
            status=row["status"],
            response_status=row["response_status"],
            response=row["response"],
            error=row["error"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            expires_at=row["expires_at"],
        )

    @property
    def disposition(self) -> CommandDisposition:
        return _DISPOSITION_BY_STATUS[self.status]

    @property
    def is_terminal(self) -> bool:
        """Whether this command's outcome is already decided and cannot be rewritten."""
        return self.status in ("completed", "failed")


@dataclass(frozen=True, slots=True)
class CommandDecision:
    """The answer to "should I run this command?" — a decision, plus the record behind it."""

    record: CommandRecord

    @property
    def command_id(self) -> str:
        return self.record.command_id

    @property
    def disposition(self) -> CommandDisposition:
        return self.record.disposition

    @property
    def is_replay(self) -> bool:
        """True when this exact command already succeeded: return its response, do not execute."""
        return self.disposition is CommandDisposition.REPLAYED

    @property
    def should_execute(self) -> bool:
        """True only when no outcome is stored yet."""
        return self.disposition is CommandDisposition.IN_FLIGHT

    def stored_response(self) -> tuple[int, Mapping[str, Any] | None] | None:
        """The response a replay must return, or None when there is nothing stored to return."""
        if not self.is_replay:
            return None
        response_status = self.record.response_status
        if response_status is None:  # pragma: no cover - the CHECK guarantees otherwise
            return None
        return response_status, self.record.response


class CommandLedger:
    """Typed access to `command_begin` / `command_complete` / `command_replay` over one connection.

    The ledger row is written by the same transaction as the work it describes, so the two commit
    together: a command can never be recorded as done without the effect, and never have the effect
    without being recorded.
    """

    def __init__(self, conn: ConnectionLike) -> None:
        self._conn = conn

    def begin(self, command_id: str, request_hash: str) -> CommandDecision:
        """Open a command, returning whether it must be executed or replayed.

        Creates the ledger row on first sight. A `command_id` already recorded against a different
        request hash raises :class:`~referraleasy_domain.errors.CommandConflictError` (RE005).
        """
        row = fetch_one(
            self._conn,
            "SELECT * FROM command_begin(%s, %s)",
            (command_id, request_hash),
        )
        return CommandDecision(CommandRecord.from_row(required_row(row, "command_begin")))

    def complete(
        self,
        command_id: str,
        request_hash: str,
        *,
        response_status: int,
        response: Mapping[str, Any] | None = None,
        status: str = "completed",
    ) -> CommandRecord:
        """Record a command's outcome so later retries replay it.

        `status` must be `completed` or `failed`; anything else, a request hash that does not match
        the row, or a command that is already terminal, raises
        :class:`~referraleasy_domain.errors.CommandConflictError` (RE005). The WHERE clause
        re-asserts the request hash, so a completion can never land on a different command's row.
        """
        # Imported here: sending a jsonb parameter needs the driver, and this module deliberately
        # does not import one at module scope.
        from psycopg.types.json import Jsonb

        body = None if response is None else Jsonb(dict(response))
        row = fetch_one(
            self._conn,
            "SELECT * FROM command_complete(%s, %s, %s, %s, %s)",
            (command_id, request_hash, response_status, body, to_text(status)),
        )
        return CommandRecord.from_row(required_row(row, "command_complete"))

    def replay(self, command_id: str, request_hash: str) -> CommandRecord:
        """Read the ledger entry for a retry, without claiming it.

        Same rejection rule as :meth:`begin`, so a caller that skips `begin` cannot read another
        request's response by guessing an id. Only a completed row carries a replayable response.

        This is the call that keeps reconciliation honest: an unknown booking outcome is resolved
        with the key it already has, and a lookup with a *different* hash is a refusal rather than a
        new booking.
        """
        row = fetch_one(
            self._conn,
            "SELECT * FROM command_replay(%s, %s)",
            (command_id, request_hash),
        )
        return CommandRecord.from_row(required_row(row, "command_replay"))
