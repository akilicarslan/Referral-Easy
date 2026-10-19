"""The turn's tool surface: everything the model can call, and nothing it cannot.

A `ToolBox` is built once per service; a `ToolSet` is started once per turn. The
split matters because the things that must not be shared across turns (the ledger,
the envelope, the payload) live on the `ToolSet`, while the things that must not be
rebuilt per turn (the gate, the policy, the transport) live on the `ToolBox`.

Everything a model can reach goes through :meth:`ToolSet.invoke`, which does four
things in a fixed order:

1. **authority** — `governed_tool_call` asks the `ToolGate`: is the tool on this
   role's allowlist (local data), and does the policy table permit it in this
   workflow state (the database's answer)? A refusal raises
   :class:`referraleasy_agent_runtime.ToolNotAllowed` or `PolicyRefused`, both of
   which project to the protocol's `rejected`. A refused call never consumes budget,
   because the check runs before the lease.
2. **budget** — the lease carries the milliseconds the call may use, narrowed by
   what remains of the turn.
3. **the handler** — with a :class:`ToolCall` the model cannot construct: the role,
   the state, the envelope and the lease are passed *here*, not accepted as
   arguments.
4. **provenance** — the result's evidence ids are recorded in the turn's ledger, so
   a decision can cite what was actually retrieved.

:meth:`ToolSet.exposed` is what the ADK agent is given. It returns one callable per
allowlisted tool, wrapped so that the ADK declaration names exactly the parameters
`tools/specs.py` declares — no `tool_context`, no `call`, nothing a model could be
invited to fill in. The refusal tests do not go through the ADK layer: they call
:meth:`ToolSet.invoke` directly, because the property under test ("the runtime
refuses, not the model") must hold on the path the model's arguments take, and
asserting it through a second wrapper would test the wrapper.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from referraleasy_agent_runtime import (
    AgentRole,
    PolicyBackend,
    ToolGate,
    TurnBudget,
    allowed_tools,
    governed_tool_call,
)
from referraleasy_agents.canonical import id_text, id_texts
from referraleasy_agents.evidence import EvidenceLedger
from referraleasy_agents.tools.call import Api, DelegationPort, ToolCall
from referraleasy_agents.tools.handlers import handler_for
from referraleasy_agents.tools.specs import TOOL_SPECS, ToolSpec
from referraleasy_contracts.generated.task import TaskEnvelope

__all__ = [
    "ToolBox",
    "ToolSet",
]


@dataclass(frozen=True, slots=True)
class _CallRecord:
    """One tool call, as the trace sees it: what, how long, and what it cited."""

    tool: str
    timeout_ms: int
    attempt: int
    evidence_ids: tuple[str, ...]

    def as_trace_entry(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "timeout_ms": self.timeout_ms,
            "attempt": self.attempt,
            "evidence_ids": list(self.evidence_ids),
        }


class ToolBox:
    """The per-service tool authority and transport."""

    def __init__(
        self,
        *,
        role: AgentRole,
        api: Api,
        policy: PolicyBackend | None = None,
        delegation: DelegationPort | None = None,
    ) -> None:
        self._role = role
        self._api = api
        self._policy = policy
        self._delegation = delegation
        # The gate is built here and handed to no one: a caller with a reference
        # could build a ToolCall that skips it, which is exactly the shape of bug
        # this class exists to make impossible.
        self._gate = ToolGate(role, policy=policy)

    @property
    def role(self) -> AgentRole:
        return self._role

    @property
    def gate(self) -> ToolGate:
        """The authority check, bound to this role and the caller's policy backend."""
        return self._gate

    @property
    def api(self) -> Api:
        return self._api

    @property
    def policy(self) -> PolicyBackend | None:
        return self._policy

    @property
    def delegation(self) -> DelegationPort | None:
        return self._delegation

    @property
    def allows(self) -> frozenset[str]:
        """The role's allowlist, from the runtime's table. Not settable, by design."""
        return allowed_tools(self._role)

    def specs(self) -> tuple[ToolSpec, ...]:
        """The specs this role may ever call, in a stable order."""
        return tuple(TOOL_SPECS[name] for name in sorted(self.allows))

    def start(
        self,
        *,
        budget: TurnBudget,
        envelope: TaskEnvelope | None = None,
        payload: Mapping[str, Any] | None = None,
        ledger: EvidenceLedger | None = None,
        workflow_state: str | None = None,
    ) -> ToolSet:
        """Begin a turn: bind the budget, the envelope and the provenance ledger.

        ``workflow_state`` is the service's own reading of the task request, used
        only when no envelope carries the state. It is never a tool argument and
        never a model-supplied value — see :attr:`ToolSet.workflow_state`.
        """
        return ToolSet(
            box=self,
            budget=budget,
            envelope=envelope,
            payload=dict(payload or {}),
            ledger=ledger or EvidenceLedger(),
            state=workflow_state or "",
        )


@dataclass(slots=True)
class ToolSet:
    """One turn's tools: the same authority every turn, this turn's data."""

    box: ToolBox
    budget: TurnBudget
    envelope: TaskEnvelope | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    calls: list[_CallRecord] = field(default_factory=list)
    #: The service's own reading of the turn's state, used when no envelope carries
    #: one. Set by `ToolBox.start`, never by a caller of a tool.
    state: str = ""

    # ── the surface ──────────────────────────────────────────────────────────────────────────

    @property
    def workflow_state(self) -> str:
        """The state the gate checks against.

        Two sources, and neither is the model. The envelope wins when it carries a
        state, because an envelope is the authenticated task; otherwise the state the
        service read from the request is used. A model that could name its own
        workflow state would be choosing which policy row applies to it, which is why
        no tool declaration has a parameter for it and `specs.py` has no way to add
        one.
        """
        if self.envelope is not None and self.envelope.workflow_state is not None:
            return self.envelope.workflow_state.value
        return self.state

    def exposed_names(self) -> tuple[str, ...]:
        """The tool names the model will be shown, sorted."""
        return tuple(sorted(self.box.allows))

    def exposed(self) -> list[Callable[..., Any]]:
        """The ADK-callable surface: one function per allowlisted tool."""
        return [_adk_tool(self, TOOL_SPECS[name]) for name in self.exposed_names()]

    # ── the call path ────────────────────────────────────────────────────────────────────────

    async def invoke(self, name: str, /, **arguments: Any) -> dict[str, Any]:
        """Authorise, budget, run and record one tool call.

        The only path to a handler. ``name`` is positional-only so that a keyword
        argument called `name` in some tool's own signature can never be confused
        with the tool being called.
        """
        with governed_tool_call(
            self.box.gate,
            self.budget,
            name,
            workflow_state=self.workflow_state or None,
        ) as lease:
            call = ToolCall(
                role=self.box.role,
                tool=name,
                workflow_state=self.workflow_state,
                api=self.box.api,
                budget=self.budget,
                lease=lease,
                ledger=self.ledger,
                envelope=self.envelope,
                payload=self.payload,
                policy=self.box.policy,
                delegation=self.box.delegation,
            )
            result = await handler_for(name)(call, **arguments)
            evidence_ids = self.ledger.record(name, result)
            self.calls.append(
                _CallRecord(
                    tool=name,
                    timeout_ms=lease.timeout_ms,
                    attempt=lease.attempt,
                    evidence_ids=tuple(id_text(item) for item in evidence_ids),
                )
            )
            return result

    # ── what the turn learned ────────────────────────────────────────────────────────────────

    def citation_ids(self) -> list[str]:
        """Every evidence id retrieved this turn, oldest first.

        A decision cites these rather than a list the model assembled: §5 requires
        `evidence_ids` on every decision, and the only defensible source for them is
        what the tools actually returned.
        """
        return id_texts(self.ledger.ids())

    def trace_entries(self) -> list[dict[str, Any]]:
        """The calls this turn made, for the trace."""
        return [record.as_trace_entry() for record in self.calls]


# ── the ADK-facing wrapper ───────────────────────────────────────────────────────────────────

#: How the ADK tool declaration is shaped. Stated once, here, because the one thing
#: it must never contain is a context parameter — and a wrapper that accidentally
#: declared one would let a model try to supply it.
_DESCRIPTION_SUFFIX: Final = (
    "\nThe tool runs under this agent's own authority: the workflow state, the turn's budget and "
    "the retrieved evidence are supplied by the service, never by you."
)


def _adk_tool(tools: ToolSet, spec: ToolSpec) -> Callable[..., Any]:
    """A callable whose ADK declaration is exactly ``spec``, bound to ``tools``.

    ADK builds a function declaration from `inspect.signature` and the type hints,
    so both are set explicitly: the signature names the spec's parameters (and only
    those), and the annotations are the spec's types. The function body takes
    ``**arguments`` because the *declaration* is what the model sees and the
    service is what validates.
    """
    parameters = [
        inspect.Parameter(
            parameter.name,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=parameter.annotation,
            default=inspect.Parameter.empty if parameter.required else None,
        )
        for parameter in spec.parameters
    ]

    async def tool(**arguments: Any) -> dict[str, Any]:
        return await tools.invoke(spec.name, **arguments)

    tool.__name__ = spec.name
    tool.__qualname__ = f"ToolSet.{spec.name}"
    tool.__doc__ = spec.docstring() + _DESCRIPTION_SUFFIX
    tool.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    tool.__annotations__ = {
        **{parameter.name: parameter.annotation for parameter in spec.parameters},
        "return": dict[str, Any],
    }
    return tool
