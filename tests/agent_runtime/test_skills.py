"""Skill selection: manifests only, the effective list in the trace, no authority granted.

The load-bearing property is the *cost* of selection. Reading eight ``SKILL.md``
files to answer "which skills exist?" is the difference between a fast turn and a
budget-shaped hole, so the suite asserts the reads that did **not** happen — a test
that only checks the returned names would pass just as happily against a loader that
read everything.

The second property is balance of power: a manifest is a document that ships next to
a prompt, and documents do not grant tool authority. That is asserted twice — once
against a hostile double, once against the real skills package, which ships
``authority: instructions_only`` for a reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from agent_runtime_doubles import RecordingSkillSource

from referraleasy_agent_runtime.errors import SkillSourceError
from referraleasy_agent_runtime.roles import AgentRole, allowed_tools
from referraleasy_agent_runtime.skills import (
    EmptySkillSource,
    SkillManifest,
    SkillsPackageSource,
    gate_manifests,
    load_instructions,
    select_skills,
)

MANIFESTS: dict[str, dict[str, Any]] = {
    "booking-confirmation": {
        "name": "booking-confirmation",
        "version": "1.0.0",
        "roles": ["scheduling"],
        "tags": ["booking", "consent"],
        "summary": "Confirm a booking with the patient before committing it.",
    },
    "slot-search": {
        "name": "slot-search",
        "roles": ["scheduling"],
        "tags": ["booking"],
        "states": ["AWAITING_SLOT_CHOICE"],
        "summary": "Find candidate slots.",
    },
    "coverage-reading": {
        "name": "coverage-reading",
        "roles": ["coverage_network"],
        "tags": ["coverage"],
        "summary": "Read a plan's coverage rules.",
    },
    "unowned": {
        "name": "unowned",
        "tags": ["booking"],
        "summary": "A manifest that declares no roles claims nothing.",
    },
}


class _FakeManifest:
    """The shape the real loader's dataclass has, minus the fields the runtime ignores."""

    def __init__(
        self,
        *,
        name: str,
        owner_agent: str | None = None,
        tags: tuple[str, ...] = (),
        summary: str = "",
        grants_tool_permissions: bool = False,
    ) -> None:
        self.name = name
        self.owner_agent = owner_agent
        self.tags = tags
        self.summary = summary
        self.grants_tool_permissions = grants_tool_permissions


def test_selection_reads_manifests_and_never_a_body() -> None:
    source = RecordingSkillSource(MANIFESTS)

    selection = select_skills(
        source, role=AgentRole.SCHEDULING, workflow_state="AWAITING_SLOT_CHOICE", tags=["booking"]
    )

    assert source.body_reads == [], "selection must not read SKILL.md"
    assert selection.names == ("booking-confirmation", "slot-search")
    assert set(source.manifest_reads) == set(MANIFESTS), "every candidate is considered"


def test_a_state_gated_skill_is_selected_only_in_its_own_state() -> None:
    """`slot-search` declares AWAITING_SLOT_CHOICE, and must not load in another state."""
    source = RecordingSkillSource(MANIFESTS)

    elsewhere = select_skills(source, role=AgentRole.SCHEDULING, workflow_state="INTAKE")

    assert "slot-search" not in elsewhere.names
    assert "AWAITING_SLOT_CHOICE" in elsewhere.rejected["slot-search"]


def test_only_the_selected_skills_bodies_are_read() -> None:
    source = RecordingSkillSource(MANIFESTS)
    selection = select_skills(
        source, role=AgentRole.SCHEDULING, workflow_state="AWAITING_SLOT_CHOICE", tags=["booking"]
    )
    manifest_reads_before = list(source.manifest_reads)

    text = load_instructions(source, selection)

    assert source.body_reads == ["booking-confirmation", "slot-search"]
    assert "instructions for booking-confirmation" in text
    assert "coverage-reading" not in text
    assert source.manifest_reads == manifest_reads_before, "the prompt build re-read no manifest"


def test_a_skill_owned_by_another_role_is_not_selected() -> None:
    source = RecordingSkillSource(MANIFESTS)

    selection = select_skills(source, role=AgentRole.PROVIDER_MATCHING, tags=["booking"])

    assert selection.names == ()
    assert "coverage-reading" in selection.rejected
    assert "booking-confirmation" in selection.rejected


def test_the_rejection_reason_is_recorded_for_every_candidate_left_out() -> None:
    """So "why did the agent not know X?" is answerable after the fact."""
    selection = select_skills(
        RecordingSkillSource(MANIFESTS),
        role=AgentRole.SCHEDULING,
        workflow_state="AWAITING_SLOT_CHOICE",
        tags=["booking"],
    )

    assert set(selection.rejected) == {"coverage-reading", "unowned"}
    assert "scheduling" in selection.rejected["coverage-reading"]
    assert "no roles" in selection.rejected["unowned"]


@pytest.mark.parametrize(
    "spelling", ["AWAITING_SLOT_CHOICE", "awaiting_slot_choice", " Awaiting_Slot_Choice "]
)
def test_workflow_state_case_and_padding_do_not_change_the_decision(spelling: str) -> None:
    """Regression: manifests are stored lower-cased, states are written upper-cased.

    Comparing the two raw forms made every state-gated skill unselectable — silently,
    because a rejected skill is recorded rather than raised.
    """
    selection = select_skills(
        RecordingSkillSource({"slot-search": MANIFESTS["slot-search"]}),
        role=AgentRole.SCHEDULING,
        workflow_state=spelling,
    )

    assert selection.names == ("slot-search",)
    assert selection.rejected == {}


def test_a_manifest_that_declares_no_roles_is_never_selected_automatically() -> None:
    selection = select_skills(
        RecordingSkillSource({"unowned": MANIFESTS["unowned"]}), role=AgentRole.SCHEDULING
    )

    assert selection.names == ()
    assert selection.rejected["unowned"] == "manifest declares no roles"


def test_an_explicit_request_is_honoured_even_when_the_gate_would_refuse_it() -> None:
    """A task that names a skill gets it: silently dropping an instruction is worse."""
    selection = select_skills(
        RecordingSkillSource(MANIFESTS),
        role=AgentRole.PROVIDER_MATCHING,
        requested=["coverage-reading"],
    )

    assert selection.names == ("coverage-reading",)


def test_an_explicit_request_for_an_unknown_skill_fails_loudly() -> None:
    with pytest.raises(SkillSourceError, match="does not provide"):
        select_skills(
            RecordingSkillSource(MANIFESTS), role=AgentRole.SCHEDULING, requested=["nope"]
        )


def test_the_trace_records_the_effective_list_not_the_available_one() -> None:
    source = RecordingSkillSource(MANIFESTS)
    selection = select_skills(
        source, role=AgentRole.SCHEDULING, workflow_state="AWAITING_SLOT_CHOICE", tags=["booking"]
    )

    entry = selection.trace_entry()

    assert entry["role"] == "scheduling"
    assert entry["effective_skills"] == ["booking-confirmation", "slot-search"]
    assert set(entry["considered"]) == set(MANIFESTS)
    assert entry["effective_skills"] != entry["considered"]
    assert [item["skill"] for item in entry["selected"]] == ["booking-confirmation", "slot-search"]
    assert entry["rejected"]


def test_a_manifest_claiming_tool_permission_is_recorded_and_ignored() -> None:
    hostile = {
        "omni-skill": {
            "name": "omni-skill",
            "roles": ["scheduling"],
            "tools": ["create_booking", "cancel_booking"],
            "grants_tool_permissions": True,
        }
    }

    selection = select_skills(RecordingSkillSource(hostile), role=AgentRole.SCHEDULING)
    entry = selection.trace_entry()

    assert selection.names == ("omni-skill",)
    assert entry["declared_tools_not_granted"] == ["cancel_booking", "create_booking"]
    assert entry["selected"][0]["manifest_claimed_tool_permissions"] is True

    # The claim changed nothing about what the role may call.
    assert allowed_tools(AgentRole.SCHEDULING) == {
        "list_slots",
        "create_booking",
        "lookup_request",
        "cancel_booking",
        "reschedule_booking",
    }
    assert allowed_tools(AgentRole.CARE_COORDINATOR) == {"delegate_task"}


def test_gate_manifests_applies_entitlement_to_someone_elses_ranking() -> None:
    """Relevance is the loader's decision; entitlement stays ours."""
    ranked = [
        SkillManifest(name="coverage-interpretation", roles=frozenset({"coverage_network"})),
        SkillManifest(name="booking-confirmation", roles=frozenset({"scheduling"})),
        SkillManifest(name="mystery", roles=frozenset()),
    ]

    selection = gate_manifests(ranked, role=AgentRole.SCHEDULING)

    assert selection.names == ("booking-confirmation",)
    assert set(selection.considered) == {
        "coverage-interpretation",
        "booking-confirmation",
        "mystery",
    }
    assert len(selection.rejected) == 2


def test_a_manifest_object_is_read_as_readily_as_a_mapping() -> None:
    """The runtime reads the loader's dataclasses without importing the loader."""
    manifest = SkillManifest.from_source(
        "fallback-name",
        _FakeManifest(
            name="referral-intake",
            owner_agent="referral_intake",
            tags=("intake", "documents"),
            summary="Gather what a referral needs.",
        ),
    )

    assert manifest.name == "referral-intake"
    assert manifest.roles == frozenset({"referral_intake"})
    assert manifest.tags == frozenset({"intake", "documents"})
    assert manifest.description == "Gather what a referral needs."


def test_an_unreadable_manifest_claims_nothing() -> None:
    """Default-deny, as everywhere else: an unknown shape is not selected."""
    manifest = SkillManifest.from_source("whatever", object())

    assert manifest.name == "whatever"
    assert manifest.roles == frozenset()
    assert manifest.tags == frozenset()


def test_an_empty_skill_source_fails_loudly_rather_than_returning_nothing() -> None:
    source = EmptySkillSource()

    assert tuple(source.list_skills()) == ()
    with pytest.raises(SkillSourceError, match="no skills are available"):
        source.load_manifest("anything")
    with pytest.raises(SkillSourceError, match="no skills are available"):
        source.load_body("anything")


# --- against the real skills package ---------------------------------------------------------
# The package is authored separately, so it is imported per test rather than at module scope:
# a missing skills package must skip these tests, not the offline ones above.


@pytest.fixture(scope="module")
def skills_package() -> Any:
    return pytest.importorskip(
        "referraleasy_skills", reason="packages/skills is authored separately; skip when absent"
    )


EXPECTED_SKILLS = {
    "booking-confirmation",
    "constraint-negotiation",
    "coverage-interpretation",
    "directory-grounding",
    "handoff-triage",
    "referral-intake",
    "referral-readiness",
    "uncertain-booking-reconciliation",
}


class _RecordingPackage:
    """Proxies the real loader and records which of its three ends were touched."""

    def __init__(self, module: Any) -> None:
        self._module = module
        self.manifest_reads: list[str] = []
        self.body_reads: list[str] = []

    def list_skills(self) -> Sequence[str]:
        return list(self._module.list_skills())

    def load_manifest(self, name: str) -> object:
        self.manifest_reads.append(name)
        return self._module.load_manifest(name)

    def load_skill(self, name: str) -> object:
        """The end the adapter reaches for; recorded here, so a body read is visible."""
        self.body_reads.append(name)
        return self._module.load_skill(name)

    def select(self, query: str = "", *, owner_agent: str | None = None) -> Sequence[object]:
        return list(self._module.select(query, owner_agent=owner_agent))


def test_the_real_package_ships_the_expected_skills(skills_package: Any) -> None:
    assert set(skills_package.list_skills()) == EXPECTED_SKILLS


def test_selecting_against_the_real_package_reads_no_instructions(skills_package: Any) -> None:
    """The expensive read happens once, for the skills that were actually chosen."""
    package = _RecordingPackage(skills_package)
    source = SkillsPackageSource(module=package)

    selection = select_skills(source, role=AgentRole.SCHEDULING, tags=["booking"])

    assert selection.names, "the scheduling role owns at least one booking skill"
    assert package.body_reads == [], "selection read SKILL.md"
    assert set(package.manifest_reads) == EXPECTED_SKILLS

    load_instructions(source, selection)
    assert package.body_reads == list(selection.names)
    assert set(package.body_reads) != EXPECTED_SKILLS, "only the selected bodies were read"


def test_the_real_manifests_grant_no_tool_permission(skills_package: Any) -> None:
    source = SkillsPackageSource(module=skills_package)

    for name in skills_package.list_skills():
        selection = select_skills(source, role=AgentRole.CARE_COORDINATOR, requested=[name])
        manifest = selection.selected[0]

        assert manifest.roles, f"{name} declares no owner, so it could never be selected"
        assert manifest.grants_tool_permissions_claimed is False, name
        assert manifest.declared_tools == frozenset(), f"{name} declares tools"

    # The coordinator requested every skill in turn and still holds only delegation.
    assert allowed_tools(AgentRole.CARE_COORDINATOR) == {"delegate_task"}


def test_the_real_package_ranks_and_we_gate(skills_package: Any) -> None:
    """The loader's `select` decides relevance; `gate_manifests` decides entitlement."""
    package = _RecordingPackage(skills_package)
    ranked = package.select("book a slot")
    assert len(ranked) > 1, "the ranking should propose more than the role may have"

    selection = gate_manifests(ranked, role=AgentRole.SCHEDULING)

    assert set(selection.names) <= {manifest.name for manifest in ranked}  # gating never adds
    assert selection.names, "scheduling should keep its own booking skills"


def test_every_skill_in_the_package_belongs_to_one_of_the_seven_roles(
    skills_package: Any,
) -> None:
    """`owner_agent` is the bridge between the two packages; it must land on a role."""
    for name in skills_package.list_skills():
        manifest = SkillManifest.from_source(name, skills_package.load_manifest(name))

        for owner in manifest.roles:
            assert AgentRole(owner), f"{name} names unknown owner {owner!r}"


def test_the_adapter_reads_instructions_from_load_skill(skills_package: Any) -> None:
    """The naming bridge, asserted directly: the loader's body is `Skill.instructions`."""
    body = SkillsPackageSource(module=skills_package).load_body("booking-confirmation")

    assert isinstance(body, str)
    assert len(body) > 100, "a SKILL.md that is nearly empty would make the prompt useless"


def test_the_adapter_satisfies_our_protocol(skills_package: Any) -> None:
    """Structurally, not by inheritance — the runtime never imports the loader at module scope."""
    source = SkillsPackageSource(module=skills_package)

    assert callable(source.list_skills)
    assert callable(source.load_manifest)
    assert callable(source.load_body)
    assert set(source.list_skills()) == EXPECTED_SKILLS
