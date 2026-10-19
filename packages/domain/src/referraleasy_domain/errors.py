"""Typed exceptions for the SQLSTATE classes the migrations raise.

The migrations refuse illegal state changes in the database, not in the application (§7). Each
refusal raises a custom SQLSTATE so the caller can react to *what* was refused without parsing a
message string — messages are for humans and will be reworded.

Deliberately does not import a database driver: the translation is duck-typed on `sqlstate`, which
psycopg (and any DBAPI 2.0 driver carrying the attribute) provides. That keeps this module — and its
tests — dependency-free.
"""

from __future__ import annotations

from typing import ClassVar, Final

__all__ = [
    "SQLSTATE_MAP",
    "AppendOnlyViolationError",
    "ApprovalRefusedError",
    "CommandConflictError",
    "DomainError",
    "HumanOwnedError",
    "IllegalTransitionError",
    "OutboxClaimError",
    "PolicyDeniedError",
    "StaleOwnershipError",
    "translate",
]


class DomainError(RuntimeError):
    """Base for every refusal originating in the database's control-state guards."""

    #: The SQLSTATE this class stands for. Deliberately a plain class attribute rather than a
    #: ClassVar: an unrecognised code is carried on the instance (see `translate`), so the value is
    #: per-instance as well as per-class.
    sqlstate: str = ""
    #: Human-readable summary of the class of refusal. Subclasses narrow it.
    summary: ClassVar[str] = "the database refused the operation"

    def __init__(
        self,
        message: str = "",
        *,
        sqlstate: str | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(message or self.summary)
        self.sqlstate = sqlstate if sqlstate is not None else type(self).sqlstate
        self.original = original


class StaleOwnershipError(DomainError):
    """RE001 — the caller does not currently own the work, or its generation/lease is stale.

    Upholds the invariant that a stale worker cannot write workflow state (§7): an expired lease or
    an old generation is refused rather than merged.
    """

    sqlstate = "RE001"
    summary = "stale ownership: lease expired, owner differs, or generation is behind"


class IllegalTransitionError(DomainError):
    """RE002 — the requested state transition is not permitted by the state machine (§7)."""

    sqlstate = "RE002"
    summary = "illegal state transition"


class HumanOwnedError(DomainError):
    """RE003 — a human owns this workflow, so automation writes are suspended (§11).

    Automation and a human can never write concurrently; resolution returns the workflow to an
    explicit safe state before automation resumes.
    """

    sqlstate = "RE003"
    summary = "workflow is human-owned; automation writes are suspended"


class ApprovalRefusedError(DomainError):
    """RE004 — a booking-consent or approval-binding rule was violated (§9).

    Covers every path that would let a booking happen without a live approval bound to
    patient + workflow + state_version + payload hash + expiry, including a second use of an
    already-consumed approval.
    """

    sqlstate = "RE004"
    summary = "approval refused: consent is missing, stale, mismatched, or already consumed"


class CommandConflictError(DomainError):
    """RE005 — a `command_id` was reused with a different payload, or its record was rewritten.

    Upholds §7's replay rule: same command + same request hash replays the stored response; the same
    command with a different hash is refused rather than silently applied.
    """

    sqlstate = "RE005"
    summary = "command_id conflict: already recorded with a different request"


class PolicyDeniedError(DomainError):
    """RE006 — the policy table does not permit this agent/tool in this workflow state (§4).

    The coordinator cannot override the table; a denial here means the call never reaches a tool.
    """

    sqlstate = "RE006"
    summary = "policy denied: agent/tool is not permitted in this workflow state"


class OutboxClaimError(DomainError):
    """RE007 — an outbox row was claimed or settled by a worker that does not hold it (§7)."""

    sqlstate = "RE007"
    summary = "outbox claim invalid: not currently held by this worker"


class AppendOnlyViolationError(DomainError):
    """RE008 — an attempt to mutate an append-only table (audit, release registry, evidence)."""

    sqlstate = "RE008"
    summary = "append-only table: mutation refused"


SQLSTATE_MAP: Final[dict[str, type[DomainError]]] = {
    cls.sqlstate: cls
    for cls in (
        StaleOwnershipError,
        IllegalTransitionError,
        HumanOwnedError,
        ApprovalRefusedError,
        CommandConflictError,
        PolicyDeniedError,
        OutboxClaimError,
        AppendOnlyViolationError,
    )
}


def translate(exc: BaseException) -> DomainError:
    """Map a driver exception to its typed counterpart, preserving the message and cause.

    Unrecognised SQLSTATEs become a plain :class:`DomainError` carrying the code, so an unexpected
    refusal is still surfaced with its identity intact rather than swallowed.
    """
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None) or ""
    cls = SQLSTATE_MAP.get(sqlstate, DomainError)
    return cls(str(exc), sqlstate=sqlstate, original=exc)
