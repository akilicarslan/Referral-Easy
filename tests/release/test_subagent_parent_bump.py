"""§13: *"if a sub-agent changes, the parent's dependency hash and version rise too."*

The sentence has two halves and they work differently, so they are tested separately:

* the **hash** moves by itself, because the parent's record covers the sub-agent manifest.
  Those are the ``*_moves_*`` tests.
* the **version** cannot move by itself — no function knows whether an edit is a patch or a
  major change, and a registry that guessed would be inventing a number. What §13 asks for is
  that CI *fails* until a person raises it (``*_without_a_bump_fails_*``) and passes once they
  have (``*_with_a_bump_*``). The bump goes through ``BuildInputs.ledger``, because that is
  where the manifest's declared version comes from; nudging the value afterwards would be
  testing a number no build produced.

The gap this module exists to demonstrate
-----------------------------------------
An Agent Card's own ``agent_hash`` does **not** cover the sub-agent directive or the
sub-agents' own instructions: ``cards._hash_payload`` reads the sub-agent *names* and nothing
else (``artifacts.AGENT_HASH_GAP``). So there is a real change — reword the sentence the
parent appends to its prompt — that moves nothing a peer could see over
``/.well-known/agent-card.json``. The manifest closes it, and
``test_the_card_hash_does_not_see_the_directive_the_manifest_does`` asserts both halves of
that sentence so nobody later deletes the field as redundant.

Mutations are applied in-process with ``monkeypatch``, to the module the build reads at call
time. No file in the shared working tree is touched, and the fact that this works at all is
itself the property under test: the hash is taken over what is shipped *now*, not over a
value cached when the module was first imported.
"""

from __future__ import annotations

import dataclasses

import pytest
from release_fixtures import (
    RELEASE_COMPONENT,
    REPO_ROOT,
    agent_named,
    build_test_manifest,
    with_versions,
)

from referraleasy_agent_runtime.roles import AgentRole
from referraleasy_release_registry.artifacts import (
    agent_hash,
    card_name,
    rfc_module,
    skill_artifacts,
)
from referraleasy_release_registry.gate import (
    VERSION_NOT_ADVANCED,
    check,
    component_states,
)
from referraleasy_release_registry.hashing import is_sha256_hex, text_digest
from referraleasy_release_registry.manifest import agent_artifact_hash, artifact_hash
from referraleasy_release_registry.versions import load_ledger

pytestmark = pytest.mark.contract

PARENT = AgentRole.PROVIDER_MATCHING
SIBLING = AgentRole.SCHEDULING
PARENT_COMPONENT = "agent-provider_matching"


def ledger_on_disk():
    """The committed ledger, read fresh: every test that bumps starts from the same file."""
    return load_ledger(REPO_ROOT / "registry" / "versions.json")


def next_agent(level="patch"):
    from release_fixtures import advance_version

    return advance_version(ledger_on_disk().agent(PARENT.value), level)


def next_release():
    from release_fixtures import advance_version

    return advance_version(ledger_on_disk().release)


def states(manifest, ledger):
    return component_states(manifest, ledger)


# ── a sub-agent's own instruction ───────────────────────────────────────────────────


@pytest.fixture
def reworded_sub_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reword ``candidate_researcher``'s instruction, in the module the build reads."""
    subagents = rfc_module("referraleasy_agents.subagents")
    monkeypatch.setattr(
        subagents,
        "_RESEARCHER_INSTRUCTION",
        f"{subagents._RESEARCHER_INSTRUCTION}\nPrefer the candidate whose evidence is newest.",
    )


def test_a_sub_agent_instruction_change_moves_the_parent(manifest, reworded_sub_agent) -> None:
    """The hash half of §13's sentence, and the reason the sub-agent manifest exists."""
    mutated = build_test_manifest()
    before = agent_named(manifest, card_name(PARENT))
    after = agent_named(mutated, card_name(PARENT))

    assert after.sub_agent_manifest_hash != before.sub_agent_manifest_hash
    assert is_sha256_hex(after.sub_agent_manifest_hash)
    assert agent_artifact_hash(after) != agent_artifact_hash(before)
    assert artifact_hash(mutated) != artifact_hash(manifest)


def test_a_sub_agent_instruction_change_does_not_move_a_sibling(
    manifest, reworded_sub_agent
) -> None:
    """A change is attributed to one component, not to the release.

    If every agent's hash moved whenever anything moved, the gate would demand seven version
    bumps for a one-line edit and its findings would stop meaning anything.
    """
    mutated = build_test_manifest()
    for role in AgentRole:
        if role is PARENT:
            continue
        assert agent_artifact_hash(agent_named(mutated, card_name(role))) == agent_artifact_hash(
            agent_named(manifest, card_name(role))
        ), role.value


def test_a_sub_agent_change_without_a_bump_fails_the_gate(manifest, reworded_sub_agent) -> None:
    """*"CI fails when an affected agent's version did not increase"* — the version half.

    Two components are named rather than one, and that is the release's own arithmetic rather
    than a quirk: the release-level artifact hash is taken over the whole manifest, so an
    agent's record changing is a change to the release as well. Shipping a new agent *is* a
    new release, and the gate says so in the same breath.
    """
    ledger = ledger_on_disk()
    mutated = build_test_manifest(ledger=ledger)
    result = check(states(manifest, ledger), states(mutated, ledger))

    assert result.failed
    assert [finding.code for finding in result.findings] == [VERSION_NOT_ADVANCED] * 2
    assert [finding.component for finding in result.findings] == [
        PARENT_COMPONENT,
        RELEASE_COMPONENT,
    ]
    assert "did not increase" in result.findings[0].detail
    assert result.need_evaluation == ()


def test_bumping_only_the_agent_is_not_enough(manifest, reworded_sub_agent) -> None:
    """A half-fix is reported, not accepted: the release carries the change too.

    This is the failure mode worth pinning. A person who reads "bump the affected agent's
    version" literally and bumps only ``provider_matching`` gets a second, different failure —
    and the two findings name different components, so the message cannot be mistaken for a
    repeat of the first.
    """
    agent_bumped = with_versions(ledger_on_disk(), agents={PARENT.value: next_agent()})
    mutated = build_test_manifest(ledger=agent_bumped)
    result = check(states(manifest, ledger_on_disk()), states(mutated, agent_bumped))
    assert result.failed
    assert [finding.component for finding in result.findings] == [RELEASE_COMPONENT]


def test_a_sub_agent_change_with_a_bump_passes_and_asks_for_evaluation(
    manifest, reworded_sub_agent
) -> None:
    """The way out: raise both declared versions, and the release may be registered.

    ``need_evaluation`` is why the gate is more than a blocker. §13's *"even a PATCH counts as
    a behaviour change and runs the evaluation"* makes a bump the trigger for the evaluation
    run, so the result must name what to evaluate — a caller reading only ``failed`` would
    skip the very thing the bump was for.
    """
    ledger = with_versions(
        ledger_on_disk(), release=next_release(), agents={PARENT.value: next_agent()}
    )
    mutated = build_test_manifest(ledger=ledger)
    result = check(states(manifest, ledger_on_disk()), states(mutated, ledger))

    assert result.ok
    assert result.changed == (PARENT_COMPONENT, RELEASE_COMPONENT)
    assert set(result.need_evaluation) == {PARENT_COMPONENT, RELEASE_COMPONENT}


def test_the_manifest_carries_the_bumped_version_and_not_the_old_one(
    manifest, reworded_sub_agent
) -> None:
    """The declaration the gate reads is the one in the ledger the build was given."""
    ledger = with_versions(ledger_on_disk(), agents={PARENT.value: next_agent("minor")})
    mutated = build_test_manifest(ledger=ledger)
    assert agent_named(mutated, card_name(PARENT)).version == next_agent("minor")
    assert agent_named(mutated, card_name(SIBLING)).version == ledger_on_disk().agent(SIBLING.value)
    assert agent_named(manifest, card_name(PARENT)).version == ledger_on_disk().agent(PARENT.value)


def test_a_patch_bump_counts_as_a_behaviour_change(manifest, reworded_sub_agent) -> None:
    """``1.0.0`` to ``1.0.1`` is enough. §13 does not ask for a minor or a major."""
    baseline = states(manifest, ledger_on_disk())
    for agent_version in (next_agent(), next_agent("minor"), next_agent("major")):
        ledger = with_versions(
            ledger_on_disk(), release=next_release(), agents={PARENT.value: agent_version}
        )
        result = check(baseline, states(build_test_manifest(ledger=ledger), ledger))
        assert not result.failed, agent_version
        assert PARENT_COMPONENT in result.need_evaluation, agent_version


def test_a_prerelease_is_not_an_increase(manifest, reworded_sub_agent) -> None:
    """``1.0.0`` to ``1.0.0-rc.1`` moved sideways, and the gate says so.

    A version that did not *increase* would otherwise satisfy the gate while changing
    behaviour, which is the hole §13's "even a PATCH counts" exists to close.
    """
    ledger = with_versions(
        ledger_on_disk(), agents={PARENT.value: ledger_on_disk().agent(PARENT.value) + "-rc.1"}
    )
    result = check(
        states(manifest, ledger_on_disk()), states(build_test_manifest(ledger=ledger), ledger)
    )
    assert result.failed
    assert PARENT_COMPONENT in {finding.component for finding in result.findings}


# ── the directive: the gap the card hash has ────────────────────────────────────────


def test_the_card_hash_does_not_see_the_directive_the_manifest_does(
    manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The justification for two fields in the §13 manifest, demonstrated rather than described.

    The directive is appended to Provider Matching's prompt at assembly time. Rewording it
    changes what the model is told; ``cards.agent_hash`` is unmoved, because the payload it
    hashes contains the sub-agent *names* and not this text. A peer reading the card cannot
    tell, and the manifest can.
    """
    subagents = rfc_module("referraleasy_agents.subagents")
    monkeypatch.setattr(
        subagents,
        "SUB_AGENT_DIRECTIVE",
        f"{subagents.SUB_AGENT_DIRECTIVE}\nNever propose a candidate you cannot cite.",
    )
    mutated = build_test_manifest()
    before = agent_named(manifest, card_name(PARENT))
    after = agent_named(mutated, card_name(PARENT))

    # The card's own published number is unchanged — the gap, shown rather than asserted.
    assert agent_hash(PARENT) == before.agent_hash == after.agent_hash
    assert after.a2a_card_hash == before.a2a_card_hash
    # The manifest moves, because it hashes the instruction the model is actually given.
    assert after.instruction_hash != before.instruction_hash
    assert after.sub_agent_manifest_hash != before.sub_agent_manifest_hash
    assert agent_artifact_hash(after) != agent_artifact_hash(before)


def test_the_directive_reaches_the_parents_instruction_and_no_one_elses() -> None:
    """``assembly.build_agent`` appends the directive for Provider Matching alone.

    ``full_instruction`` restates that assembly, so this test is what keeps the two from
    drifting: if the assembler started appending the directive to a second role, the
    manifest would still hash only Provider Matching's and the omission would be silent.
    """
    from referraleasy_release_registry.artifacts import full_instruction

    directive = rfc_module("referraleasy_agents.subagents").SUB_AGENT_DIRECTIVE
    assert directive in full_instruction(PARENT)
    for role in AgentRole:
        if role is PARENT:
            continue
        assert directive not in full_instruction(role), role.value


# ── tools and skills: the other two per-agent inputs ────────────────────────────────


def test_a_tool_description_change_moves_only_the_agents_that_can_call_it(
    manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool description is part of each agent that may call the tool.

    ``find_candidates`` is Provider Matching's alone, so the change is attributed to it and
    to nobody else — including ``search_directory``, which both Provider Matching and
    Recovery Handoff may call.
    """
    specs = rfc_module("referraleasy_agents.tools.specs")
    original = specs.TOOL_SPECS
    monkeypatch.setattr(
        specs,
        "TOOL_SPECS",
        {
            **original,
            "find_candidates": dataclasses.replace(
                original["find_candidates"],
                summary=f"{original['find_candidates'].summary} Prefer the newest evidence.",
            ),
        },
    )
    mutated = build_test_manifest()

    before = agent_named(manifest, card_name(PARENT))
    after = agent_named(mutated, card_name(PARENT))
    assert (
        after.tool_description_hashes["find_candidates"]
        != before.tool_description_hashes["find_candidates"]
    )
    assert (
        after.tool_description_hashes["search_directory"]
        == before.tool_description_hashes["search_directory"]
    )
    assert agent_artifact_hash(after) != agent_artifact_hash(before)
    for role in AgentRole:
        if role is PARENT:
            continue
        assert agent_artifact_hash(agent_named(mutated, card_name(role))) == agent_artifact_hash(
            agent_named(manifest, card_name(role))
        ), role.value


def test_a_skill_body_change_moves_its_owner_and_not_a_sibling(
    manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill is read from its package, so a body edit is seen without a version bump.

    ``directory-grounding`` belongs to Provider Matching. Rewriting one line of its
    ``SKILL.md`` must move that agent's hash — this is the §13 field that connects
    ``packages/skills`` to a release — and must not move Scheduling's.
    """
    loader = rfc_module("referraleasy_skills")
    original = loader.load_skill

    def load_skill(name: str):
        skill = original(name)
        if name != "directory-grounding":
            return skill
        return dataclasses.replace(skill, instructions=f"{skill.instructions}\nCheck the NPI.")

    monkeypatch.setattr(loader, "load_skill", load_skill)
    mutated = build_test_manifest()
    artifact = skill_artifacts()["directory-grounding"]

    before = agent_named(manifest, card_name(PARENT))
    after = agent_named(mutated, card_name(PARENT))
    assert artifact.owner_agent == PARENT.value
    assert (
        before.skill_hashes[artifact.agent_card_skill_id]
        != after.skill_hashes[artifact.agent_card_skill_id]
    )
    assert agent_artifact_hash(after) != agent_artifact_hash(before)
    assert agent_artifact_hash(agent_named(mutated, card_name(SIBLING))) == agent_artifact_hash(
        agent_named(manifest, card_name(SIBLING))
    )


def test_a_skill_version_is_not_part_of_its_content_hash_but_the_ledger_must_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two properties that look like one, and the reason a version bump is a separate act.

    A version changes nothing a model reads, so it is excluded from the content hash —
    otherwise bumping a skill's version would make its owner look changed and trip the gate
    for no behaviour change. But a skill's version is *also* declared in
    ``registry/versions.json``, so raising it in the package without raising it in the ledger
    leaves two answers to one question, and the build refuses rather than picking one.
    """
    from referraleasy_release_registry.errors import VersionLedgerError

    loader = rfc_module("referraleasy_skills")
    original = loader.load_manifest
    content_before = skill_artifacts()["booking-confirmation"].content_hash

    def load_manifest(name: str):
        manifest = original(name)
        if name != "booking-confirmation":
            return manifest
        raw = {**manifest.raw, "version": "2.0.0"}
        return dataclasses.replace(manifest, version="2.0.0", raw=raw)

    monkeypatch.setattr(loader, "load_manifest", load_manifest)
    after = skill_artifacts()["booking-confirmation"]
    assert after.version == "2.0.0"
    assert after.content_hash == content_before, (
        "a version is a declaration, not content; the content hash must not follow it"
    )
    with pytest.raises(VersionLedgerError) as refusal:
        build_test_manifest()
    assert "booking-confirmation" in str(refusal.value)


# ── the record the gate keys on ─────────────────────────────────────────────────────


def test_every_per_agent_field_is_covered_by_the_gate_s_unit(manifest) -> None:
    """``agent_artifact_hash`` moves for a change to *any* per-agent field.

    This is what makes keying the gate on the whole record rather than on the card's
    ``agent_hash`` load-bearing, so it is asserted field by field rather than argued.
    """
    agent = manifest.agents[0]
    for field in (
        "version",
        "agent_hash",
        "instruction_hash",
        "tool_description_hashes",
        "skill_hashes",
        "sub_agent_manifest_hash",
        "a2a_card_hash",
    ):
        changed = agent.model_copy(update={field: None})
        assert agent_artifact_hash(changed) != agent_artifact_hash(agent), field


def test_the_release_hash_is_not_the_sum_of_its_agents(manifest) -> None:
    """The release-level component is its own artifact, not a digest of the agent list.

    It carries the provider, guardrail, rag and source blocks, which belong to no single
    agent — so a deployment that switched embedding model would move ``referraleasy`` and no
    ``agent-*`` component, and the gate must still see something changed.
    """
    other = build_test_manifest(env={"EMBEDDING_MODEL_ID": "test/another-embedding"})
    assert check(
        component_states(manifest, ledger_on_disk()),
        component_states(other, ledger_on_disk()),
    ).failed, "changing the embedding model is a behaviour change and must not pass the gate"
    for role in AgentRole:
        assert agent_artifact_hash(agent_named(other, card_name(role))) == agent_artifact_hash(
            agent_named(manifest, card_name(role))
        ), role.value


def test_the_instruction_hash_is_a_digest_of_the_assembled_instruction(manifest) -> None:
    """The field names an artifact, and this is which one: what the model is handed.

    Recomputing it here is not redundant with the build. The build could hash the briefing
    without the directive, or the briefing plus a skills block the assembler never adds, and
    every other test in this module would still pass.
    """
    from referraleasy_release_registry.artifacts import full_instruction

    for role in AgentRole:
        agent = agent_named(manifest, card_name(role))
        assert agent.instruction_hash == text_digest(full_instruction(role)), role.value
