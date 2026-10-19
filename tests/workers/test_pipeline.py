"""The booking leg, assembled: claim, resume, act, checkpoint, advance (§7, §11, §17).

Every rule this file pins is a statement about *what happens with more than one of* something — two
replicas, two attempts, two sources of truth — so each one gets a test that fails if the behaviour is
deleted rather than a comment saying it holds:

* **one booking per logical attempt.** The command id is derived from (workflow, key, step), so a
  replica arriving at a step whose command is already recorded replays the ledger's outcome and goes
  to the lookup instead of sending again. The booking is sent once, by whoever won the lease — and the
  *same* key is what the next owner sends under if the winner's answer never landed.
* **resume, not restart.** A worker that dies mid-leg leaves a verified checkpoint behind it, and the
  next replica starts at the step *after* it: one send, three attempts, one command.
* **the write is never re-planned from an unverified outcome.** `BOOKING_UNVERIFIED` is the state
  whose meaning is "a request may be out there", so a plan containing `send_booking` from there is the
  double-booking bug — refused by the policy table (`RE006`) *before* the lookup could run.
  `booking_steps_for` drops it, including when the payload proves nothing, because every unreadable
  answer advances the workflow without a checkpoint.
* **a lost lease cannot land a late result.** The send may leave the process; the record of it may
  not. That is the one thing that makes the resume above safe rather than a second-booking vector.
* **`BOOKED` rests on the durable record, not on the facility's word.** The `booking_request` row is
  what 007's guard requires, and the same step writes it, in that order.
* **a handoff the patient has not consented to is staged and not queued** (§11), and an escalation is
  named for its cause — a spent reconciliation is `prolonged_unknown_booking`, not "something unknown
  happened".

The doubles are the facility and the clock (`worker_doubles.py`); the database, the command ledger,
the policy table and the transition table are the real ones, because those are what these claims are
about. `db` and `db2` are two real connections, and the tests that assert two-replica behaviour commit
between them the way a replica does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from worker_doubles import (
    SPEC_CAPS,
    FakeClock,
    ScriptedBookingApi,
    Seeder,
    expire_lease,
    workflow_payload,
    workflow_row,
)

from referraleasy_agent_runtime import UpstreamTransient
from referraleasy_contracts.generated.booking import BookingAttemptResult
from referraleasy_contracts.generated.common import HandoffReasonClass
from referraleasy_domain import Workflow
from referraleasy_domain.errors import StaleOwnershipError
from referraleasy_workers.booking import BookingAttemptView, LookupOutcome
from referraleasy_workers.booking_record import existing_booking_request
from referraleasy_workers.checkpoint import HANDOFF_KEY, VERIFIED, read_checkpoint
from referraleasy_workers.commands import run_command
from referraleasy_workers.events import BOOKING_UNVERIFIED_STATE
from referraleasy_workers.hitl import HITL_TRIGGERS, queued_handoffs
from referraleasy_workers.ownership import OwnedWorkflow
from referraleasy_workers.reconcile import BookingReconciler, EscalationReason
from referraleasy_workers.recovery import BOOKED_STATE
from referraleasy_workers.service import (
    BOOKING_STEPS,
    SEND_BOOKING_STEP,
    BookingPipeline,
    booking_steps_for,
    command_id_for,
)
from referraleasy_workers.settings import WorkerSettings

#: The worker the environment fixture names (`WORKER_ID=w-1`), so the pipeline and the leased row
#: agree about who is working. A mismatch would be refused by the lease, which would make every test
#: here a test of something else.
WORKER = "w-1"
#: The other replica: a second connection, a second worker id, one workflow.
REPLICA_B = "w-b"

KEY = "idem-booking-1"
#: The pair the seeded approval is bound to (`Seeder.consent_ready` hashes exactly this), so the send
#: carries the payload the patient consented to rather than something a test invented.
REQUEST = {"slot_id": "S-1", "doctor_id": "D-9"}
BOOKING_REF = "B-1"


async def no_sleep(_seconds: float) -> None:
    """The reconciler's pause, removed. No test in this suite waits on a backoff curve."""
    return None


def pipeline(
    api: ScriptedBookingApi,
    settings: WorkerSettings,
    *,
    steps: Sequence[str] = BOOKING_STEPS,
    lookup_budget: int = 2,
) -> BookingPipeline:
    """The real pipeline over a scripted facility, with the one loop it does not own made instant."""
    return BookingPipeline(
        api=api,
        caps=SPEC_CAPS,
        settings=settings,
        reconciler=BookingReconciler(
            api=api, caps=SPEC_CAPS, lookup_budget=lookup_budget, sleep=no_sleep
        ),
        clock=FakeClock(),
        steps=steps,
    )


def run(
    conn: Any,
    api: ScriptedBookingApi,
    settings: WorkerSettings,
    workflow: OwnedWorkflow,
    *,
    steps: Sequence[str] | None = None,
    key: str = KEY,
    request: dict[str, str] | None = None,
    **kwargs: Any,
) -> Any:
    """One pipeline run, entered the way a worker enters it."""
    return asyncio.run(
        pipeline(api, settings, steps=steps or BOOKING_STEPS).run(
            conn,
            workflow=workflow,
            key=key,
            request=REQUEST if request is None else request,
            **kwargs,
        )
    )


# ---------------------------------------------------------------------------
# The ordinary path, and what it rests on
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_confirmed_booking_reaches_booked_with_the_record_that_proves_it(
    db: Any, seed: Seeder, api: ScriptedBookingApi, settings: WorkerSettings
) -> None:
    """The facility answered `created`, and the workflow may only say `BOOKED` once the row agrees.

    007's guard refuses the state without a `confirmed` record, and the record is only legitimate if
    it spends the patient's own approval — so the assertions are about the pair: the state, *and* the
    row whose `external_ref` is the facility's answer.
    """
    row, _approval_id, _payload_hash = seed.in_flight()
    owned = OwnedWorkflow(db, WORKER, row)

    outcome = run(db, api, settings, owned)

    assert outcome.state == BOOKED_STATE
    assert outcome.booked is True
    assert outcome.executed_steps == (SEND_BOOKING_STEP,)
    assert outcome.detail == "the facility confirmed the booking"
    assert outcome.attempt is not None
    assert outcome.attempt.result is BookingAttemptResult.created
    assert api.created_keys == [KEY]
    assert api.lookup_keys == [], "a definite answer needs no lookup"

    stored = read_checkpoint(Workflow.from_row(workflow_row(db, row.id)))
    assert stored.verified is True
    assert stored.checkpoint is not None
    assert stored.checkpoint.step == SEND_BOOKING_STEP

    record = existing_booking_request(db, row.id)
    assert record is not None, "`BOOKED` may not be claimed without a booking_request row"
    assert record["state"] == "confirmed"
    assert record["external_ref"] == BOOKING_REF
    assert record["requested_by"] == f"workers:{WORKER}"
    assert workflow_row(db, row.id)["state"] == BOOKED_STATE


@pytest.mark.integration
def test_a_resumed_replica_skips_the_send_and_looks_up_instead(
    db: Any, seed: Seeder, settings: WorkerSettings
) -> None:
    """One send, three attempts, one command — and the second replica starts *after* the send.

    A's budget is spent with the request already on the wire, which is `outcome_unknown`: the step is
    complete (the ledger holds its outcome) but the answer is not knowable, so A checkpoints the step
    and moves the workflow to `BOOKING_UNVERIFIED`. The next replica reads that checkpoint, plans
    `confirm_outcome` alone, and resolves the question the only way §7 allows — a lookup under the
    same key.

    The assertion that carries the requirement is the count: three create calls before, three after. A
    replica that re-planned the send would make it four, and would be booking twice.
    """
    row, _approval_id, _payload_hash = seed.in_flight()
    owned = OwnedWorkflow(db, WORKER, row)
    api = ScriptedBookingApi(
        create=[
            UpstreamTransient("no answer from the facility") for _ in range(SPEC_CAPS.max_attempts)
        ]
    )

    first = run(db, api, settings, owned, steps=(SEND_BOOKING_STEP,))

    assert first.state == BOOKING_UNVERIFIED_STATE
    assert first.booking_may_exist is True
    assert first.booked is False
    assert first.executed_steps == (SEND_BOOKING_STEP,)
    assert len(api.create_calls) == SPEC_CAPS.max_attempts
    assert {key for key, _command, _timeout in api.create_calls} == {KEY}, "one command, one key"
    staged = read_checkpoint(Workflow.from_row(workflow_row(db, row.id)))
    assert staged.verified is True
    assert staged.checkpoint is not None
    assert staged.checkpoint.step == SEND_BOOKING_STEP
    assert staged.checkpoint.data["sent_by_this_replica"] is True
    assert staged.checkpoint.data["command_id"] == command_id_for(row.id, KEY, SEND_BOOKING_STEP)

    second = run(db, api, settings, owned)

    assert second.resumed is True
    assert second.resume_reason == VERIFIED
    assert second.executed_steps == ("confirm_outcome",)
    assert second.state == BOOKED_STATE and second.booked is True
    assert len(api.create_calls) == SPEC_CAPS.max_attempts, "the resumed replica did not re-send"
    assert api.lookup_keys == [KEY]
    assert existing_booking_request(db, row.id)["state"] == "confirmed"


@pytest.mark.integration
def test_a_replica_taking_over_an_unverified_workflow_looks_up_rather_than_sending(
    db: Any, seed: Seeder, api: ScriptedBookingApi, settings: WorkerSettings
) -> None:
    """The crash case, and the one `booking_steps_for` exists for (§7).

    Every route into `BOOKING_UNVERIFIED` on an *unreadable* answer — an exhausted attempt budget, a
    transport timeout, an abandonment with a write in flight — advances the workflow without a
    checkpoint, because there is no completed step to record. The next replica therefore plans the
    whole leg, `send_booking` included, and the plan is what it must not follow: the state it is
    standing in *is* the statement that a request may already be out there.

    The checkpoint is deleted first so the payload proves nothing, and the send is asserted to have
    never happened. Without the filter, the policy table refuses `create_booking` from this state
    (`RE006`) before the lookup is reached, so there is no version of this that passes by accident.
    """
    row, _approval_id, _payload_hash = seed.in_flight()
    owned = OwnedWorkflow(db, WORKER, row)
    owned.advance(BOOKING_UNVERIFIED_STATE)
    db.execute("UPDATE workflow SET payload = payload - 'checkpoint' WHERE id = %s", (row.id,))

    outcome = run(db, api, settings, owned)

    assert outcome.resumed is False
    assert outcome.resume_reason == "no checkpoint"
    assert outcome.executed_steps == ("confirm_outcome",)
    assert outcome.state == BOOKED_STATE and outcome.booked is True
    assert api.create_calls == [], "the write was not planned from an unverified outcome"
    assert api.lookup_keys == [KEY]
    assert outcome.detail.startswith("no checkpoint; the booking write is not planned")


# ---------------------------------------------------------------------------
# Two replicas
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_second_replica_dedupes_on_the_command_id_and_does_not_send_again(
    db: Any, db2: Any, seed: Seeder, api: ScriptedBookingApi, settings: WorkerSettings
) -> None:
    """Replica A recorded the command and died before checkpointing anything; B replays and looks up.

    This is the interleaving the ledger is for. B plans `send_booking`, because the workflow is still
    in `BOOKING_IN_FLIGHT` and the payload proves nothing — so the *command id* is the only thing
    between B and a second booking, and it is derived from (workflow, key, step) rather than minted
    per replica. B's replay comes back with no local value, which `_send_booking` reads as "somebody
    else's request is the one on the wire": it checkpoints, moves to `BOOKING_UNVERIFIED`, and resolves
    with a lookup under the same key, in the same run.

    A's side is driven through `run_command` rather than a whole pipeline run, deliberately: a full A
    run would have moved the workflow past the point B is taking over, and there would be nothing left
    to test. The artefact under test is the ledger row, and this writes exactly it.
    """
    row, _approval_id, _payload_hash = seed.in_flight()
    db.commit()  # B has to be able to see the workflow it is taking over

    command_id = command_id_for(row.id, KEY, SEND_BOOKING_STEP)

    async def a_sends(lease: Any) -> BookingAttemptView:
        return await api.create_booking(
            key=KEY, payload=REQUEST, command_id=command_id, timeout_ms=lease.timeout_ms
        )

    recorded = asyncio.run(
        run_command(
            db,
            command_id=command_id,
            request={"idempotency_key": KEY, **REQUEST},
            execute=a_sends,
            caps=SPEC_CAPS,
            clock=FakeClock(),
            site="scheduling:create_booking",
            write_attempt=True,
        )
    )
    assert recorded.executed is True and recorded.deduplicated is False
    assert len(api.create_calls) == 1, "replica A sent it"
    db.commit()

    expire_lease(db2, row.id)
    replica_b = OwnedWorkflow.claim(db2, row.id, REPLICA_B, expected_generation=row.generation)
    db2.commit()

    outcome = run(db2, api, settings, replica_b)

    assert outcome.deduplicated_onto_another_replica is True
    assert outcome.sent_by_this_replica is False
    assert len(api.create_calls) == 1, "replica B replayed the ledger instead of sending"
    assert outcome.executed_steps == ("confirm_outcome",)
    assert outcome.state == BOOKED_STATE and outcome.booked is True
    assert api.lookup_keys == [KEY]
    skipped = next(record for record in outcome.steps if record.step == SEND_BOOKING_STEP)
    assert skipped.skipped is True
    assert "already recorded" in skipped.detail
    assert workflow_row(db2, row.id)["owner_worker_id"] == REPLICA_B


@pytest.mark.integration
def test_a_replica_that_lost_its_lease_cannot_land_its_late_booking(
    db: Any,
    db2: Any,
    seed: Seeder,
    api: ScriptedBookingApi,
    settings: WorkerSettings,
    refusals: Callable[[type[BaseException]], Any],
) -> None:
    """The requirement the resume above depends on: the send may happen, its result may not land (§7).

    A's lease lapses and B takes the workflow over. A then finishes its step — the facility answers
    `created`, so a request really is on the wire — and every write it attempts is refused, the
    checkpoint by this package's compare-and-set, which re-reads the row and raises `RE001`. What A
    leaves behind is nothing: the workflow row is exactly B's, no checkpoint was written, and the
    `booking_request` row A had recorded in the same transaction went with it, because the refusal
    aborts that transaction. A late result that lands *nothing* is what makes the next owner's resume
    safe.

    The epilogue is the other half of the same rule. B sends under the *same* key and the facility
    answers `duplicate_replayed` — which is why the idempotency key, not the ledger, is the last guard
    on a duplicate booking.
    """
    row, _approval_id, _payload_hash = seed.in_flight()
    db.commit()
    stale = OwnedWorkflow(db, WORKER, row)

    expire_lease(db2, row.id)
    replica_b = OwnedWorkflow.claim(db2, row.id, REPLICA_B, expected_generation=row.generation)
    db2.commit()

    with refusals(StaleOwnershipError):
        run(db, api, settings, stale)

    assert len(api.create_calls) == 1, "the request left the process before the write was refused"
    assert api.lookup_keys == [], "and it never got as far as the lookup"

    current = workflow_row(db, row.id)
    assert current["state"] == "BOOKING_IN_FLIGHT"
    assert current["owner_worker_id"] == REPLICA_B
    assert current["generation"] == replica_b.generation
    assert read_checkpoint(Workflow.from_row(current)).checkpoint is None
    assert existing_booking_request(db, row.id) is None, "the late record did not survive"

    takeover = ScriptedBookingApi(default_create=BookingAttemptResult.duplicate_replayed)
    second = run(db2, takeover, settings, replica_b)

    assert second.state == BOOKED_STATE
    assert takeover.created_keys == [key for key, _command, _timeout in api.create_calls] == [KEY]
    assert takeover.distinct_lookup_keys == set(), "a definite answer needs no lookup"


# ---------------------------------------------------------------------------
# The end of the automatic path (§11)
# ---------------------------------------------------------------------------


def unanswerable_facility() -> ScriptedBookingApi:
    """A facility that answers nothing and gives up on nothing: the lookup budget is what runs out."""
    return ScriptedBookingApi(default_lookup=LookupOutcome.LOOKUP_FAILED_OR_TIMED_OUT)


@pytest.mark.integration
def test_a_spent_reconciliation_escalates_for_the_reason_it_ran_out(
    db: Any, seed: Seeder, settings: WorkerSettings
) -> None:
    """An unanswerable booking goes to a person, named as `prolonged_unknown_booking` (§11).

    The reason class is the *cause* — the budget ran out with a request possibly in flight — and not
    "something went wrong". That is what the trigger list is for: a human reading
    `prolonged_unknown_booking` knows to go and look at the facility, and the patient is told that the
    summary they approved has been shared rather than that a system failed.
    """
    row, _approval_id, _payload_hash = seed.in_flight()
    owned = OwnedWorkflow(db, WORKER, row)
    owned.advance(BOOKING_UNVERIFIED_STATE)
    api = unanswerable_facility()

    outcome = run(db, api, settings, owned, consent_given_at=datetime.now(UTC))

    assert outcome.reconciliation is not None
    assert outcome.reconciliation.escalation_reason is EscalationReason.BUDGET_EXHAUSTED
    assert api.distinct_lookup_keys == {KEY}, "escalated under the same key, never a second one"
    assert outcome.state == "HUMAN_HANDOFF"
    assert outcome.needs_human is True
    assert outcome.booked is False
    assert outcome.handoff is not None
    assert outcome.handoff.queued is True
    assert outcome.handoff.request.reason_class is HandoffReasonClass.prolonged_unknown_booking
    assert outcome.patient_message == (
        "We could not finish this automatically. We have shared the summary you approved with "
        "a member of our team, and they will pick it up."
    )

    queued = queued_handoffs(db)
    assert len(queued) == 1
    assert queued[0].workflow_id == row.id
    assert queued[0].state == "open"
    assert queued[0].reason_class is HandoffReasonClass.prolonged_unknown_booking


@pytest.mark.integration
def test_an_escalation_without_the_patients_consent_is_staged_and_not_queued(
    db: Any, seed: Seeder, settings: WorkerSettings
) -> None:
    """§11's gate, end to end: the patient sees the exact text before anything is shared.

    Consent is not a flag on a handoff row — the row *is* the share — so a handoff with no consent is
    not written at all. What the run leaves is the staging: the summary the patient will be shown,
    under the workflow payload, with the workflow still in `BOOKING_UNVERIFIED` and nothing in the
    queue. The staged text is compared to the request's own text, because "the patient reviews the
    summary" is only true if the two are the same string.
    """
    row, _approval_id, _payload_hash = seed.in_flight()
    owned = OwnedWorkflow(db, WORKER, row)
    owned.advance(BOOKING_UNVERIFIED_STATE)
    api = unanswerable_facility()

    outcome = run(db, api, settings, owned)

    assert outcome.handoff is not None
    assert outcome.handoff.awaiting_consent is True
    assert outcome.handoff.staged is True
    assert outcome.handoff.request.consent_given_at is None
    assert outcome.state == BOOKING_UNVERIFIED_STATE, "no consent means no move"
    assert outcome.needs_human is True
    assert outcome.patient_message == (
        "We could not finish this automatically. Here is exactly what we would share with a "
        "member of our team — please review it and tell us whether we may."
    )

    staged = workflow_payload(db, row.id)[HANDOFF_KEY]
    assert staged["patient_visible_summary"] == outcome.handoff.request.patient_visible_summary
    assert staged["reason_class"] == HandoffReasonClass.prolonged_unknown_booking.value
    assert staged["consent_given_at"] is None
    assert staged["sources"] == list(outcome.handoff.request.sources)
    assert staged["sources"], "the human gets the evidence lines, not just a sentence"
    assert staged["ticket_ref"]

    assert queued_handoffs(db) == []
    assert outcome.handoff.request.reason_class in [
        trigger.reason_class for trigger in HITL_TRIGGERS
    ]


# ---------------------------------------------------------------------------
# Two pure rules the rest of the file leans on
# ---------------------------------------------------------------------------


def test_the_booking_write_is_dropped_from_a_plan_for_an_unverified_workflow() -> None:
    """`booking_steps_for` is a filter with one condition, and the condition is the whole rule (§7).

    It is not a general policy engine: it knows about exactly the state whose meaning is "a request
    may be out there", it never reorders a plan, and it is idempotent. Everywhere else the plan is
    left alone, because everywhere else the state itself says whether a booking may be attempted —
    and the policy table, not this function, is what answers that.
    """
    assert SEND_BOOKING_STEP in BOOKING_STEPS
    assert booking_steps_for(BOOKING_UNVERIFIED_STATE, BOOKING_STEPS) == ("confirm_outcome",)
    assert booking_steps_for(BOOKING_UNVERIFIED_STATE, (SEND_BOOKING_STEP,)) == ()
    assert booking_steps_for(BOOKING_UNVERIFIED_STATE, ("confirm_outcome",)) == ("confirm_outcome",)
    assert booking_steps_for(BOOKING_UNVERIFIED_STATE, ()) == ()
    for state in ("BOOKING_IN_FLIGHT", "AWAITING_FINAL_APPROVAL", "HUMAN_HANDOFF"):
        assert booking_steps_for(state, BOOKING_STEPS) == BOOKING_STEPS


def test_the_command_id_is_one_deterministic_id_per_workflow_key_and_step() -> None:
    """The mechanism behind "sent once": two replicas derive the *same* id, or nothing dedupes.

    A random id per replica would be the double-booking bug wearing a UUID; an id that ignored the key
    would merge two logical bookings; an id that ignored the step would make the confirmation an alias
    of the send. A `UUID` object bound to `command_dedup.command_id` (a `text` column) is refused by
    PostgreSQL rather than coerced, so the spelling is part of the contract too.
    """
    workflow_id = "00000000-0000-0000-0000-000000000001"
    same = command_id_for(workflow_id, KEY, SEND_BOOKING_STEP)
    assert same == command_id_for(workflow_id, KEY, SEND_BOOKING_STEP)
    assert (
        len(
            {
                same,
                command_id_for(workflow_id, "another-key", SEND_BOOKING_STEP),
                command_id_for(workflow_id, KEY, "confirm_outcome"),
                command_id_for("00000000-0000-0000-0000-000000000002", KEY, SEND_BOOKING_STEP),
            }
        )
        == 4
    )
    assert isinstance(same, str)
    assert len(same) == 36
