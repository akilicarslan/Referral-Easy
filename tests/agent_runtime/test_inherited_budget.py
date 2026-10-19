"""A remote task cannot replace its caller's deadline or remaining quotas."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from referraleasy_agent_runtime import AgentRole, BudgetCaps, BudgetExhausted, TurnBudget
from referraleasy_agent_runtime.errors import ConfigurationError


def declaration(*, seconds=60, model_calls=2, tool_calls=3, delegations=1):
    deadline = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
    return {
        "task_id": str(uuid4()),
        "workflow_id": str(uuid4()),
        "command_id": str(uuid4()),
        "goal": "Check source data.",
        "workflow_state": "INTAKE",
        "current_state_version": 1,
        "allowed_actions": [],
        "deadline": deadline,
        "release_manifest_id": "rel-20261016-8e00275",
        "call_budget": {
            "deadline_at": deadline,
            "remaining_model_calls": model_calls,
            "remaining_tool_calls": tool_calls,
            "remaining_delegations": delegations,
        },
        "synthetic": True,
    }


def test_remote_usage_consumes_the_callers_remaining_quota():
    budget = TurnBudget.inherited(
        BudgetCaps(), declaration(model_calls=1), role=AgentRole.CARE_COORDINATOR
    )
    budget.account_remote({"model_calls": 1, "tool_calls": 2, "delegations": 0})
    with pytest.raises(BudgetExhausted), budget.model_call(site="parent-after-child"):
        pytest.fail("The child already spent the only remaining model call.")


def test_expired_remote_work_cannot_start_a_new_turn():
    budget = TurnBudget.inherited(
        BudgetCaps(), declaration(seconds=-1), role=AgentRole.REFERRAL_INTAKE
    )
    with pytest.raises(BudgetExhausted):
        budget.require_time()


def test_zero_quota_is_not_replaced_by_a_service_default():
    budget = TurnBudget.inherited(
        BudgetCaps(), declaration(model_calls=0), role=AgentRole.CARE_COORDINATOR
    )
    with pytest.raises(BudgetExhausted), budget.model_call(site="no-quota"):
        pytest.fail("A zero quota must refuse the call.")


def test_malformed_peer_usage_cannot_refund_calls():
    budget = TurnBudget(BudgetCaps())
    with pytest.raises(ConfigurationError):
        budget.account_remote({"model_calls": -1, "tool_calls": 0, "delegations": 0})
