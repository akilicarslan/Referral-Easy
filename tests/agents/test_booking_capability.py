"""WS4 S1 (ADR-0046): the scheduling agent writes only with a dispatch capability. Pinned before it is fixed.

Today the agent decides to write from two things it was handed: a self-describing approval snapshot
it validates without asking anyone (`check_approval`), and a caller-supplied `reconcile_only` flag
(`workflow.py`). Neither is authority. WS4 S5 makes the write require a signed, single-use dispatch
capability minted by the gateway after `reserve_dispatch`; without one, the only thing the agent may
do is look the request up.

Both tests are `xfail(strict=True)` and were checked with `--runxfail` to fail on the POST itself.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

import pytest
from agents_fixtures import (
    HOSPITAL_ID,
    PATIENT_ID,
    PROVIDER_ID,
    SLOT_ID,
    AllowAllPolicy,
    StubApi,
    envelope,
    toolset_for,
)

from referraleasy_agent_runtime import AgentRole
from referraleasy_agents.canonical import payload_hash
from referraleasy_agents.errors import NoApprovalRefused
from referraleasy_agents.workflow import execute_workflow
from referraleasy_contracts.generated.booking import BookingPayload

KEY = "booking-key-0001"
STATE = "BOOKING_IN_FLIGHT"
PAYLOAD: Mapping[str, Any] = {
    "patient_id": PATIENT_ID,
    "provider_id": PROVIDER_ID,
    "hospital_id": HOSPITAL_ID,
    "slot_id": SLOT_ID,
    "visit_type": "first_visit",
}

PENDING_S5 = pytest.mark.xfail(
    strict=True,
    reason="B2: create_booking writes on a self-validated approval snapshot and the caller's "
    "reconcile_only flag; WS4 S5 requires a gateway-minted dispatch capability",
)


def _snapshot() -> dict[str, Any]:
    """A snapshot that passes `check_approval` today: live, matching, granted. No capability."""
    canonical = BookingPayload.model_validate(dict(PAYLOAD)).model_dump(
        mode="json", exclude_none=True
    )
    return {
        "approval_id": "0f0f0f0f-0000-4000-8000-0000000000a1",
        "payload_hash": payload_hash(canonical),
        "state_version": 6,
        "expires_at": "2030-01-01T00:00:00+00:00",
        "granted_by": "patient",
    }


class _CountingHospital(StubApi):
    """Answers reads; records every POST and refuses it, so nothing downstream runs on a write."""

    def __init__(self) -> None:
        super().__init__(
            {"/appointments/lookup_request": {"booking_request": None, "outcome": "not_booked"}}
        )
        self.posts: list[str] = []

    async def post(self, path: str, payload: Mapping[str, Any], **_: Any) -> dict[str, Any]:
        del payload
        self.posts.append(path)
        raise ConnectionRefusedError("the counting hospital accepts no writes")


def _scheduling_tools(api: _CountingHospital, payload: Mapping[str, Any]):
    _, tools = toolset_for(
        AgentRole.SCHEDULING,
        api=api,
        policy=AllowAllPolicy(),
        workflow_state=STATE,
        envelope=envelope(workflow_state=STATE),
        payload={"idempotency_key": KEY, **payload},
    )
    return tools


@PENDING_S5
@pytest.mark.asyncio
async def test_create_booking_without_capability_never_posts() -> None:
    api = _CountingHospital()
    tools = _scheduling_tools(api, {"approval": _snapshot()})

    failure: BaseException | None = None
    try:
        await tools.invoke("create_booking", idempotency_key=KEY, payload=dict(PAYLOAD))
    except Exception as error:
        failure = error

    assert api.posts == [], "the hospital was asked to book without a dispatch capability"
    assert isinstance(failure, NoApprovalRefused)


@PENDING_S5
@pytest.mark.asyncio
async def test_reconcile_only_false_cannot_force_a_write() -> None:
    """Attack: an injected `reconcile_only: false` with no capability is a lookup, not a write."""
    api = _CountingHospital()
    data = {
        "idempotency_key": KEY,
        "booking_payload": dict(PAYLOAD),
        "approval": _snapshot(),
        "reconcile_only": False,
    }
    tools = _scheduling_tools(api, data)

    # The shape of the answer is S5's concern; the POST count is this test's.
    with contextlib.suppress(Exception):
        await execute_workflow(tools, AgentRole.SCHEDULING, data)

    assert api.posts == [], "a caller-supplied flag turned a lookup into a hospital write"
