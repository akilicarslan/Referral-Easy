"""Store accepted work and fence each execution with a separate claim.

The command stores the pending patch with its patient input. A worker can claim
the work only after the command receipt commits. Storage owns atomic claims.
This module owns job format, expiry, claim identity and recovery decisions.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from referraleasy_gateway.timing import Clock

if TYPE_CHECKING:
    from referraleasy_gateway.control import WorkflowRow


class ContinuationKind(StrEnum):
    ANSWER = "answer"
    MESSAGE = "message"
    BOOKING_CONSENT = "booking_consent"
    LOOKUP = "lookup"


class ContinuationReplaced(RuntimeError):
    """The claim expired, finished, or belongs to another execution."""


class ContinuationExpired(ValueError):
    """The accepted command expired. Its current owner must request recovery."""


@dataclass(frozen=True, slots=True)
class ContinuationClaim:
    workflow_id: UUID
    command_id: str
    token: str
    kind: ContinuationKind
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ContinuationRecord:
    """The stored format shared by the adapters. Callers use the lifecycle."""

    command_id: str
    kind: ContinuationKind = ContinuationKind.ANSWER
    pending: bool = True
    expires_at: datetime | None = None
    owner: str | None = None
    lease_until: datetime | None = None

    @classmethod
    def read(cls, payload: Mapping[str, Any]) -> ContinuationRecord | None:
        job = payload.get("coordination_job")
        if not isinstance(job, Mapping) or not job.get("command_id"):
            return None
        return cls(
            command_id=job["command_id"],
            kind=ContinuationKind(job.get("kind", "answer")),
            pending=job.get("pending") is True,
            expires_at=datetime.fromisoformat(job["expires_at"]) if job.get("expires_at") else None,
            owner=job.get("owner"),
            lease_until=datetime.fromisoformat(job["lease_until"])
            if job.get("lease_until")
            else None,
        )

    def patch(self) -> dict[str, Any]:
        job: dict[str, Any] = {
            "command_id": self.command_id,
            "kind": self.kind.value,
            "pending": self.pending,
        }
        for key in ("expires_at", "lease_until"):
            value = getattr(self, key)
            if value is not None:
                job[key] = value.isoformat()
        if self.owner is not None:
            job["owner"] = self.owner
        return {"coordination_job": job}

    def due(self, *, committed: bool, now: datetime) -> bool:
        return self.pending and committed and (self.lease_until is None or self.lease_until <= now)

    def held_by(self, claim: ContinuationClaim, *, now: datetime) -> bool:
        return bool(
            self.pending
            and self.command_id == claim.command_id
            and self.owner == claim.token
            and self.lease_until is not None
            and self.lease_until > now
        )

    def claimed(self, workflow_id: UUID, token: str) -> ContinuationClaim:
        return ContinuationClaim(workflow_id, self.command_id, token, self.kind, self.expires_at)


class ContinuationStore(Protocol):
    """Atomic storage operations. Lease checks use the storage clock."""

    def pending_continuations(self) -> tuple[UUID, ...]: ...

    def claim_continuation(
        self, workflow_id: UUID, token: str, *, lease_seconds: float
    ) -> ContinuationClaim | None: ...

    def renew_continuation(
        self, claim: ContinuationClaim, *, lease_seconds: float
    ) -> WorkflowRow | None: ...

    def finish_continuation(self, claim: ContinuationClaim) -> bool: ...


class ContinuationLifecycle:
    """Create pending work, claim it once, and refuse stale execution."""

    def __init__(self, store: ContinuationStore, *, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        self._lease_seconds = 120.0

    def pending_patch(
        self, command_id: str, kind: ContinuationKind = ContinuationKind.ANSWER
    ) -> Mapping[str, Any]:
        """Include this patch in the same write as the accepted patient input."""
        return ContinuationRecord(
            command_id=command_id, kind=kind, expires_at=self._clock.now() + timedelta(minutes=5)
        ).patch()

    def pending(self) -> tuple[UUID, ...]:
        return self._store.pending_continuations()

    def claim(self, workflow_id: UUID) -> ContinuationClaim | None:
        """Use a new token for every attempt, including attempts in one process."""
        return self._store.claim_continuation(
            workflow_id, str(uuid4()), lease_seconds=self._lease_seconds
        )

    def claim_pending(self) -> Iterator[ContinuationClaim]:
        for workflow_id in self.pending():
            claim = self.claim(workflow_id)
            if claim is not None:
                yield claim

    def refresh(self, claim: ContinuationClaim) -> WorkflowRow:
        """Renew ownership and check expiry before the next step or result write."""
        row = self.recoverable(claim)
        if row is None:
            raise ContinuationReplaced()
        if claim.expires_at is not None and claim.expires_at <= self._clock.now():
            raise ContinuationExpired("The queued coordination command expired.")
        return row

    def recoverable(self, claim: ContinuationClaim) -> WorkflowRow | None:
        """Permit recovery of an expired job only while its claim is still live."""
        return self._store.renew_continuation(claim, lease_seconds=self._lease_seconds)

    def finish(self, claim: ContinuationClaim) -> bool:
        return self._store.finish_continuation(claim)
