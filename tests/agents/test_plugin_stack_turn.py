"""WS3 S1 (ADR-0045): the plugin stack on a real `Runner`, with Provider Matching's sub-agents.

Two facts the plugin design rests on, proven through the real path rather than assumed from ADK's
source: the sub-agents behind `AgentTool` run under the *same* plugin instances as the parent, and a
typed refusal raised by a plugin leaves the turn as that refusal, not as ADK's `RuntimeError`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

import pytest
from agents_fixtures import EVIDENCE_ID, AllowAllPolicy, StubApi, handler_for, run_turn
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest

from referraleasy_agent_runtime import AgentRole, BudgetCaps, TurnBudget
from referraleasy_agent_runtime.budget import BudgetExhausted, CapKind
from referraleasy_agent_runtime.plugins import GuardPlugin, TurnScope, build_plugin_stack
from referraleasy_agents import handler as handler_module
from referraleasy_agents import subagents
from referraleasy_agents.assembly import adk_node_name
from referraleasy_agents.offline_model import TextStep, ToolCallStep, scripted_model
from referraleasy_contracts.generated.common import ResultOutcome


class _SpyPlugin(GuardPlugin):
    slot = "provenance"
    instances: ClassVar[list[_SpyPlugin]] = []

    def __init__(self, scope: TurnScope) -> None:
        super().__init__(scope)
        self.model_calls: list[str] = []
        _SpyPlugin.instances.append(self)

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        self.model_calls.append(callback_context.agent_name)


class _RefusingBudgetPlugin(GuardPlugin):
    slot = "budget"

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        raise BudgetExhausted(CapKind.MODEL_CALLS, limit=0, used=0, site="spend reservation")


def _with_plugins(monkeypatch: pytest.MonkeyPatch, *classes: type[GuardPlugin]) -> None:
    def stack(scope: TurnScope, plugins: Any = ()) -> Any:
        return build_plugin_stack(scope, [cls(scope) for cls in classes])

    monkeypatch.setattr(handler_module, "build_plugin_stack", stack)


_PARENT_ANSWER: dict[str, Any] = {
    "candidates": [],
    "alternatives": [],
    "constraint_change": None,
    "mandatory_conditions": {
        "specialty_identified": True,
        "approval_present": False,
        "slot_valid": False,
    },
    "decision": {
        "action": "provider_matching:find_candidates",
        "reason_code": "no_candidate_matched",
        "evidence_ids": [EVIDENCE_ID],
        "missing_fields": [],
        "next_allowed_actions": ["provider_matching:find_candidates"],
    },
}


@pytest.mark.asyncio
async def test_agent_tool_inherits_same_plugin_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    _SpyPlugin.instances.clear()
    _with_plugins(monkeypatch, _SpyPlugin)
    api = StubApi(
        {
            "/directory/search": {
                "candidates": [],
                "result_kind": "candidates_found",
                "evidence_id": EVIDENCE_ID,
            }
        }
    )
    handler = handler_for(
        AgentRole.PROVIDER_MATCHING,
        model=scripted_model(
            [
                ToolCallStep(tool="candidate_researcher", args={"request": "build the pool"}),
                ToolCallStep(tool="constraint_analyst", args={"request": "review the pool"}),
                TextStep(text=json.dumps(_PARENT_ANSWER)),
            ]
        ),
        api=api,
        policy=AllowAllPolicy(),
        sub_agent_models={
            subagents.CANDIDATE_RESEARCHER: scripted_model(
                [
                    ToolCallStep(tool="search_directory", args={"query": "cardiologists"}),
                    TextStep(text=json.dumps({"candidates": [], "strategy": "directory_search"})),
                ]
            ),
            subagents.CONSTRAINT_ANALYST: scripted_model(
                [TextStep(text=json.dumps({"candidates": [], "alternatives": []}))]
            ),
        },
    )

    report = await run_turn(
        AgentRole.PROVIDER_MATCHING,
        script=[],
        workflow_state="PROVIDER_MATCHING",
        handler=handler,
        api=api,
    )

    assert report.result.outcome is ResultOutcome.no_match
    # One instance for the turn, and it saw every model call in the tree — the sub-agents' included.
    # A copied plugin would have recorded the sub-agents' calls somewhere this list cannot see.
    assert len(_SpyPlugin.instances) == 1
    seen = _SpyPlugin.instances[0].model_calls
    assert set(seen) == {
        adk_node_name(AgentRole.PROVIDER_MATCHING),
        subagents.CANDIDATE_RESEARCHER,
        subagents.CONSTRAINT_ANALYST,
    }


@pytest.mark.asyncio
async def test_a_plugin_refusal_leaves_the_turn_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADK wraps the plugin's exception in `RuntimeError`; the handler hands back the refusal."""
    _with_plugins(monkeypatch, _RefusingBudgetPlugin)

    with pytest.raises(BudgetExhausted) as caught:
        await run_turn(
            AgentRole.REFERRAL_READINESS,
            script=[TextStep(text="{}")],
            workflow_state="READINESS_CHECK",
        )

    assert caught.value.site == "spend reservation"
    assert caught.value.to_result().outcome is ResultOutcome.retryable_error


class _HangingPlugin(GuardPlugin):
    """Stands in for anything that never returns: a hung tool, a provider that holds the socket."""

    slot = "circuit_breaker"

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        await asyncio.sleep(5)


@pytest.mark.asyncio
async def test_run_async_wrapped_in_turn_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing inside the runner can outlive the turn: a hang becomes `DEADLINE`, retryable."""
    _with_plugins(monkeypatch, _HangingPlugin)
    role = AgentRole.REFERRAL_READINESS

    with pytest.raises(BudgetExhausted) as caught:
        await run_turn(
            role,
            script=[TextStep(text="{}")],
            workflow_state="READINESS_CHECK",
            budget=TurnBudget(BudgetCaps(), role=role, ttl_ms=50),
        )

    assert caught.value.cap is CapKind.DEADLINE
    assert caught.value.to_result().outcome is ResultOutcome.retryable_error


@pytest.mark.asyncio
async def test_a_booking_role_that_times_out_must_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout in the role that can book may have cut a write short: `outcome_unknown`, never a
    retry that could book twice."""
    _with_plugins(monkeypatch, _HangingPlugin)
    role = AgentRole.SCHEDULING

    with pytest.raises(BudgetExhausted) as caught:
        await run_turn(
            role,
            script=[TextStep(text="{}")],
            workflow_state="BOOKING_IN_FLIGHT",
            budget=TurnBudget(BudgetCaps(), role=role, ttl_ms=50),
        )

    assert caught.value.after_write_attempt is True
    assert caught.value.to_result().outcome is ResultOutcome.outcome_unknown
