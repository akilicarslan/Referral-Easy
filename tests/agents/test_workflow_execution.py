"""Standard plans use governed tools and run only independent work in parallel."""

import asyncio

import pytest
from agents_fixtures import AllowAllPolicy, StubApi, body_for, envelope

from referraleasy_agent_runtime import AgentRole, BudgetCaps, TurnBudget
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.tools.toolbox import ToolBox
from referraleasy_agents.workflow import execute_workflow


class BarrierDelegation:
    def __init__(self):
        self.entered = set()
        self.both = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, *, target_role, request, budget):
        self.entered.add(target_role)
        if len(self.entered) == 2:
            self.both.set()
        await self.release.wait()
        return {"state": "completed", "outcome": "completed", "is_success": True, "output": {}}


@pytest.mark.asyncio
async def test_plan_coverage_and_matching_start_before_either_finishes():
    roles = [AgentRole.COVERAGE_NETWORK, AgentRole.PROVIDER_MATCHING]
    data = {
        "patient_message": "",
        "workflow_summary": "A recorded referral.",
        "specialist_inputs": {r.value: body_for(r) for r in roles},
        "execution_plan": [
            {"target": r.value, "step": r.value, "goal": "Check source data.", "independent": True}
            for r in roles
        ],
    }
    port = BarrierDelegation()
    tools = ToolBox(
        role=AgentRole.CARE_COORDINATOR, api=StubApi(), policy=AllowAllPolicy(), delegation=port
    ).start(
        budget=TurnBudget(BudgetCaps()),
        envelope=envelope(workflow_state="PROVIDER_MATCHING"),
        payload=data,
    )
    task = asyncio.create_task(execute_workflow(tools, AgentRole.CARE_COORDINATOR, data))
    try:
        await asyncio.wait_for(port.both.wait(), timeout=1)
        assert port.entered == set(roles)
    finally:
        port.release.set()
    result = await task
    assert set(result.specialist_results) == {r.value for r in roles}


@pytest.mark.asyncio
async def test_dependent_steps_cannot_claim_independence():
    roles = [AgentRole.REFERRAL_READINESS, AgentRole.SCHEDULING]
    data = {
        "specialist_inputs": {r.value: body_for(r) for r in roles},
        "execution_plan": [
            {"target": r.value, "step": r.value, "goal": "Check data.", "independent": True}
            for r in roles
        ],
    }
    tools = ToolBox(role=AgentRole.CARE_COORDINATOR, api=StubApi(), policy=AllowAllPolicy()).start(
        budget=TurnBudget(BudgetCaps()), envelope=envelope(), payload=data
    )
    with pytest.raises(UnknownDataError, match="not independent"):
        await execute_workflow(tools, AgentRole.CARE_COORDINATOR, data)
    assert tools.trace_entries() == []
