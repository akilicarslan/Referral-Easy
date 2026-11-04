"""WS3 S1 (ADR-0045): the plugin stack's order is a contract, and a plugin's refusal stays typed.

ADK stops at the first plugin that returns a value and re-raises a plugin's exception as
`RuntimeError`. These tests pin the two consequences the framework exists for: the order cannot be
changed by whoever assembles a turn, and a typed refusal survives ADK's wrapper.
"""

from __future__ import annotations

import pytest
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.plugins.base_plugin import BasePlugin

from referraleasy_agent_runtime import AgentRole, BudgetCaps, TurnBudget
from referraleasy_agent_runtime.budget import BudgetExhausted, CapKind
from referraleasy_agent_runtime.errors import ToolNotAllowed
from referraleasy_agent_runtime.plugins import (
    PLUGIN_ORDER,
    GuardPlugin,
    PluginStackError,
    TurnScope,
    build_plugin_stack,
    unwrap_plugin_error,
)
from referraleasy_contracts.generated.common import ResultOutcome


def _scope() -> TurnScope:
    role = AgentRole.PROVIDER_MATCHING
    return TurnScope(role=role, budget=TurnBudget(BudgetCaps(), role=role))


def _plugin(slot: str, scope: TurnScope) -> GuardPlugin:
    cls = type(f"{slot.title()}Plugin", (GuardPlugin,), {"slot": slot})
    return cls(scope)


def test_plugin_order_is_contract() -> None:
    """The order the index fixes (binding resolution 3). Changing it is an ADR, not a refactor."""
    assert PLUGIN_ORDER == (
        "identity",
        "tool_policy",
        "budget",
        "circuit_breaker",
        "untrusted_content",
        "screening",
        "redaction",
        "provenance",
    )


def test_the_full_stack_in_order_is_accepted_and_named_by_slot() -> None:
    scope = _scope()
    stack = build_plugin_stack(scope, [_plugin(slot, scope) for slot in PLUGIN_ORDER])
    assert [plugin.name for plugin in stack] == list(PLUGIN_ORDER)


def test_any_subset_in_order_is_accepted() -> None:
    """Slots are reserved before the plugins exist; an empty slot is a no-op."""
    scope = _scope()
    assert build_plugin_stack(scope) == []
    subset = [_plugin("budget", scope), _plugin("provenance", scope)]
    assert build_plugin_stack(scope, subset) == subset


def test_rejects_reordered_or_unknown_plugin() -> None:
    scope = _scope()
    with pytest.raises(PluginStackError, match="canonical order"):
        build_plugin_stack(scope, [_plugin("redaction", scope), _plugin("identity", scope)])
    with pytest.raises(PluginStackError, match="not one of"):
        _plugin("logging", scope)
    with pytest.raises(PluginStackError, match="not a GuardPlugin"):
        build_plugin_stack(scope, [BasePlugin(name="identity")])  # type: ignore[list-item]
    with pytest.raises(PluginStackError, match="twice"):
        build_plugin_stack(scope, [_plugin("budget", scope), _plugin("budget", scope)])


def test_a_plugin_built_for_another_turn_is_refused() -> None:
    """Plugins are per turn: one built for another turn's scope would charge another turn's budget."""
    with pytest.raises(PluginStackError, match="different turn"):
        build_plugin_stack(_scope(), [_plugin("budget", _scope())])


def test_plugin_error_unwraps_to_typed_outcome() -> None:
    """ADK's wrapper is removed, so `BudgetExhausted` projects to `retryable_error`."""
    refusal = BudgetExhausted(CapKind.MODEL_CALLS, limit=6, used=6)
    try:
        try:
            raise refusal
        except BudgetExhausted as inner:
            raise RuntimeError("Error in plugin 'budget' during 'before_model_callback'") from inner
    except RuntimeError as wrapped:
        typed = unwrap_plugin_error(wrapped)
    assert typed is refusal
    assert typed.to_result().outcome is ResultOutcome.retryable_error


def test_nested_wrappers_are_unwrapped() -> None:
    """An `AgentTool` sub-runner adds one wrapper per level."""
    refusal = ToolNotAllowed(
        "not allowed",
        role="care_coordinator",
        tool="search_directory",
        workflow_state="PROVIDER_MATCHING",
    )
    outer = RuntimeError("outer")
    middle = RuntimeError("middle")
    middle.__cause__ = refusal
    outer.__cause__ = middle
    assert unwrap_plugin_error(outer) is refusal


def test_adk_call_limit_becomes_model_call_budget_exhaustion() -> None:
    typed = unwrap_plugin_error(
        LlmCallsLimitExceededError("limit"), model_call_limit=12, role="provider_matching"
    )
    assert isinstance(typed, BudgetExhausted)
    assert typed.cap is CapKind.MODEL_CALLS
    assert typed.limit == 12


def test_an_untyped_error_is_left_alone() -> None:
    """Unwrapping never invents a classification: an unknown failure stays terminal."""
    error = RuntimeError("something else")
    assert unwrap_plugin_error(error) is error
