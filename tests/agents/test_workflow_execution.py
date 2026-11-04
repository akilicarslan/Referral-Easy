"""Standard plans use governed tools and run only independent work in parallel."""

import asyncio

import pytest
from agents_fixtures import EVIDENCE_ID, AllowAllPolicy, StubApi, body_for, envelope

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


# ---------------------------------------------------------------------------
# WS3 S7: the workflow path has no retry loop of its own
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000
        self.slept: list[int] = []

    def monotonic_ms(self) -> int:
        return self.now

    async def sleep_ms(self, milliseconds: int) -> None:
        self.slept.append(milliseconds)
        self.now += milliseconds


class _FlakyApi(StubApi):
    def __init__(self, responses, failures):
        super().__init__(responses)
        self.failures = list(failures)

    async def get(self, path, **kwargs):
        if self.failures:
            self.calls.append(("GET", path, {}))
            raise self.failures.pop(0)
        return await super().get(path, **kwargs)


_DIRECTORY = {
    "/directory/search": {
        "candidates": [],
        "result_kind": "candidates_found",
        "evidence_id": EVIDENCE_ID,
    }
}


def _matching_tools(api, clock):
    return ToolBox(role=AgentRole.PROVIDER_MATCHING, api=api, policy=AllowAllPolicy()).start(
        budget=TurnBudget(BudgetCaps(), clock=clock, role=AgentRole.PROVIDER_MATCHING),
        envelope=envelope(workflow_state="PROVIDER_MATCHING"),
        workflow_state="PROVIDER_MATCHING",
    )


@pytest.mark.asyncio
async def test_no_private_backoff_loop_retry_after_is_honoured():
    """The wait is the upstream's `Retry-After`, not a private `150 * 2**n` curve."""
    from referraleasy_agent_runtime import UpstreamTransient
    from referraleasy_agents.workflow import _invoke

    clock = _Clock()
    api = _FlakyApi(_DIRECTORY, [UpstreamTransient("busy", status_code=503, retry_after_s=2.0)])
    tools = _matching_tools(api, clock)

    answer = await _invoke(tools, "search_directory", query="cardiologists")

    assert answer["result_kind"] == "candidates_found"
    assert clock.slept == [2000]
    assert len(api.calls) == 2


@pytest.mark.asyncio
async def test_workflow_retries_stop_at_the_shared_attempt_cap():
    from referraleasy_agent_runtime import BudgetExhausted, CapKind, UpstreamTransient
    from referraleasy_agents.workflow import _invoke

    caps = BudgetCaps()
    clock = _Clock()
    api = _FlakyApi(
        _DIRECTORY,
        [UpstreamTransient("busy", status_code=503) for _ in range(caps.max_attempts + 2)],
    )
    tools = _matching_tools(api, clock)

    with pytest.raises(BudgetExhausted) as caught:
        await _invoke(tools, "search_directory", query="cardiologists")

    assert caught.value.cap is CapKind.ATTEMPTS
    assert len(api.calls) == caps.max_attempts


@pytest.mark.asyncio
async def test_delegation_is_never_retried_by_the_workflow():
    """A delegation is the child's turn, with its own retries; repeating it multiplies the tree."""
    from referraleasy_agent_runtime import UpstreamTransient
    from referraleasy_agents.workflow import _invoke

    class FlakyDelegation:
        sends = 0

        async def send(self, *, target_role, request, budget):
            FlakyDelegation.sends += 1
            raise UpstreamTransient("peer unavailable", status_code=503)

    roles = [AgentRole.COVERAGE_NETWORK]
    data = {"specialist_inputs": {r.value: body_for(r) for r in roles}}
    tools = ToolBox(
        role=AgentRole.CARE_COORDINATOR,
        api=StubApi(),
        policy=AllowAllPolicy(),
        delegation=FlakyDelegation(),
    ).start(
        budget=TurnBudget(BudgetCaps(), clock=_Clock()),
        envelope=envelope(workflow_state="PROVIDER_MATCHING"),
        payload=data,
    )

    with pytest.raises(UpstreamTransient):
        await _invoke(tools, "delegate_task", target_role="coverage_network", goal="Check cover.")

    assert FlakyDelegation.sends == 1
