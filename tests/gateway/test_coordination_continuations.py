"""Both stores enforce the same continuation lifecycle and claim fences."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from referraleasy_domain import canonical_request_hash
from referraleasy_gateway.commands import CommandService, DispatchTicket
from referraleasy_gateway.continuations import (
    ContinuationExpired,
    ContinuationKind,
    ContinuationLifecycle,
    ContinuationRecord,
    ContinuationReplaced,
)
from referraleasy_gateway.coordination import CoordinationError, CoordinationService
from referraleasy_gateway.errors import BookingHaltedError


@pytest.fixture
def lifecycle(control, clock):
    return ContinuationLifecycle(control, clock=clock)


APPROVAL = "a0000000-0000-4000-8000-000000000001"


def dispatching_commands(row):
    """A command plane whose switch is released: it mints a ticket for the stored approval."""
    commands = Mock(spec=CommandService)
    commands.authorise_dispatch.return_value = DispatchTicket(
        workflow_id=row.workflow_id, approval_id=APPROVAL
    )
    return commands


def enqueue(control, lifecycle, row, *, commit=True, kind=ContinuationKind.ANSWER):
    command_id = str(uuid4())
    digest = canonical_request_hash({"command_id": command_id})
    control.begin_command(command_id, digest)
    control.update_payload(
        row.workflow_id,
        expected_state_version=row.state_version,
        patch=lifecycle.pending_patch(command_id, kind),
    )
    if commit:
        control.complete_command(
            command_id, digest, response_status=202, response={"accepted": True}
        )
    return command_id, digest


def expire_lease(control, claim):
    """Simulate process loss at the storage boundary without waiting two minutes."""
    row = control.load(claim.workflow_id)
    record = ContinuationRecord.read(row.payload)
    assert record is not None
    expired = replace(record, lease_until=datetime.now(UTC) - timedelta(seconds=1))
    control.update_payload(
        row.workflow_id, expected_state_version=row.state_version, patch=expired.patch()
    )


def test_work_waits_for_the_patient_command_to_commit(control, lifecycle):
    row = control.create("continuation-patient")
    command, digest = enqueue(control, lifecycle, row, commit=False)
    assert lifecycle.pending() == ()
    assert lifecycle.claim(row.workflow_id) is None
    control.complete_command(command, digest, response_status=202, response={"accepted": True})
    assert lifecycle.pending() == (row.workflow_id,)
    claim = lifecycle.claim(row.workflow_id)
    assert claim is not None and claim.command_id == command


def test_missing_command_receipt_cannot_start_work(control, lifecycle):
    row = control.create("continuation-patient")
    control.update_payload(
        row.workflow_id,
        expected_state_version=row.state_version,
        patch=lifecycle.pending_patch(str(uuid4())),
    )
    assert lifecycle.pending() == ()
    assert lifecycle.claim(row.workflow_id) is None


def test_only_one_worker_claims_work(control, lifecycle, clock):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    other = ContinuationLifecycle(control, clock=clock)
    with ThreadPoolExecutor(max_workers=2) as workers:
        claims = list(workers.map(lambda worker: worker.claim(row.workflow_id), [lifecycle, other]))
    assert sum(claim is not None for claim in claims) == 1
    assert lifecycle.pending() == ()


def test_expired_claim_can_resume_and_stale_worker_cannot_finish(control, lifecycle, clock):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    first = lifecycle.claim(row.workflow_id)
    assert first is not None
    expire_lease(control, first)
    assert lifecycle.pending() == (row.workflow_id,)
    replacement = ContinuationLifecycle(control, clock=clock)
    resumed = replacement.claim(row.workflow_id)
    assert resumed is not None and resumed.token != first.token
    assert lifecycle.finish(first) is False
    with pytest.raises(ContinuationReplaced):
        lifecycle.refresh(first)
    assert replacement.refresh(resumed).workflow_id == row.workflow_id
    assert replacement.finish(resumed) is True
    assert lifecycle.pending() == ()


def test_expired_claim_cannot_renew_recover_or_finish_before_takeover(control, lifecycle):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    claim = lifecycle.claim(row.workflow_id)
    assert claim is not None
    expire_lease(control, claim)
    with pytest.raises(ContinuationReplaced):
        lifecycle.refresh(claim)
    assert lifecycle.recoverable(claim) is None
    assert lifecycle.finish(claim) is False
    assert lifecycle.pending() == (row.workflow_id,)


def test_same_worker_uses_a_new_token_when_it_reclaims_the_same_command(control, lifecycle):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    first = lifecycle.claim(row.workflow_id)
    assert first is not None
    expire_lease(control, first)
    second = lifecycle.claim(row.workflow_id)
    assert second is not None and second.command_id == first.command_id
    assert second.token != first.token
    assert lifecycle.finish(first) is False
    assert lifecycle.recoverable(first) is None
    assert lifecycle.finish(second) is True


def test_finishing_old_work_cannot_erase_the_next_patient_answer(control, lifecycle):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    first = lifecycle.claim(row.workflow_id)
    assert first is not None
    next_command, _ = enqueue(control, lifecycle, row)
    assert lifecycle.finish(first) is False
    assert lifecycle.recoverable(first) is None
    assert lifecycle.pending() == (row.workflow_id,)
    next_claim = lifecycle.claim(row.workflow_id)
    assert next_claim is not None and next_claim.command_id == next_command
    assert lifecycle.finish(next_claim) is True


def test_finished_work_cannot_renew_recover_or_finish_again(control, lifecycle):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    claim = lifecycle.claim(row.workflow_id)
    assert claim is not None
    assert lifecycle.finish(claim) is True
    assert lifecycle.finish(claim) is False
    assert lifecycle.recoverable(claim) is None
    assert lifecycle.claim(row.workflow_id) is None
    with pytest.raises(ContinuationReplaced):
        lifecycle.refresh(claim)


@pytest.mark.parametrize("kind", list(ContinuationKind))
def test_claim_retains_intent_and_refresh_reads_current_workflow(control, lifecycle, kind):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row, kind=kind)
    claim = lifecycle.claim(row.workflow_id)
    assert claim is not None and claim.kind is kind
    moved = control.advance(
        row.workflow_id, expected_state_version=row.state_version, to_state="PROVIDER_MATCHING"
    )
    assert lifecycle.refresh(claim).state_version == moved.state_version


def test_job_expires_at_five_minutes_but_its_owner_can_recover(control, lifecycle, clock):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    clock.advance_ms(299_999)
    claim = lifecycle.claim(row.workflow_id)
    assert claim is not None
    assert lifecycle.refresh(claim).workflow_id == row.workflow_id
    clock.advance_ms(1)
    with pytest.raises(ContinuationExpired, match="expired"):
        lifecycle.refresh(claim)
    assert lifecycle.recoverable(claim).workflow_id == row.workflow_id
    assert lifecycle.finish(claim) is True


def test_missing_workflow_has_no_claim(lifecycle):
    assert lifecycle.claim(uuid4()) is None


def test_earlier_job_format_remains_readable(control, lifecycle):
    row = control.create("continuation-patient")
    command, digest = enqueue(control, lifecycle, row, commit=False)
    control.update_payload(
        row.workflow_id,
        expected_state_version=row.state_version,
        patch={"coordination_job": {"command_id": command, "pending": True}},
    )
    control.complete_command(command, digest, response_status=202, response={"accepted": True})
    claim = lifecycle.claim(row.workflow_id)
    assert claim is not None and claim.kind is ContinuationKind.ANSWER
    assert claim.expires_at is None
    assert lifecycle.refresh(claim).workflow_id == row.workflow_id
    assert lifecycle.finish(claim) is True


def test_pending_scan_is_bounded_in_both_adapters(control, lifecycle):
    for _ in range(33):
        row = control.create("continuation-patient", ticket_ref=str(uuid4()))
        enqueue(control, lifecycle, row)
    assert len(lifecycle.pending()) == 32


def test_expired_job_requests_recovery_without_calling_specialists(control, lifecycle, clock):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    clock.advance_ms(300_000)
    coordinator = Mock()
    worker = CoordinationService(
        control=control, commands=Mock(spec=CommandService), coordinator=coordinator, clock=clock
    )
    worker._recover = Mock()
    assert worker.run_pending() == 1
    worker._recover.assert_called_once()
    coordinator.execute.assert_not_called()
    assert lifecycle.pending() == ()


def test_result_from_replaced_work_cannot_write_specialist_results(control, lifecycle, clock):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    claim = lifecycle.claim(row.workflow_id)
    assert claim is not None

    def replace_work(**kwargs):
        enqueue(control, lifecycle, row)
        return {"output": {"specialist_results": {"scheduling": {"output": {"slots": []}}}}}

    worker = CoordinationService(
        control=control,
        commands=Mock(spec=CommandService),
        coordinator=Mock(execute=replace_work),
        clock=clock,
    )
    with pytest.raises(ContinuationReplaced):
        worker._call(claim, row, {"scheduling": {}}, step="slots")
    assert "specialist_results" not in control.load(row.workflow_id).payload
    assert lifecycle.pending() == (row.workflow_id,)


def test_result_after_job_expiry_cannot_write_specialist_results(control, lifecycle, clock):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    claim = lifecycle.claim(row.workflow_id)
    assert claim is not None

    def expire_work(**kwargs):
        clock.advance_ms(300_000)
        return {"output": {"specialist_results": {"scheduling": {"output": {"slots": []}}}}}

    worker = CoordinationService(
        control=control,
        commands=Mock(spec=CommandService),
        coordinator=Mock(execute=expire_work),
        clock=clock,
    )
    with pytest.raises(ContinuationExpired):
        worker._call(claim, row, {"scheduling": {}}, step="slots")
    assert "specialist_results" not in control.load(row.workflow_id).payload


def test_lost_claim_during_failure_cannot_request_recovery(control, lifecycle, clock):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    worker = CoordinationService(
        control=control, commands=Mock(spec=CommandService), coordinator=Mock(), clock=clock
    )

    def fail(claim):
        expire_lease(control, claim)
        raise RuntimeError("Specialist failed after lease expiry.")

    worker._resume = fail
    worker._recover = Mock()
    assert worker.run_pending() == 1
    worker._recover.assert_not_called()
    assert lifecycle.pending() == (row.workflow_id,)


@pytest.mark.parametrize("error", [ContinuationReplaced, ContinuationExpired])
def test_lost_booking_claim_cannot_record_an_outcome(control, lifecycle, clock, error):
    row = control.create(
        "continuation-patient",
        payload={
            "booking_idempotency_key": "original-key",
            "booking_payload": {},
            "booking_authority": {"approval_id": APPROVAL},
            "selected_slot_version": 1,
        },
    )
    enqueue(control, lifecycle, row)
    claim = lifecycle.claim(row.workflow_id)
    assert claim is not None
    row = lifecycle.refresh(claim)
    commands = dispatching_commands(row)
    worker = CoordinationService(
        control=control, commands=commands, coordinator=Mock(), clock=clock
    )
    worker._call = Mock(side_effect=error)
    with pytest.raises(error):
        worker._book(claim, row, SimpleNamespace(scheduling=lambda: {}))
    commands.book.assert_not_called()


def test_failed_recovery_keeps_work_for_a_replacement(control, lifecycle, clock):
    row = control.create("continuation-patient")
    enqueue(control, lifecycle, row)
    worker = CoordinationService(
        control=control, commands=Mock(spec=CommandService), coordinator=Mock(), clock=clock
    )
    failed_claims = []

    def fail(claim):
        failed_claims.append(claim)
        raise RuntimeError("The specialist is unavailable.")

    worker._resume = fail
    worker._recover = Mock(side_effect=RuntimeError("Recovery storage is unavailable."))
    with pytest.raises(RuntimeError, match="Recovery storage"):
        worker.run_pending()
    assert len(failed_claims) == 1
    expire_lease(control, failed_claims[0])
    assert lifecycle.pending() == (row.workflow_id,)
    resumed = lifecycle.claim(row.workflow_id)
    assert resumed is not None
    assert lifecycle.finish(resumed) is True


def test_timeout_after_lease_loss_cannot_record_a_booking_outcome(control, lifecycle, clock):
    row = control.create(
        "continuation-patient",
        payload={
            "booking_idempotency_key": "original-key",
            "booking_payload": {},
            "booking_authority": {"approval_id": APPROVAL},
            "selected_slot_version": 1,
        },
    )
    enqueue(control, lifecycle, row)
    claim = lifecycle.claim(row.workflow_id)
    assert claim is not None
    row = lifecycle.refresh(claim)
    commands = dispatching_commands(row)
    worker = CoordinationService(
        control=control, commands=commands, coordinator=Mock(), clock=clock
    )

    def timeout(*args, **kwargs):
        expire_lease(control, claim)
        raise TimeoutError("The booking request timed out after lease loss.")

    worker._call = timeout
    with pytest.raises(ContinuationReplaced):
        worker._book(claim, row, SimpleNamespace(scheduling=lambda: {}))
    commands.book.assert_not_called()


def _claimed_booking(control, lifecycle, **payload):
    row = control.create(
        "continuation-patient",
        payload={
            "booking_idempotency_key": "original-key",
            "booking_payload": {},
            "booking_authority": {"approval_id": APPROVAL},
            "booking_screening_text": "Please book it.",
            "selected_slot_version": 1,
            **payload,
        },
    )
    enqueue(control, lifecycle, row)
    claim = lifecycle.claim(row.workflow_id)
    assert claim is not None
    return claim, lifecycle.refresh(claim)


def test_a_halt_stops_the_first_send_before_the_hospital_is_asked(control, lifecycle, clock):
    """§16: the switch is checked where the external effect starts. Nothing is sent, and nothing is
    marked as sent, so a release later dispatches the booking instead of only looking it up."""
    claim, row = _claimed_booking(control, lifecycle)
    commands = Mock(spec=CommandService)
    commands.authorise_dispatch.side_effect = BookingHaltedError("halted")
    worker = CoordinationService(
        control=control, commands=commands, coordinator=Mock(), clock=clock
    )
    worker._call = Mock()

    with pytest.raises(BookingHaltedError):
        worker._book(claim, row, SimpleNamespace(scheduling=lambda: {}))

    worker._call.assert_not_called()
    commands.book.assert_not_called()
    assert "booking_sent" not in control.load(row.workflow_id).payload


def test_a_resend_after_a_crash_reconciles_and_records_while_halted(control, lifecycle, clock):
    """The other half: a booking already sent is looked up and recorded even with the switch on.
    Refusing that record would leave the hospital's appointment with no booking row."""
    claim, row = _claimed_booking(control, lifecycle, booking_sent=True)
    commands = Mock(spec=CommandService)
    commands.authorise_dispatch.side_effect = BookingHaltedError("halted")
    worker = CoordinationService(
        control=control, commands=commands, coordinator=Mock(), clock=clock
    )
    worker._call = Mock(return_value={"scheduling": {}})

    worker._book(claim, row, SimpleNamespace(scheduling=lambda: {}))

    commands.authorise_dispatch.assert_not_called()
    sent = worker._call.call_args.args[2]["scheduling"]
    assert sent["reconcile_only"] is True
    assert commands.book.call_args.kwargs["outcome"] == "unverified"


def test_a_ticket_for_another_approval_sends_nothing(control, lifecycle, clock):
    """The ticket is bound to the approval being sent, not a bare "the switch was off" flag."""
    claim, row = _claimed_booking(control, lifecycle)
    commands = Mock(spec=CommandService)
    commands.authorise_dispatch.return_value = DispatchTicket(
        workflow_id=row.workflow_id, approval_id="some-other-approval"
    )
    worker = CoordinationService(
        control=control, commands=commands, coordinator=Mock(), clock=clock
    )
    worker._call = Mock()

    with pytest.raises(CoordinationError, match="different approval"):
        worker._book(claim, row, SimpleNamespace(scheduling=lambda: {}))

    worker._call.assert_not_called()
    assert "booking_sent" not in control.load(row.workflow_id).payload
