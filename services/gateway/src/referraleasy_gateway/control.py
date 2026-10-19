"""The control-state port: what the gateway needs from the workflow store, and nothing more.

§7 puts the rules in PostgreSQL — transitions are atomic and optimistically version-checked, retries
are deduplicated by `command_id`, and a booking exists only against a live approval. The gateway must
not restate any of that, so it does not: this module is a *port* with the operations the transport
actually performs, and **two** implementations of it that a reader can compare line by line, because
the correspondence between them is the thing that has to stay true.

* `InMemoryControlState` — the memory profile, for the offline demo and the unit job, which run with
  no database at all. It enforces the two checks that are the transport's own contract rather than
  the state machine's: **the version the caller presented must be the version the row is at** (§9: a
  stale `ClientAction` is refused, never applied) and **one command_id has one outcome** (§7: replay
  is deduplicated). It also substitutes for the database's transition guard, from `transitions.py` —
  the transcription `tests/gateway/test_policy_table.py` re-parses out of migration 014 on every run
  — because the memory profile has no `workflow_transition` table to be wrong for it. It is a
  stand-in for the *rules the transport owns*, not a simulation of PostgreSQL.
* `PostgresControlState` — the same port over `referraleasy_domain` and the real database, selected by
  `GATEWAY_CONTROL_BACKEND=postgres`. It holds no rule of its own: the transition table, ownership and
  the lease, `UNIQUE(approval_id)` and command dedup are the schema's, and this class is the shape
  that lets the transport perform its eight operations against them. Where it *does* compare
  something — the presented `state_version`, and whether an edge exists — it asks the database
  (`workflow_transition`, or the row itself) and says so at the call site.

**The one thing both profiles must do identically, and why the database does not do it for them.**
The version check is the transport's contract: a card or a booking names the version it was issued
against, and a caller whose view is behind must be refused rather than merged (§7). PostgreSQL
enforces it too — `workflow_advance`'s `WHERE` clause and `workflow_guard` both refuse a stale write
— but it reports that refusal as `RE001`, because `workflow_advance` cannot tell "you are stale" from
"you do not own this row" in a single statement. So `PostgresControlState` names the refusal the port
documents. It is a *naming*, not the enforcement: the read that produces it authorises nothing, and a
race that slips past it is still refused by the database, as `RE001`, which is the correct answer
there. `tests/gateway/test_postgres_control_state.py` asserts both halves of that.

**Why the booking write is not one statement.** `record_booking` with a transition is the longest
operation here — the workflow read, the claim when one is needed, the insert, the outcome update when
there is one, and the move — and the obvious reaction ("that should be a single statement") has a
specific answer rather than a stylistic one: the parts of a data-modifying CTE cannot see each other's
effects, so a sibling CTE cannot read the row another one inserted, and 008's `booking_request_guard`
is a `BEFORE UPDATE` trigger, which means an outcome can only be recorded by *updating* the row the
insert wrote. The smallest correct unit is therefore a short transaction in which every statement
commits together or none of them do, and which ends with the request rather than outliving it — §7's
rule is about transactions that span work, not about the statements that are the work.

Command dedup reuses `referraleasy_domain`'s own `CommandDecision`/`CommandRecord` types rather than
defining gateway-shaped twins, so a caller cannot tell the two profiles apart and a change to the
ledger's semantics is one change.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

from referraleasy_contracts.generated.common import BookingState, WorkflowState
from referraleasy_domain import (
    ApprovalRefusedError,
    BookingRequest,
    CommandConflictError,
    CommandDecision,
    CommandLedger,
    CommandRecord,
    IllegalTransitionError,
    Workflow,
    WorkflowStore,
)
from referraleasy_domain.approval import PAYLOAD_HASH_KEY
from referraleasy_domain.workflow import fetch_all, fetch_one, fetch_scalar
from referraleasy_gateway.continuations import (
    ContinuationClaim,
    ContinuationRecord,
    ContinuationStore,
)
from referraleasy_gateway.errors import ContractViolationError
from referraleasy_gateway.psql import session
from referraleasy_gateway.transitions import is_legal_edge

__all__ = [
    "BookingAttempt",
    "BookingRow",
    "ControlState",
    "InMemoryControlState",
    "PostgresControlState",
    "SeedableControlState",
    "WorkflowRow",
]


@dataclass(frozen=True, slots=True)
class WorkflowRow:
    """One workflow, as the gateway needs to see it.

    A projection of the database row rather than a mirror of it: the columns nothing in the transport
    reads (owner lease, audit columns) are absent, so the port cannot grow a dependency on them by
    accident.
    """

    workflow_id: UUID
    ticket_ref: str
    patient_ref: str
    state: str
    state_version: int
    generation: int
    payload: Mapping[str, Any]

    @property
    def booking_payload_hash(self) -> str | None:
        """The hash of the payload the patient would be consenting to (§9).

        It travels on `workflow.payload` under the key the database's consent guard reads, which is
        why the key is imported from the domain rather than spelled out here.
        """
        value = self.payload.get(PAYLOAD_HASH_KEY)
        return value if isinstance(value, str) else None


#: The contract's `BookingState`, the column `booking_request.state`, and the only place they meet.
#:
#: The contract's vocabulary is the port's: a caller asks for a `BookingState` and
#: `BookingRow.state` answers with one. 008's column has four values of its own, its guard
#: (`booking_request_guard`) turns on them, and 014's consent guard reads `state = 'confirmed'` to
#: decide whether a workflow may be marked `BOOKED` — so the two vocabularies have to be joined
#: *somewhere*, and joining them in one table is what keeps a third spelling from appearing.
#:
#: Two of the contract's six values are absent on purpose, and they are refused by name rather than
#: mapped to the nearest of the four: `PENDING_REVIEW` (no coordinator has looked yet) and
#: `CANCEL_PENDING` are not states this service can record, and a mapping that guessed would write a
#: value the contract does not mean. `CONFIRMED` carries the same hazard one level down — 008's
#: `booking_request_confirmed_has_ref` wants the hospital's own reference with it, and one invented
#: in this file would name an appointment nobody issued.
_BOOKING_COLUMN: Final[dict[BookingState, str]] = {
    BookingState.REQUESTED: "pending",
    BookingState.CONFIRMED: "confirmed",
    BookingState.REJECTED: "failed",
    BookingState.CANCELLED: "cancelled",
}

#: The column back to the contract's state, derived so the two directions cannot drift apart.
_BOOKING_STATE: Final[dict[str, BookingState]] = {
    column: state for state, column in _BOOKING_COLUMN.items()
}


def _assert_recordable_booking(outcome: BookingState, external_ref: str | None) -> None:
    """Refuse an outcome this port cannot write, naming the constraint that would refuse it later.

    A *naming*, not a second enforcement: an outcome outside `booking_request_state_known`, and a
    `confirmed` row with no `external_ref` (`booking_request_confirmed_has_ref`), are both refused by
    the database. Asking here means the caller gets "the contract does not have this state" instead
    of a driver error from inside an INSERT, and it means the memory profile — which has no CHECK to
    fail on — refuses exactly what the database refuses.
    """
    if outcome not in _BOOKING_COLUMN:
        recordable = ", ".join(sorted(state.value for state in _BOOKING_COLUMN))
        raise ContractViolationError(
            f"{outcome!r} is not a booking outcome this port can record; the recordable ones are "
            f"{recordable}. `PENDING_REVIEW` and `CANCEL_PENDING` describe a booking nobody has "
            "written yet (008's booking_request_state_known)"
        )
    if outcome == BookingState.CONFIRMED and not (external_ref or "").strip():
        raise ContractViolationError(
            "a confirmed booking must carry the hospital's own external_ref: 008's "
            "`booking_request_confirmed_has_ref` refuses a `confirmed` row without one, and a "
            "reference invented here would name an appointment no hospital issued. A caller with no "
            "confirmation records "
            f"{BookingState.REQUESTED.value!r} instead, which is how a booking ends up in "
            f"{WorkflowState.BOOKING_UNVERIFIED.value}"
        )


def _booking_transition(
    advance_to: str | None, expected_state_version: int | None
) -> tuple[str, int] | None:
    """The transition a booking is recorded with, or `None` for a booking recorded alone.

    A booking is written with its transition or without one, never with half of one — and the half
    worth naming is the version, because a move that presented a version this process made up instead
    of the one the caller read is exactly the merge §7's version check exists to refuse.
    """
    if advance_to is None and expected_state_version is None:
        return None
    if advance_to is None or expected_state_version is None:
        raise ContractViolationError(
            "advance_to and expected_state_version are one request: pass both to record the booking "
            "and its transition together, or neither to record only the booking "
            f"(got advance_to={advance_to!r}, expected_state_version={expected_state_version!r})"
        )
    return advance_to, expected_state_version


@dataclass(frozen=True, slots=True)
class BookingRow:
    """One booking attempt. `UNIQUE(approval_id)` is the database's; here it is a listed invariant.

    **`state` is the contract's `BookingState` in both profiles, and that is now the port's one
    answer.** It used to be two: the memory profile wrote `REQUESTED` and a real row read `pending`
    for the same fact, with this docstring explaining why the difference was reported rather than
    smoothed over. What changed is not the profiles but the *port* — `record_booking` carries the
    outcome the caller received, so a booking the gateway asked for and a booking a hospital
    confirmed are no longer the same write, and the two vocabularies are joined once, at
    `_BOOKING_COLUMN`, instead of being left for every caller to reconcile.
    """

    booking_id: UUID
    workflow_id: UUID
    approval_id: UUID
    patient_ref: str
    payload_hash: str
    state: BookingState
    external_ref: str | None
    requested_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "booking_id": str(self.booking_id),
            "workflow_id": str(self.workflow_id),
            "approval_id": str(self.approval_id),
            "state": self.state.value,
            "payload_hash": self.payload_hash,
            "external_ref": self.external_ref,
            "requested_at": self.requested_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class BookingAttempt:
    """What one `record_booking` did: the row it wrote, and the move it made with it.

    `workflow` is `None` when the caller asked for the row on its own — a caller that has an outcome
    to record and no transition to make. When it is not `None`, the row and the transition were
    written by the same operation, so a transition that is refused leaves no booking row behind:
    that is §7's "state transitions are atomic" applied to the pair, and in the PostgreSQL profile
    it is
    one transaction rather than two writes that happen to follow each other.
    """

    booking: BookingRow
    workflow: WorkflowRow | None


def _handoff_record(payload: Mapping[str, Any], ticket_ref: str, consent_at: str) -> dict[str, Any]:
    draft = payload.get("handoff_draft")
    if not isinstance(draft, Mapping) or not draft.get("patient_visible_summary"):
        raise ContractViolationError("No reviewed handoff summary is recorded.")
    existing = payload.get("handoff")
    if isinstance(existing, Mapping) and existing.get("consent_given_at"):
        return dict(existing)
    return {
        **draft,
        "consent_given_at": consent_at,
        "handoff_id": str(uuid4()),
        "ticket_ref": f"HND-{ticket_ref}",
        "status": "queued",
    }


class ControlState(ContinuationStore, Protocol):
    """Version-checked workflow operations and atomic continuation storage."""

    def load(self, workflow_id: UUID) -> WorkflowRow | None:
        """Return the current workflow, if it exists."""
        ...

    def queue_handoff(
        self, workflow_id: UUID, *, expected_state_version: int, consent_at: str
    ) -> Mapping[str, Any]: ...

    def confirm_booking(
        self, workflow_id: UUID, *, expected_state_version: int, external_ref: str
    ) -> BookingAttempt: ...

    def advance(
        self, workflow_id: UUID, *, expected_state_version: int, to_state: str
    ) -> WorkflowRow:
        """Move the workflow, refusing a stale version. Raises `IllegalTransitionError` (RE002)."""
        ...

    def assert_can_advance(
        self, workflow_id: UUID, *, expected_state_version: int, to_state: str
    ) -> None:
        """Whether `advance` would accept this move, with nothing written. Raises the same refusals.

        The transport has work to do between the two calls — the props for the destination card are
        validated, and a card that cannot be rendered has to refuse the whole command rather than
        leave the workflow advanced showing nothing. Asking first is what makes that ordering
        possible; answering the *state machine's* refusals first is what keeps an impossible target
        from being reported as a props problem.
        """
        ...

    def update_payload(
        self, workflow_id: UUID, *, expected_state_version: int, patch: Mapping[str, Any]
    ) -> WorkflowRow:
        """Record a patient's answer. Does not move the workflow."""
        ...

    def begin_command(self, command_id: str, request_hash: str) -> CommandDecision:
        """Open a command, or report that it is already recorded."""
        ...

    def complete_command(
        self,
        command_id: str,
        request_hash: str,
        *,
        response_status: int,
        response: Mapping[str, Any],
    ) -> CommandRecord:
        """Record a command's outcome so a retry replays it rather than repeating it."""
        ...

    def record_booking(
        self,
        *,
        workflow_id: UUID,
        approval_id: UUID,
        patient_ref: str,
        payload_hash: str,
        payload: Mapping[str, Any],
        requested_by: str,
        outcome: BookingState = BookingState.REQUESTED,
        external_ref: str | None = None,
        advance_to: str | None = None,
        expected_state_version: int | None = None,
    ) -> BookingAttempt:
        """Write the booking row, and optionally move the workflow it authorises, as one operation.

        **The outcome is the caller's, and it defaults to asking.** A caller with no answer from a
        hospital records `REQUESTED` — 008's `pending` — and stores no reference, which is what every
        previous caller of this method meant. A caller that *has* a confirmation records `CONFIRMED`
        with the hospital's own `external_ref`, and that is the only thing that makes
        `workflow.state = 'BOOKED'` reachable: 014's `workflow_booking_consent_guard` refuses that
        state unless a `confirmed` `booking_request` exists, and the guard is the authority here, not
        this method. Nothing about the guard is relaxed by this signature; what it adds is a way for
        a caller that *has* the confirmation to say so.

        `advance_to` and `expected_state_version` are the other half. Passing them writes the row and
        the transition as one operation, which is not a convenience: the confirmation has to exist
        before the transition is attempted, and a transition refused *after* the row was written
        would leave a booking against a spent approval on a workflow that never moved. Both profiles
        therefore guarantee "both or neither" — the PostgreSQL profile with one transaction, the
        memory profile by taking the row back out — and `BookingAttempt.workflow` is `None` only when
        the caller asked for the row on its own.

        Refuses a second booking against an approval already spent. The memory profile raises
        `ApprovalRefusedError` (RE004) for that, restating `UNIQUE(approval_id)`. The postgres
        profile does not: its database refuses the write with the unique index itself — a `23505`,
        arriving as `referraleasy_domain.DomainError` — because the index is checked when the row is
        inserted and 008's consume trigger is `AFTER INSERT`. The refusal is the same refusal; the
        code is not the same code. Recorded here rather than asserted away, and in
        `tests/gateway/test_postgres_control_state.py` on both sides. In the product it does not
        surface: `CommandService.book` checks the approval one layer up and answers RE004 from there,
        so this method is the last gate rather than the first.

        Refuses an `outcome` this port cannot write, and a `CONFIRMED` one with no `external_ref`, as
        `ContractViolationError` (422): see `_assert_recordable_booking`, which names the two
        constraints that make the same refusal in the database.
        """
        ...

    def bookings_for(self, workflow_id: UUID) -> tuple[BookingRow, ...]:
        """Every booking attempt for a workflow. The evidence for "exactly one booking"."""
        ...


class SeedableControlState(ControlState, Protocol):
    """A `ControlState` that can also insert a workflow. **Not part of the port.**

    §7's workflows are created by the referral intake flow, and the gateway only ever reads them —
    so `create` is deliberately absent from `ControlState`, which is the surface the transport is
    written against and which must not grow a write nothing in §7 asks for. What needs it is one
    demo route: a demo has to start somewhere, and §18 asks the starting point to be labelled rather
    than implied. Both profiles implement it because both are used to run the same tests, and the
    tests are the reason it is typed at all rather than reached for with a `getattr`.
    """

    def create(
        self,
        patient_ref: str,
        *,
        ticket_ref: str = ...,
        state: str = ...,
        payload: Mapping[str, Any] | None = ...,
        workflow_id: UUID | None = ...,
    ) -> WorkflowRow:
        """Insert a workflow at `state_version` 1. A fixture, in both profiles."""
        ...


@dataclass
class _WorkflowRecord:
    workflow_id: UUID
    ticket_ref: str
    patient_ref: str
    state: str
    state_version: int
    generation: int
    payload: dict[str, Any] = field(default_factory=dict)


class InMemoryControlState:
    """The memory profile. Version-checked, command-deduplicated, not persisted.

    The version check is the one place in this class that *is* the transport's rule rather than the
    state machine's, and it is written the way §7 states it: a write presents the version it read, and
    a mismatch is a refusal (`RE002`) instead of a merge. The legal edges come from `transitions.py`
    for the reason the module docstring gives, and `assert_can_advance` asks the same questions
    `advance` answers without moving anything — so the transport can refuse a destination card it
    cannot render *before* the move rather than discovering it afterwards. One of those questions is
    the database's rather than the state machine's — whether a booking has been confirmed, before
    `BOOKED` — and this profile carries it for the same reason it carries the edge table: a
    deployment profile that answered it differently would be a bug the other profile could not see.
    """

    def __init__(self) -> None:
        self._workflows: dict[UUID, _WorkflowRecord] = {}
        self._bookings: dict[UUID, list[BookingRow]] = {}
        self._commands: dict[str, CommandRecord] = {}
        self._lock = RLock()

    def pending_continuations(self) -> tuple[UUID, ...]:
        with self._lock:
            return tuple(
                row.workflow_id for row in self._workflows.values() if self._continuation_due(row)
            )[:32]

    def _continuation_due(self, row: _WorkflowRecord) -> bool:
        job = ContinuationRecord.read(row.payload)
        command = self._commands.get(job.command_id) if job else None
        return job is not None and job.due(
            committed=command is not None and command.status == "completed", now=datetime.now(UTC)
        )

    def claim_continuation(
        self, workflow_id: UUID, token: str, *, lease_seconds: float
    ) -> ContinuationClaim | None:
        with self._lock:
            row = self._workflows.get(workflow_id)
            if row is None or not self._continuation_due(row):
                return None
            job = ContinuationRecord.read(row.payload)
            assert job is not None
            claimed = replace(
                job, owner=token, lease_until=datetime.now(UTC) + timedelta(seconds=lease_seconds)
            )
            row.payload.update(claimed.patch())
            return claimed.claimed(workflow_id, token)

    def finish_continuation(self, claim: ContinuationClaim) -> bool:
        with self._lock:
            row = self._workflows.get(claim.workflow_id)
            job = ContinuationRecord.read(row.payload) if row else None
            if row is None or job is None or not job.held_by(claim, now=datetime.now(UTC)):
                return False
            row.payload.update(replace(job, pending=False).patch())
            return True

    def queue_handoff(
        self, workflow_id: UUID, *, expected_state_version: int, consent_at: str
    ) -> Mapping[str, Any]:
        with self._lock:
            row = self._require(workflow_id)
            if row.state_version != expected_state_version or row.state != "HUMAN_HANDOFF":
                raise IllegalTransitionError("Handoff consent names a stale workflow.")
            staged = _handoff_record(row.payload, row.ticket_ref, consent_at)
            row.payload["handoff"] = staged
            return staged

    def confirm_booking(
        self, workflow_id: UUID, *, expected_state_version: int, external_ref: str
    ) -> BookingAttempt:
        _assert_recordable_booking(BookingState.CONFIRMED, external_ref)
        with self._lock:
            row = self._require(workflow_id)
            if row.state != "BOOKING_UNVERIFIED" or row.state_version != expected_state_version:
                raise IllegalTransitionError("Reconciliation names a stale workflow.")
            bookings = self._bookings.get(workflow_id, [])
            index = next(
                (i for i, b in enumerate(bookings) if b.state is BookingState.REQUESTED), None
            )
            if index is None:
                raise ContractViolationError("No pending booking can be reconciled.")
            original = bookings[index]
            confirmed = replace(original, state=BookingState.CONFIRMED, external_ref=external_ref)
            bookings[index] = confirmed
            try:
                moved = self.advance(
                    workflow_id, expected_state_version=expected_state_version, to_state="BOOKED"
                )
            except Exception:
                bookings[index] = original
                raise
            return BookingAttempt(booking=confirmed, workflow=moved)

    def renew_continuation(
        self, claim: ContinuationClaim, *, lease_seconds: float
    ) -> WorkflowRow | None:
        with self._lock:
            row = self._workflows.get(claim.workflow_id)
            job = ContinuationRecord.read(row.payload) if row else None
            now = datetime.now(UTC)
            if row is None or job is None or not job.held_by(claim, now=now):
                return None
            row.payload.update(
                replace(job, lease_until=now + timedelta(seconds=lease_seconds)).patch()
            )
            return self._row(row)

    # -- seeding and demo helpers ---------------------------------------------------------------

    def create(
        self,
        patient_ref: str,
        *,
        ticket_ref: str = "T-DEMO-1",
        state: str = WorkflowState.INTAKE.value,
        payload: Mapping[str, Any] | None = None,
        workflow_id: UUID | None = None,
    ) -> WorkflowRow:
        """Insert a workflow. Not part of the port: the PostgreSQL profile's workflows are inserted
        by whatever creates a referral, and the gateway only ever reads them.

        `state_version` starts at 1, not 0, because that is `workflow`'s column default in 003 and
        the check constraint is `state_version >= 1`. A fixture at version 0 would accept a card at
        version 0 that the real row could never have issued.
        """
        record = _WorkflowRecord(
            workflow_id=workflow_id or uuid4(),
            ticket_ref=ticket_ref,
            patient_ref=patient_ref,
            state=state,
            state_version=1,
            generation=0,
            payload=dict(payload or {}),
        )
        with self._lock:
            self._workflows[record.workflow_id] = record
        return self._row(record)

    # -- the port -------------------------------------------------------------------------------

    def load(self, workflow_id: UUID) -> WorkflowRow | None:
        with self._lock:
            record = self._workflows.get(workflow_id)
            return None if record is None else self._row(record)

    def advance(
        self, workflow_id: UUID, *, expected_state_version: int, to_state: str
    ) -> WorkflowRow:
        self._assert_known_state(to_state)
        with self._lock:
            record = self._require(workflow_id)
            self._assert_move_is_possible(
                workflow_id,
                record,
                expected_state_version=expected_state_version,
                to_state=to_state,
            )
            self._assert_booked_needs_a_confirmation(workflow_id, to_state)
            record.state = to_state
            record.state_version += 1
            record.generation += 1
            return self._row(record)

    def assert_can_advance(
        self, workflow_id: UUID, *, expected_state_version: int, to_state: str
    ) -> None:
        """The refusals `advance` makes, asked without the write.

        One implementation, two callers: a pre-flight that repeated the checks could become laxer than
        the write it precedes, and the drift would be invisible — the write would still refuse, the
        transport would simply have done work it should not have. The lock is taken for the length of
        the read, like every other read here.
        """
        self._assert_known_state(to_state)
        with self._lock:
            self._assert_move_is_possible(
                workflow_id,
                self._require(workflow_id),
                expected_state_version=expected_state_version,
                to_state=to_state,
            )
            self._assert_booked_needs_a_confirmation(workflow_id, to_state)

    def _assert_booked_needs_a_confirmation(self, workflow_id: UUID, to_state: str) -> None:
        """014's `workflow_booking_consent_guard`, second clause: `BOOKED` needs a confirmation.

        The memory profile's restatement of a database rule, in the same spirit as
        `_assert_move_is_possible`'s edge check and for the same reason: this profile has no trigger
        to substitute for, so it substitutes *itself*, and both profiles then answer the same
        question the same way. Without this the memory profile would mark a workflow `BOOKED` on a
        booking nobody confirmed, and the two profiles would disagree about the one rule §7 states
        most plainly — a confirmed booking is what `booked` means.

        Called with the lock held, next to the other refusals, and last: the state machine answers
        first (as the triggers fire after `workflow_advance`'s own checks), so a move that is already
        illegal as an edge is reported as that rather than as a consent problem.
        """
        if to_state != WorkflowState.BOOKED.value:
            return
        if any(row.state == BookingState.CONFIRMED for row in self._bookings.get(workflow_id, ())):
            return
        raise ApprovalRefusedError(
            f"workflow {workflow_id} cannot be marked BOOKED: no confirmed booking_request exists "
            "(RE004, 014's workflow_booking_consent_guard)"
        )

    @staticmethod
    def _assert_known_state(to_state: str) -> None:
        try:
            WorkflowState(to_state)
        except ValueError as exc:
            # A state the contract does not have is not a transition, it is a typo. Refused here
            # rather than stored, so the memory profile cannot hold a state the database never would.
            raise IllegalTransitionError(
                f"{to_state!r} is not one of the sixteen workflow states (RE002)"
            ) from exc

    @staticmethod
    def _assert_move_is_possible(
        workflow_id: UUID,
        record: _WorkflowRecord,
        *,
        expected_state_version: int,
        to_state: str,
    ) -> None:
        """Everything that makes a move illegal, in the order the refusals are useful."""
        if to_state == record.state:
            # `workflow_transition_no_self_loop` in 003. A state_version bumped without a state
            # change would make the transition counter meaningless, and 003's trigger refuses it
            # for the same reason.
            raise IllegalTransitionError(
                f"workflow {workflow_id} is already in {to_state}; state_version is the "
                "transition counter and moves only with a transition (RE002)"
            )
        if not is_legal_edge(record.state, to_state):
            # The PostgreSQL profile never reaches this branch: `workflow_advance` reads
            # `workflow_transition` and raises RE002 for an edge that is not in it. The memory
            # profile substitutes for those constraints, so it carries them — see `transitions`.
            raise IllegalTransitionError(
                f"{record.state} -> {to_state} is not an edge in workflow_transition; the state "
                "machine is the database's, and this profile mirrors it rather than inventing "
                "its own (RE002, §7)"
            )
        if record.state_version != expected_state_version:
            raise IllegalTransitionError(
                f"workflow {workflow_id} is at state_version {record.state_version}, but the "
                f"caller presented {expected_state_version}; a stale version is refused rather "
                "than applied (RE002, §7)"
            )

    def update_payload(
        self, workflow_id: UUID, *, expected_state_version: int, patch: Mapping[str, Any]
    ) -> WorkflowRow:
        with self._lock:
            record = self._require(workflow_id)
            if record.state_version != expected_state_version:
                raise IllegalTransitionError(
                    f"workflow {workflow_id} is at state_version {record.state_version}, but the "
                    f"caller presented {expected_state_version} (RE002, §7)"
                )
            record.payload.update(patch)
            return self._row(record)

    def begin_command(self, command_id: str, request_hash: str) -> CommandDecision:
        with self._lock:
            existing = self._commands.get(command_id)
            if existing is None:
                record = CommandRecord(
                    command_id=command_id,
                    request_hash=request_hash,
                    status="in_progress",
                    response_status=None,
                    response=None,
                    error=None,
                    created_at=datetime.now(UTC),
                    completed_at=None,
                    expires_at=None,
                )
                self._commands[command_id] = record
                return CommandDecision(record=record)
            if existing.request_hash != request_hash:
                raise CommandConflictError(
                    f"command_id {command_id} is already recorded with a different request; "
                    "reusing an id for a different body is refused (RE005, §7)"
                )
            return CommandDecision(record=existing)

    def complete_command(
        self,
        command_id: str,
        request_hash: str,
        *,
        response_status: int,
        response: Mapping[str, Any],
    ) -> CommandRecord:
        with self._lock:
            existing = self._commands.get(command_id)
            if existing is None or existing.request_hash != request_hash:
                raise CommandConflictError(
                    f"no command {command_id} is open with this request hash (RE005, §7)"
                )
            if existing.is_terminal:
                raise CommandConflictError(
                    f"command {command_id} is already {existing.status}; it cannot be completed "
                    "again (RE005, §7)"
                )
            completed = CommandRecord(
                command_id=command_id,
                request_hash=request_hash,
                status="completed",
                response_status=response_status,
                response=dict(response),
                error=None,
                created_at=existing.created_at,
                completed_at=datetime.now(UTC),
                expires_at=None,
            )
            self._commands[command_id] = completed
            return completed

    def record_booking(
        self,
        *,
        workflow_id: UUID,
        approval_id: UUID,
        patient_ref: str,
        payload_hash: str,
        payload: Mapping[str, Any],
        requested_by: str,
        outcome: BookingState = BookingState.REQUESTED,
        external_ref: str | None = None,
        advance_to: str | None = None,
        expected_state_version: int | None = None,
    ) -> BookingAttempt:
        """Write the row, and move the workflow with it, without persisting either.

        `payload` and `requested_by` are accepted and not kept: this profile has no payload column
        and no evidence to write, and the binding it would have to reproduce is checked one layer up
        by `CommandService.book`. The database's copy of them is its own (008 reproduces the binding
        out of the approval row), which is documented on the PostgreSQL implementation.

        The pair — the write and the transition — is emulated rather than transactional: a move that
        `advance` refuses takes the row back out. Emulating it is the point. A caller that books, is
        refused, and then reads `bookings_for` must see what the other profile's rollback leaves
        behind, which is nothing; a booking row against a spent approval on a workflow that never
        moved is a state neither profile should be able to produce.
        """
        _assert_recordable_booking(outcome, external_ref)
        transition = _booking_transition(advance_to, expected_state_version)
        with self._lock:
            rows = self._bookings.setdefault(workflow_id, [])
            for row in rows:
                if row.approval_id == approval_id:
                    # `UNIQUE(approval_id)`, restated: an approval authorises exactly one booking,
                    # even after the first is cancelled (§9, and 008's comment on the constraint).
                    raise ApprovalRefusedError(
                        f"approval {approval_id} already authorised booking {row.booking_id}; an "
                        "approval is single-use, so a second booking needs a second approval "
                        "(RE004, §9)"
                    )
            written = BookingRow(
                booking_id=uuid4(),
                workflow_id=workflow_id,
                approval_id=approval_id,
                patient_ref=patient_ref,
                payload_hash=payload_hash,
                state=outcome,
                external_ref=external_ref,
                requested_at=datetime.now(UTC),
            )
            rows.append(written)
        if transition is None:
            return BookingAttempt(booking=written, workflow=None)
        to_state, version = transition
        try:
            # Outside the lock: `advance` takes it, and a `Lock` is not reentrant.
            moved = self.advance(workflow_id, expected_state_version=version, to_state=to_state)
        except Exception:
            # The rollback, in the one place this profile can perform it. `bookings_for` and the
            # `UNIQUE(approval_id)` loop above both read this list, so a row left here would be a
            # booking that the other profile would have rolled back — and the approval that the
            # failed attempt would have spent is still live, because nothing was ever consumed.
            with self._lock:
                rows.remove(written)
            raise
        return BookingAttempt(booking=written, workflow=moved)

    def bookings_for(self, workflow_id: UUID) -> tuple[BookingRow, ...]:
        with self._lock:
            return tuple(self._bookings.get(workflow_id, ()))

    # -- internals ------------------------------------------------------------------------------

    def _require(self, workflow_id: UUID) -> _WorkflowRecord:
        record = self._workflows.get(workflow_id)
        if record is None:  # pragma: no cover - callers load first and answer 404
            raise IllegalTransitionError(f"no workflow {workflow_id} (RE002)")
        return record

    @staticmethod
    def _row(record: _WorkflowRecord) -> WorkflowRow:
        return WorkflowRow(
            workflow_id=record.workflow_id,
            ticket_ref=record.ticket_ref,
            patient_ref=record.patient_ref,
            state=record.state,
            state_version=record.state_version,
            generation=record.generation,
            payload=dict(record.payload),
        )


# ---------------------------------------------------------------------------
# The PostgreSQL profile
# ---------------------------------------------------------------------------


class PostgresControlState:
    """The deployment profile: the same port, over `referraleasy_domain` and the real database.

    Four things decide this class's shape, and each of them is a constraint rather than a choice:

    **One connection per operation, opened and closed around the statements that make it up.** §7
    forbids holding a transaction open across a workflow, and these operations are single statements
    or a short series of them — `record_booking` with a transition is the longest of them, a handful
    of statements in one transaction that commits before the card is rendered (see the module
    docstring for why that transaction is not one statement). There is no pool because the workspace
    has no pool dependency, and
    inventing a half-pool here — a shared connection with a lock, or a hand-rolled checkout — would
    trade a visible cost (a connect per request on a local socket) for an invisible one (a stale
    connection that fails once, under load, in the one service that has no retry).

    **A worker identity, because `workflow_advance` requires one.** §7 gives every workflow an
    `owner_worker_id` and a live `lease_expires_at`, and `workflow_advance` refuses a caller that
    does not hold the row. The gateway *is* the actor that performs this transition — in the demo
    profile there is no agent runtime, and `POST /transitions` is the coordinator (§4) — so it takes
    the row for the length of one request with `workflow_claim`, exactly as a worker would. The lease
    is short and renewable; a gateway that dies mid-request leaves a claim that expires, not a row
    that is stuck.

    **Reads that decide an answer read the database.** Whether an edge exists is a question for
    `workflow_transition`, and whether the caller's version is current is a question for the row.
    Neither answer is transcribed into this file: the memory profile needs `transitions.py` because it
    has no table, and this profile has the table.

    **`create` is not part of the port.** It exists for the same reason
    `InMemoryControlState.create` does — the demo's seed route has to put a workflow *somewhere* — and
    it is labelled as a fixture in both, because workflows in a real deployment are created by the
    referral intake flow, not by the surface that renders them.
    """

    def __init__(self, dsn: str, *, worker_id: str) -> None:
        self._dsn = dsn
        self._worker_id = worker_id

    @property
    def worker_id(self) -> str:
        """The identity this store claims rows under, so a log line can name it."""
        return self._worker_id

    def pending_continuations(self) -> tuple[UUID, ...]:
        with self._session() as connection:
            rows = fetch_all(
                connection,
                """
                SELECT w.id FROM workflow w JOIN command_dedup c
                  ON c.command_id = w.payload->'coordination_job'->>'command_id'
                WHERE c.status = 'completed'
                  AND w.payload->'coordination_job'->>'pending' = 'true'
                  AND coalesce((w.payload->'coordination_job'->>'lease_until')::timestamptz,
                               '-infinity'::timestamptz) <= now()
                ORDER BY w.created_at LIMIT 32
            """,
                (),
            )
        return tuple(row["id"] for row in rows)

    def claim_continuation(
        self, workflow_id: UUID, token: str, *, lease_seconds: float
    ) -> ContinuationClaim | None:
        with self._session() as connection:
            row = fetch_one(
                connection,
                """
                UPDATE workflow w SET payload = jsonb_set(payload, '{coordination_job}',
                    payload->'coordination_job' || jsonb_build_object(
                        'owner', %s::text, 'lease_until', now() + %s * interval '1 second'))
                WHERE w.id = %s AND payload->'coordination_job'->>'pending' = 'true'
                  AND coalesce((payload->'coordination_job'->>'lease_until')::timestamptz,
                               '-infinity'::timestamptz) <= now()
                  AND EXISTS (SELECT 1 FROM command_dedup c WHERE c.command_id =
                    w.payload->'coordination_job'->>'command_id' AND c.status = 'completed')
                RETURNING w.*
            """,
                (token, lease_seconds, workflow_id),
            )
        if row is None:
            return None
        job = ContinuationRecord.read(row["payload"])
        assert job is not None
        return job.claimed(workflow_id, token)

    def finish_continuation(self, claim: ContinuationClaim) -> bool:
        with self._session() as connection:
            row = fetch_one(
                connection,
                """
                UPDATE workflow SET payload = jsonb_set(payload, '{coordination_job,pending}', 'false')
                WHERE id = %s AND payload->'coordination_job'->>'command_id' = %s
                  AND payload->'coordination_job'->>'owner' = %s
                  AND payload->'coordination_job'->>'pending' = 'true'
                  AND (payload->'coordination_job'->>'lease_until')::timestamptz > now()
                RETURNING id
            """,
                (claim.workflow_id, claim.command_id, claim.token),
            )
        return row is not None

    def renew_continuation(
        self, claim: ContinuationClaim, *, lease_seconds: float
    ) -> WorkflowRow | None:
        with self._session() as connection:
            row = fetch_one(
                connection,
                """
                UPDATE workflow SET payload = jsonb_set(payload, '{coordination_job,lease_until}',
                    to_jsonb(now() + %s * interval '1 second'))
                WHERE id = %s AND payload->'coordination_job'->>'command_id' = %s
                  AND payload->'coordination_job'->>'owner' = %s
                  AND payload->'coordination_job'->>'pending' = 'true'
                  AND (payload->'coordination_job'->>'lease_until')::timestamptz > now()
                RETURNING *
            """,
                (lease_seconds, claim.workflow_id, claim.command_id, claim.token),
            )
        return _project(Workflow.from_row(row)) if row is not None else None

    def queue_handoff(
        self, workflow_id: UUID, *, expected_state_version: int, consent_at: str
    ) -> Mapping[str, Any]:
        with self._session() as connection, connection.transaction():
            locked = fetch_one(
                connection, "SELECT * FROM workflow WHERE id = %s FOR UPDATE", (workflow_id,)
            )
            if locked is None:
                raise IllegalTransitionError("The handoff workflow does not exist.")
            row = Workflow.from_row(locked)
            _assert_current_version(row, expected_state_version)
            if row.state != "HUMAN_HANDOFF":
                raise IllegalTransitionError("Handoff consent is not available in this state.")
            staged = _handoff_record(row.payload, row.ticket_ref, consent_at)
            ticket = fetch_one(
                connection,
                """
                INSERT INTO handoff (workflow_id, ticket_ref, reason, state)
                VALUES (%s, %s, %s, 'open')
                ON CONFLICT (workflow_id) WHERE state IN ('open', 'claimed')
                DO UPDATE SET reason = handoff.reason RETURNING id
            """,
                (workflow_id, staged["ticket_ref"], staged["reason_class"]),
            )
            assert ticket is not None
            staged["handoff_id"] = str(ticket["id"])
            connection.execute(
                "UPDATE workflow SET payload = payload || %s WHERE id = %s",
                (_json({"handoff": staged}), workflow_id),
            )
        return staged

    def confirm_booking(
        self, workflow_id: UUID, *, expected_state_version: int, external_ref: str
    ) -> BookingAttempt:
        _assert_recordable_booking(BookingState.CONFIRMED, external_ref)
        with self._session() as connection, connection.transaction():
            row = self._require(connection, workflow_id)
            _assert_current_version(row, expected_state_version)
            if row.state != "BOOKING_UNVERIFIED":
                raise IllegalTransitionError("Only an unverified booking can be reconciled.")
            owned = self._hold(connection, row)
            booking = fetch_one(
                connection,
                """
                UPDATE booking_request SET state = 'confirmed', external_ref = %s
                WHERE workflow_id = %s AND state = 'pending'
                RETURNING id AS booking_id, workflow_id, approval_id, patient_ref, payload_hash,
                          external_ref, requested_at
            """,
                (external_ref, workflow_id),
            )
            if booking is None:
                raise ContractViolationError("No pending booking can be reconciled.")
            moved = WorkflowStore(connection).advance(
                workflow_id,
                self._worker_id,
                expected_generation=owned.generation,
                expected_state_version=expected_state_version,
                to_state="BOOKED",
            )
        confirmed = BookingRow(**booking, state=BookingState.CONFIRMED)
        return BookingAttempt(booking=confirmed, workflow=_project(moved))

    def ping(self) -> None:
        """Prove the database answers *and* that the schema this adapter needs is in it.

        Called once, at startup. A DSN can name a server that is not there, and it can name a database
        whose migrations have never been applied; both produce a gateway that starts, reports healthy,
        and fails every request it is sent. That is the worst of the available failures, so both are
        turned into a refusal to start instead.

        The query is the cheapest one that is still about the schema rather than about the socket: if
        `workflow_transition` is readable, 003 has run, and 014 is what populates it. Nothing is
        cached — every operation below opens its own connection — so this is a check, not a driver.
        """
        with self._session() as connection:
            fetch_scalar(connection, "SELECT count(*) FROM workflow_transition")

    # -- seeding and demo helpers ---------------------------------------------------------------

    def create(
        self,
        patient_ref: str,
        *,
        ticket_ref: str = "T-DEMO-1",
        state: str = WorkflowState.INTAKE.value,
        payload: Mapping[str, Any] | None = None,
        workflow_id: UUID | None = None,
    ) -> WorkflowRow:
        """Insert a workflow, for the demo profile's seed route. Not part of the port.

        The id comes from the column default rather than from `uuid4()` here, so the gateway and any
        other writer share one generator. `state_version` starts at 1 and `generation` at 0 because
        those are the column defaults and the constraints say so; a fixture that inserted a version
        the real row could never have issued would accept a card the database would refuse.
        """
        with self._session() as connection:
            row = fetch_one(
                connection,
                """
                INSERT INTO workflow (id, ticket_ref, patient_ref, state, payload)
                VALUES (coalesce(%s, gen_random_uuid()), %s, %s, %s, %s)
                RETURNING *
                """,
                (workflow_id, ticket_ref, patient_ref, state, _json(payload or {})),
            )
        return _project(Workflow.from_row(_required(row, "the workflow INSERT")))

    # -- the port -------------------------------------------------------------------------------

    def load(self, workflow_id: UUID) -> WorkflowRow | None:
        with self._session() as connection:
            row = WorkflowStore(connection).load(workflow_id)
        return None if row is None else _project(row)

    def advance(
        self, workflow_id: UUID, *, expected_state_version: int, to_state: str
    ) -> WorkflowRow:
        """Claim the row if this process does not already hold it, then move it, in one transaction.

        The claim and the advance are one transaction so a crash between them cannot leave the
        workflow claimed by a gateway that is not going to move it — the pair commits together or not
        at all. Both statements are short and neither is held open across the request: §7's rule is
        about transactions that outlive the work, and this one ends before the card is rendered.
        """
        with self._session() as connection, connection.transaction():
            row = self._require(connection, workflow_id)
            _assert_current_version(row, expected_state_version)
            owned = self._hold(connection, row)
            moved = WorkflowStore(connection).advance(
                workflow_id,
                self._worker_id,
                expected_generation=owned.generation,
                expected_state_version=expected_state_version,
                to_state=to_state,
            )
        return _project(moved)

    def assert_can_advance(
        self, workflow_id: UUID, *, expected_state_version: int, to_state: str
    ) -> None:
        """The three refusals `advance` makes, asked of the database without writing anything.

        Read-only on purpose: this runs before the transport validates the destination card, and a
        pre-flight that claimed the row or bumped a counter would be a write on a request that is
        still going to be refused. Nothing is read into this process's memory to be trusted later —
        `advance` re-reads and the database re-checks — so two reads of the same row cannot make a
        write legal that was not.
        """
        with self._session() as connection:
            row = self._require(connection, workflow_id)
            _assert_edge(connection, row, to_state)
            _assert_current_version(row, expected_state_version)

    def update_payload(
        self, workflow_id: UUID, *, expected_state_version: int, patch: Mapping[str, Any]
    ) -> WorkflowRow:
        """Merge `patch` into `payload`, in one compare-and-set statement.

        The version is compared by PostgreSQL, in the `WHERE` clause, in the same statement that
        writes — the same compare-and-set §7 describes for a transition. `payload || patch` is a
        shallow merge, which is what the memory profile's `dict.update` is, so an answer appended to
        `answers` behaves identically in both profiles.

        Two database rules bite here that the memory profile cannot express, and both are correct:
        `workflow_guard` refuses a write to a workflow a human owns (`RE003`, §11), and
        `workflow_booking_consent_guard` is not consulted because the state does not move.
        """
        with self._session() as connection:
            row = fetch_one(
                connection,
                """
                UPDATE workflow
                   SET payload = payload || %s
                 WHERE id = %s AND state_version = %s
                RETURNING *
                """,
                (_json(patch), workflow_id, expected_state_version),
            )
        if row is None:
            raise IllegalTransitionError(
                f"workflow {workflow_id} is not at state_version {expected_state_version}, or does "
                "not exist; a payload write presents the version it read and is refused rather than "
                "merged when the row has moved (RE002, §7)"
            )
        return _project(Workflow.from_row(row))

    def begin_command(self, command_id: str, request_hash: str) -> CommandDecision:
        """Open a command. `command_begin` compares the stored hash and raises `RE005` on a mismatch."""
        with self._session() as connection:
            return CommandLedger(connection).begin(command_id, request_hash)

    def complete_command(
        self,
        command_id: str,
        request_hash: str,
        *,
        response_status: int,
        response: Mapping[str, Any],
    ) -> CommandRecord:
        """Record a command's outcome. `command_complete` refuses a terminal row (`RE005`, §7)."""
        with self._session() as connection:
            return CommandLedger(connection).complete(
                command_id,
                request_hash,
                response_status=response_status,
                response=dict(response),
            )

    def record_booking(
        self,
        *,
        workflow_id: UUID,
        approval_id: UUID,
        patient_ref: str,
        payload_hash: str,
        payload: Mapping[str, Any],
        requested_by: str,
        outcome: BookingState = BookingState.REQUESTED,
        external_ref: str | None = None,
        advance_to: str | None = None,
        expected_state_version: int | None = None,
    ) -> BookingAttempt:
        """Write the booking attempt against the approval it names, and move the workflow with it.

        The three binding arguments are *not* sent to the database, and that asymmetry is the point:
        `booking_request_create` copies workflow, patient and payload hash out of the approval row, so
        a booking whose binding disagrees with its approval cannot be written — the composite foreign
        key in 008 has nothing to match. Passing the caller's copies would invite the belief that the
        caller's copies are what the row holds; the memory profile does write them, which is one place
        the two profiles differ in strictness rather than in outcome, since the gateway compares the
        hashes itself before it gets here (`CommandService.book`).

        **With a transition, every statement is one transaction.** The version is checked,
        the row is claimed, the booking is written, the outcome is recorded on it, and the workflow
        moves — and a refusal anywhere in that list rolls the whole list back. That ordering is not
        taste: 014's consent guard refuses `BOOKED` unless a `confirmed` `booking_request` exists
        *by the time the workflow moves*, so the confirmation has to be written first, and "first"
        only means anything if the two commit together. It is also what makes the pair safe for the
        caller — a transition refused after the write would otherwise leave a booking against a spent
        approval on a workflow that never moved.

        A second booking against the same approval is refused by `booking_request_one_per_approval`
        and arrives as a `23505`, not as RE004 — the index is checked at INSERT, before 008's
        `AFTER INSERT` consume trigger runs. See `ControlState.record_booking`.
        """
        _assert_recordable_booking(outcome, external_ref)
        transition = _booking_transition(advance_to, expected_state_version)
        column = _BOOKING_COLUMN[outcome]
        if transition is None:
            with self._session() as connection:
                written = self._write_booking(
                    connection,
                    approval_id=approval_id,
                    requested_by=requested_by,
                    external_ref=external_ref,
                    payload=payload,
                    column=column,
                )
            return BookingAttempt(booking=written, workflow=None)
        to_state, version = transition
        with self._session() as connection, connection.transaction():
            row = self._require(connection, workflow_id)
            _assert_current_version(row, version)
            owned = self._hold(connection, row)
            written = self._write_booking(
                connection,
                approval_id=approval_id,
                requested_by=requested_by,
                external_ref=external_ref,
                payload=payload,
                column=column,
            )
            moved = WorkflowStore(connection).advance(
                workflow_id,
                self._worker_id,
                expected_generation=owned.generation,
                expected_state_version=version,
                to_state=to_state,
            )
        return BookingAttempt(booking=written, workflow=_project(moved))

    def _write_booking(
        self,
        connection: Any,
        *,
        approval_id: UUID,
        requested_by: str,
        external_ref: str | None,
        payload: Mapping[str, Any],
        column: str,
    ) -> BookingRow:
        """`booking_request_create`, and then the outcome, inside the caller's transaction.

        Two statements rather than one because the outcome is a *transition of a row that has to
        exist first*: 008's `booking_request_guard` is a BEFORE UPDATE trigger, so `confirmed` and
        `external_ref` can only arrive by updating the `pending` row the insert wrote. One statement
        cannot do it — the parts of a data-modifying CTE do not see each other's effects, and
        `booking_request_create` returns the row it inserted — and the pair is safe together only
        because the caller opened a transaction: a refusal of either statement leaves neither.

        `pending` (the caller asked for a booking and heard nothing) is not written back over
        itself. The UPDATE is skipped rather than repeated: `booking_request_guard` would allow it,
        but a write that changes nothing is still a write, and the row's default is already the value
        the contract means.
        """
        written = WorkflowStore(connection).record_booking(
            approval_id, requested_by, external_ref=external_ref, payload=payload
        )
        if column != _BOOKING_COLUMN[BookingState.REQUESTED]:
            row = fetch_one(
                connection,
                """
                UPDATE booking_request
                   SET state = %s, external_ref = %s
                 WHERE id = %s
                RETURNING *
                """,
                (column, external_ref, written.id),
            )
            written = BookingRequest.from_row(_required(row, "the booking_request UPDATE"))
        return _booking_row(written)

    def bookings_for(self, workflow_id: UUID) -> tuple[BookingRow, ...]:
        """Every booking attempt for a workflow, oldest first.

        The state is translated through `_BOOKING_STATE` like every other read of the column: the
        projection is the contract's, and a caller that could tell which profile answered by reading
        one field would be able to tell for all of them.
        """
        with self._session() as connection:
            rows = fetch_all(
                connection,
                """
                SELECT id AS booking_id, workflow_id, approval_id, patient_ref, payload_hash,
                       state, external_ref, requested_at
                  FROM booking_request
                 WHERE workflow_id = %s
                 ORDER BY requested_at, id
                """,
                (workflow_id,),
            )
        return tuple(
            BookingRow(
                booking_id=row["booking_id"],
                workflow_id=row["workflow_id"],
                approval_id=row["approval_id"],
                patient_ref=row["patient_ref"],
                payload_hash=row["payload_hash"],
                state=_BOOKING_STATE[row["state"]],
                external_ref=row["external_ref"],
                requested_at=row["requested_at"],
            )
            for row in rows
        )

    # -- internals ------------------------------------------------------------------------------

    @contextmanager
    def _session(self) -> Iterator[Any]:
        """One connection for the length of one operation. See `psql` for why it is not pooled.

        A wrapper rather than a call at each site so that the session this class opens is named in
        one place, which is what makes "every operation here opens exactly one" checkable by reading.
        """
        with session(self._dsn) as connection:
            yield connection

    def _require(self, connection: Any, workflow_id: UUID) -> Workflow:
        row = WorkflowStore(connection).load(workflow_id)
        if row is None:
            raise IllegalTransitionError(f"no workflow {workflow_id} (RE002)")
        return row

    def _hold(self, connection: Any, row: Workflow) -> Workflow:
        """Return a row this process owns with a live lease, claiming it if it does not already.

        A gateway that has already moved this workflow holds it until the lease runs out, and
        `workflow_claim` deliberately refuses to re-claim a row that is still leased — to the same
        worker or any other. So the re-claim is conditional on the lease being gone, which is the
        database's own rule read back rather than a second copy of it: the comparison below cannot
        make a write legal, it only decides whether to ask for one.
        """
        held = (
            row.owner_worker_id == self._worker_id
            and row.lease_expires_at is not None
            and row.lease_expires_at > datetime.now(UTC)
        )
        if held:
            return row
        return WorkflowStore(connection).claim(
            row.id, self._worker_id, expected_generation=row.generation
        )


def _json(value: Mapping[str, Any]) -> Any:
    """A `jsonb` parameter.

    Imported here rather than at module scope, exactly as `referraleasy_domain` does it and for the
    same reason: adapting a parameter to `jsonb` is the driver's job, and importing this module must
    not require the driver to be installed.
    """
    from psycopg.types.json import Jsonb

    return Jsonb(dict(value))


def _required(row: dict[str, Any] | None, what: str) -> dict[str, Any]:
    if row is None:  # pragma: no cover - INSERT ... RETURNING always returns the row it wrote
        raise IllegalTransitionError(f"{what} returned no row (RE002)")
    return row


def _booking_row(written: BookingRequest) -> BookingRow:
    """A `booking_request` row as the port's projection of it.

    The state goes through `_BOOKING_STATE` on the way out, in both directions of the mapping: what
    this class reads and what it writes are the same four values, and the port's caller sees the
    contract's. The columns nothing in the transport reads (`confirmed_at`, `failed_at`,
    `last_error`, the payload) are dropped here rather than exposed and then ignored.
    """
    return BookingRow(
        booking_id=written.id,
        workflow_id=written.workflow_id,
        approval_id=written.approval_id,
        patient_ref=written.patient_ref,
        payload_hash=written.payload_hash,
        state=_BOOKING_STATE[written.state],
        external_ref=written.external_ref,
        requested_at=written.requested_at,
    )


def _project(workflow: Workflow) -> WorkflowRow:
    """A database row as the port's projection of it."""
    return WorkflowRow(
        workflow_id=workflow.id,
        ticket_ref=workflow.ticket_ref,
        patient_ref=workflow.patient_ref,
        state=workflow.state,
        state_version=workflow.state_version,
        generation=workflow.generation,
        payload=dict(workflow.payload),
    )


def _assert_current_version(row: Workflow, expected_state_version: int) -> None:
    """Refuse a caller whose view of the row is behind, naming the port's refusal (RE002, §7).

    This is a *naming*, not the enforcement — see the module docstring. The database refuses the same
    move in `workflow_advance`'s `WHERE` clause and in `workflow_guard`, as `RE001`; this branch exists
    so the two profiles answer a stale caller with the same code, and so the transport's pre-flight
    and its write agree with each other.
    """
    if row.state_version != expected_state_version:
        raise IllegalTransitionError(
            f"workflow {row.id} is at state_version {row.state_version}, but the caller presented "
            f"{expected_state_version}; a stale version is refused rather than applied (RE002, §7)"
        )


def _assert_edge(connection: Any, row: Workflow, to_state: str) -> None:
    """Whether `workflow_transition` holds this edge — asked of the table, not of a transcription.

    The self-loop case is answered from the same table: 003's `workflow_transition_no_self_loop`
    means such a row cannot exist, so the query already returns false. It is named separately only
    because §7's rule there is about `state_version` counting transitions, and a refusal that says
    "already in INTAKE" tells the caller that; the *rule* is the constraint, not this branch.
    """
    exists = fetch_scalar(
        connection,
        """
        SELECT EXISTS (
            SELECT 1 FROM workflow_transition
             WHERE from_state = %s AND to_state = %s
        )
        """,
        (row.state, to_state),
    )
    if exists:
        return
    if to_state == row.state:
        raise IllegalTransitionError(
            f"workflow {row.id} is already in {to_state}; state_version is the transition counter "
            "and moves only with a transition, and workflow_transition has no self-loop row for it "
            "(RE002)"
        )
    raise IllegalTransitionError(
        f"{row.state} -> {to_state} is not an edge in workflow_transition; the state machine is the "
        "database's, and this profile asks it rather than carrying its own copy (RE002, §7)"
    )
