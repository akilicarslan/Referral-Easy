"""Whose records a call is about is the service's fact, never the model's choice.

Two boundaries, both from §5/§6, and both made structural rather than requested:

* **Identity is read, not accepted.** `require_workflow_id`, `require_command_id`
  and `require_patient_id` are the only way a tool learns which workflow, which
  command or which patient it is acting for. None of them is a tool parameter
  (`tools/specs.py` declares no such name and `test_tool_surface.py` asserts it),
  so the failure this file guards against is a *future edit* reintroducing one.
  Each function is tested for both answers: the fact when the service supplied it,
  and a refusal when it did not — default-deny, because a booking written into an
  unknown workflow is worse than a turn that failed.

* **A delegation derives its child's identity; it never dictates it.**
  `delegate_task` builds the child's §5 envelope from the parent's own: same
  workflow, same command, the same state version, the same deadline, the same
  release, the parent's task id as `parent_task_id`. The model chooses the target
  and the goal and nothing else. The child's task id and idempotency key are
  deterministic, so a re-sent delegation is the same task.

The last block pins two type traps the type checker found and this file keeps from
returning: every contract id is a pydantic `RootModel` (so `int(StateVersion(...))`
is a `TypeError` and the envelope must be read with `.root`/`id_text`), and a
coordinator with no delegation transport must fail *loudly* rather than report a
delegation it never made.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from agents_fixtures import (
    COMMAND_ID,
    CONSTRAINT_ID,
    EVIDENCE_ID,
    PATIENT_ID,
    RELEASE_MANIFEST_ID,
    TASK_ID,
    WORKFLOW_ID,
    AllowAllPolicy,
    StubApi,
    body_for,
    constraint,
    envelope,
)

from referraleasy_agent_runtime import (
    AgentRole,
    BudgetCaps,
    BudgetExhausted,
    TurnBudget,
    UnknownRoleError,
)
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.tools.call import (
    ToolCall,
    require_command_id,
    require_patient_id,
    require_workflow_id,
)
from referraleasy_agents.tools.toolbox import ToolBox
from referraleasy_contracts.generated.common import StateVersion

pytestmark = pytest.mark.contract


class RecordingPort:
    """A `DelegationPort` that records what it was asked to deliver and answers."""

    def __init__(self, answer: Mapping[str, Any] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.budgets: list[Any] = []
        self._answer = (
            answer
            if answer is not None
            else {"state": "completed", "outcome": "ok", "is_success": True}
        )

    async def send(
        self, *, target_role: AgentRole, request: Mapping[str, Any], budget: Any
    ) -> Mapping[str, Any]:
        self.sent.append({"target_role": target_role, "request": dict(request)})
        self.budgets.append(budget)
        return self._answer


def coordinator_toolset(
    *,
    port: Any = None,
    budget: TurnBudget | None = None,
    envelope_: Any = None,
    payload: Mapping[str, Any] | None = None,
    role: AgentRole = AgentRole.CARE_COORDINATOR,
) -> Any:
    """A started `ToolSet` for the coordinator, with the port this test supplies."""
    box = ToolBox(role=role, api=StubApi(), policy=AllowAllPolicy(), delegation=port)
    return box.start(
        budget=budget or TurnBudget(BudgetCaps(), role=role),
        envelope=envelope_,
        payload=payload,
        workflow_state="INTAKE",
    )


async def delegate(
    *,
    port: Any,
    target_role: str = AgentRole.PROVIDER_MATCHING.value,
    goal: str = "find candidates for this referral",
    envelope_: Any = None,
    constraint_ids: list[str] | None = None,
    evidence_ids: list[str] | None = None,
    call_budget: TurnBudget | None = None,
) -> dict[str, Any]:
    tools = coordinator_toolset(
        port=port,
        budget=call_budget,
        envelope_=envelope_ if envelope_ is not None else envelope(workflow_state="INTAKE"),
        payload={
            "specialist_inputs": {
                role.value: body_for(role)
                for role in AgentRole
                if role is not AgentRole.CARE_COORDINATOR
            }
        },
    )
    result: dict[str, Any] = await tools.invoke(
        "delegate_task",
        target_role=target_role,
        goal=goal,
        constraint_ids=constraint_ids,
        evidence_ids=evidence_ids,
    )
    return result


# ── identity is read from the task, never from the model ─────────────────────────────────────


def _call(*, envelope_: Any = None, payload: Mapping[str, Any] | None = None) -> ToolCall:
    return ToolCall(
        role=AgentRole.SCHEDULING,
        tool="create_booking",
        workflow_state="BOOKING_IN_FLIGHT",
        api=StubApi(),
        budget=TurnBudget(BudgetCaps(), role=AgentRole.SCHEDULING),
        lease=Any,  # type: ignore[arg-type]  # `require_*` reads the identity, not the lease
        envelope=envelope_,
        payload=dict(payload or {}),
    )


def test_the_workflow_comes_from_the_envelope() -> None:
    assert require_workflow_id(_call(envelope_=envelope())) == WORKFLOW_ID


def test_a_call_with_no_envelope_has_no_workflow_to_be_attributed_to() -> None:
    with pytest.raises(UnknownDataError) as raised:
        require_workflow_id(_call())
    assert "no task envelope" in str(raised.value)


def test_the_command_comes_from_the_envelope() -> None:
    assert require_command_id(_call(envelope_=envelope())) == COMMAND_ID


def test_a_call_with_no_envelope_carries_no_command_id() -> None:
    with pytest.raises(UnknownDataError):
        require_command_id(_call())


def test_the_patient_comes_from_the_turn_s_own_input_first() -> None:
    """§6's two service-supplied sources, in order."""
    call = _call(
        envelope_=envelope(patient_details_id=PATIENT_ID), payload={"patient_id": PATIENT_ID}
    )
    assert require_patient_id(call) == PATIENT_ID


def test_the_patient_falls_back_to_the_envelope_s_indirection() -> None:
    call = _call(envelope_=envelope(patient_details_id=PATIENT_ID))
    assert require_patient_id(call) == PATIENT_ID


def test_a_call_with_no_patient_anywhere_is_refused_rather_than_invented() -> None:
    with pytest.raises(UnknownDataError) as raised:
        require_patient_id(_call(envelope_=envelope()))
    assert "no patient" in str(raised.value)


def test_the_state_version_is_read_through_root_and_is_an_int() -> None:
    """`StateVersion` is a `RootModel`: `.root` is the int, `int(...)` is a `TypeError`."""
    call = _call(envelope_=envelope(current_state_version=7))
    assert call.current_state_version == 7
    assert isinstance(call.current_state_version, int)
    with pytest.raises(TypeError):
        # the trap is the point of the line: a `RootModel` is not an `int`
        int(StateVersion(7))  # type: ignore[call-overload]


# ── the child's envelope is derived ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_child_envelope_carries_the_parent_s_identity_field_for_field() -> None:
    port = RecordingPort()
    result = await delegate(port=port)
    request = port.sent[0]["request"]
    child = request["payload"]["task_envelope"]

    assert {"task_envelope", "command_id"} <= request["payload"].keys()
    assert request["payload"]["specialty"] == "cardiology"
    assert "patient_id" not in request["payload"], "Matching receives only its own input document"
    assert request["payload"]["command_id"] == COMMAND_ID
    assert child["workflow_id"] == WORKFLOW_ID
    assert child["command_id"] == COMMAND_ID
    assert child["parent_task_id"] == TASK_ID
    assert child["current_state_version"] == 7
    assert child["release_manifest_id"] == RELEASE_MANIFEST_ID
    assert child["workflow_state"] == "INTAKE"
    parent = envelope(workflow_state="INTAKE")
    assert child["deadline"] == parent.model_dump(mode="json")["deadline"]
    assert child["call_budget"] == parent.model_dump(mode="json")["call_budget"]
    assert child["goal"] == "find candidates for this referral"
    assert result["target_role"] == "provider_matching"
    assert result["task_id"] == child["task_id"]
    assert result["outcome"] == "ok" and result["is_success"] is True


@pytest.mark.asyncio
async def test_a_resent_delegation_is_the_same_task_not_a_second_one() -> None:
    first, second = RecordingPort(), RecordingPort()
    await delegate(port=first)
    await delegate(port=second)
    assert (
        first.sent[0]["request"]["idempotency_key"] == second.sent[0]["request"]["idempotency_key"]
    )
    assert (
        first.sent[0]["request"]["payload"]["task_envelope"]["task_id"]
        == second.sent[0]["request"]["payload"]["task_envelope"]["task_id"]
    )


@pytest.mark.asyncio
async def test_a_different_goal_is_a_different_task() -> None:
    first, second = RecordingPort(), RecordingPort()
    await delegate(port=first, goal="find candidates")
    await delegate(port=second, goal="check coverage")
    assert (
        first.sent[0]["request"]["idempotency_key"] != second.sent[0]["request"]["idempotency_key"]
    )


@pytest.mark.asyncio
async def test_a_delegation_narrows_constraints_and_evidence_and_cannot_add_them() -> None:
    port = RecordingPort()
    envelope_ = envelope(
        workflow_state="INTAKE",
        constraints=[constraint()],
        evidence_refs=[EVIDENCE_ID],
    )
    await delegate(
        port=port, envelope_=envelope_, constraint_ids=[CONSTRAINT_ID], evidence_ids=[EVIDENCE_ID]
    )
    child = port.sent[0]["request"]["payload"]["task_envelope"]
    assert [item["constraint_id"] for item in child["constraints"]] == [CONSTRAINT_ID]
    assert child["evidence_refs"] == [{"evidence_id": EVIDENCE_ID}]

    with pytest.raises(UnknownDataError) as raised:
        await delegate(
            port=RecordingPort(),
            envelope_=envelope_,
            constraint_ids=["0f0f0f0f-0000-4000-8000-0000000000bb"],
        )
    assert "does not carry" in str(raised.value)

    with pytest.raises(UnknownDataError) as raised:
        await delegate(
            port=RecordingPort(),
            envelope_=envelope_,
            evidence_ids=["0f0f0f0f-0000-4000-8000-0000000000cc"],
        )
    assert "does not reference" in str(raised.value)


@pytest.mark.asyncio
async def test_a_delegation_spends_the_turn_s_own_delegation_budget() -> None:
    port = RecordingPort()
    await delegate(port=port)
    assert port.budgets[0].snapshot().delegations == 1

    with pytest.raises(BudgetExhausted):
        await delegate(
            port=RecordingPort(),
            call_budget=TurnBudget(BudgetCaps(max_delegations=0), role=AgentRole.CARE_COORDINATOR),
        )


@pytest.mark.asyncio
async def test_delegation_refuses_what_it_cannot_do() -> None:
    with pytest.raises(UnknownDataError) as raised:
        await delegate(port=None)
    assert "no delegation transport" in str(raised.value)

    with pytest.raises(UnknownRoleError):
        await delegate(port=RecordingPort(), target_role="not_a_role")

    with pytest.raises(UnknownDataError) as raised:
        await delegate(port=RecordingPort(), target_role=AgentRole.CARE_COORDINATOR.value)
    assert "cannot delegate to itself" in str(raised.value)

    with pytest.raises(UnknownDataError):
        await delegate(port=RecordingPort(), goal="   ")


@pytest.mark.asyncio
async def test_an_answer_that_is_not_a_protocol_result_is_a_failure() -> None:
    port = RecordingPort(answer={"message": "I found some providers"})
    with pytest.raises(UnknownDataError) as raised:
        await delegate(port=port)
    assert "is_success" in str(raised.value) and "outcome" in str(raised.value)
