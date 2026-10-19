"""The seven Agent Cards: immutable, complete, and cross-checked against reality.

§13 requires an immutable card per agent whose `agent_hash` covers what the agent is. A card is
easy to write and easy to let drift, so nothing here is asserted against the card itself:

* the card's tool list is compared with the runtime's allowlist, so a card cannot advertise a tool
  the agent does not hold (and cannot hide one it does);
* its skills are compared with the manifests that name the role as owner, so §13's agreement
  between registry and packages is a test rather than a hope;
* its schema ids are looked up in the schema registry, so a card cannot name a schema that does not
  exist;
* the hash is recomputed, and then moved once per field §13 lists — a hash that covered nothing
  would otherwise pass every equality check.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from agents_fixtures import CARD_NAME_BY_ROLE, ROLES, SKILL_BY_ROLE
from policy_table import all_roles

import referraleasy_skills as skills_package
from referraleasy_agent_runtime import AgentRole, allowed_tools, is_write_tool, service_name
from referraleasy_agents import assembly, subagents
from referraleasy_agents.cards import (
    CARD_VERSION,
    ROLE_BY_CARD_NAME,
    SUB_AGENT_NAMES,
    agent_hash,
    card_for,
    card_payload,
    cards,
    schema_version,
)
from referraleasy_agents.schemas import (
    INPUT_SCHEMAS,
    OUTPUT_SCHEMAS,
    input_schema_id,
    output_schema_id,
)
from referraleasy_contracts.generated.task import Skill

pytestmark = pytest.mark.contract

#: The pattern the contract pins a card name to.
CARD_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")

#: A SHA-256 hex digest.
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _hash_mutations() -> Sequence[tuple[str, Mapping[str, Any]]]:
    """One mutation per field §13 says must move the hash.

    Each entry changes exactly one thing, so a hash that ignored a field would show up as a
    mutation that did **not** move it — which is the only way this property can be tested. The
    skills mutation is a synthetic skill rather than an empty tuple, because the Care Coordinator
    legitimately owns none and `()` would not be a change for it.
    """
    invented = Skill(
        id="invented.skill",
        name="invented-skill",
        description="A skill no manifest provides.",
        skill_version="1.0.0",
    )
    return (
        ("description", {"description": "a changed description"}),
        ("instruction", {"instruction": "a changed instruction"}),
        ("tools", {"tools": ("create_booking",)}),
        ("sub_agents", {"sub_agents": ("invented-sub-agent",)}),
        ("skills", {"skills": (invented,)}),
    )


def test_every_role_has_a_card_and_every_card_a_role() -> None:
    assert set(cards()) == set(all_roles())
    assert {name: role for name, role in ROLE_BY_CARD_NAME.items()} == {
        CARD_NAME_BY_ROLE[role]: role for role in ROLES
    }


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_a_card_name_is_the_protocol_shape(role: AgentRole) -> None:
    name = card_for(role).name
    assert CARD_NAME_PATTERN.match(name), name
    assert name == CARD_NAME_BY_ROLE[role]


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_the_adk_node_name_derives_from_the_card_name_by_one_substitution(role: AgentRole) -> None:
    """ADK refuses a hyphen, so the node name is the card name with `-` for `_`, nothing else."""
    node = assembly.adk_node_name(role)
    assert node.isidentifier()
    assert node == card_for(role).name.replace("-", "_")
    assert node != service_name(role)  # the deployment name is a third thing


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_a_card_s_tools_are_exactly_the_runtime_s_allowlist(role: AgentRole) -> None:
    assert card_for(role).tools == sorted(allowed_tools(role))


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_only_scheduling_publishes_a_write_tool(role: AgentRole) -> None:
    published = card_for(role).tools
    assert published is not None, "an agent with no tools at all could not do its job"
    writes = sorted(tool for tool in published if is_write_tool(tool))
    expected = writes if role is AgentRole.SCHEDULING else []
    assert writes == expected
    if role is not AgentRole.SCHEDULING:
        assert not writes


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_a_card_s_schema_ids_resolve(role: AgentRole) -> None:
    card = card_for(role)
    assert card.input_schema_id == input_schema_id(role)
    assert card.output_schema_id == output_schema_id(role)
    assert card.input_schema_id in INPUT_SCHEMAS
    assert card.output_schema_id in OUTPUT_SCHEMAS
    assert card.input_schema_id.endswith(f".input.{schema_version()}")
    assert card.output_schema_id.endswith(f".output.{schema_version()}")


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_a_card_s_skills_are_the_manifests_that_own_the_role(role: AgentRole) -> None:
    expected = sorted(
        (manifest.agent_card_skill_id, manifest.name, manifest.version)
        for manifest in skills_package.list_manifests()
        if manifest.owner_agent == role.value
    )
    actual = sorted((skill.id, skill.name, skill.skill_version) for skill in card_for(role).skills)
    assert actual == expected


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_the_skills_map_matches_the_cards(role: AgentRole) -> None:
    """§6's ownership map and the cards are two statements of the same fact."""
    assert {skill.name for skill in card_for(role).skills} == set(SKILL_BY_ROLE.get(role, ()))


def test_the_care_coordinator_owns_no_skill() -> None:
    """§6 gives it none: its behaviour is the policy table, not a package."""
    assert SKILL_BY_ROLE.get(AgentRole.CARE_COORDINATOR, ()) == ()
    assert card_for(AgentRole.CARE_COORDINATOR).skills == []


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_only_provider_matching_declares_sub_agents(role: AgentRole) -> None:
    declared = tuple(card_for(role).sub_agents or ())
    assert declared == SUB_AGENT_NAMES.get(role, ())
    if role is not AgentRole.PROVIDER_MATCHING:
        assert declared == ()


def test_the_sub_agent_spellings_are_one_substitution_apart() -> None:
    protocol = SUB_AGENT_NAMES[AgentRole.PROVIDER_MATCHING]
    node = subagents.sub_agent_names()
    assert protocol == ("candidate-researcher", "constraint-analyst")
    assert node == ("candidate_researcher", "constraint_analyst")
    assert (
        node == tuple(subagents.protocol_name(name) for name in protocol)
        or tuple(subagents.protocol_name(item) for item in node) == protocol
    )
    for name in node:
        assert name.isidentifier()
    assert subagents.sub_agent_names() == (
        subagents.CANDIDATE_RESEARCHER,
        subagents.CONSTRAINT_ANALYST,
    )


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_a_card_hash_is_the_agent_hash(role: AgentRole) -> None:
    card = card_for(role)
    assert HASH_PATTERN.match(card.agent_hash), card.agent_hash
    assert card.agent_hash == agent_hash(role)


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_every_field_13_lists_moves_the_hash(role: AgentRole) -> None:
    base = agent_hash(role)
    for field, mutation in _hash_mutations():
        moved = agent_hash(role, **mutation)
        assert moved != base, f"{role.value}: changing {field} did not move the agent hash"
        assert HASH_PATTERN.match(moved)
    assert agent_hash(role) == base  # and the default is stable


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_a_card_read_is_a_copy_a_caller_cannot_write_through(role: AgentRole) -> None:
    """The registry's own immutability guarantee.

    `AgentCard` is a generated contract model that is **not** frozen — a finding this test records:
    the assertion below proves the type *is* mutable, and the rest proves that mutability cannot
    reach the registry, because every read is a deep copy.
    """
    first = card_for(role)
    assert first == card_for(role)
    assert first is not card_for(role)

    first.name = "renamed"  # mutable by type, and this is what makes the copy necessary
    assert card_for(role).name == CARD_NAME_BY_ROLE[role]
    assert card_for(role).agent_hash == agent_hash(role)

    with pytest.raises(TypeError):
        cards()[role] = first  # type: ignore[index]


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_a_card_declares_the_transport_auth_and_capabilities(role: AgentRole) -> None:
    card = card_for(role)
    assert card.auth.scheme == "bearer"
    assert card.auth.verified_at_transport is True
    assert card.auth.required_scopes == ["referraleasy.a2a.invoke"]
    assert card.capabilities.streaming is True
    assert card.capabilities.pushNotifications is False
    assert card.capabilities.stateTransitionHistory is True


@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_the_card_payload_is_json_ready(role: AgentRole) -> None:
    import json

    payload = card_payload(role)
    assert json.loads(json.dumps(payload)) == payload
    assert payload["name"] == CARD_NAME_BY_ROLE[role]
    assert payload["agent_hash"] == agent_hash(role)


def test_the_card_version_is_a_release_not_an_edit() -> None:
    assert CARD_VERSION == "2.0.0"
    assert all(card_for(role).version == CARD_VERSION for role in ROLES)


def test_a_card_description_states_what_the_agent_may_not_do() -> None:
    """§3's forbidden column is part of the published identity, not only of the prompt.

    Checked as substrings rather than by extracting noun phrases from the briefing: the wording of
    each description is a human's, and the property under test is that the boundary is *published*.
    """
    published: Mapping[AgentRole, tuple[str, ...]] = {
        AgentRole.CARE_COORDINATOR: ("never write a booking", "change a patient's choice"),
        AgentRole.REFERRAL_INTAKE: ("cannot diagnose", "specialty the referral does not state"),
        AgentRole.COVERAGE_NETWORK: ("cannot guarantee coverage or payment", "invent a rule"),
        AgentRole.PROVIDER_MATCHING: ("cannot choose for the patient", "relax a hard constraint"),
        AgentRole.REFERRAL_READINESS: ("cannot assume a document", "grant approval"),
        AgentRole.SCHEDULING: ("live approval", "the only holder of booking write authority"),
        AgentRole.RECOVERY_HANDOFF: ("cannot override a policy block", "forever"),
    }
    assert set(published) == set(ROLES)
    for role, phrases in published.items():
        description = card_for(role).description.lower()
        for phrase in phrases:
            assert phrase.lower() in description, (
                f"{role.value}: the card description does not state {phrase!r}, so §3's forbidden "
                "column is published nowhere a caller can read it"
            )
