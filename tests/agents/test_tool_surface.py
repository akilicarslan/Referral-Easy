"""What a model can reach, and what it is shown, are the same closed set.

Three surfaces have to agree, and a disagreement between any two of them is a hole:

* `tools/specs.py` — the declaration a model is shown;
* `tools/handlers.py` — the functions behind those declarations;
* `referraleasy_agent_runtime.roles.ROLE_TOOLS` — the allowlist every call is refused against.

They are asserted equal to each other and to the union of the seven allowlists. On top of that
agreement, this file checks the two properties that make the surface safe rather than merely
consistent:

* **no declaration carries a service-supplied parameter** — a model invited to supply a workflow
  state would be choosing its own permissions, and one invited to supply a workflow, a command or a
  patient would be choosing whose records its call lands in (§6). The names are listed in
  `SERVICE_SUPPLIED` below and the check is over every tool.
* **no role is shown a tool it cannot call** — the surface of a role is its allowlist, not a
  superset of it.

The identity half of the first property is the one that changed code rather than a test: `workflow_id`,
`command_id`, `patient_id` and `task_envelope` were parameters of four tools until this file showed
what a model could do with them, and they are now read from the task (`tools/call.py`).
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

import pytest
from agents_fixtures import ROLES
from policy_table import POLICY_ROWS

from referraleasy_agent_runtime import AgentRole, allowed_tools
from referraleasy_agents import subagents
from referraleasy_agents.tools import call as call_module
from referraleasy_agents.tools.handlers import HANDLERS
from referraleasy_agents.tools.specs import TOOL_SPECS, ParamSpec
from referraleasy_agents.tools.toolbox import ToolBox, ToolSet

pytestmark = pytest.mark.contract

#: Names that must never appear in a tool declaration, in two groups.
#:
#: * **runtime context** — the call context, the budget, the authority, the transport. Every one of
#:   them is supplied by the service from the authenticated envelope (§9).
#: * **identity** — *whose* records this call is about. `workflow_id`, `command_id`, `patient_id`,
#:   `patient_details_id`, `current_state_version` and the envelope itself are read from the task
#:   (`tools/call.py`: `require_workflow_id`, `require_command_id`, `require_patient_id`), because a
#:   model that can name them can attribute a booking write, a document certification or a constraint
#:   to a workflow or patient other than the one whose task it is answering.
SERVICE_SUPPLIED: frozenset[str] = frozenset(
    {
        "approval_policy",
        "budget",
        "call",
        "client",
        "command_id",
        "current_state_version",
        "delegation",
        "envelope",
        "gate",
        "lease",
        "ledger",
        "patient_details_id",
        "patient_id",
        "policy",
        "role",
        "state_version",
        "task_envelope",
        "tool_context",
        "turn_id",
        "workflow_id",
        "workflow_state",
    }
)

#: The tool names the union of the seven allowlists contains, sorted.
UNION_OF_ALLOWLISTS = tuple(sorted({tool for role in ROLES for tool in allowed_tools(role)}))

#: Every (role, tool) pair that exists. Parametrising over the pairs rather than the cross product is
#: not tidiness: a skip list that grows to a hundred entries is a test suite whose real coverage is
#: unreadable, and the surface of a role *is* its allowlist.
ROLE_TOOL_PAIRS: tuple[tuple[AgentRole, str], ...] = tuple(
    (role, tool) for role in ROLES for tool in sorted(allowed_tools(role))
)


def _api_free_toolset(role: AgentRole) -> ToolSet:
    """A `ToolSet` for ``role`` with an API that refuses to be called.

    The surface is what is under test, not the transport; a tool that were actually invoked would
    fail loudly here rather than quietly returning something.
    """

    class _Unused:
        async def get(self, path: str, **_: object) -> dict[str, object]:
            raise AssertionError(f"the surface test should not have called GET {path}")

        async def post(self, path: str, payload: object, **_: object) -> dict[str, object]:
            raise AssertionError(f"the surface test should not have called POST {path}")

    box = ToolBox(role=role, api=_Unused())
    from referraleasy_agent_runtime import BudgetCaps, TurnBudget

    return box.start(budget=TurnBudget(BudgetCaps(), role=role))


def _exposed_signature(role: AgentRole, tool: str) -> inspect.Signature:
    for callable_ in _api_free_toolset(role).exposed():
        if callable_.__name__ == tool:
            return inspect.signature(callable_)
    raise AssertionError(f"{role.value} does not expose {tool}")


def test_the_three_surfaces_are_one_set() -> None:
    assert tuple(sorted(TOOL_SPECS)) == UNION_OF_ALLOWLISTS
    assert tuple(sorted(HANDLERS)) == UNION_OF_ALLOWLISTS


def test_the_surface_is_the_nineteen_3_tools() -> None:
    """§3's tool list, as a count a reader can check against the section."""
    assert len(UNION_OF_ALLOWLISTS) == 19


def test_every_tool_is_named_by_the_policy_table() -> None:
    """A tool with no policy row could never be called: the table is default-deny."""
    named = {row.tool for row in POLICY_ROWS}
    assert set(UNION_OF_ALLOWLISTS) <= named


def test_the_policy_table_also_names_platform_actions_no_agent_holds() -> None:
    """The complement, asserted rather than assumed: these are the platform's own steps.

    A patient's choice, a recorded consent and a resolved ticket are transitions the workflow
    performs on a *person's* decision. The policy table names them because they are policy-relevant
    and because the table is the vocabulary of the state machine — and no allowlist contains one,
    which is what keeps them unreachable from every model.
    """
    platform_actions = sorted({row.tool for row in POLICY_ROWS} - set(UNION_OF_ALLOWLISTS))
    assert platform_actions == ["await_patient_choice", "capture_consent", "resolve_ticket"]


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_a_role_is_shown_exactly_its_allowlist(role: AgentRole) -> None:
    assert _api_free_toolset(role).exposed_names() == tuple(sorted(allowed_tools(role)))


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_no_role_is_shown_a_tool_it_does_not_hold(role: AgentRole) -> None:
    held = set(allowed_tools(role))
    shown = set(_api_free_toolset(role).exposed_names())
    assert shown == held
    assert shown <= set(UNION_OF_ALLOWLISTS)


@pytest.mark.parametrize("tool", UNION_OF_ALLOWLISTS)
def test_a_declaration_carries_no_runtime_context(tool: str) -> None:
    spec = TOOL_SPECS[tool]
    declared = {parameter.name for parameter in spec.parameters}
    assert not declared & SERVICE_SUPPLIED
    assert "call" not in declared


def test_no_declaration_anywhere_names_the_workflow_the_patient_or_the_state() -> None:
    """The identity rule over the whole surface, in one assertion a reader can check.

    Written as its own test rather than left to the parametrised one above, because the failure it
    guards against was real: four tools took `workflow_id`, `command_id`, `patient_id` or a whole
    `task_envelope` from the model, which is how a call lands in a record nobody asked it to touch.
    """
    offenders = {
        tool: sorted({parameter.name for parameter in spec.parameters} & SERVICE_SUPPLIED)
        for tool, spec in TOOL_SPECS.items()
    }
    assert {tool: names for tool, names in offenders.items() if names} == {}


@pytest.mark.parametrize(
    ("role", "tool"),
    ROLE_TOOL_PAIRS,
    ids=[f"{role.value}-{tool}" for role, tool in ROLE_TOOL_PAIRS],
)
def test_the_adk_declaration_is_exactly_the_spec(role: AgentRole, tool: str) -> None:
    """The callable ADK turns into a declaration, compared with the spec it claims to be."""
    spec = TOOL_SPECS[tool]
    signature = _exposed_signature(role, tool)
    declared = tuple(signature.parameters)
    assert declared == tuple(parameter.name for parameter in spec.parameters)
    for parameter in signature.parameters.values():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    for name, parameter in signature.parameters.items():
        spec_parameter = spec_for_parameter(tool, name)
        assert parameter.annotation == spec_parameter.annotation
        assert (parameter.default is inspect.Parameter.empty) is spec_parameter.required
    # `dict[str, Any]`, spelled out rather than compared with a bare `dict`: the wrapper's own
    # annotation is what ADK reads, and `dict[str, object]` is a *different* type here.
    assert signature.return_annotation == dict[str, Any]


def spec_for_parameter(tool: str, name: str) -> ParamSpec:
    for parameter in TOOL_SPECS[tool].parameters:
        if parameter.name == name:
            return parameter
    raise AssertionError(f"{tool} has no declared parameter {name!r}")


@pytest.mark.parametrize("tool", UNION_OF_ALLOWLISTS)
def test_a_handler_takes_exactly_the_declared_parameters(tool: str) -> None:
    handler = HANDLERS[tool]
    parameters = tuple(inspect.signature(handler).parameters.values())
    assert parameters, f"{tool} takes no arguments at all"
    assert parameters[0].name == "call", f"{tool}'s first parameter must be the ToolCall"
    declared = {parameter.name for parameter in TOOL_SPECS[tool].parameters}
    actual = {
        parameter.name
        for parameter in parameters[1:]
        if parameter.kind is not inspect.Parameter.VAR_KEYWORD
    }
    assert actual == declared
    for parameter in parameters[1:]:
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{tool}.{parameter.name} must be keyword-only: a positional argument could be supplied "
            "by a caller that is not the model's argument object"
        )


@pytest.mark.parametrize("tool", UNION_OF_ALLOWLISTS)
def test_a_declaration_documents_its_parameters_and_its_evidence(tool: str) -> None:
    docstring = TOOL_SPECS[tool].docstring()
    assert "Args:" in docstring
    for parameter in TOOL_SPECS[tool].parameters:
        assert f"    {parameter.name}:" in docstring
    assert "evidence_ids" in docstring


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_the_exposed_callables_name_themselves_after_the_tool(role: AgentRole) -> None:
    for callable_ in _api_free_toolset(role).exposed():
        assert callable_.__name__ in allowed_tools(role)
        assert callable_.__doc__
        assert "never by you" in callable_.__doc__ or "supplied by the service" in callable_.__doc__


def test_no_allowlist_contains_a_sub_agent_name() -> None:
    """A sub-agent is a delegation, not authority: it is never a tool in a role's allowlist."""
    names = set(UNION_OF_ALLOWLISTS) | {
        subagents.CANDIDATE_RESEARCHER,
        subagents.CONSTRAINT_ANALYST,
    }
    assert len(names) == len(UNION_OF_ALLOWLISTS) + 2
    assert subagents.sub_agent_names() not in [tuple(sorted(allowed_tools(role))) for role in ROLES]


def test_the_two_sub_agents_hold_a_subset_of_their_parent_s_tools() -> None:
    parent = set(allowed_tools(AgentRole.PROVIDER_MATCHING))
    assert set(subagents.RESEARCHER_TOOLS) <= parent
    assert set(subagents.ANALYST_TOOLS) <= parent
    assert set(subagents.ANALYST_TOOLS) <= set(subagents.RESEARCHER_TOOLS) | {"search_directory"}


def test_a_sub_agent_asking_for_a_tool_the_role_lacks_is_a_configuration_error() -> None:
    """A sub-agent silently given no tools would answer from memory and look like it had searched."""
    tools = _api_free_toolset(AgentRole.REFERRAL_INTAKE)
    with pytest.raises(ValueError) as caught:
        subagents._tools_named(tools, ("find_candidates",), owner="a test")
    assert "find_candidates" in str(caught.value)


def test_only_the_coordinator_can_delegate() -> None:
    holders = [role for role in ROLES if "delegate_task" in allowed_tools(role)]
    assert holders == [AgentRole.CARE_COORDINATOR]


def test_only_scheduling_holds_a_write_tool() -> None:
    from referraleasy_agent_runtime import is_write_tool

    holders: Mapping[AgentRole, tuple[str, ...]] = {
        role: tuple(sorted(tool for tool in allowed_tools(role) if is_write_tool(tool)))
        for role in ROLES
    }
    assert holders[AgentRole.SCHEDULING] == (
        "cancel_booking",
        "create_booking",
        "reschedule_booking",
    )
    for role, writes in holders.items():
        if role is not AgentRole.SCHEDULING:
            assert writes == (), f"{role.value} holds {writes}"


def test_exactly_one_tool_declares_a_payload_and_it_is_the_booking_body() -> None:
    """`payload` is a legitimate name for one tool's subject matter, and for no other tool's.

    `create_booking` writes an approval-bound booking body and calls it `payload`; `ToolCall.payload`
    is the turn's own input document, which is a different object with the same name. The name is
    kept, and the collision is pinned here so it cannot spread to a second tool where it would be
    ambiguous — and so that the parameters the *model* may supply stay what the model may choose.
    """
    declaring = sorted(
        tool for tool, spec in TOOL_SPECS.items() if "payload" in {p.name for p in spec.parameters}
    )
    assert declaring == ["create_booking"]
    first = next(iter(TOOL_SPECS["create_booking"].parameters))
    assert first.name == "idempotency_key"


def test_the_write_subject_declares_every_field_the_booking_guards_read() -> None:
    """`WriteGuardSubject` is those guards' contract, and it must name all they read.

    The guards are the booking write's whole defence, and `forbidden.py` calls them directly with a
    hand-built subject — which only proves anything if the protocol says what they may read. A guard
    that reached for a sixth field would be reading it off whatever object it was handed.
    `payload` is the fifth and the one that is easy to miss: `enforce_idempotency_key` reads the
    workflow's own key from the turn's input, which is half of §7's "no duplicate under a new key".
    """
    declared = {
        name
        for name, member in inspect.getmembers(call_module.WriteGuardSubject)
        if isinstance(member, property)
    }
    assert declared == {"current_state_version", "envelope", "payload", "role", "workflow_state"}
