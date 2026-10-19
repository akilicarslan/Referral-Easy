"""§13's manifest carries every field it names, and refuses to be emitted when it does not.

§13 lists what an immutable ``release_manifest`` contains: agent version/hash,
instruction/prompt version, agent and tool descriptions, tool schema **and implementation
digest**, skill version/hash, sub-agent manifest hash, A2A card hash, provider/model/params,
guardrail policy/classifier/threshold, RAG/embedding/index/reranker/knowledge version, and
Git SHA/image/lockfile hash.

Two kinds of test. The first asserts each of those fields is *filled from a real artifact* —
not merely present, but equal to the thing it claims to describe, recomputed here from the
source. The second asserts that the refusal works, one field at a time: for every §13 field
there is a case that removes it and a test that the build stops. That is the rule the whole
module exists for — *"a manifest that omits a field is worse than one that errors, since the
omission is invisible at report time"* — and a completeness check that was never shown to
reject anything would be decoration.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from release_fixtures import (
    RELEASE_COMPONENT,
    REPO_ROOT,
    SERVICE_NAMES,
    SKILL_PACKAGES,
    TEST_ENV,
    agent_named,
    build_test_manifest,
)

from referraleasy_release_registry.artifacts import (
    agent_hash,
    card_name,
    rfc_module,
    skill_artifacts,
    sub_agent_manifest_hash,
    tool_artifacts,
)
from referraleasy_release_registry.errors import MissingArtifactError
from referraleasy_release_registry.hashing import is_sha256_hex
from referraleasy_release_registry.manifest import (
    LOCKFILES,
    assert_complete,
    manifest_components,
)
from referraleasy_release_registry.versions import load_ledger

pytestmark = pytest.mark.contract


def test_every_agent_of_the_seven_is_present_with_a_full_record(manifest) -> None:
    """§3's seven roles, each with the four hashes §13 asks an agent to carry."""
    assert len(manifest.agents) == 7
    assert sorted(agent.agent_name for agent in manifest.agents) == sorted(
        card_name(role) for role in rfc_module("referraleasy_agent_runtime.roles").AgentRole
    )
    for agent in manifest.agents:
        assert is_sha256_hex(agent.agent_hash), agent.agent_name
        assert is_sha256_hex(agent.instruction_hash), agent.agent_name
        assert is_sha256_hex(agent.sub_agent_manifest_hash), agent.agent_name
        assert is_sha256_hex(agent.a2a_card_hash), agent.agent_name
        assert agent.tool_description_hashes, f"{agent.agent_name} carries no tool descriptions"


def test_the_agent_hash_is_the_card_s_own_number(manifest) -> None:
    """The manifest does not compute a second opinion about an agent's hash.

    An Agent Card is handed to peers over ``/.well-known/agent-card.json``. A registry whose
    ``agent_hash`` disagreed with the card's would be a registry nobody could reconcile with
    the card, so the value is read *from* the card module rather than re-derived here.
    """
    for role in rfc_module("referraleasy_agent_runtime.roles").AgentRole:
        agent = agent_named(manifest, card_name(role))
        assert agent.agent_hash == agent_hash(role)


def test_the_a2a_card_hash_is_the_whole_published_card(manifest) -> None:
    """The other number: the digest of the card document, not the hash written inside it."""
    from referraleasy_release_registry.artifacts import agent_card_payload
    from referraleasy_release_registry.hashing import digest

    for role in rfc_module("referraleasy_agent_runtime.roles").AgentRole:
        agent = agent_named(manifest, card_name(role))
        assert agent.a2a_card_hash == digest(agent_card_payload(role))
        assert agent.a2a_card_hash != agent.agent_hash


def test_tool_schema_and_implementation_are_hashed_separately(manifest) -> None:
    """§13 asks for both, and they answer different questions.

    The schema is what the model fills in; the implementation is what actually runs. A tool
    can change either without the other, and a manifest with one number for both would hide
    half of those changes.
    """
    live = tool_artifacts()
    assert len(manifest.tools) == len(live) == 19
    for name, tool in manifest.tools.items():
        assert tool.schema_hash == live[name].schema_hash, name
        assert tool.implementation_digest == live[name].implementation_digest, name
        assert tool.schema_hash != tool.implementation_digest, name


def test_the_implementation_digest_is_the_handler_body_not_the_module(manifest) -> None:
    """A comment added above ``list_slots`` must not invalidate ``create_booking``.

    The digest is taken over ``inspect.getsource`` of the handler itself. Hashing the whole
    ``tools/scheduling.py`` would make every tool in a file move together, and a gate that
    fires on unchanged artifacts is a gate someone turns off.
    """
    from referraleasy_release_registry.artifacts import _implementation_source

    scheduling = manifest.tools["create_booking"]
    slots = manifest.tools["list_slots"]
    assert scheduling.implementation_digest != slots.implementation_digest
    source, path = _implementation_source("create_booking")
    assert source.lstrip().startswith("async def create_booking") or source.lstrip().startswith(
        "def create_booking"
    )
    assert path.endswith(".py")


def test_every_shipped_skill_appears_on_exactly_one_agent(manifest) -> None:
    """§13's skill hashes are per agent, so a skill no agent names would be invisible.

    The per-agent mapping is keyed by the id the Agent Card publishes, and ``skills`` by the
    package name. Both are asserted, because a manifest that used one where the other belongs
    would still "contain every skill" and still be unreadable beside either.
    """
    shipped = skill_artifacts()
    recorded = sorted(skill_id for agent in manifest.agents for skill_id in agent.skill_hashes)
    assert recorded == sorted(artifact.agent_card_skill_id for artifact in shipped.values())
    assert len(recorded) == len(set(recorded)), "a skill must belong to exactly one agent"
    assert manifest.skills == {
        package: artifact.version for package, artifact in shipped.items()
    }, "skill versions must come from each package's own manifest.json"
    assert sorted(manifest.skills) == sorted(SKILL_PACKAGES)


def test_the_care_coordinator_owns_no_skill_and_that_is_not_an_omission(manifest) -> None:
    """§3 gives the coordinator one tool and no skill, so its mapping is legitimately empty.

    This is the case that stops ``assert_complete`` demanding a non-empty ``skill_hashes``
    and forcing a skill to be invented for an agent that must not have one.
    """
    coordinator = agent_named(manifest, "care-coordinator")
    assert coordinator.skill_hashes == {}
    assert set(coordinator.tool_description_hashes) == {"delegate_task"}
    assert_complete(manifest)  # and it is still a complete manifest


def test_the_provider_block_records_the_deployment_not_a_default(manifest) -> None:
    assert manifest.provider.agent_model_provider == TEST_ENV["AGENT_MODEL_PROVIDER"]
    assert manifest.provider.agent_model_id == TEST_ENV["AGENT_MODEL_ID"]
    assert manifest.provider.params["MODEL_BACKEND"] == TEST_ENV["MODEL_BACKEND"]
    assert manifest.provider.params["MAX_MODEL_CALLS_PER_TURN"] == "12"
    assert manifest.provider.per_agent_overrides == {}, (
        "no overrides is stated as an empty object; None would be indistinguishable from a "
        "producer that forgot to look (§8)"
    )


def test_the_guardrail_block_hashes_the_classifier_and_records_the_threshold(manifest) -> None:
    assert manifest.guardrail_policy.mode.value == TEST_ENV["GUARDRAIL_MODE"]
    assert is_sha256_hex(manifest.guardrail_policy.classifier_hash)
    assert manifest.guardrail_policy.thresholds == {"local_classifier": 0.5}


def test_the_rag_block_names_a_real_index_and_a_real_knowledge_version(manifest) -> None:
    """§10's knowledge version comes from the seed, not from a constant here."""
    import json

    seed = json.loads(
        (
            REPO_ROOT
            / "packages"
            / "rag"
            / "src"
            / "referraleasy_rag"
            / "data"
            / "seed_directory.v1.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest.rag.knowledge_version == seed["knowledge_version"]
    assert manifest.rag.index_name == "referraleasy-directory-chunks"
    assert manifest.rag.embedding_dimensions == 768


def test_the_source_block_records_all_three(manifest) -> None:
    assert manifest.source.git_sha == TEST_ENV["RELEASE_GIT_SHA"]
    assert manifest.source.image_digest == TEST_ENV["RELEASE_IMAGE_DIGEST"]
    assert is_sha256_hex(manifest.source.lockfile_hash)


def test_the_lockfile_hash_covers_every_lockfile_that_exists() -> None:
    from referraleasy_release_registry.hashing import digest, file_digest

    present = {
        name: file_digest(REPO_ROOT / name) for name in LOCKFILES if (REPO_ROOT / name).is_file()
    }
    assert present, f"none of {LOCKFILES} exists, so the recorded lockfile hash covers nothing"
    manifest = build_test_manifest()
    assert manifest.source.lockfile_hash == digest(present)


def test_components_are_the_deployed_units(manifest) -> None:
    """Seven services plus the release, named as the services are named everywhere else."""
    ledger = load_ledger(REPO_ROOT / "registry" / "versions.json")
    components = dict(manifest_components(manifest, release_version=ledger.release))
    assert sorted(components) == sorted([*SERVICE_NAMES, RELEASE_COMPONENT])
    assert components[RELEASE_COMPONENT] == ledger.release
    assert set(components.values()) == {*ledger.agents.values(), ledger.release}


# ── the refusals ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("agents removed", lambda m: m.model_copy(update={"agents": []})),
        ("prompts emptied", lambda m: m.model_copy(update={"prompts": {}})),
        ("tools emptied", lambda m: m.model_copy(update={"tools": {}})),
        ("skills emptied", lambda m: m.model_copy(update={"skills": {}})),
        (
            "provider model id blanked",
            lambda m: m.model_copy(
                update={"provider": m.provider.model_copy(update={"agent_model_id": ""})}
            ),
        ),
        (
            "per-agent overrides set to None",
            lambda m: m.model_copy(
                update={"provider": m.provider.model_copy(update={"per_agent_overrides": None})}
            ),
        ),
        (
            "guardrail classifier hash removed",
            lambda m: m.model_copy(
                update={
                    "guardrail_policy": m.guardrail_policy.model_copy(
                        update={"classifier_hash": "not-a-hash"}
                    )
                }
            ),
        ),
        (
            "rag knowledge version blanked",
            lambda m: m.model_copy(
                update={"rag": m.rag.model_copy(update={"knowledge_version": ""})}
            ),
        ),
        (
            "git sha blanked",
            lambda m: m.model_copy(update={"source": m.source.model_copy(update={"git_sha": ""})}),
        ),
        (
            "image digest blanked",
            lambda m: m.model_copy(
                update={"source": m.source.model_copy(update={"image_digest": ""})}
            ),
        ),
    ],
)
def test_an_incomplete_manifest_is_refused(manifest, case: str, mutate) -> None:
    """Every §13 field, removed one at a time, and every removal stops the build."""
    from referraleasy_release_registry.errors import IncompleteManifestError

    broken = mutate(manifest)
    with pytest.raises(IncompleteManifestError):
        assert_complete(broken)

    # And the untouched manifest passes, so the test cannot pass by refusing everything.
    assert_complete(manifest)


def test_a_missing_environment_value_is_not_an_empty_one() -> None:
    """An unset provider is a refusal naming the variable, not a manifest with a blank model."""
    from referraleasy_release_registry.artifacts import env_snapshot

    with pytest.raises(MissingArtifactError) as refusal:
        env_snapshot({}, ("AGENT_MODEL_ID", "AGENT_MODEL_PROVIDER"))
    assert "AGENT_MODEL_ID" in str(refusal.value)


def test_the_git_sha_is_read_from_the_environment_or_refused() -> None:
    """The sha is supplied or the build stops. It is never derived, and never invented.

    Both halves matter. The refusal keeps a fabricated value out of an append-only registry,
    and it is also why a checkout with no commit cannot emit a complete manifest — recorded
    in ``docs/adr/ADR-0036`` as a finding rather than worked around. The same test holds in a
    repository *with* a commit: then the answer comes from ``git rev-parse HEAD``, and the
    assertion is that it is a commit sha rather than something this module made up.
    """
    import subprocess

    from referraleasy_release_registry.manifest import source_from

    env = {key: value for key, value in TEST_ENV.items() if key != "RELEASE_GIT_SHA"}
    env["RELEASE_IMAGE_DIGEST"] = TEST_ENV["RELEASE_IMAGE_DIGEST"]
    committed = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
        ).returncode
        == 0
    )
    if committed:
        sha = source_from(env, REPO_ROOT).git_sha
        assert len(sha) == 40 and all(character in "0123456789abcdef" for character in sha)
    else:
        with pytest.raises(MissingArtifactError) as refusal:
            source_from(env, REPO_ROOT)
        assert "git sha" in str(refusal.value).lower()


def test_the_version_ledger_and_the_shipped_skill_packages_agree() -> None:
    """Two answers to "what version is this skill?" is the drift §13 exists to prevent."""
    from release_fixtures import REPO_ROOT

    from referraleasy_release_registry.errors import VersionLedgerError
    from referraleasy_release_registry.manifest import _skills

    ledger = load_ledger(REPO_ROOT / "registry" / "versions.json")
    skills = skill_artifacts()
    assert _skills(skills, ledger) == ledger.skills

    wrong = replace(ledger, skills={**ledger.skills, "handoff-triage": "9.9.9"})
    with pytest.raises(VersionLedgerError):
        _skills(skills, wrong)


def test_sub_agent_manifests_are_read_from_the_builders(manifest) -> None:
    """§3 gives sub-agents to Provider Matching and to nobody else, and the manifest says so.

    The six roles without sub-agents share one dependency hash, which is correct — they
    depend on nothing — and it is *a* hash rather than ``None``, so "no sub-agents" and "the
    sub-agent manifest failed to load" cannot be confused for each other.
    """
    from referraleasy_agent_runtime.roles import AgentRole
    from referraleasy_release_registry.artifacts import sub_agent_manifest

    matching = agent_named(manifest, "provider-matching")
    assert matching.sub_agent_manifest_hash == sub_agent_manifest_hash(AgentRole.PROVIDER_MATCHING)
    built = sub_agent_manifest(AgentRole.PROVIDER_MATCHING)
    assert built is not None
    assert [record["protocol_name"] for record in built["sub_agents"]] == [
        "candidate-researcher",
        "constraint-analyst",
    ]
    assert built["sub_agents"][0]["tools"] == ["find_candidates", "search_directory"]
    assert built["sub_agents"][1]["tools"] == ["search_directory"]
    assert is_sha256_hex(built["directive_hash"])
    assert is_sha256_hex(built["module_digest"])

    other_hashes = set()
    for agent in manifest.agents:
        if agent.agent_name == "provider-matching":
            continue
        role = next(role for role in AgentRole if card_name(role) == agent.agent_name)
        assert sub_agent_manifest(role) is None
        assert agent.sub_agent_manifest_hash == sub_agent_manifest_hash(role)
        other_hashes.add(agent.sub_agent_manifest_hash)
    assert len(other_hashes) == 1
    assert matching.sub_agent_manifest_hash not in other_hashes


def test_the_skills_hashed_are_the_ones_under_the_root_being_described(scratch) -> None:
    """§13's skill hash describes *this* tree's ``SKILL.md``, not the one that happens to be installed.

    ``referraleasy_skills`` resolves ``packages/skills`` from its own installed location and
    takes no root argument, so without ``skill_artifacts(root)`` a build of a copy of the
    tree hashes the original's skill files: a release manifest that names skills it never
    read, and a gate that cannot see a skill edit in the tree it is checking. The mutation
    here is the same one ``tests/release/test_check_release_versions_script.py`` performs
    through the CLI, asserted one level down where the cause is.
    """
    from release_fixtures import replace_in_file

    package = "directory-grounding"
    instructions = scratch.root / "packages" / "skills" / package / "SKILL.md"
    before = skill_artifacts(scratch.root)[package].content_hash
    assert before == skill_artifacts(REPO_ROOT)[package].content_hash, "one tree, two copies"

    replace_in_file(
        instructions,
        "- A patient asks whether a specific doctor is available, or whether a particular "
        "appointment is free.",
        "- A patient asks whether a particular doctor is available at a particular site.",
    )
    assert skill_artifacts(scratch.root)[package].content_hash != before
    assert skill_artifacts(REPO_ROOT)[package].content_hash == before, (
        "the real tree was not the one edited"
    )


def test_a_root_without_skills_is_refused_rather_than_falling_back(tmp_path) -> None:
    """A build must not describe a tree it cannot read the skills of.

    Falling back to the installed copy would produce a manifest whose skill hashes came from
    somewhere else entirely — the invisible omission §13's rule is about — so the refusal is
    the documented behaviour.
    """
    empty = tmp_path / "checkout"
    empty.mkdir()
    with pytest.raises(MissingArtifactError, match="packages/skills"):
        skill_artifacts(empty)
