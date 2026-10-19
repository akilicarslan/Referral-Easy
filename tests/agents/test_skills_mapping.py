"""§6's skill map, checked against the shipped packages and through a real turn.

§6 assigns eight skill packages to seven roles — `referral-intake` to Referral
Intake, `coverage-interpretation` to Coverage & Network, `constraint-negotiation`
*and* `directory-grounding` to Provider Matching, `referral-readiness` to Referral
Readiness, `booking-confirmation` *and* `uncertain-booking-reconciliation` to
Scheduling, `handoff-triage` to Recovery & Handoff, and nothing at all to Care
Coordinator. This file makes that sentence checkable in three directions:

* **against the packages** — `SkillsPackageSource` reads the eight
  `packages/skills/*/manifest.json` files, and each one's `owner_agent` is the role
  the map assigns it to. The map is not restated as a fixture's opinion: the same
  selection runs over all sixteen workflow states, so a manifest that started
  claiming a state would show up here rather than at demo time;
* **through a real turn** — one offline turn per role, through the real ADK `Runner`,
  asserts the *effective* skill list the agent was built with, which is the list that
  reached its instruction, not the list that happened to be installed;
* **at the prompt** — the bodies loaded are exactly the selected ones, in selection
  order, joined by the runtime's separator. A skill that is rejected is recorded as
  rejected with its reason (`selection.rejected`), so "why did this agent not know
  X?" has an answer in the trace.

Care Coordinator's empty selection is the §3 claim that it owns no skill: it
coordinates, and the specialist's own skill informs the specialist. Its rejections
below name the manifest that *does* apply, which is what makes the empty list a
finding rather than a missing configuration.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from agents_fixtures import (
    ROLES,
    SKILL_BY_ROLE,
    minimal_output,
    run_turn,
)
from policy_table import all_states

from referraleasy_agent_runtime import (
    AgentRole,
    EmptySkillSource,
    SkillManifest,
    SkillSelection,
    SkillsPackageSource,
    load_instructions,
    select_skills,
)
from referraleasy_agents.offline_model import TextStep
from referraleasy_contracts.generated.common import ResultOutcome

pytestmark = pytest.mark.contract

#: The state each role actually works in, for the turn tests. A manifest that
#: claimed a narrower state set would fail there, which is the point.
STATE_BY_ROLE: Mapping[AgentRole, str] = {
    AgentRole.COVERAGE_NETWORK: "COVERAGE_CHECK",
    AgentRole.SCHEDULING: "BOOKING_IN_FLIGHT",
}

#: The separator `load_instructions` joins bodies with.
SEPARATOR = "\n\n---\n\n"


class CountingSource:
    """The shipped packages, with a record of every manifest and body read."""

    def __init__(self) -> None:
        self.inner = SkillsPackageSource()
        self.manifests: list[str] = []
        self.bodies: list[str] = []

    def list_skills(self) -> Sequence[str]:
        return self.inner.list_skills()

    def load_manifest(self, name: str) -> object:
        self.manifests.append(name)
        return self.inner.load_manifest(name)

    def load_body(self, name: str) -> str:
        self.bodies.append(name)
        return self.inner.load_body(name)


def state_for(role: AgentRole) -> str:
    return STATE_BY_ROLE.get(role, "INTAKE")


def title_of(source: Any, name: str) -> str:
    """The skill's H1: what its body calls itself, and what the prompt contains.

    Read from the body rather than from the manifest, because it is the *prompt* whose
    contents are under test — and because the shipped loader's manifest object exposes
    a `title` the runtime's reader deliberately does not depend on.
    """
    body: str = source.load_body(name)
    for line in body.splitlines():
        if line.startswith("# "):
            return line.strip()
    raise AssertionError(f"{name}'s SKILL.md has no H1 heading")


def roles_of(name: str) -> frozenset[str]:
    """The roles a shipped manifest claims, read the way the runtime reads them."""
    source = SkillsPackageSource()
    return SkillManifest.from_source(name, source.load_manifest(name)).roles


# ── the map, against the packages ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES, ids=lambda role: role.value)
def test_a_role_selects_exactly_the_skills_the_map_gives_it(role: AgentRole) -> None:
    source = SkillsPackageSource()
    selection = select_skills(source, role=role, workflow_state=state_for(role))
    assert selection.names == SKILL_BY_ROLE.get(role, ())


@pytest.mark.parametrize("state", all_states())
def test_the_map_does_not_depend_on_the_workflow_state(state: str) -> None:
    """No shipped manifest claims a state, so every role is informed in every state.

    If one started claiming states, selection would become state-dependent and the
    effective list would differ between turns of the same role — a difference the
    handler would record but nobody would be looking for.
    """
    source = SkillsPackageSource()
    for role in ROLES:
        selection = select_skills(source, role=role, workflow_state=state)
        assert set(selection.names) == set(SKILL_BY_ROLE.get(role, ())), (
            f"{role.value} in {state}: {selection.names}"
        )


def test_every_shipped_manifest_names_the_role_the_map_gives_it() -> None:
    """`owner_agent` is the map's other half: read from the files, not from a fixture."""
    source = SkillsPackageSource()
    owners = {name: roles_of(name) for name in source.list_skills()}

    assert len(owners) == 8, f"expected the eight §6 packages, found {sorted(owners)}"
    by_role: dict[str, list[str]] = {}
    for name, claimed in owners.items():
        assert len(claimed) == 1, f"{name} claims {sorted(claimed)}; a skill package owns one role"
        by_role.setdefault(next(iter(claimed)), []).append(name)
    for role, skills in SKILL_BY_ROLE.items():
        assert sorted(by_role.get(role.value, [])) == sorted(skills), (
            f"{role.value}: manifests say {sorted(by_role.get(role.value, []))}"
        )
    assert AgentRole.CARE_COORDINATOR.value not in by_role, "§3: the coordinator owns no skill"


def test_the_care_coordinator_is_offered_the_other_roles_skills_and_takes_none() -> None:
    """§3: it coordinates. Every rejection names the manifest that does apply."""
    source = SkillsPackageSource()
    selection = select_skills(source, role=AgentRole.CARE_COORDINATOR, workflow_state="INTAKE")
    assert selection.names == ()
    assert set(selection.rejected) == set(source.list_skills()), (
        "a skill the coordinator was offered is neither selected nor rejected with a reason"
    )
    assert all("manifest applies to" in reason for reason in selection.rejected.values())
    assert all("not care_coordinator" in reason for reason in selection.rejected.values())


@pytest.mark.parametrize("role", ROLES, ids=lambda role: role.value)
def test_a_role_is_told_which_of_its_siblings_skills_apply_instead(role: AgentRole) -> None:
    """The rejected set is the complement, with a reason each — the trace's audit trail."""
    source = SkillsPackageSource()
    selection = select_skills(source, role=role, workflow_state=state_for(role))
    others = set(source.list_skills()) - set(SKILL_BY_ROLE.get(role, ()))
    assert set(selection.rejected) == others
    assert all(reason.strip() for reason in selection.rejected.values())


# ── the prompt ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES, ids=lambda role: role.value)
def test_only_the_role_s_own_skills_reach_its_instruction(role: AgentRole) -> None:
    """Selection reads no body; `load_instructions` reads exactly the selected ones."""
    counting = CountingSource()
    selection = select_skills(counting, role=role, workflow_state=state_for(role))
    assert counting.bodies == [], "selection read a skill body; selection reads manifests only"

    text = load_instructions(counting, selection)
    assert counting.bodies == list(selection.names), (
        "the prompt was built from a body outside the effective list"
    )
    if not selection.names:
        assert text == ""
        return
    assert text.count(SEPARATOR) == len(selection.names) - 1
    plain = SkillsPackageSource()
    for name in selection.names:
        assert title_of(plain, name) in text
    for name in set(plain.list_skills()) - set(selection.names):
        assert title_of(plain, name) not in text, f"{name} informed {role.value}'s prompt"


# ── a whole turn ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES, ids=lambda role: role.value)
async def test_a_turn_records_the_skills_it_was_actually_built_with(role: AgentRole) -> None:
    """The real `Runner`, the real selection: the report's list is the agent's, not the catalogue's."""
    counting = CountingSource()
    report = await run_turn(
        role,
        script=[TextStep(text=json.dumps(minimal_output(role)))],
        workflow_state=state_for(role),
        skill_source=counting,
    )

    expected = tuple(SKILL_BY_ROLE.get(role, ()))
    assert report.skills == expected
    skills_trace = report.trace["skills"]
    assert skills_trace["role"] == role.value
    assert skills_trace["effective_skills"] == list(expected)
    assert set(skills_trace["rejected"]) == set(counting.list_skills()) - set(expected)
    assert set(counting.bodies) == set(expected)


@pytest.mark.asyncio
async def test_a_deployment_with_no_skills_still_serves_a_turn() -> None:
    """§6's map is a selection, not a dependency: a deployment without the package runs.

    Coverage & Network is the role used here because its minimal answer carries no
    patient text — a turn with one suspends by design (see `test_handler_turn.py`), and
    that suspension would make this test about the suspension rather than the skills.
    """
    report = await run_turn(
        AgentRole.COVERAGE_NETWORK,
        script=[TextStep(text=json.dumps(minimal_output(AgentRole.COVERAGE_NETWORK)))],
        workflow_state="COVERAGE_CHECK",
        skill_source=EmptySkillSource(),
    )
    assert report.skills == ()
    assert report.trace["skills"]["effective_skills"] == []
    assert report.trace["skills"]["considered"] == []
    assert report.result.outcome is ResultOutcome.completed


def test_a_selection_carries_the_role_and_state_it_was_made_for() -> None:
    """The trace entry is self-describing: an auditor should not have to guess the inputs."""
    selection = select_skills(
        SkillsPackageSource(), role=AgentRole.SCHEDULING, workflow_state="BOOKING_IN_FLIGHT"
    )
    assert isinstance(selection, SkillSelection)
    entry = selection.trace_entry()
    assert entry["role"] == "scheduling"
    assert entry["workflow_state"] == "BOOKING_IN_FLIGHT"
    assert entry["effective_skills"] == sorted(SKILL_BY_ROLE[AgentRole.SCHEDULING])
    assert entry["considered"] == list(SkillsPackageSource().list_skills())
