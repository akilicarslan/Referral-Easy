"""Where the two control-state profiles are not the same, said as tests rather than as a comment.

`tests/gateway/` is parameterised over `backend`, so every test there has to pass twice: once against
`InMemoryControlState` and once against `PostgresControlState` on a real server. That is the only
honest way to know the two answer the same questions the same way, and it is why the suite grew a
`memory_only` marker: a test that *cannot* pass in both profiles is not silently weakened, it is
marked, and this module holds the guard that keeps the marked set exactly the documented one.

Three kinds of test live here.

1. **The boundary.** `test_only_the_known_gaps_are_memory_only` fails the moment a test outside the
   documented set acquires the marker, and the moment one of the documented tests stops needing it.
   The gap cannot grow quietly and it cannot stay stale.
2. **Claims both profiles make.** The profile that serves is the profile the settings named, and the
   store underneath survives a restart while §12's event log does not — true in both profiles, and a
   finding rather than a property: nothing stores the log.
3. **Claims only the database can make** (`@pytest.mark.integration`, skipped without a DSN). The
   point of the PostgreSQL profile is §7's sentence that the schema is the store of record, so these
   tests walk *underneath the adapter* — `WorkflowStore`, `CommandLedger`, raw SQL — and ask the
   database directly. An adapter that re-implemented a rule in Python would leave every one of them
   passing while the rule stopped being enforced, which is the failure mode they exist to catch.

Nothing in this module tests the adapter against a weaker version of a rule. Where the database's own
answer differs from what the port promises (a stale version is `RE001` to the database and `RE002` to
the port), both halves are asserted, side by side, so the difference is a recorded fact.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway_fixtures import (
    MEMORY_ONLY_MARKER,
    PATIENT_REF,
    TEST_DATABASE_URL,
    TEST_WORKER_ID,
    Demo,
    allow_all_pipeline,
    demo_settings,
    hospital_ref,
)

from referraleasy_contracts.generated.common import BookingState, WorkflowState
from referraleasy_domain.approval import PAYLOAD_HASH_KEY
from referraleasy_domain.commands import CommandLedger
from referraleasy_domain.errors import (
    ApprovalRefusedError,
    CommandConflictError,
    DomainError,
    IllegalTransitionError,
    StaleOwnershipError,
)
from referraleasy_domain.workflow import WorkflowStore, fetch_scalar
from referraleasy_gateway.app import create_app
from referraleasy_gateway.config import GatewaySettings
from referraleasy_gateway.control import PostgresControlState, SeedableControlState
from referraleasy_gateway.psql import session
from referraleasy_gateway.timing import ManualClock

# ---------------------------------------------------------------------------
# 1. The boundary
# ---------------------------------------------------------------------------

#: Every test that can only pass in the memory profile, and none other. Each one carries a docstring
#: naming which guarantee the schema enforces differently; this set is the index of those reasons.
#:
#: A test earns a place here only by failing in the postgres profile *for a reason the schema is right
#: about*. "The assertion was inconvenient" is not a reason, and the guard below is written so that
#: adding a name is a deliberate, reviewable act rather than a way to make a red run green.
#:
#: **It held ten names until `record_booking` learned to carry an outcome.** Nine of them were the
#: same finding — the port wrote `pending` whatever the scheduler said, so 014's consent guard refused
#: the transition into `BOOKED` that every one of those tests asserted on — and closing it closed all
#: nine at once. Two are left, and neither is about the port being unable to *write* what the
#: scheduler reported:
#:
#: * one is about whose clock decides that a consent has expired, and no change to this port will fix
#:   that;
#: * one reaches its assertion by moving a booked workflow back into `BOOKING_IN_FLIGHT`, and 014's
#:   consent guard refuses that move — the approval the first booking spent is not bound to the
#:   version the workflow is now on. The schema is right about it, and the memory profile has no
#:   liveness check at that transition to disagree with it.
MEMORY_ONLY: frozenset[str] = frozenset(
    {
        # §9's expiry is the database's clock, and the gateway's test clock is nine months behind it.
        "test_an_expired_consent_is_refused_on_the_wire_before_the_row_is_written",
        # 014 refuses the re-entry into BOOKING_IN_FLIGHT; the memory profile has nothing to refuse
        # it at that transition with. Both sides of it are asserted below:
        # `test_reconciliation_needs_a_live_approval_and_a_spent_one_is_not_it` for the move the
        # database refuses, and the `test_one_approval_...` one for the second write it refuses.
        "test_a_second_booking_with_the_same_approval_is_refused_as_re004",
    }
)


def test_only_the_known_gaps_are_memory_only(request: pytest.FixtureRequest) -> None:
    """No test needs the memory profile without being documented here, and none documented runs unmarked.

    Written as a test because a comment cannot fail. Without it the marker is a hole that widens one
    quiet commit at a time — the first test marked because a run was red, the tenth because marking
    was the local convention — and the suite would still report success twice while asserting the
    same thing once. It also fails in the other direction, when a gap is closed: a name left in the
    set after the profile stopped needing it is a stale claim about the system, and this suite does
    not keep those.

    It reads the *session's* items, so it sees what was collected rather than what this module
    happens to contain, and the marker is looked up by node name rather than by identity so a
    parametrised instance reports the function that carries it.
    """
    collected = {item.originalname for item in request.session.items}
    marked = {
        item.originalname
        for item in request.session.items
        if item.get_closest_marker(MEMORY_ONLY_MARKER) is not None
    }
    undeclared = marked - MEMORY_ONLY
    assert not undeclared, (
        f"these tests need the memory profile and are not documented as needing it: "
        f"{sorted(undeclared)}\n"
        "A test that cannot run in the postgres profile is a finding: fix it, or add it to "
        "MEMORY_ONLY with a docstring naming the guarantee the schema enforces differently."
    )
    still_running = (MEMORY_ONLY - marked) & collected
    assert not still_running, (
        f"these tests are documented as memory-only and ran without the marker: "
        f"{sorted(still_running)}\n"
        "The gap this module records has either closed — in which case remove the name — or the "
        "marker was lost, in which case the postgres profile is no longer being asked."
    )


# ---------------------------------------------------------------------------
# 2. Claims both profiles make
# ---------------------------------------------------------------------------


def test_the_profile_the_settings_name_is_the_profile_that_serves(
    settings: GatewaySettings, clock: ManualClock
) -> None:
    """`create_app` with nothing injected wires the store the profile names, and says so.

    Nothing is passed for `control`, so this is the path production takes: `_control_state` reads
    `GATEWAY_CONTROL_BACKEND`, builds the store, and — for postgres — makes it prove the database
    answers before the app is returned. A test that injected the store would prove only that the
    suite can build one.
    """
    app = create_app(settings=settings, clock=clock)
    with TestClient(app) as client:
        health = client.get("/healthz")
    assert health.status_code == 200, health.text
    assert health.json()["control_backend"] == settings.control_backend

    if settings.control_backend == "memory":
        # The other half of the claim: the offline profile names no database at all, so "it started"
        # is evidence that nothing opened a socket on the way up.
        assert settings.database_url == ""


def test_the_event_log_does_not_survive_a_restart(
    settings: GatewaySettings,
    clock: ManualClock,
    control: SeedableControlState,
) -> None:
    """§12's stream is in memory in *both* profiles, and a second app replays nothing.

    This is the finding, asserted rather than described. `EventLog` is a per-process object that no
    migration has a table for — `audit` is the tamper-evident chain, not the transport log — so a
    restart loses the stream's history while the workflow itself is still there, at the state it
    reached. A reconnecting patient is therefore served from a store that knows where they are and a
    log that knows nothing about how they got there.

    Both halves are asserted, because either alone would be misleading: "no events" would also be
    true of an app that failed to start, and "the workflow moved" would also be true of a log that
    persisted.
    """
    first = Demo(TestClient(create_app(settings=settings, clock=clock, control=control)))
    workflow_id = first.seed()
    moved = first.move(
        workflow_id,
        version=1,
        to_state=WorkflowState.PROVIDER_MATCHING,
    )
    assert moved.status_code == 200, moved.text
    before = first.events(workflow_id)
    assert before.status_code == 200, before.text
    assert "id: " in before.text, "the stream did not carry the event the move produced"

    restarted = Demo(TestClient(create_app(settings=settings, clock=clock, control=control)))
    after = restarted.events(workflow_id)
    assert after.status_code == 200, after.text
    assert "id: " not in after.text, (
        "the restarted app replayed the previous process's events, so something is persisting a log "
        "the EventLog does not know about"
    )
    assert restarted.snapshot(workflow_id).json()["state"] == "PROVIDER_MATCHING", (
        "the workflow is gone as well as the log, which is a different finding from this one"
    )
    # Why there is no third assertion: `EventLog` is per-process state and no migration creates a
    # table it could load from, so "nothing is persisted" is the *absence* of a loader rather than a
    # behaviour a test can observe. It is checked here as the two facts above — a restarted app
    # replays nothing, and the workflow it does not replay is still at its state — and stated in
    # `services/gateway/README.md` so a reader is not left to infer it.


def test_a_stale_version_is_refused_by_each_profile_s_own_authority(
    backend: str, control: SeedableControlState
) -> None:
    """The same write, refused by the mechanism that owns the rule — and the codes differ.

    The port promises `IllegalTransitionError` (RE002) for a stale `state_version`, and both profiles
    deliver it. Underneath, the postgres profile's *database* answers `RE001`: `workflow_advance`
    checks ownership, generation and version in one `WHERE` clause, so it cannot tell "you are stale"
    from "someone else owns this" and reports both as a stale worker. The adapter reads the row and
    *names* RE002 before issuing the statement; the database then refuses independently, in its own
    vocabulary, and would refuse even if the adapter's read never happened.

    Both halves are asserted. Naming is worth having — the transport's contract is RE002 — but it is
    not enforcement, and a test that stopped at the adapter's answer would let this be forgotten.
    """
    workflow_id = control.create(PATIENT_REF, ticket_ref="T-POSTGRES-STALE").workflow_id
    moved = control.advance(workflow_id, expected_state_version=1, to_state="PROVIDER_MATCHING")
    assert moved.state_version == 2

    with pytest.raises(IllegalTransitionError) as refusal:
        control.advance(workflow_id, expected_state_version=1, to_state="AWAITING_HOSPITAL_CHOICE")
    assert "RE002" in str(refusal.value)

    if backend != "postgres":
        return

    with (
        session(TEST_DATABASE_URL) as connection,
        pytest.raises(StaleOwnershipError) as database_refusal,
    ):
        WorkflowStore(connection).advance(
            workflow_id,
            TEST_WORKER_ID,
            expected_generation=moved.generation,
            expected_state_version=1,
            to_state="AWAITING_HOSPITAL_CHOICE",
        )
    assert database_refusal.value.sqlstate == "RE001", (
        "the database no longer reports a stale version as RE001; the divergence this test records "
        "is gone, and the adapter's naming is now the whole of the refusal rather than half of it"
    )
    assert "stale" in str(database_refusal.value)


def test_the_consent_guard_refuses_booked_without_a_recorded_confirmation(
    demo: Demo, control: SeedableControlState
) -> None:
    """§7's booking gate, in both profiles: `BOOKED` needs a confirmation *on the booking row*.

    014's `workflow_booking_consent_guard` allows `BOOKED` only when a `confirmed` `booking_request`
    exists for the workflow, and `record_booking` is the only write that can produce one — with the
    scheduler's outcome, which is why that parameter exists. This test is the other half of that
    change: the *absence* of an outcome is still refused. A guard that let a booking nobody confirmed
    through would be worse than one that let nothing through, because the state it produces is a
    claim about a hospital.

    Both profiles, and both directions in each, because either one alone is satisfiable by a profile
    that is wrong: a port that refuses every move to `BOOKED` passes the first half, and one that
    never checks passes the second. The booking is written through the port rather than over HTTP
    because the transport derives the destination from the outcome — it never *asks* for `BOOKED`
    after an unconfirmed report — so the refusal is only reachable where a caller names the
    destination itself, which a coordinator's `POST /transitions` can.

    The refusals agree on more than the code: the memory profile restates the trigger's message
    verbatim, so this is one assertion about one rule rather than two assertions about two wordings.

    Both halves write the booking *with* its transition, which is also the assertion that the pair is
    atomic in this profile: the refusal leaves no booking row behind — in the memory profile because
    the row is taken back out, in the PostgreSQL one because the transaction never committed — and a
    `bookings_for` that still reported one would mean the two halves of the write had come apart.
    """
    workflow_id = _uuid(demo.seed())
    approval_id = _uuid(demo.in_booking_flight(str(workflow_id)))

    row = control.load(workflow_id)
    assert row is not None and row.state == "BOOKING_IN_FLIGHT", (
        "the walk through the demo profile did not reach the state this gate is about"
    )
    binding = {
        "workflow_id": row.workflow_id,
        "approval_id": approval_id,
        "patient_ref": row.patient_ref,
        "payload_hash": str(row.payload[PAYLOAD_HASH_KEY]),
        "payload": row.payload["booking_payload"],
        "requested_by": TEST_WORKER_ID,
        # No outcome: the default. The gateway asked; no hospital has answered.
        "advance_to": "BOOKED",
        "expected_state_version": 7,
    }

    with pytest.raises(ApprovalRefusedError) as refusal:
        control.record_booking(**binding)
    assert "no confirmed booking_request exists" in str(refusal.value)
    assert control.bookings_for(row.workflow_id) == (), (
        "the refused transition left the booking row behind: the write and the move are not one "
        "operation in this profile"
    )
    unchanged = control.load(workflow_id)
    assert unchanged is not None and (unchanged.state, unchanged.state_version) == (
        "BOOKING_IN_FLIGHT",
        7,
    )

    # The same gate, satisfied the only way it can be: a confirmation and the hospital's reference.
    second_id = _uuid(demo.seed())
    second_approval = _uuid(demo.in_booking_flight(str(second_id)))
    second_row = control.load(second_id)
    assert second_row is not None

    reference = hospital_ref()
    confirmed = control.record_booking(
        workflow_id=second_row.workflow_id,
        approval_id=second_approval,
        patient_ref=second_row.patient_ref,
        payload_hash=str(second_row.payload[PAYLOAD_HASH_KEY]),
        payload=second_row.payload["booking_payload"],
        requested_by=TEST_WORKER_ID,
        outcome=BookingState.CONFIRMED,
        external_ref=reference,
        advance_to="BOOKED",
        expected_state_version=7,
    )
    assert confirmed.booking.state is BookingState.CONFIRMED
    assert confirmed.booking.external_ref == reference, (
        "the reference the scheduler gave is not the one the row holds, so `BOOKED` rests on a "
        "booking nobody confirmed"
    )
    assert confirmed.workflow is not None, "the port was asked for the move and did not make it"
    assert (confirmed.workflow.state, confirmed.workflow.state_version) == ("BOOKED", 8)
    assert control.bookings_for(second_row.workflow_id)[0].state is BookingState.CONFIRMED


# ---------------------------------------------------------------------------
# 3. What only the database can say
# ---------------------------------------------------------------------------


def _live_settings() -> GatewaySettings:
    """The demo profile served by PostgreSQL, with a worker id a test can build a second writer on.

    The worker id is pinned rather than left to `resolved_worker_id` because these tests walk under
    the adapter: `workflow_advance` requires the caller to be the current owner, and a synthetic
    second identity would be testing a different worker rather than the same one.
    """
    assert TEST_DATABASE_URL
    return demo_settings(
        GATEWAY_CONTROL_BACKEND="postgres",
        DATABASE_URL=TEST_DATABASE_URL,
        GATEWAY_WORKER_ID=TEST_WORKER_ID,
    )


@pytest.fixture
def live_database() -> Iterator[Any]:
    """A connection to the real server, or a skip that CI's database job turns into a failure.

    The skip is the convention `tests/domain/conftest.py` set and it is honest here for the same
    reason: these tests are *about* the server, there is nothing to assert without one, and every one
    of them carries `integration` so the job that has a database is the job that runs them — where a
    skip fails the build rather than passing quietly.
    """
    if not TEST_DATABASE_URL:
        pytest.skip(
            "TEST_DATABASE_URL is not set: these tests ask the database what it enforces, and there "
            "is no database to ask. CI runs them in the job that provides one."
        )
    with session(TEST_DATABASE_URL) as connection:
        yield connection


@pytest.fixture
def live_demo(clock: ManualClock, live_database: Any) -> Iterator[Demo]:
    """The gateway served by PostgreSQL, with real layers behind the guardrail seam.

    Built with nothing injected for `control`, so `_control_state` runs and the app has to prove the
    database answers before it exists. The two model layers are doubles because §9 closes the write
    path otherwise, and a test about what the *database* refuses cannot be run with the write path
    shut for an unrelated reason.

    It depends on `live_database` rather than reaching for `TEST_DATABASE_URL` itself, so that the
    *skip* happens in one place: a run with no DSN skips these tests instead of failing them for a
    reason that reads like a broken assertion.
    """
    settings = _live_settings()
    app: FastAPI = create_app(settings=settings, clock=clock, guardrails=allow_all_pipeline(clock))
    with TestClient(app) as client:
        yield Demo(client)


@pytest.mark.integration
def test_the_gateway_serves_from_a_real_database(live_demo: Demo, live_database: Any) -> None:
    """A workflow seeded over HTTP is a row in PostgreSQL, and the walk through it is real too.

    The end of the seeding that matters is not the response body: it is that `create` went through
    the profile's own store and the consent was written by `PostgresApprovalLedger` into the
    `approval` table. Both are read back with SQL, because the response could be right about a
    workflow that only ever existed in a dictionary.
    """
    workflow_id = live_demo.seed()
    approval_id = live_demo.in_booking_flight(workflow_id)

    with live_database.cursor() as cursor:
        cursor.execute(
            "SELECT state, state_version, owner_worker_id FROM workflow WHERE id = %s",
            (workflow_id,),
        )
        state, state_version, owner = cursor.fetchone()
        cursor.execute(
            """
            SELECT a.patient_ref, a.state_version, a.consumed_at, a.approved_by
              FROM approval a
             WHERE a.id = %s
            """,
            (approval_id,),
        )
        patient_ref, bound_version, consumed_at, approved_by = cursor.fetchone()

    assert (state, state_version, owner, patient_ref, bound_version, consumed_at) == (
        "BOOKING_IN_FLIGHT",
        7,
        TEST_WORKER_ID,
        PATIENT_REF,
        6,
        None,
    )
    assert approved_by == PATIENT_REF


@pytest.mark.integration
def test_the_transition_table_is_what_decides_which_edges_exist(live_demo: Demo) -> None:
    """The edge is absent from `workflow_transition`, and that is why the write is refused.

    `PROVIDER_MATCHING -> AWAITING_DOCTOR_CHOICE` skips a state. Nothing in the adapter is asked, so
    nothing in the adapter can be the answer: `workflow_guard` looks the pair up and raises RE002.
    The row's absence is asserted too, because a refusal that came from a list compiled into a
    function would look identical from here.
    """
    workflow_id = live_demo.seed()
    moved = live_demo.move(workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
    assert moved.status_code == 200, moved.text

    assert TEST_DATABASE_URL
    with session(TEST_DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM workflow_transition"
                " WHERE from_state = 'PROVIDER_MATCHING' AND to_state = 'AWAITING_DOCTOR_CHOICE'"
            )
            assert cursor.fetchone()[0] == 0

        with pytest.raises(IllegalTransitionError) as refusal:
            WorkflowStore(connection).advance(
                workflow_id,
                TEST_WORKER_ID,
                expected_generation=_generation(live_demo, workflow_id),
                expected_state_version=2,
                to_state="AWAITING_DOCTOR_CHOICE",
            )
    assert refusal.value.sqlstate == "RE002"
    assert "illegal transition" in str(refusal.value)
    assert "AWAITING_DOCTOR_CHOICE" in str(refusal.value)


@pytest.mark.integration
def test_one_approval_authorises_one_booking_and_the_database_is_what_says_so(
    live_demo: Demo,
) -> None:
    """§9's single use is `booking_request_one_per_approval`, enforced by an index and not by a check.

    The gateway's own gate refuses a spent approval one layer up and returns `RE004`, so this test
    goes under it: the second write is issued directly, against the same approval, and PostgreSQL
    refuses it as a unique violation. That is the point of the constraint being an index rather than
    a guard — it cannot be bypassed by a writer that skipped the guard, which is what a second
    service would be.

    `DomainError` rather than `ApprovalRefusedError` is asserted deliberately. The port's docstring
    promises RE004 for a spent approval, and the database's actual answer for the second booking is
    `23505`: the consume trigger is `AFTER INSERT`, so the index is reached first. The refusal is
    real and the code is not the promised one — recorded here rather than smoothed into a
    `pytest.raises(ApprovalRefusedError)` that would pass for the wrong reason or fail for an
    unhelpful one.

    The row is also read back as a *column*, because the port no longer returns it verbatim: a record
    written with no outcome reads `REQUESTED` to the caller and `pending` in `booking_request.state`,
    and both halves of that are asserted. A port that returned the column, or one that wrote the
    contract's spelling into it, would pass half of this.
    """
    workflow_id = live_demo.seed()
    approval_id = live_demo.in_booking_flight(workflow_id)

    assert TEST_DATABASE_URL
    store = PostgresControlState(TEST_DATABASE_URL, worker_id=TEST_WORKER_ID)
    row = store.load(_uuid(workflow_id))
    assert row is not None and row.state == "BOOKING_IN_FLIGHT"

    written = store.record_booking(
        workflow_id=row.workflow_id,
        approval_id=_uuid(approval_id),
        patient_ref=PATIENT_REF,
        payload_hash=str(row.payload[PAYLOAD_HASH_KEY]),
        payload=row.payload["booking_payload"],
        requested_by=TEST_WORKER_ID,
    ).booking
    assert written.state is BookingState.REQUESTED, (
        "the port's vocabulary is the contract's, and `REQUESTED` is what a caller that received no "
        "outcome asked for; see `_BOOKING_COLUMN`"
    )
    with session(TEST_DATABASE_URL) as connection:
        column = fetch_scalar(
            connection, "SELECT state FROM booking_request WHERE id = %s", (written.booking_id,)
        )
    assert column == "pending", (
        "the column is 008's four-value vocabulary and the port's is the contract's six; the mapping "
        "between them is `_BOOKING_COLUMN`, and a row written with a contract spelling would mean "
        "the mapping is not where the two meet"
    )
    assert len(store.bookings_for(row.workflow_id)) == 1

    with pytest.raises(DomainError) as refusal:
        store.record_booking(
            workflow_id=row.workflow_id,
            approval_id=_uuid(approval_id),
            patient_ref=PATIENT_REF,
            payload_hash=written.payload_hash,
            payload=row.payload["booking_payload"],
            requested_by=TEST_WORKER_ID,
        )
    assert getattr(refusal.value, "sqlstate", None) == "23505"
    assert "booking_request_one_per_approval" in str(refusal.value)
    assert len(store.bookings_for(row.workflow_id)) == 1


@pytest.mark.integration
def test_the_schema_refuses_to_mark_a_workflow_booked_without_a_confirmation(
    live_demo: Demo, live_database: Any
) -> None:
    """§7's booking gate, at the database, for the caller that reports no confirmation.

    A booking row exists — written by the gateway, against a live approval — and the workflow still
    cannot be marked `BOOKED`, because `workflow_booking_consent_guard` requires a `confirmed`
    `booking_request` and this one was recorded without an outcome, so it reads `pending`. The guard
    is right: nothing has confirmed the hospital's side, and a `BOOKED` that meant "we asked" would be
    the lie the whole gate exists to prevent.

    This test was written as the finding that the port *could not* say otherwise — `record_booking`
    had no outcome parameter, so the gateway could not record the confirmation the guard was asking
    for, and every booking test that ended at `BOOKED` carried `memory_only`. The port grew the
    outcome and the gap closed; this half did not move, because it is the half that is about the
    *absence* of a confirmation, and a test asserting the gate opens would not assert that it shuts.
    """
    workflow_id = live_demo.seed()
    approval_id = live_demo.in_booking_flight(workflow_id)

    assert TEST_DATABASE_URL
    store = PostgresControlState(TEST_DATABASE_URL, worker_id=TEST_WORKER_ID)
    row = store.load(_uuid(workflow_id))
    assert row is not None
    store.record_booking(
        workflow_id=row.workflow_id,
        approval_id=_uuid(approval_id),
        patient_ref=PATIENT_REF,
        payload_hash=str(row.payload[PAYLOAD_HASH_KEY]),
        payload=row.payload["booking_payload"],
        requested_by=TEST_WORKER_ID,
    )

    with (
        session(TEST_DATABASE_URL) as connection,
        pytest.raises(ApprovalRefusedError) as refusal,
    ):
        WorkflowStore(connection).advance(
            workflow_id,
            TEST_WORKER_ID,
            expected_generation=_generation(live_demo, workflow_id),
            expected_state_version=7,
            to_state="BOOKED",
        )
    assert "no confirmed booking_request exists" in str(refusal.value)

    with live_database.cursor() as cursor:
        cursor.execute("SELECT state FROM workflow WHERE id = %s", (workflow_id,))
        assert cursor.fetchone()[0] == "BOOKING_IN_FLIGHT", (
            "the workflow moved despite the refusal, so the guard is not what refused it"
        )


@pytest.mark.integration
def test_a_refused_transition_rolls_back_the_booking_and_the_approval_it_spent(
    live_demo: Demo, live_database: Any
) -> None:
    """The booking and its transition commit together, so a refused move leaves nothing behind.

    This test used to assert the opposite — that a refused transition left the booking row it had
    already written, a `pending` booking against a spent approval on a workflow that never moved —
    and it was the argument for putting the outcome on the write, where the row and the move could
    become one operation. It now asserts that the artefact is gone, from underneath the adapter.

    Asserted in SQL rather than through `bookings_for`, because the port reporting no row would only
    prove the adapter's view of a transaction it opened itself: the row count proves the row is not
    in the table. `consumed_at IS NULL` proves something stricter, and it is the one that matters for
    §9 — 008's consume trigger ran *inside* the transaction, so its effect was rolled back with
    everything else and the patient's consent was not spent by an attempt that failed. The last call
    spends it for real, which is what "still live" has to mean.

    The outcome recorded is `failed` rather than the default, so the UPDATE that records an outcome
    runs before the refusal: the rollback this asserts is the whole four-statement operation, not the
    insert alone.
    """
    workflow_id = live_demo.seed()
    approval_id = live_demo.in_booking_flight(workflow_id)

    assert TEST_DATABASE_URL
    store = PostgresControlState(TEST_DATABASE_URL, worker_id=TEST_WORKER_ID)
    row = store.load(_uuid(workflow_id))
    assert row is not None and row.state == "BOOKING_IN_FLIGHT"

    binding = {
        "workflow_id": row.workflow_id,
        "approval_id": _uuid(approval_id),
        "patient_ref": PATIENT_REF,
        "payload_hash": str(row.payload[PAYLOAD_HASH_KEY]),
        "payload": row.payload["booking_payload"],
        "requested_by": TEST_WORKER_ID,
    }
    with pytest.raises(ApprovalRefusedError) as refusal:
        store.record_booking(
            **binding,
            outcome=BookingState.REJECTED,
            advance_to="BOOKED",  # nothing confirmed it, so 014's guard refuses the move
            expected_state_version=7,
        )
    assert "no confirmed booking_request exists" in str(refusal.value)

    with live_database.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM booking_request WHERE workflow_id = %s", (workflow_id,)
        )
        assert cursor.fetchone()[0] == 0, (
            "the refused transition left the booking row behind, so the write and the move are two "
            "operations again"
        )
        cursor.execute("SELECT consumed_at FROM approval WHERE id = %s", (approval_id,))
        assert cursor.fetchone()[0] is None, (
            "the attempt spent the approval even though it was rolled back, so §9's consent is gone "
            "for a booking that does not exist"
        )
        cursor.execute("SELECT state, state_version FROM workflow WHERE id = %s", (workflow_id,))
        assert cursor.fetchone() == ("BOOKING_IN_FLIGHT", 7)

    # Consent that survived the failure is consent that can authorise the booking it was granted for.
    completed = store.record_booking(
        **binding,
        outcome=BookingState.CONFIRMED,
        external_ref=hospital_ref(),
        advance_to="BOOKED",
        expected_state_version=7,
    )
    assert completed.workflow is not None and completed.workflow.state == "BOOKED"
    assert completed.booking.state is BookingState.CONFIRMED


@pytest.mark.integration
def test_reconciliation_needs_a_live_approval_and_a_spent_one_is_not_it(
    live_demo: Demo, live_database: Any
) -> None:
    """`BOOKING_UNVERIFIED -> BOOKING_IN_FLIGHT` is a legal edge that can never be taken.

    §7 resolves an unknown booking outcome by reconciliation, and the edge is in the table for it. The
    consent guard refuses the move anyway, because it demands an approval live at the *current*
    version and the approval was spent by the booking that produced the uncertainty. So the recovery
    path exists in the state machine and is closed by §9's single-use rule.

    That is a finding about the two rules meeting, not a bug in either: re-entering `BOOKING_IN_FLIGHT`
    means attempting a second booking, which needs new consent. Recorded here so the next reader does
    not spend the afternoon discovering it.
    """
    workflow_id = live_demo.seed()
    approval_id = live_demo.in_booking_flight(workflow_id)
    assert approval_id  # spent by the booking below, which is the point

    unverified = live_demo.move(
        workflow_id,
        version=7,
        to_state=WorkflowState.BOOKING_UNVERIFIED,
        props={"notice": "the hospital has not confirmed the slot yet"},
    )
    assert unverified.status_code == 200, unverified.text
    assert live_demo.snapshot(workflow_id).json()["state"] == "BOOKING_UNVERIFIED"

    with live_database.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM workflow_transition"
            " WHERE from_state = 'BOOKING_UNVERIFIED' AND to_state = 'BOOKING_IN_FLIGHT'"
        )
        assert cursor.fetchone()[0] == 1, "the edge this test is about is not in the table"

    with (
        session(TEST_DATABASE_URL) as connection,
        pytest.raises(ApprovalRefusedError) as refusal,
    ):
        WorkflowStore(connection).advance(
            workflow_id,
            TEST_WORKER_ID,
            expected_generation=_generation(live_demo, workflow_id),
            expected_state_version=8,
            to_state="BOOKING_IN_FLIGHT",
        )
    assert "no live approval" in str(refusal.value)


@pytest.mark.integration
def test_the_command_ledger_refuses_a_reused_id_with_a_different_body(
    live_demo: Demo, live_database: Any
) -> None:
    """§7's replay rule, asked of the ledger directly: same id replays, a different body does not.

    The adapter checks nothing here — it hands the id and the hash to the function and returns what it
    says — so this is the database's rule in the database's terms. Three outcomes are asserted, not
    one, because each is a different claim about the same function: an id never seen before is work to
    do, an id recorded against the same hash is a retry to answer, and the same id against a different
    hash is a refusal. A function that raised on every second call would satisfy the refusal alone
    while breaking every client retry in the product, and a function that answered `replay` before the
    command had completed would answer a retry with a result that was still being produced.
    """
    # A fresh id per run: these tests do not reset the database, and a fixed one would make the
    # "not yet recorded" assertion below depend on whether the suite had ever been run before.
    command_id = f"postgres-replay-{uuid4().hex}"

    with live_database.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM command_dedup WHERE command_id = %s", (command_id,))
        assert cursor.fetchone()[0] == 0

    with session(TEST_DATABASE_URL) as connection:
        ledger = CommandLedger(connection)
        first = ledger.begin(command_id, "a" * 64)
        assert first.is_replay is False, "an id nobody has used is work to do, not a replay"
        assert ledger.begin(command_id, "a" * 64).is_replay is False, (
            "an uncompleted command replayed: a client retrying a request that is still running would "
            "be handed a result that does not exist yet"
        )
        ledger.complete(command_id, "a" * 64, response_status=200, response={"booking_id": "b-1"})
        replayed = ledger.begin(command_id, "a" * 64)
        assert replayed.is_replay is True
        assert replayed.record.response == {"booking_id": "b-1"}
        with pytest.raises(CommandConflictError) as refusal:
            ledger.begin(command_id, "b" * 64)
    assert refusal.value.sqlstate == "RE005"
    assert "different request hash" in str(refusal.value)


def _uuid(value: str) -> UUID:
    """The id as the ports want it. `Demo` speaks strings because JSON does."""
    return UUID(str(value))


def _generation(demo: Demo, workflow_id: str) -> int:
    """The row's current generation, read the way a worker would read it."""
    assert TEST_DATABASE_URL
    store = PostgresControlState(TEST_DATABASE_URL, worker_id=TEST_WORKER_ID)
    row = store.load(_uuid(workflow_id))
    assert row is not None
    return int(row.generation)
