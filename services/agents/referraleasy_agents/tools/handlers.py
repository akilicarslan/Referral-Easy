"""The 19 §3 tools, mapped to the functions that implement them.

One table, so "which tools exist" is one place rather than nineteen imports. A
declared tool with no handler is a hole in the surface — the model sees a
declaration and the call crashes — so `test_tool_surface.py` asserts that this
table's keys are exactly the union of the seven roles' allowlists, and exactly the
keys of `tools/specs.py`.

Handlers are called with keyword arguments only, and each one declares exactly the
parameters its spec does. An undeclared keyword is a `TypeError`, which is the
correct failure for a call that could not have come from the declaration.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any, Final

from referraleasy_agents.tools import (
    coordination,
    coverage,
    handoff,
    intake,
    matching,
    readiness,
    scheduling,
)

__all__ = [
    "HANDLERS",
    "ToolHandler",
    "handler_for",
]

#: A handler: the call context positionally, the model's arguments by keyword, a
#: JSON-ready result.
#:
#: ``...`` rather than the exact parameter list, on purpose: each handler declares its
#: own keyword-only parameters, and restating all nineteen signatures here would be a
#: second copy of the tool surface that `tools/specs.py` owns — and the two would
#: drift. What is *not* loosened by ``...`` is the rule those signatures carry, because
#: it is asserted directly on the functions: `test_tool_surface.py` checks that every
#: handler takes the `ToolCall` first and every other parameter keyword-only and
#: declared, so an argument no declaration could have produced is a `TypeError`.
ToolHandler = Callable[..., Awaitable[dict[str, Any]]]

_HANDLERS: Final[Mapping[str, ToolHandler]] = MappingProxyType(
    {
        # Referral Intake
        "validate_referral": intake.validate_referral,
        "resolve_location": intake.resolve_location,
        "patient_preferences": intake.patient_preferences,
        # Coverage & Network
        "check_plan": coverage.check_plan,
        "check_provider_network": coverage.check_provider_network,
        "get_coverage_rules": coverage.get_coverage_rules,
        # Provider Matching
        "find_candidates": matching.find_candidates,
        "search_directory": matching.search_directory,
        # Referral Readiness
        "get_referral_requirements": readiness.get_referral_requirements,
        "inspect_document": readiness.inspect_document,
        "validate_referral_package": readiness.validate_referral_package,
        # Scheduling
        "list_slots": scheduling.list_slots,
        "create_booking": scheduling.create_booking,
        "lookup_request": scheduling.lookup_request,
        "cancel_booking": scheduling.cancel_booking,
        "reschedule_booking": scheduling.reschedule_booking,
        # Recovery & Handoff
        "classify_error": handoff.classify_error,
        "create_handoff": handoff.create_handoff,
        # Care Coordinator
        "delegate_task": coordination.delegate_task,
    }
)

HANDLERS: Final[Mapping[str, ToolHandler]] = _HANDLERS


def handler_for(name: str) -> ToolHandler:
    """The handler for ``name``. Raises :class:`KeyError` for a name with no implementation."""
    try:
        return _HANDLERS[name]
    except KeyError as error:
        raise KeyError(
            f"no handler for tool {name!r}; implemented tools are {sorted(_HANDLERS)}"
        ) from error
