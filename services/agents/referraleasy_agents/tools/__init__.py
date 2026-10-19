"""The tools package: the §3 surface, as declarations, handlers and one gate.

Import order below is the layering, and it is acyclic on purpose:

``call`` -> ``handlers`` -> ``toolbox``

* :mod:`referraleasy_agents.tools.call` — what a tool call is given (and the two
  protocols a handler may use).
* :mod:`referraleasy_agents.tools.specs` — the 19 declarations the model is shown.
* :mod:`referraleasy_agents.tools.handlers` — the 19 implementations.
* :mod:`referraleasy_agents.tools.toolbox` — the one path a model's arguments take,
  through the runtime's `ToolGate` and `TurnBudget`.

Nothing else in the service calls a handler directly. `test_tool_surface.py`
asserts the three sets agree — declarations, handlers, and the union of the seven
roles' allowlists — because a tool declared but not gated, or gated but not
declared, is the kind of gap that only shows up in production.
"""

from __future__ import annotations

from referraleasy_agents.tools.call import Api, DelegationPort, ToolCall
from referraleasy_agents.tools.handlers import HANDLERS, ToolHandler, handler_for
from referraleasy_agents.tools.specs import TOOL_SPECS, ParamSpec, ToolSpec, spec_for
from referraleasy_agents.tools.toolbox import ToolBox, ToolSet

__all__ = [
    "HANDLERS",
    "TOOL_SPECS",
    "Api",
    "DelegationPort",
    "ParamSpec",
    "ToolBox",
    "ToolCall",
    "ToolHandler",
    "ToolSet",
    "ToolSpec",
    "handler_for",
    "spec_for",
]
