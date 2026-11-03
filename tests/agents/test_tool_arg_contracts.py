"""WS2 S1-S2 (ADR-0044): every model-filled tool argument is typed and classified.

The declarations in `tools/specs.py` are what a model is shown and, since WS2 S1, what
`ToolSet.invoke` holds every call to. These tests are the gate over the whole surface:

* every id-shaped parameter carries an `IdClass`, and every `OWNED` one names a registered resolver
  (and a synthetic unclassified spec proves the gate can fail);
* no parameter is a free `str`;
* the ADK declaration a model actually receives carries the constraints;
* a malformed id — a path, not a UUID — is refused before any transport call.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

import pytest
from agents_fixtures import AllowAllPolicy, StubApi, envelope, toolset_for
from google.adk.tools.function_tool import FunctionTool

from referraleasy_agent_runtime import AgentRole, allowed_tools
from referraleasy_agents.errors import ArgumentRejected
from referraleasy_agents.tools.argtypes import RESOLVERS, IdClass
from referraleasy_agents.tools.specs import TOOL_SPECS, ParamSpec, ToolSpec

pytestmark = pytest.mark.contract

#: A parameter whose name says it identifies something.
ID_SHAPED = re.compile(r"(^|_)(id|ids|key|ref)$|_role$")

#: The parameters WS2 S4 removes from the model's reach. Classified as pending until then.
PENDING_REMOVAL = frozenset({"booking_request_id", "idempotency_key", "new_slot_id"})


def _contract_gaps(specs: Iterable[ToolSpec]) -> list[str]:
    """What the classification gate reports. Shared by the real and the negative test."""
    gaps: list[str] = []
    for spec in specs:
        for parameter in spec.parameters:
            schema = _property_schema(spec, parameter)
            uuid_typed = "uuid" in repr(schema)
            if (ID_SHAPED.search(parameter.name) or uuid_typed) and parameter.id_class is None:
                gaps.append(f"{spec.name}.{parameter.name}: id-shaped but unclassified")
            if parameter.id_class is IdClass.OWNED and parameter.resolver not in RESOLVERS:
                gaps.append(f"{spec.name}.{parameter.name}: owned with no registered resolver")
    return gaps


def _property_schema(spec: ToolSpec, parameter: ParamSpec) -> Mapping[str, Any]:
    return spec.args_model().model_json_schema()["properties"][parameter.name]


def _free_strings(node: Any, path: str = "") -> list[str]:
    """Every string leaf with no pattern, enum, const or length cap."""
    found: list[str] = []
    if isinstance(node, Mapping):
        if node.get("type") == "string" and not (
            {"pattern", "enum", "const", "maxLength"} & set(node)
        ):
            found.append(path or "(root)")
        for key, value in node.items():
            found += _free_strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _free_strings(value, f"{path}[{index}]")
    return found


def test_every_id_shaped_parameter_is_classified() -> None:
    assert _contract_gaps(TOOL_SPECS.values()) == []


def test_the_classifier_catches_an_unclassified_id() -> None:
    """Negative: the gate above can fail. A plain `foo_id: str` is reported."""
    synthetic = ToolSpec(
        name="synthetic_tool",
        summary="A tool that names a record with no class.",
        parameters=(ParamSpec(name="foo_id", annotation=str, description="Some record."),),
    )
    assert _contract_gaps([synthetic]) == ["synthetic_tool.foo_id: id-shaped but unclassified"]


def test_pending_removals_are_the_only_service_supplied_parameters_left() -> None:
    """The (a) class is a to-do list, and it is exactly the one WS2 S4 works through."""
    pending = {
        parameter.name
        for spec in TOOL_SPECS.values()
        for parameter in spec.parameters
        if parameter.id_class is IdClass.SERVICE_SUPPLIED_PENDING
    }
    assert pending == PENDING_REMOVAL


@pytest.mark.parametrize("tool", sorted(TOOL_SPECS))
def test_no_parameter_is_an_unconstrained_str(tool: str) -> None:
    schema = TOOL_SPECS[tool].args_model().model_json_schema()
    assert _free_strings(schema) == []


def test_the_free_string_check_catches_a_bare_str() -> None:
    """Negative: the check above can fail."""
    bare = ToolSpec(
        name="synthetic_text",
        summary="A tool with an unbounded text field.",
        parameters=(ParamSpec(name="note", annotation=str, description="Anything."),),
    )
    assert _free_strings(bare.args_model().model_json_schema()) == ["properties.note"]


@pytest.mark.parametrize("tool", sorted(TOOL_SPECS))
def test_an_undeclared_argument_is_refused_by_the_model(tool: str) -> None:
    assert TOOL_SPECS[tool].args_model().model_json_schema()["additionalProperties"] is False


def _declaration(tool: str) -> Mapping[str, Any]:
    role = next(role for role in AgentRole if tool in allowed_tools(role))
    _, tools = toolset_for(role, api=StubApi(), policy=AllowAllPolicy())
    callable_ = next(item for item in tools.exposed() if item.__name__ == tool)
    schema = FunctionTool(callable_)._get_declaration().parameters_json_schema
    assert isinstance(schema, Mapping)
    return schema


@pytest.mark.parametrize("tool", sorted(TOOL_SPECS))
def test_adk_declaration_carries_constraints(tool: str) -> None:
    """What the model is shown, not what the spec says: the constraints survive ADK's conversion."""
    declared = _declaration(tool)["properties"]
    for parameter in TOOL_SPECS[tool].parameters:
        text = repr(declared[parameter.name])
        if parameter.id_class in {IdClass.OWNED, IdClass.PUBLIC} or parameter.name in {
            "booking_request_id",
            "new_slot_id",
        }:
            assert "'format': 'uuid'" in text, f"{tool}.{parameter.name} is not declared a uuid"
        if parameter.name.endswith("_ids"):
            assert "'maxItems'" in text, f"{tool}.{parameter.name} has no item cap"
    assert _free_strings(declared) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document_id",
    ["../appointments/x/cancel", "0f0f0f0f-0000-4000-8000-000000000001/../../admin", "", "*"],
)
async def test_path_traversal_id_never_reaches_transport(document_id: str) -> None:
    """Attack: an id that is a path is refused by the contract; the API stub sees nothing."""
    api = StubApi()
    _, tools = toolset_for(
        AgentRole.REFERRAL_READINESS,
        api=api,
        policy=AllowAllPolicy(),
        workflow_state="READINESS_CHECK",
        envelope=envelope(workflow_state="READINESS_CHECK"),
    )

    with pytest.raises(ArgumentRejected) as rejected:
        await tools.invoke("inspect_document", document_id=document_id)

    assert "document_id" in str(rejected.value)
    assert api.calls == []


@pytest.mark.asyncio
async def test_extra_argument_is_rejected() -> None:
    """`extra="forbid"`: a model cannot smuggle a field the declaration does not have."""
    api = StubApi()
    _, tools = toolset_for(AgentRole.PROVIDER_MATCHING, api=api, policy=AllowAllPolicy())

    with pytest.raises(ArgumentRejected) as rejected:
        await tools.invoke("search_directory", query="cardiologists", patient_id="someone-else")

    assert "patient_id" in str(rejected.value)
    assert api.calls == []


@pytest.mark.asyncio
async def test_an_oversized_id_list_is_refused() -> None:
    api = StubApi()
    _, tools = toolset_for(
        AgentRole.REFERRAL_READINESS,
        api=api,
        policy=AllowAllPolicy(),
        workflow_state="READINESS_CHECK",
        envelope=envelope(workflow_state="READINESS_CHECK"),
    )
    ids = [f"0f0f0f0f-0000-4000-8000-{index:012d}" for index in range(21)]

    with pytest.raises(ArgumentRejected):
        await tools.invoke(
            "validate_referral_package",
            hospital_id="0f0f0f0f-0000-4000-8000-0000000000b1",
            visit_type="first_visit",
            document_ids=ids,
        )

    assert api.calls == []
