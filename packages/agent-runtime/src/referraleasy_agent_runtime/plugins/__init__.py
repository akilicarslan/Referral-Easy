"""The ADK plugin stack every agent turn runs under (WS3 S1, ADR-0045).

ADK 2.12's `PluginManager` calls plugins in list order and stops at the first one that returns a
value; an exception a plugin raises is re-raised as ``RuntimeError(...) from e``. Order is therefore
behaviour, and it is fixed here rather than left to whoever assembles a runner:

    Identity → ToolPolicy → Budget → CircuitBreaker → UntrustedContent → Screening → Redaction → Provenance

* **Refusing plugins** (the first four, and Screening when it blocks) raise a typed
  `AgentRuntimeError`. `errors.unwrap_plugin_error` recovers it from ADK's `RuntimeError`, so a
  refusal is projected as what it is and never as ``terminal_error``.
* **Transforming plugins** (UntrustedContent, Redaction) mutate the request or response in place and
  return ``None``, so they never short-circuit the plugins after them.
* **Provenance** observes only.

Plugins are built per turn, from a `TurnScope`, and read nothing from session state: ADK's
`AgentTool` merges a child's ``state_delta`` back into the parent, so session state is writable from
below and cannot hold authority.

The slots are reserved before the plugins exist. Until a workstream lands its plugin, its slot is
simply empty — `build_plugin_stack` accepts any subset, in order.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar, Final

from google.adk.plugins.base_plugin import BasePlugin

from referraleasy_agent_runtime.errors import ConfigurationError
from referraleasy_agent_runtime.plugins.errors import unwrap_plugin_error
from referraleasy_agent_runtime.plugins.scope import TurnScope

__all__ = [
    "PLUGIN_ORDER",
    "GuardPlugin",
    "PluginStackError",
    "TurnScope",
    "build_plugin_stack",
    "unwrap_plugin_error",
]

#: The canonical order. A plugin's position is its `slot`'s index here.
PLUGIN_ORDER: Final[tuple[str, ...]] = (
    "identity",
    "tool_policy",
    "budget",
    "circuit_breaker",
    "untrusted_content",
    "screening",
    "redaction",
    "provenance",
)


class PluginStackError(ConfigurationError):
    """A plugin stack that is not the canonical one. Raised while assembling a turn, before any call."""


class GuardPlugin(BasePlugin):
    """A plugin that occupies one slot of `PLUGIN_ORDER`.

    Subclasses set `slot`. The ADK plugin name is the slot, so a log line from `PluginManager`
    ("Error in plugin 'budget' …") names the guard rather than a class.
    """

    slot: ClassVar[str] = ""

    def __init__(self, scope: TurnScope) -> None:
        if self.slot not in PLUGIN_ORDER:
            raise PluginStackError(
                f"{type(self).__name__}.slot={self.slot!r} is not one of {PLUGIN_ORDER}"
            )
        super().__init__(name=self.slot)
        self.scope = scope


def build_plugin_stack(scope: TurnScope, plugins: Iterable[GuardPlugin] = ()) -> list[BasePlugin]:
    """Validate `plugins` against `PLUGIN_ORDER` and return them as ADK's plugin list.

    Refused: anything that is not a `GuardPlugin`, a slot outside the order, a slot taken twice, a
    plugin built for a different turn, and any ordering other than the canonical one. The builder
    does not sort: a caller that passed the wrong order has the wrong model of the stack, and
    silently fixing the list would hide that.
    """
    stack = list(plugins)
    seen: list[str] = []
    for plugin in stack:
        if not isinstance(plugin, GuardPlugin):
            raise PluginStackError(
                f"{type(plugin).__name__} is not a GuardPlugin; only the slots in PLUGIN_ORDER may "
                "run in an agent turn"
            )
        if plugin.slot in seen:
            raise PluginStackError(f"plugin slot {plugin.slot!r} is filled twice")
        if plugin.scope is not scope:
            raise PluginStackError(
                f"plugin {plugin.slot!r} was built for a different turn; plugins are per turn"
            )
        seen.append(plugin.slot)
    expected = [slot for slot in PLUGIN_ORDER if slot in seen]
    if seen != expected:
        raise PluginStackError(
            f"plugins are in the order {seen}; the canonical order is {expected}"
        )
    return list(stack)
