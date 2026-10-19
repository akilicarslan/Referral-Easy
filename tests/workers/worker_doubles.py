"""Doubles, seeders and database helpers shared by the worker tests. Uniquely named, on purpose.

**Why this is not `conftest.py`.** pytest puts every `conftest.py` it collects onto `sys.path` under
the bare name `conftest`, so `from conftest import Seeder` resolves to whichever directory's conftest
pytest imported *last* — an order-dependent import that is green in the default alphabetical run and
fails under a different one. `tests/test_test_layout.py` enforces the rule by AST, and
`tests/agent_runtime/agent_runtime_doubles.py` and `tests/skills/skills_fixtures.py` carry the same
explanation. Nothing in this suite imports from `conftest`.

**Why the package under test is imported as an installed one.** `services/workers` is a uv workspace
member: the root `pyproject.toml` lists it in `[tool.uv.workspace].members`, declares
`referraleasy-workers` as a dependency and gives it a `[tool.uv.sources]` entry, so `uv sync` installs
it into the shared virtualenv and `import referraleasy_workers` works from any directory. This suite
therefore adds **nothing** to `sys.path`, deliberately: a path insert here would make a packaging
break invisible, and the packaging is now asserted directly by `tests/workers/test_settings.py`.
(Until Phase 5 this module imported a `worker_bootstrap` helper that did insert the source root; the
helper is gone, because the insert it existed for is gone.)

**What is real and what is doubled.** The seeder writes real rows into a real PostgreSQL database, and
every rule the tests assert on lives in SQL that the same migrations created. What is doubled is what
has no business being real in a unit test: the booking facility (an HTTP API), the notification sink,
and the clock. A test that replaces the *database* with a double would be testing nothing, because the
claim this package makes is that the database owns the rules.
"""

from __future__ import annotations

import collections
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from referraleasy_agent_runtime import BudgetCaps
from referraleasy_contracts.generated.booking import BookingAttemptResult
from referraleasy_contracts.generated.common import HandoffReasonClass
from referraleasy_workers.booking import (
    CancelView,
    LookupOutcome,
    LookupView,
    RescheduleView,
)
from referraleasy_workers.outbox import (
    DeliveryFailure,
    DeliveryOutcome,
    DeliveryResult,
)

__all__ = [
    "GUARDED_TABLES",
    "MIGRATIONS_RUNNER",
    "OPERATOR",
    "PATH_TO_FINAL_APPROVAL",
    "REPO_ROOT",
    "RESET_TABLES",
    "TRANSITION_EDGES",
    "WORKERS_SRC",
    "FakeClock",
    "FakeTransitions",
    "ScriptedBookingApi",
    "ScriptedSender",
    "Seeder",
    "expect_database_refusal",
    "expect_refusal",
    "expire_lease",
    "queue_and_claim_ticket",
    "reset",
    "workflow_payload",
    "workflow_row",
]

#: `tests/workers/worker_doubles.py` -> `tests/workers` -> `tests` -> repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The source root of the package under test. The workspace install points *here* (editable), which
#: is why no test in this directory needs to put it on `sys.path` — see this module's docstring.
WORKERS_SRC = REPO_ROOT / "services" / "workers" / "src"

#: The migration runner, loaded from its file rather than imported: `migrations/` is not a workspace
#: member (only `services/workers` and the packages are), and a test helper that re-derived its rules
#: would be able to disagree with the runner `make migrate` uses.
MIGRATIONS_RUNNER = REPO_ROOT / "migrations" / "run.py"

#: Reset between tests. `schema_migrations` is deliberately absent (the ledger is not test state),
#: and so are the vocabulary and policy-seed tables — they are part of the schema, and a test that
#: emptied them would be testing a different database than the one that ships.
RESET_TABLES = (
    "audit",
    "release_event",
    "release_registry",
    "handoff",
    "booking_request",
    "approval",
    "command_dedup",
    "outbox",
    "task",
    "session",
    "workflow",
    "evidence",
)

#: Tables whose purpose is to refuse deletion, so emptying them is a deliberate act.
GUARDED_TABLES = ("audit", "evidence", "release_registry", "release_event")

#: A legal route from INTAKE to AWAITING_FINAL_APPROVAL. Every pair is a `workflow_transition` row,
#: which is why the walk is written out rather than clever: an invented shortcut would be refused.
PATH_TO_FINAL_APPROVAL: tuple[str, ...] = (
    "COVERAGE_CHECK",
    "PROVIDER_MATCHING",
    "AWAITING_HOSPITAL_CHOICE",
    "AWAITING_DOCTOR_CHOICE",
    "READINESS_CHECK",
    "AWAITING_SLOT_CHOICE",
    "AWAITING_FINAL_APPROVAL",
)


def reset(conn: Any) -> None:
    """Empty the data tables, stepping explicitly around the immutability guards."""
    conn.rollback()
    with conn.cursor() as cur:
        for table in GUARDED_TABLES:
            cur.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        cur.execute("TRUNCATE " + ", ".join(RESET_TABLES) + " RESTART IDENTITY CASCADE")
        for table in GUARDED_TABLES:
            cur.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
    conn.commit()


@contextmanager
def expect_database_refusal(conn: Any, sqlstate: str) -> Iterator[None]:
    """Assert the *database* refused — by SQLSTATE, with the driver's own error.

    Deliberately not `expect_refusal`: that asserts a Python exception type, and some of the claims in
    this suite are stronger — that no Python guard was needed, or that a Python guard and the
    database's own rule refuse the same thing. The statement is issued raw, the driver's
    `DatabaseError` is what comes back, and the SQLSTATE is what identifies which rule stopped it.
    """
    import psycopg

    with pytest.raises(psycopg.errors.DatabaseError) as refusal, conn.transaction():
        yield
    assert refusal.value.sqlstate == sqlstate, f"{refusal.value!r} did not carry {sqlstate}"


@contextmanager
def expect_refusal(conn: Any, expected: type[BaseException]) -> Iterator[None]:
    """Assert that a call is refused, and leave the connection usable afterwards.

    A refused statement aborts its transaction, so a rollback has to happen before the test can do
    anything else. It is done as a **savepoint** rather than a rollback of the whole transaction,
    because rolling back would also discard the rows the test just seeded — leaving the test unable
    to check that a refused transition left the workflow exactly where it was.
    """
    with pytest.raises(expected), conn.transaction():
        yield


def workflow_row(conn: Any, workflow_id: Any) -> Mapping[str, Any]:
    """The `workflow` row, read straight. A test that asserts on state must read the row, not a
    cached object that the code under test happened to update."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM workflow WHERE id = %s", (workflow_id,))
        row = cur.fetchone()
    assert row is not None, f"workflow {workflow_id} does not exist"
    return row


def workflow_payload(conn: Any, workflow_id: Any) -> Mapping[str, Any]:
    """The workflow's payload, as a dict. Where checkpoints and handoff staging live."""
    return dict(workflow_row(conn, workflow_id)["payload"] or {})


#: The operator every test that needs one uses, unless it is a test *about* which operator holds a
#: ticket. Constants because the assertions are about *whose* write landed, and a typo in one of them
#: would read as a passing test about a third person.
OPERATOR = "op-1"


def expire_lease(conn: Any, workflow_id: Any) -> None:
    """Push a live lease into the past, so the next replica may claim.

    A row edit rather than a sleep: the suite does not wait on wall-clock time, and *how* a lease
    expires is not what is under test — that the next claim notices is. `workflow_guard` permits this
    (it guards the generation, the owner and the state, not the lease), which is worth knowing: the
    lease is the only ownership field a raw writer can move.

    Only valid on a row that is already claimed, and the schema says so rather than this helper:
    `workflow_lease_is_whole` refuses a lease timestamp on an unowned row, because a lease without an
    owner is a claim that did not happen.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (workflow_id,),
        )


def queue_and_claim_ticket(conn: Any, workflow_id: Any, *, operator_id: str = OPERATOR) -> Any:
    """Hand the workflow to a human: one open ticket, claimed, so `human_owner_id` is set.

    The two acts a real handoff performs, in the order the schema allows: automation queues the row
    (with the patient's consent staged, because the gate is not optional here), and the operator then
    claims the ticket — which is the only thing that sets `human_owner_id`.
    """
    from referraleasy_workers.hitl import HumanQueue

    queue = HumanQueue(conn)
    record = queue.queue(handoff_request(workflow_id=workflow_id))
    return queue.claim(record.id, operator_id=operator_id)


class FakeClock:
    """The agent runtime's `Clock`, driven by the test. No test in this suite sleeps for real."""

    def __init__(self, start_ms: int = 1_000_000) -> None:
        self._now_ms = start_ms
        self.slept_ms: list[int] = []

    def monotonic_ms(self) -> int:
        return self._now_ms

    async def sleep_ms(self, milliseconds: int) -> None:
        self.slept_ms.append(milliseconds)
        self._now_ms += milliseconds

    def advance(self, milliseconds: int) -> None:
        self._now_ms += milliseconds


#: Enough of the state machine for the tests that must not need a database: the edges recovery and
#: the reschedule planner actually consult, copied from `workflow_transition` as it ships. The
#: integration tests read the real table; this exists so a pure test can be pure, and a drift between
#: the two is what `test_recovery.py`'s real-table tests are for.
TRANSITION_EDGES: dict[str, set[str]] = {
    "INTAKE": {
        "COVERAGE_CHECK",
        "PROVIDER_MATCHING",
        "HUMAN_HANDOFF",
        "CANCELLED",
        "ABANDONED",
        "FAILED",
    },
    "COVERAGE_CHECK": {
        "PROVIDER_MATCHING",
        "RECOVERY",
        "HUMAN_HANDOFF",
        "FAILED",
        "CANCELLED",
        "ABANDONED",
    },
    "PROVIDER_MATCHING": {
        "AWAITING_HOSPITAL_CHOICE",
        "COVERAGE_CHECK",
        "RECOVERY",
        "HUMAN_HANDOFF",
        "FAILED",
    },
    "READINESS_CHECK": {"AWAITING_SLOT_CHOICE", "RECOVERY", "HUMAN_HANDOFF", "FAILED"},
    "AWAITING_SLOT_CHOICE": {
        "AWAITING_FINAL_APPROVAL",
        "READINESS_CHECK",
        "HUMAN_HANDOFF",
        "CANCELLED",
    },
    "AWAITING_FINAL_APPROVAL": {
        "BOOKING_IN_FLIGHT",
        "AWAITING_SLOT_CHOICE",
        "HUMAN_HANDOFF",
        "ABANDONED",
    },
    "BOOKING_IN_FLIGHT": {"BOOKED", "BOOKING_UNVERIFIED", "RECOVERY", "HUMAN_HANDOFF", "FAILED"},
    "BOOKING_UNVERIFIED": {"BOOKED", "BOOKING_IN_FLIGHT", "RECOVERY", "HUMAN_HANDOFF", "FAILED"},
    "BOOKED": {"AWAITING_SLOT_CHOICE", "CANCELLED", "FAILED"},
    "RECOVERY": {
        "AWAITING_SLOT_CHOICE",
        "PROVIDER_MATCHING",
        "READINESS_CHECK",
        "HUMAN_HANDOFF",
        "FAILED",
    },
    "HUMAN_HANDOFF": {
        "INTAKE",
        "PROVIDER_MATCHING",
        "READINESS_CHECK",
        "AWAITING_SLOT_CHOICE",
        "FAILED",
    },
}


class FakeTransitions:
    """`TransitionRules` over a dict of edges, for the tests that must not need a database.

    Deliberately *not* a stub that answers `True`: the whole point of the planner taking rules is that
    it asks, so a double that always permitted would hide every refusal this suite is about.
    """

    def __init__(self, edges: Mapping[str, set[str]] | None = None) -> None:
        self.edges = {state: set(targets) for state, targets in (edges or TRANSITION_EDGES).items()}

    def permits(self, from_state: str, to_state: str) -> bool:
        return to_state in self.edges.get(from_state, set())

    def targets(self, from_state: str) -> frozenset[str]:
        return frozenset(self.edges.get(from_state, set()))


# ---------------------------------------------------------------------------
# The booking facility
# ---------------------------------------------------------------------------


class ScriptedBookingApi:
    """A booking facility whose every answer the test chose in advance.

    Implements :class:`~referraleasy_workers.booking.BookingApi`, so the pipeline and the reconciler
    run exactly the code they run in production; only the wire is missing. Every call is recorded —
    the key *and* the `command_id` and the timeout — because the interesting assertions in this suite
    are about how many calls were made and under which key, not only about what came back.
    """

    def __init__(
        self,
        *,
        create: Sequence[Any] | None = None,
        lookup: Sequence[Any] | None = None,
        cancel: Sequence[Any] | None = None,
        reschedule: Sequence[Any] | None = None,
        default_create: BookingAttemptResult = BookingAttemptResult.created,
        default_lookup: LookupOutcome = LookupOutcome.BOOKING_FOUND_UNDER_SAME_KEY,
    ) -> None:
        self._create: collections.deque[Any] = collections.deque(create or ())
        self._lookup: collections.deque[Any] = collections.deque(lookup or ())
        self._cancel: collections.deque[Any] = collections.deque(cancel or ())
        self._reschedule: collections.deque[Any] = collections.deque(reschedule or ())
        self._default_create = default_create
        self._default_lookup = default_lookup

        #: `(key, command_id, timeout_ms)` per create attempt. The command id is what makes "one
        #: retry owner, one command" assertable, and the timeout is what makes "the attempt is sized
        #: to its lease" assertable.
        self.create_calls: list[tuple[str, str | None, int | None]] = []
        self.lookup_keys: list[str] = []
        self.cancel_calls: list[str] = []
        self.reschedule_calls: list[tuple[str, Mapping[str, Any]]] = []

    # -- the three calls the worker makes ------------------------------------

    async def create_booking(
        self,
        *,
        key: str,
        payload: Mapping[str, Any],
        command_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> Any:
        self.create_calls.append((key, command_id, timeout_ms))
        answer = self._pop(self._create, _attempt_view, key=key, result=self._default_create)
        return answer

    async def lookup_request(self, *, key: str) -> LookupView:
        self.lookup_keys.append(key)
        return self._pop(self._lookup, _lookup_view, key=key, outcome=self._default_lookup)

    async def cancel(self, *, booking_request_id: str, reason: str | None = None) -> CancelView:
        del reason
        self.cancel_calls.append(booking_request_id)
        return self._pop(
            self._cancel,
            _cancel_view,
            booking_request_id=booking_request_id,
        )

    async def reschedule(
        self, *, booking_request_id: str, payload: Mapping[str, Any]
    ) -> RescheduleView:
        self.reschedule_calls.append((booking_request_id, dict(payload)))
        return self._pop(
            self._reschedule,
            _reschedule_view,
            booking_request_id=booking_request_id,
        )

    # -- inspection ---------------------------------------------------------

    @property
    def created_keys(self) -> list[str]:
        return [key for key, _command, _timeout in self.create_calls]

    @property
    def distinct_lookup_keys(self) -> set[str]:
        """Every key a lookup was issued under. §7's requirement is that this stays a single key."""
        return set(self.lookup_keys)

    @property
    def every_lookup_used_one_key(self) -> bool:
        return len(self.distinct_lookup_keys) <= 1

    # -- internals ----------------------------------------------------------

    def _pop(self, queue: collections.deque[Any], build: Callable[..., Any], **kwargs: Any) -> Any:
        """Take the next scripted answer, or build the default one.

        A scripted entry may be a value, a callable that receives the call's keyword arguments
        (for an answer that depends on the key), or an exception instance to raise (for a transport
        failure, which is the one thing the API is allowed to raise).
        """
        scripted = queue.popleft() if queue else None
        if isinstance(scripted, BaseException):
            raise scripted
        if callable(scripted):
            scripted = scripted(**kwargs)
        if scripted is None:
            return build(**kwargs)
        return scripted


def _attempt_view(
    *,
    key: str,
    result: BookingAttemptResult = BookingAttemptResult.created,
    http_status: int = 200,
    booking_request_id: str | None = "B-1",
    detail: str | None = None,
    retry_after_s: float | None = None,
) -> Any:
    from referraleasy_workers.booking import BookingAttemptView

    return BookingAttemptView(
        result=result,
        idempotency_key=key,
        http_status=http_status,
        booking_request_id=booking_request_id,
        detail=detail,
        retry_after_s=retry_after_s,
    )


def _lookup_view(
    *,
    key: str,
    outcome: LookupOutcome = LookupOutcome.BOOKING_FOUND_UNDER_SAME_KEY,
    http_status: int = 200,
    booking_request_id: str | None = "B-1",
    booking_state: str | None = None,
    other_key: str | None = None,
    facility_reconciliation: str | None = None,
    detail: str | None = None,
    retry_after_s: float | None = None,
) -> LookupView:
    return LookupView(
        outcome=outcome,
        idempotency_key=key,
        http_status=http_status,
        booking_request_id=booking_request_id,
        booking_state=booking_state,
        other_key=other_key,
        facility_reconciliation=facility_reconciliation,
        detail=detail,
        retry_after_s=retry_after_s,
    )


def _cancel_view(
    *,
    booking_request_id: str,
    confirmed: bool = False,
    http_status: int = 200,
    booking_state: str | None = None,
    detail: str | None = None,
) -> CancelView:
    del booking_request_id
    return CancelView(
        requested=True,
        confirmed=confirmed,
        http_status=http_status,
        booking_state=booking_state,
        detail=detail,
    )


def _reschedule_view(
    *,
    booking_request_id: str,
    result: BookingAttemptResult = BookingAttemptResult.created,
    http_status: int = 200,
    old_booking_cancelled: bool = False,
    cancellation_deferred_reason: str | None = None,
    key: str | None = None,
    detail: str | None = None,
) -> RescheduleView:
    return RescheduleView(
        result=result,
        http_status=http_status,
        idempotency_key=key,
        booking_request_id=booking_request_id,
        superseded_booking_request_id=None,
        old_booking_cancelled=old_booking_cancelled,
        cancellation_deferred_reason=cancellation_deferred_reason,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# The notification sink
# ---------------------------------------------------------------------------


class ScriptedSender:
    """An outbox sender with a script, and a record of what it was handed.

    Entry kinds, and what each is for:

    * a :class:`~referraleasy_workers.outbox.DeliveryResult` — the sender answered; `EMPTY` is the
      "nothing to deliver" answer that must never enter the retry loop;
    * a :class:`~referraleasy_workers.outbox.DeliveryFailure` — one attempt failed, carrying whatever
      `Retry-After` the far side named;
    * any other exception — a transport error, which the worker must treat as a failed attempt rather
      than as a crash.

    The script is consumed in order, so the *n*-th delivery of a row gets the *n*-th entry, which is
    how "it failed twice and then succeeded" is expressed.
    """

    def __init__(self, script: Sequence[Any] | None = None) -> None:
        self.script: collections.deque[Any] = collections.deque(script or ())
        #: Every message handed to :meth:`send`, in order, so a test can assert a row was attempted
        #: exactly once per pass or never again after it went terminal.
        self.sent: list[Any] = []

    async def send(self, message: Any) -> DeliveryResult:
        self.sent.append(message)
        entry = self.script.popleft() if self.script else None
        if isinstance(entry, BaseException):
            raise entry
        if entry is None:
            return DeliveryResult(detail="delivered")
        return entry

    @property
    def deliveries(self) -> int:
        return len(self.sent)

    def scripted(self, *entries: Any) -> ScriptedSender:
        """Append entries. Chainable, so a test can state its whole script in one expression."""
        self.script.extend(entries)
        return self


def delivery_failure(
    *,
    status: int = 500,
    retry_after_s: float | None = None,
    message: str = "notification delivery failed",
) -> DeliveryFailure:
    """One failed attempt, optionally with the far side's own delay."""
    return DeliveryFailure(message, status_code=status, retry_after_s=retry_after_s)


def nothing_to_deliver(reason: str = "the facility had nothing to deliver") -> DeliveryResult:
    """A valid "there was nothing to deliver": a success that must not be retried."""
    return DeliveryResult(outcome=DeliveryOutcome.EMPTY, detail=reason)


def delivered(report: str = "delivered") -> DeliveryResult:
    return DeliveryResult(detail=report)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


class Seeder:
    """Connection-bound helpers that insert rows directly, plus the composite setups this suite
    needs over and over.

    Setup, not the thing under test: it writes rows the way a fixture, a previous phase or another
    service would have left them. Every call the package is responsible for goes through a store.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    # -- single rows --------------------------------------------------------

    def workflow(
        self,
        *,
        ticket_ref: str | None = None,
        patient_ref: str = "P-77",
        state: str = "INTAKE",
        payload: dict | None = None,
    ) -> str:
        """Insert a workflow in `state` and return its id, as a string.

        A string rather than a `UUID`: `command_dedup.command_id` and several function parameters are
        `text`, and PostgreSQL refuses a `uuid`-typed parameter against a `text` parameter instead of
        coercing it. Passing text everywhere keeps that from being a per-call decision.

        The ticket reference is fresh unless a test names one, because `workflow_active_ticket_key` is
        unique on it: the index reads `WHERE state NOT IN ('resolved', 'failed', 'cancelled')`, names
        the state vocabulary never uses in lowercase, and is therefore *global* rather than partial —
        so a second workflow in one test, or one opened while another suite holds the same reference,
        is a duplicate-key error. Callers that assert on the reference pass their own.
        """
        from psycopg.types.json import Jsonb

        if ticket_ref is None:
            ticket_ref = f"T-{uuid4().hex[:10].upper()}"
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow (ticket_ref, patient_ref, state, payload)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (ticket_ref, patient_ref, state, Jsonb(payload or {})),
            )
            return str(cur.fetchone()["id"])

    def set_booking_payload_hash(self, workflow_id: Any, payload_hash: str) -> None:
        """Attach the booking payload hash the workflow is currently consenting to.

        No function does this: the hash travels on `workflow.payload`, and whoever assembles the
        booking payload writes it there. The `::text` cast is required because `jsonb_build_object`
        takes variadic "any" arguments, so PostgreSQL cannot infer a parameter's type.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow SET payload = payload"
                " || jsonb_build_object('booking_payload_hash', %s::text) WHERE id = %s",
                (payload_hash, workflow_id),
            )

    def evidence(
        self,
        *,
        evidence_id: str = "EV-1",
        fresh_for: str = "1 hour",
        kind: str = "consent",
    ) -> str:
        """Insert an authoritative, fresh evidence row and return its primary key."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO evidence (evidence_id, kind, source_system, source_id,
                                      authoritative, observed_at, fresh_for, content_hash)
                VALUES (%s, %s, 'consent-service', 'src-1', true, now(), %s::interval, %s)
                RETURNING id
                """,
                (evidence_id, kind, fresh_for, "a" * 64),
            )
            return str(cur.fetchone()["id"])

    def approval(
        self,
        workflow_id: Any,
        *,
        state_version: int,
        payload_hash: str,
        evidence_id: str,
        patient_ref: str = "P-77",
        expires_in: str = "1 hour",
    ) -> str:
        """Record a patient approval bound to the given epoch and payload hash.

        `expires_in` has to be positive: `approval_expiry_after_approval` requires
        `expires_at > approved_at`, and `approval_no_rebinding` then makes `expires_at` immutable.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approval (workflow_id, patient_ref, evidence_id, state_version,
                                      booking_payload_hash, channel, approved_by, expires_at)
                VALUES (%s, %s, %s, %s, %s, 'patient_self_service', 'patient',
                        now() + %s::interval)
                RETURNING id
                """,
                (workflow_id, patient_ref, evidence_id, state_version, payload_hash, expires_in),
            )
            return str(cur.fetchone()["id"])

    def outbox(
        self,
        *,
        dedupe_key: str = "notify-1",
        kind: str = "notify_patient",
        max_attempts: int = 5,
        attempt_count: int = 0,
        payload: dict | None = None,
    ) -> str:
        """Queue one notification."""
        from psycopg.types.json import Jsonb

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO outbox (dedupe_key, kind, max_attempts, attempt_count, payload)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (dedupe_key, kind, max_attempts, attempt_count, Jsonb(payload or {})),
            )
            return str(cur.fetchone()["id"])

    # -- composites ---------------------------------------------------------

    def walk(self, workflow_id: Any, states: Sequence[str], *, worker: str = "w-1") -> Any:
        """Claim a workflow and advance it along `states`, through the store.

        Every step re-reads the generation and state_version from the previous call's row, which is
        the only way to drive an optimistic-concurrency API honestly.
        """
        from referraleasy_domain import WorkflowStore

        store = WorkflowStore(self._conn)
        row = store.claim(workflow_id, worker, expected_generation=0)
        for state in states:
            row = store.advance(
                row.id,
                worker,
                expected_generation=row.generation,
                expected_state_version=row.state_version,
                to_state=state,
            )
        return row

    def to_final_approval(self, *, patient_ref: str = "P-77", worker: str = "w-1") -> Any:
        """A workflow waiting for the patient's approval, claimed and leased to `worker`."""
        workflow_id = self.workflow(patient_ref=patient_ref)
        return self.walk(workflow_id, PATH_TO_FINAL_APPROVAL, worker=worker)

    def consent_ready(
        self,
        *,
        patient_ref: str = "P-77",
        worker: str = "w-1",
        expires_in: str = "1 hour",
    ) -> tuple[Any, str, str]:
        """`(workflow, approval_id, payload_hash)` — consent captured, booking about to be attempted.

        The approval is bound to the workflow's *current* `state_version`, which is the version the
        transition into `BOOKING_IN_FLIGHT` checks. `expires_in` is the window the patient left;
        granting a very short one is how a test stages consent that lapses.
        """
        from referraleasy_domain import canonical_booking_payload_hash

        payload_hash = canonical_booking_payload_hash({"slot_id": "S-1", "doctor_id": "D-9"})
        workflow = self.to_final_approval(patient_ref=patient_ref, worker=worker)
        self.set_booking_payload_hash(workflow.id, payload_hash)
        evidence_id = self.evidence()
        approval_id = self.approval(
            workflow.id,
            state_version=workflow.state_version,
            payload_hash=payload_hash,
            evidence_id=evidence_id,
            patient_ref=patient_ref,
            expires_in=expires_in,
        )
        return workflow, approval_id, payload_hash

    def in_flight(self, *, worker: str = "w-1", **kwargs: Any) -> tuple[Any, str, str]:
        """`(workflow, approval_id, payload_hash)` with the workflow in `BOOKING_IN_FLIGHT`.

        The only state from which the booking leg is legal, and the state the consent guard admits
        with a live approval bound to the *previous* `state_version` — which is what
        :meth:`consent_ready` produces, and the only way this walk succeeds.
        """
        from referraleasy_domain import WorkflowStore

        workflow, approval_id, payload_hash = self.consent_ready(worker=worker, **kwargs)
        row = WorkflowStore(self._conn).advance(
            workflow.id,
            worker,
            expected_generation=workflow.generation,
            expected_state_version=workflow.state_version,
            to_state="BOOKING_IN_FLIGHT",
        )
        return row, approval_id, payload_hash

    def booking_request(
        self,
        workflow_id: Any,
        *,
        approval_id: str,
        external_ref: str = "B-1",
        requested_by: str = "seed",
    ) -> str:
        """Record a booking against an approval, through the domain store (which spends it)."""
        from referraleasy_domain import WorkflowStore

        record = WorkflowStore(self._conn).record_booking(
            approval_id, requested_by, external_ref=external_ref, payload={"seeded": True}
        )
        return str(record.id)


def handoff_request(
    *,
    workflow_id: Any,
    reason_class: HandoffReasonClass = HandoffReasonClass.no_match,
    summary: str = "Here is what we would share with a member of our team:\n- a referral",
    sources: Sequence[str] = ("lookup #1 definitively_absent_for_same_key (200)",),
    consent_given_at: Any = None,
) -> Any:
    """A `HandoffRequest`, with a real summary and real evidence lines."""
    from datetime import UTC, datetime

    from referraleasy_workers.hitl import HandoffRequest

    return HandoffRequest(
        workflow_id=workflow_id,
        reason_class=reason_class,
        patient_visible_summary=summary,
        reason_detail="staged by the test",
        sources=tuple(sources),
        consent_given_at=consent_given_at or datetime.now(UTC),
    )


#: The caps a worker test runs under when it does not care which they are. The spec's numbers, so a
#: drift between `.env.example` and the code fails here rather than in production.
SPEC_CAPS = BudgetCaps(
    attempt_timeout_ms=3000,
    max_attempts=3,
    turn_deadline_ms=30000,
    http_tool_timeout_ms=1500,
)


def async_noop() -> Awaitable[None]:
    """An awaitable that does nothing, for a `sleep` injection point."""

    async def _go() -> None:
        return None

    return _go()
