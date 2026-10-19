"""Workflow ownership, transitions and the booking gate (§7, §9) — a typed caller, not a copy.

Every rule below already exists in the database, and none of it is restated here:

* which transitions are legal            -> `workflow_transition` rows (guard trigger, RE002)
* who may currently write                -> owner + live lease + generation (RE001)
* whether a human has suspended automation -> `human_owner_id` (guard trigger, RE003)
* whether a booking has live consent     -> composite FK + consent guard (RE004)

So this module's job is small and deliberate: call the function, hand back the row, and translate
the refusal into the exception that says which rule was refused. There is no `if` here that decides
whether a transition is allowed — a second copy of the state machine would drift from the one that
actually enforces it, and the copy would be the one that is wrong.

**Transactions are the caller's, and they must be short.** These calls are single statements; wrap
each in its own transaction (`with conn.transaction():`) and commit. Never hold a transaction open
across the life of a workflow: `workflow_claim` and `workflow_advance` both take a row lock, and a
worker that keeps that lock while it does its real work serialises every other worker behind it. The
lease exists precisely so that ownership can outlive the transaction without the lock doing so.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, Self
from uuid import UUID

from referraleasy_domain.errors import DomainError, translate

__all__ = [
    "BookingRequest",
    "ConnectionLike",
    "CursorLike",
    "Workflow",
    "WorkflowStore",
    "execute_function",
    "fetch_all",
    "fetch_one",
    "fetch_scalar",
    "required_row",
    "to_text",
]


# ---------------------------------------------------------------------------
# Shared database plumbing
# ---------------------------------------------------------------------------
# This belongs in a `_db` module of its own, but the file-ownership rule for the
# domain package allows only these five store modules, so it lives in the lowest
# store every other store is built on. `policy`, `commands`, `outbox` and
# `handoff` import from here; nothing here imports from them, so the graph stays
# a DAG.


class CursorLike(Protocol):
    """The slice of a DBAPI cursor this package uses."""

    @property
    def description(self) -> Sequence[Any] | None: ...

    def execute(self, query: str, params: Sequence[Any] = ...) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> Sequence[Any]: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc_info: object) -> None: ...


class ConnectionLike(Protocol):
    """The slice of a DBAPI connection this package uses.

    Structural on purpose: the stores never import a driver, so a psycopg
    connection, a pool checkout, or a test double all satisfy this equally.
    """

    def cursor(self) -> CursorLike: ...

    def transaction(self) -> AbstractContextManager[Any]:
        """A block that begins, commits, and rolls back — a savepoint when one is already open.

        Only `drain` uses it, to contain a refused settle without poisoning the caller's
        transaction. psycopg's `Connection.transaction()` has exactly this behaviour, so no
        adapter is needed.
        """
        ...


#: Attributes a driver puts on an exception that carries a PostgreSQL SQLSTATE.
#: Mirrors the duck-typing in `errors.translate` — psycopg sets `sqlstate`,
#: other DBAPI 2.0 drivers set `pgcode`.
_REFUSAL_ATTRS: Final[tuple[str, ...]] = ("sqlstate", "pgcode")


@contextmanager
def _translated() -> Iterator[None]:
    """Turn a database refusal into its typed counterpart; leave everything else alone.

    The check is duck-typed rather than `except psycopg.Error` so that this package imports no
    driver (§7: the driver is an implementation detail of the deployment, not of the rules). A
    Python-level bug — a typo, a bad argument — carries no SQLSTATE and is re-raised untouched, so
    a real programming error can never masquerade as a domain refusal.
    """
    try:
        yield
    except Exception as exc:
        if any(getattr(exc, attr, None) for attr in _REFUSAL_ATTRS):
            raise translate(exc) from exc
        raise


def _column_names(cur: CursorLike) -> list[str]:
    return [getattr(column, "name", str(column)) for column in (cur.description or ())]


def _as_mapping(cur: CursorLike, row: Any) -> dict[str, Any]:
    """Normalise a result row to a dict, whatever the connection's row factory is.

    The caller owns the connection and may have set `row_factory=dict_row` or left the default
    tuple. Reading `description` makes the store work with either instead of demanding a
    configuration the caller was never told about.
    """
    if isinstance(row, Mapping):
        return dict(row)
    return dict(zip(_column_names(cur), row, strict=True))


def fetch_all(conn: ConnectionLike, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Run `query` and return every row as a dict."""
    with _translated(), conn.cursor() as cur:
        cur.execute(query, params)
        return [_as_mapping(cur, row) for row in cur.fetchall()]


def fetch_one(
    conn: ConnectionLike, query: str, params: Sequence[Any] = ()
) -> dict[str, Any] | None:
    """Run `query` and return the first row as a dict, or None when there is no row."""
    with _translated(), conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        return None if row is None else _as_mapping(cur, row)


def fetch_scalar(conn: ConnectionLike, query: str, params: Sequence[Any] = ()) -> Any:
    """Run `query` and return the first column of the first row (None when there is no row)."""
    with _translated(), conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        if row is None:
            return None
        if isinstance(row, Mapping):
            return next(iter(row.values()), None)
        return row[0]


def execute_function(conn: ConnectionLike, query: str, params: Sequence[Any] = ()) -> None:
    """Call a void-returning function for its effect — its refusal is the whole result."""
    fetch_scalar(conn, query, params)


def to_text(value: str) -> str:
    """Normalise a `StrEnum` member (or a plain str) to a plain `str` for the wire.

    `str()` on a `StrEnum` member yields its value, not its repr, so a caller may pass either a
    `WorkflowState` or a raw string and both reach PostgreSQL identically.
    """
    return str(value)


def required_row(row: dict[str, Any] | None, function: str) -> dict[str, Any]:
    """Unwrap a row the database promises to return.

    Every function this is used on either returns a row or raises, so reaching the error would mean
    the database changed shape underneath the client. It is a real check rather than an `assert`
    because assertions vanish under `-O`, and a checksum of "something is deeply wrong" should not
    be optimisable away.
    """
    if row is None:  # pragma: no cover - the functions raise rather than return no row
        raise DomainError(f"{function} returned no row and raised nothing")
    return row


# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Workflow:
    """One `workflow` row, as the database currently holds it.

    A faithful mirror of the columns, not an interpretation of them. `state` stays a plain `str`
    rather than a `WorkflowState` because the column is the source of truth and a value the code
    does not know yet should still be readable instead of raising on construction.
    """

    id: UUID
    ticket_ref: str
    patient_ref: str
    specialty: str | None
    state: str
    state_version: int
    generation: int
    owner_worker_id: str | None
    lease_expires_at: datetime | None
    human_owner_id: str | None
    human_claimed_at: datetime | None
    payload: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Self:
        return cls(
            id=row["id"],
            ticket_ref=row["ticket_ref"],
            patient_ref=row["patient_ref"],
            specialty=row["specialty"],
            state=row["state"],
            state_version=row["state_version"],
            generation=row["generation"],
            owner_worker_id=row["owner_worker_id"],
            lease_expires_at=row["lease_expires_at"],
            human_owner_id=row["human_owner_id"],
            human_claimed_at=row["human_claimed_at"],
            payload=row["payload"] or {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @property
    def is_human_owned(self) -> bool:
        """Whether an operator currently holds this workflow (§11).

        Informational only — for routing and for explaining a refusal. It is deliberately **not** a
        precondition anyone checks before writing: the guard trigger reads `human_owner_id` itself,
        and a caller that consults this property instead would be trusting a row it read earlier.
        """
        return self.human_owner_id is not None


@dataclass(frozen=True, slots=True)
class BookingRequest:
    """One `booking_request` row: a booking attempt authorised by exactly one approval (§9)."""

    id: UUID
    approval_id: UUID
    workflow_id: UUID
    patient_ref: str
    payload_hash: str
    state: str
    external_ref: str | None
    requested_by: str
    requested_at: datetime
    confirmed_at: datetime | None
    failed_at: datetime | None
    last_error: str | None
    payload: Mapping[str, Any]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Self:
        return cls(
            id=row["id"],
            approval_id=row["approval_id"],
            workflow_id=row["workflow_id"],
            patient_ref=row["patient_ref"],
            payload_hash=row["payload_hash"],
            state=row["state"],
            external_ref=row["external_ref"],
            requested_by=row["requested_by"],
            requested_at=row["requested_at"],
            confirmed_at=row["confirmed_at"],
            failed_at=row["failed_at"],
            last_error=row["last_error"],
            payload=row["payload"] or {},
        )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

#: `workflow_claim`'s own default. Repeated here only to document the caller-visible value.
DEFAULT_CLAIM_LEASE_SECONDS: Final[int] = 60
#: `workflow_advance`'s own default.
DEFAULT_ADVANCE_LEASE_SECONDS: Final[int] = 30


class WorkflowStore:
    """Typed access to `workflow_claim` / `workflow_advance` over one connection.

    Holds a *connection*, never a transaction. See the module docstring: the caller decides the
    transaction boundaries, and the only correct scope for these calls is a short one.
    """

    def __init__(self, conn: ConnectionLike) -> None:
        self._conn = conn

    def load(self, workflow_id: UUID | str) -> Workflow | None:
        """Read the current row, or None when no such workflow exists.

        Missing is a normal answer rather than an exception: the database defines no "not found"
        refusal, and inventing one here would give callers a rule to handle that the system does
        not actually have.
        """
        row = fetch_one(self._conn, "SELECT * FROM workflow WHERE id = %s", (workflow_id,))
        return None if row is None else Workflow.from_row(row)

    def claim(
        self,
        workflow_id: UUID | str,
        worker_id: str,
        *,
        expected_generation: int,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> Workflow:
        """Take (or retake, once the previous lease has expired) ownership of a workflow.

        `expected_generation` is the generation the caller *read* — passing 0 asserts "never
        claimed", not "give me anything". A loser of a claim race, or a caller working from a stale
        read, is refused by the database with :class:`~referraleasy_domain.errors.StaleOwnershipError`
        rather than allowed to proceed with a slightly-wrong idea of the world.
        """
        row = fetch_one(
            self._conn,
            "SELECT * FROM workflow_claim(%s, %s, %s, %s)",
            (workflow_id, worker_id, expected_generation, lease_seconds),
        )
        return Workflow.from_row(required_row(row, "workflow_claim"))

    def advance(
        self,
        workflow_id: UUID | str,
        worker_id: str,
        *,
        expected_generation: int,
        expected_state_version: int,
        to_state: str,
        lease_seconds: int = DEFAULT_ADVANCE_LEASE_SECONDS,
    ) -> Workflow:
        """Move a workflow the caller owns to `to_state`, and renew the lease by doing so.

        `to_state` is a plain state name; a contract `WorkflowState` may be passed directly, since
        it is a `StrEnum` and therefore already a `str`.

        Both tokens the caller read must be supplied, and both are checked in the one `UPDATE` that
        performs the write:

        * `expected_generation` — proves the caller is still the owner it thinks it is
          (:class:`~referraleasy_domain.errors.StaleOwnershipError`, RE001)
        * `expected_state_version` — proves the state has not moved under it
          (:class:`~referraleasy_domain.errors.IllegalTransitionError`, RE002, along with an edge the
          state machine does not have)

        A human owner, meanwhile, suspends automation entirely
        (:class:`~referraleasy_domain.errors.HumanOwnedError`, RE003).
        """
        row = fetch_one(
            self._conn,
            "SELECT * FROM workflow_advance(%s, %s, %s, %s, %s, %s)",
            (
                workflow_id,
                worker_id,
                expected_generation,
                expected_state_version,
                to_text(to_state),
                lease_seconds,
            ),
        )
        return Workflow.from_row(required_row(row, "workflow_advance"))

    # -- the booking gate ---------------------------------------------------

    def has_live_approval(
        self,
        workflow_id: UUID | str,
        patient_ref: str,
        state_version: int,
        booking_payload_hash: str,
        *,
        at: datetime | None = None,
    ) -> bool:
        """Whether an unconsumed, unexpired approval matches the full binding tuple (§9).

        All five parts must agree: workflow, patient, state_version, payload hash, and expiry.

        This answers a question; it does not authorise anything. The booking itself is refused by
        the composite foreign key and the consent guard whether or not anyone called this first, so
        a caller that skips it is not less safe — only less informed.
        """
        params: tuple[Any, ...] = (
            workflow_id,
            patient_ref,
            state_version,
            booking_payload_hash,
        )
        query = "SELECT approval_is_live(%s, %s, %s, %s)"
        if at is not None:
            query = "SELECT approval_is_live(%s, %s, %s, %s, %s)"
            params = (*params, at)
        return bool(fetch_scalar(self._conn, query, params))

    def record_booking(
        self,
        approval_id: UUID | str,
        requested_by: str,
        *,
        external_ref: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> BookingRequest:
        """Record a booking attempt against an approval.

        The binding columns are copied from the approval by the function itself, so there is no
        argument here for a hash the caller could get wrong: a booking exists only if the approval
        it names agrees with it on workflow, patient and payload hash, and
        `UNIQUE(approval_id)` makes a second booking on one approval impossible even after the
        first is cancelled (the approval is single-use, not re-usable).

        INSERTing also spends the approval. An expired, consumed, mismatched or non-existent
        approval is refused with :class:`~referraleasy_domain.errors.ApprovalRefusedError` (RE004).
        """
        # Imported here, not at module scope: routing a jsonb parameter needs the driver, and the
        # rest of this module deliberately does not (see `_translated`).
        from psycopg.types.json import Jsonb

        row = fetch_one(
            self._conn,
            "SELECT * FROM booking_request_create(%s, %s, %s, %s)",
            (approval_id, requested_by, external_ref, Jsonb(dict(payload or {}))),
        )
        return BookingRequest.from_row(required_row(row, "booking_request_create"))
