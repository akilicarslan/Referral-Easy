"""What a caller sends is what the callee's schema accepts — tested, not assumed.

A task crosses a seam, and the seam has two halves: the request a caller *builds*, and the input
schema the callee *validates that request against*. Each half is well tested on its own — a sender's
tests assert the body it produces, a receiver's tests hand-build a body its own schema accepts — and
nothing tested them against **each other**. That is the shape of hole where both halves pass and the
system still does not work:

* The gateway's dispatch sends ``{"message", "command_id", "state"}`` to the care coordinator, whose
  ``CoordinatorInput`` requires ``patient_message`` and ``workflow_summary`` and forbids unknown
  keys. Every coordinator turn is refused at input validation and projects to ``terminal_error`` —
  which is what the live stack showed, every time, and what this file would have caught before it
  reached a running container.
* The coordinator's ``delegate_task`` sends a child ``{"task_envelope", "command_id"}`` carrying no
  specialist input at all, so every *delegated* turn is refused the same way, one hop further down.

The request under test is the one the real code path produces, captured from the real call — never
transcribed by hand, because a payload someone typed into a test is a second implementation that can
agree with the schema while the shipped one does not. It is then split exactly as the receiving
handler splits it (``_split_payload``) and validated against the receiving role's own schema
(``_validated_input``), which is the only reading that decides anything.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import httpx
import pytest
from agents_fixtures import COMMAND_ID, AllowAllPolicy, StubApi, body_for, envelope

from referraleasy_agent_runtime import AgentRole, BudgetCaps, TurnBudget
from referraleasy_agents.handler import _split_payload, _turn_prompt, _validated_input
from referraleasy_agents.tools.toolbox import ToolBox
from referraleasy_contracts import AgentTaskRequest
from referraleasy_gateway.dispatch import (
    DELEGATIONS_PER_TURN,
    MODEL_CALLS_PER_TURN,
    TOOL_CALLS_PER_TURN,
    CoordinatorDispatch,
)
from referraleasy_gateway.events import EventLog
from referraleasy_gateway.timing import SystemClock

pytestmark = pytest.mark.contract

#: Every role the coordinator may delegate to. It is excluded from its own list: `delegate_task`
#: refuses a self-call, so there is no request to validate.
SPECIALISTS = [role for role in AgentRole if role is not AgentRole.CARE_COORDINATOR]


def test_specialist_source_documents_stay_out_of_the_coordinator_model_prompt() -> None:
    payload = {
        "patient_message": "cardiology",
        "workflow_summary": "A recorded referral",
        "specialist_inputs": {"referral_intake": {"referral_source": "PRIVATE_SOURCE_DOCUMENT"}},
    }
    request = AgentTaskRequest(
        workflow_id=str(uuid4()),
        session_id="session",
        workflow_state="INTAKE",
        task="Coordinate the referral",
        idempotency_key="task-1",
        trace_id="trace-1",
        payload=payload,
    )
    prompt = _turn_prompt(AgentRole.CARE_COORDINATOR, request, payload)
    assert "PRIVATE_SOURCE_DOCUMENT" not in prompt
    assert "referral_intake" in prompt
    assert "available_specialist_inputs" in prompt


class _Recorder:
    """A `DelegationPort` that keeps the request it was asked to deliver.

    A recorder rather than a socket: the assertion is about the *document*, and a live peer would
    add a service, a token and a timeout to what is being proved.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(
        self, *, target_role: AgentRole, request: Mapping[str, Any], budget: Any
    ) -> Mapping[str, Any]:
        self.sent.append(dict(request))
        return {"state": "completed", "outcome": "ok", "is_success": True}


async def _the_coordinator_delegates_to(target: AgentRole) -> dict[str, Any]:
    """Run the real `delegate_task` and return the request it handed the port."""
    port = _Recorder()
    tools = ToolBox(
        role=AgentRole.CARE_COORDINATOR, api=StubApi(), policy=AllowAllPolicy(), delegation=port
    ).start(
        budget=TurnBudget(BudgetCaps(), role=AgentRole.CARE_COORDINATOR),
        envelope=envelope(workflow_state="INTAKE"),
        payload={"specialist_inputs": {target.value: body_for(target)}},
        workflow_state="INTAKE",
    )
    await tools.invoke("delegate_task", target_role=target.value, goal="decide what happens next")
    return port.sent[0]


#: A release id with the contract's shape. The value is not the claim — that the envelope carries
#: *a* release is — so this is not tied to whichever manifest `make eval` last built.
RELEASE_ID = "rel-20261016-8e00275"


class _GatewayTransport:
    """An httpx-shaped client that records the gateway's POST and answers like the agent route."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        self.calls.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(
            200,
            json={"result": {"outcome": "completed", "a2a_state": "completed"}},
            request=httpx.Request("POST", url),
        )


def _await_post(transport: _GatewayTransport, timeout: float = 3.0) -> dict[str, Any]:
    """The dispatch is queued to a worker thread; wait for the POST it makes."""
    deadline = time.monotonic() + timeout
    while not transport.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert transport.calls, "the dispatcher never sent the task"
    return transport.calls[0]


def test_the_gateway_s_task_validates_against_the_coordinator_s_own_input_schema() -> None:
    """Hop 1, driven through the real `CoordinatorDispatch` and read by the real schema.

    This is the check that would have caught the defect the live stack showed: the dispatch used to
    send `{"message", "command_id", "state"}`, and `CoordinatorInput` refuses that document at
    validation, before the model is called. It is also the check that keeps the envelope in the
    request — without one, `delegate_task` has no workflow, command, state version or release to
    derive a child task from.
    """
    transport = _GatewayTransport()
    dispatcher = CoordinatorDispatch(
        base_url="http://agent-{role}:8080",
        token="service-token",
        release_manifest_id=RELEASE_ID,
        log=EventLog(synthetic=True),
        clock=SystemClock(),
        client=transport,  # type: ignore[arg-type]
    )

    assert dispatcher.submit(
        workflow_id=uuid4(),
        workflow_state="INTAKE",
        state_version=3,
        command_id=COMMAND_ID,
        task="The patient sent this message while the workflow was in INTAKE: 'a cardiologist'.",
        patient_message="a cardiologist",
        workflow_summary="Referral ticket T-1 is in state INTAKE at version 3. The patient has "
        "sent 1 message(s) in this workflow and recorded 0 answer(s).",
        synthetic=True,
    )

    request = _await_post(transport)
    body, parsed, command_id = _split_payload(request["json"]["payload"])
    assert parsed is not None, "a root task must carry its envelope"
    assert command_id == COMMAND_ID

    validated = _validated_input(AgentRole.CARE_COORDINATOR, body)
    refused = getattr(validated, "detail", None)
    assert refused is None, (
        f"the care coordinator refuses the task the gateway sends it: {refused}. Its input schema "
        "requires `patient_message` and `workflow_summary` and forbids every other key."
    )


def test_the_envelope_s_budget_mirrors_the_runtime_s_defaults() -> None:
    """The root caller declares §8's counters; the runtime must read the same numbers.

    They are mirrored rather than imported because the gateway must not depend on the agent
    assembly (`dispatch.py` says why). A mirror that drifts would hand a delegation chain a budget
    the runtime's own ceilings do not agree with, so the equality is a test rather than a comment.
    """
    from referraleasy_agent_runtime.budget import (
        DEFAULT_MAX_DELEGATIONS_PER_TURN,
        DEFAULT_MAX_MODEL_CALLS_PER_TURN,
        DEFAULT_MAX_TOOL_CALLS_PER_TURN,
    )

    assert MODEL_CALLS_PER_TURN == DEFAULT_MAX_MODEL_CALLS_PER_TURN
    assert TOOL_CALLS_PER_TURN == DEFAULT_MAX_TOOL_CALLS_PER_TURN
    assert DELEGATIONS_PER_TURN == DEFAULT_MAX_DELEGATIONS_PER_TURN


@pytest.mark.asyncio
@pytest.mark.parametrize("target", SPECIALISTS, ids=lambda role: role.value)
async def test_a_delegated_task_validates_against_the_target_s_own_input_schema(
    target: AgentRole,
) -> None:
    """The receiving handler validates its input before the turn starts — so this must pass."""
    request = await _the_coordinator_delegates_to(target)

    body, parsed, command_id = _split_payload(request["payload"])
    assert parsed is not None, "a delegation must carry the child's envelope"
    assert command_id == str(request["payload"]["command_id"])

    validated = _validated_input(target, body)
    refused = getattr(validated, "detail", None)
    assert refused is None, (
        f"{target.value} refuses the task the coordinator delegates to it: {refused}. The "
        "delegation carries the envelope and the goal and no input document, and the receiving "
        "handler validates its input before the turn begins — so this is a refusal, not a "
        "degradation."
    )
