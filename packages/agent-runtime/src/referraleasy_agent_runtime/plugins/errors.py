"""Recovering a typed failure from ADK's plugin wrapper (WS3 S1).

ADK re-raises a plugin's exception as ``RuntimeError("Error in plugin …") from e``, and reports a
breached ``RunConfig.max_llm_calls`` as its own `LlmCallsLimitExceededError`. Neither is an
`AgentRuntimeError`, so without this the app's projection would report a budget refusal as
``terminal_error`` — the failure class the gateway hands to a human, for something a fresh turn
could have answered.
"""

from __future__ import annotations

from google.adk.agents.invocation_context import LlmCallsLimitExceededError

from referraleasy_agent_runtime.budget import BudgetExhausted, CapKind
from referraleasy_agent_runtime.errors import AgentRuntimeError

__all__ = ["unwrap_plugin_error"]

#: How deep `__cause__` is followed. A nested `AgentTool` runner adds one wrapper per level; anything
#: deeper than this is not a chain ADK produced.
_MAX_DEPTH = 8


def unwrap_plugin_error(
    error: BaseException, *, model_call_limit: int | None = None, role: str | None = None
) -> BaseException:
    """The typed failure behind `error`, or `error` itself when there is none.

    Walks `__cause__` to the first `AgentRuntimeError`. ADK's own call-limit error becomes
    `BudgetExhausted(MODEL_CALLS)`: the RunConfig limit is a backstop under the runtime's cap, so
    reaching it means the model-call budget is spent.
    """
    current: BaseException | None = error
    for _ in range(_MAX_DEPTH):
        if current is None:
            break
        if isinstance(current, AgentRuntimeError):
            return current
        if isinstance(current, LlmCallsLimitExceededError):
            limit = model_call_limit if model_call_limit is not None else 0
            return BudgetExhausted(
                CapKind.MODEL_CALLS,
                limit=limit,
                used=limit,
                agent_role=role,
                site="adk run_config backstop",
                cause=current,
            )
        current = current.__cause__
    return error
