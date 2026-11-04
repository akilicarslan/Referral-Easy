"""WS4 S1 (ADR-0046): a refused dispatch sends nothing to the hospital. Pinned before it is fixed.

Release blocker B2: the coordinator asks the hospital to book after a check that looks only at the
kill switch and at whether an `approval_id` is present (`CommandService.authorise_dispatch`). The
authoritative approval check runs in `book()`, *after* the appointment exists. These tests run the
real `CoordinationService._book` against a real `CommandService` and `ApprovalService`, with the
scheduling call — the only path to `POST /appointments` — replaced by a counter.

The halt case passes today. Every other case is `xfail(strict=True)`: it fails now, and WS4 S3
(reserve-then-dispatch) must turn it green — at which point `strict` makes the stale marker itself a
failure, so the fix cannot land without the marker being removed. Each one was checked with
`--runxfail` to fail on the send itself (or, for revocation, on the missing ledger operation), not
on the fixture.

The plan's third case, "an approval that expires between the send and the record does not orphan
the appointment", needs S3's dispatch claim to state its precondition, so it lands with S3.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from referraleasy_domain import ApprovalRefusedError, canonical_request_hash
from referraleasy_gateway.approval import ApprovalService, InMemoryApprovalLedger
from referraleasy_gateway.commands import CommandService
from referraleasy_gateway.continuations import ContinuationKind, ContinuationLifecycle
from referraleasy_gateway.coordination import CoordinationService, WorkflowInputs
from referraleasy_gateway.errors import BookingHaltedError
from referraleasy_gateway.killswitch import KillSwitch

#: A dispatch refusal is one of these. Anything else — including no exception — is a send.
REFUSALS = (ApprovalRefusedError, BookingHaltedError)

PAYLOAD: dict[str, Any] = {
    "patient_id": "dispatch-patient",
    "hospital_id": "hospital-1",
    "provider_id": "provider-1",
    "slot_id": "slot-1",
}

#: Short enough that an approval can expire while the continuation's lease (minutes) is still held,
#: so an "expired" test fails on the approval and not on the work queue.
APPROVAL_TTL_SECONDS = 10

PENDING_S3 = pytest.mark.xfail(
    strict=True,
    reason="B2: authorise_dispatch checks only the halt and the presence of an approval_id; "
    "WS4 S3 (reserve_dispatch) closes it",
)


@pytest.fixture
def lifecycle(control, clock):
    return ContinuationLifecycle(control, clock=clock)


def _enqueue(control, lifecycle, row) -> None:
    command_id = str(uuid4())
    digest = canonical_request_hash({"command_id": command_id})
    control.begin_command(command_id, digest)
    control.update_payload(
        row.workflow_id,
        expected_state_version=row.state_version,
        patch=lifecycle.pending_patch(command_id, ContinuationKind.ANSWER),
    )
    control.complete_command(command_id, digest, response_status=202, response={"accepted": True})


class _Dispatch:
    """The world `_book` runs in, with the hospital call counted instead of made."""

    def __init__(self, control, lifecycle, clock) -> None:
        self.control = control
        self.clock = clock
        self.ledger = InMemoryApprovalLedger()
        self.approvals = ApprovalService(self.ledger, clock=clock, ttl_seconds=APPROVAL_TTL_SECONDS)
        self.switch = KillSwitch()
        self.commands = CommandService(
            control=control,
            surfaces=Mock(),
            log=Mock(),
            approvals=self.approvals,
            policy=Mock(),
            guardrails=Mock(),
            clock=clock,
            latencies=Mock(),
            kill_switch=self.switch,
        )
        # Recording is downstream of the send and not what these tests are about.
        self.commands.book = Mock()  # type: ignore[method-assign]
        self.worker = CoordinationService(
            control=control, commands=self.commands, coordinator=Mock(), clock=clock
        )
        # The only route to `POST /appointments` is the scheduling agent, reached through `_call`.
        self.hospital = Mock(return_value={"scheduling": {}})
        self.worker._call = self.hospital  # type: ignore[method-assign]
        self.lifecycle = lifecycle

    def approved_workflow(self, *, authority: dict[str, Any] | None = None, **payload: Any):
        row = self.control.create(PAYLOAD["patient_id"], payload={})
        binding = self.approvals.bind(
            workflow_id=row.workflow_id,
            patient_ref=row.patient_ref,
            state_version=row.state_version,
            payload=PAYLOAD,
        )
        row = self.control.update_payload(
            row.workflow_id,
            expected_state_version=row.state_version,
            patch={
                "booking_idempotency_key": "dispatch-key",
                "booking_payload": dict(PAYLOAD),
                "booking_authority": {
                    "approval_id": str(binding.approval_id),
                    "state_version": binding.state_version,
                    **(authority or {}),
                },
                "booking_screening_text": "Please book it.",
                "selected_slot_version": 1,
                **payload,
            },
        )
        _enqueue(self.control, self.lifecycle, row)
        claim = self.lifecycle.claim(row.workflow_id)
        assert claim is not None
        return binding, claim, self.lifecycle.refresh(claim)

    def book(self, claim, row) -> None:
        self.worker._book(claim, row, cast(WorkflowInputs, SimpleNamespace(scheduling=lambda: {})))

    def assert_nothing_sent(self, row) -> None:
        assert self.hospital.call_count == 0, "the hospital was asked to book"
        assert "booking_sent" not in self.control.load(row.workflow_id).payload


def _revoke(dispatch: _Dispatch, approval_id: UUID) -> None:
    """Revocation is introduced by WS4 S2; until then there is no way to express it."""
    dispatch.ledger.revoke(approval_id, by="patient", at=dispatch.clock.now())  # type: ignore[attr-defined]


@pytest.fixture
def dispatch(control, lifecycle, clock) -> _Dispatch:
    return _Dispatch(control, lifecycle, clock)


def test_a_halted_dispatch_sends_nothing(dispatch: _Dispatch) -> None:
    """The one refusal that already happens before the send (§16)."""
    _, claim, row = dispatch.approved_workflow()
    dispatch.switch.engage(reason="incident", by="oncall", now=dispatch.clock.now())

    with pytest.raises(BookingHaltedError):
        dispatch.book(claim, row)

    dispatch.assert_nothing_sent(row)


@PENDING_S3
def test_an_expired_approval_sends_nothing(dispatch: _Dispatch) -> None:
    binding, claim, row = dispatch.approved_workflow()
    dispatch.clock.advance_ms(
        (binding.expires_at - dispatch.clock.now()).total_seconds() * 1000 + 1
    )

    with pytest.raises(REFUSALS):
        dispatch.book(claim, row)

    dispatch.assert_nothing_sent(row)


@PENDING_S3
def test_a_consumed_approval_sends_nothing(dispatch: _Dispatch) -> None:
    binding, claim, row = dispatch.approved_workflow()
    dispatch.approvals.consume(binding.approval_id)

    with pytest.raises(REFUSALS):
        dispatch.book(claim, row)

    dispatch.assert_nothing_sent(row)


@PENDING_S3
def test_a_revoked_approval_sends_nothing(dispatch: _Dispatch) -> None:
    binding, claim, row = dispatch.approved_workflow()
    _revoke(dispatch, binding.approval_id)

    with pytest.raises(REFUSALS):
        dispatch.book(claim, row)

    dispatch.assert_nothing_sent(row)


@PENDING_S3
def test_a_payload_changed_after_consent_sends_nothing(dispatch: _Dispatch) -> None:
    """Hash drift: the patient approved one slot, the workflow now holds another."""
    _, claim, row = dispatch.approved_workflow(booking_payload={**PAYLOAD, "slot_id": "slot-2"})

    with pytest.raises(REFUSALS):
        dispatch.book(claim, row)

    dispatch.assert_nothing_sent(row)


@PENDING_S3
def test_an_authority_naming_another_consent_version_sends_nothing(dispatch: _Dispatch) -> None:
    """Version drift: the stored snapshot is not authority; the binding's own version is."""
    binding, claim, row = dispatch.approved_workflow(authority={"state_version": 10_000})
    assert binding.state_version != 10_000

    with pytest.raises(REFUSALS):
        dispatch.book(claim, row)

    dispatch.assert_nothing_sent(row)
