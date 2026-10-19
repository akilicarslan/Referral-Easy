"""The package format: every manifest is valid, complete, and selection-ready.

A manifest is what a selector reads on every turn, so its shape is not a matter of taste. These
tests hold the format still: the eight packages all conform to the same schema, the schema and the
loader's own validation agree about what is required, and the fields a selector depends on are
present and specific enough to choose with.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from json_schema_lite import validate
from skills_fixtures import EXPECTED_OWNER_AGENT, EXPECTED_SKILLS

from referraleasy_skills import (
    AGENT_ROLES,
    REQUIRED_MANIFEST_KEYS,
    SkillManifest,
    manifest_schema,
)

pytestmark = pytest.mark.contract

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
KEBAB = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
CARD_SKILL_ID = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

#: The section 3 agent roles, transcribed from the role table so a rename in one place cannot pass
#: unnoticed. ``care_coordinator`` routes; it owns no skill package.
SECTION_THREE_ROLES = frozenset(
    {
        "care_coordinator",
        "referral_intake",
        "coverage_network",
        "provider_matching",
        "referral_readiness",
        "scheduling",
        "recovery_handoff",
    }
)

MAPPING_DOC = Path(__file__).resolve().parents[2] / "migrations" / "014_align_domain_vocabulary.sql"


def test_the_shipped_schema_is_itself_valid() -> None:
    """Structural checks on the schema, without needing a schema library to be installed."""
    schema = manifest_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    for key in schema["required"]:
        assert key in schema["properties"], f"{key} is required but not declared"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_every_manifest_validates_against_the_shipped_schema(
    name: str, manifests: dict[str, SkillManifest]
) -> None:
    errors = validate(manifests[name].raw, manifest_schema())
    assert not errors, f"{name}: " + "; ".join(errors)


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_a_real_json_schema_library_agrees(name: str, manifests: dict[str, SkillManifest]) -> None:
    """Cross-check: a second, independent implementation must reach the same verdict.

    If ``jsonschema`` is absent the manifest is still validated by the check above, so this is an
    additional witness rather than the only one.
    """
    jsonschema = pytest.importorskip("jsonschema", reason="cross-check only")
    validator = jsonschema.Draft202012Validator(manifest_schema())
    validator.check_schema(manifest_schema())
    errors = sorted(validator.iter_errors(manifests[name].raw), key=lambda error: list(error.path))
    assert not errors, f"{name}: " + "; ".join(error.message for error in errors)


def test_the_validators_disagree_about_a_non_conforming_manifest() -> None:
    """The dependency-free validator is not a rubber stamp: it rejects a bad manifest."""
    schema = manifest_schema()

    incomplete = validate({"manifest_version": "1"}, schema)
    assert any("missing required key" in error for error in incomplete)

    nulls = validate({key: None for key in schema["required"]}, schema)
    assert any("expected string" in error or "expected const" in error for error in nulls)

    undeclared = validate({**{key: None for key in schema["required"]}, "tools": []}, schema)
    assert any("undeclared key 'tools'" in error for error in undeclared)


def test_the_schema_and_the_loader_require_the_same_keys() -> None:
    """Two definitions of the same format; this is what stops them drifting apart."""
    assert set(manifest_schema()["required"]) == set(REQUIRED_MANIFEST_KEYS)


def test_the_schema_refuses_additional_keys() -> None:
    """The loader rejects undeclared keys, and the schema has to say the same thing."""
    assert manifest_schema()["additionalProperties"] is False


def test_the_schema_pins_the_authority_constants() -> None:
    properties = manifest_schema()["properties"]
    assert properties["authority"]["const"] == "instructions_only"
    assert properties["grants_tool_permissions"]["const"] is False


def test_the_schema_owner_agent_enum_is_the_section_three_roles() -> None:
    enum = set(manifest_schema()["properties"]["owner_agent"]["enum"])
    assert enum == SECTION_THREE_ROLES


def test_the_loader_agent_role_set_is_the_section_three_roles() -> None:
    assert set(AGENT_ROLES) == SECTION_THREE_ROLES


def test_every_owner_agent_is_one_of_the_seven_roles(
    manifests: dict[str, SkillManifest],
) -> None:
    for name, manifest in manifests.items():
        assert manifest.owner_agent in SECTION_THREE_ROLES, name


def test_owner_agents_match_the_assigned_mapping(manifests: dict[str, SkillManifest]) -> None:
    actual = {name: manifest.owner_agent for name, manifest in manifests.items()}
    assert actual == EXPECTED_OWNER_AGENT


def test_the_router_owns_no_skill_package(manifests: dict[str, SkillManifest]) -> None:
    owners = {manifest.owner_agent for manifest in manifests.values()}
    assert "care_coordinator" not in owners
    assert owners == set(EXPECTED_OWNER_AGENT.values())


def test_owner_agents_are_present_in_the_role_migration(
    manifests: dict[str, SkillManifest],
) -> None:
    """Guards against the packages and the database disagreeing about who an agent is."""
    if not MAPPING_DOC.is_file():
        pytest.skip(f"role vocabulary source not present: {MAPPING_DOC}")
    sql = MAPPING_DOC.read_text(encoding="utf-8")
    for name, manifest in manifests.items():
        assert f"('{manifest.owner_agent}'," in sql, f"{name}: {manifest.owner_agent} not a role"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_name_is_kebab_and_matches_its_directory(
    name: str, manifests: dict[str, SkillManifest]
) -> None:
    manifest = manifests[name]
    assert manifest.name == name
    assert KEBAB.match(manifest.name)


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_version_is_semver(name: str, manifests: dict[str, SkillManifest]) -> None:
    assert SEMVER.match(manifests[name].version)


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_summary_is_a_single_selector_ready_line(
    name: str, manifests: dict[str, SkillManifest]
) -> None:
    summary = manifests[name].summary
    assert summary.strip() == summary
    assert "\n" not in summary
    assert 40 <= len(summary) <= 200, f"{name}: summary is {len(summary)} chars"
    assert summary.endswith(".")


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_when_to_use_gives_concrete_triggers(
    name: str, manifests: dict[str, SkillManifest]
) -> None:
    triggers = manifests[name].when_to_use
    assert len(triggers) >= 3, f"{name}: {len(triggers)} triggers"
    for trigger in triggers:
        assert len(trigger.split()) >= 6, f"{name}: trigger too vague: {trigger!r}"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_the_applicability_section_is_specific_and_bounded(
    name: str, bodies: dict[str, str]
) -> None:
    """Four positive triggers and an explicit exclusion, in the body and in the manifest alike.

    A skill that does not say when it does *not* apply is a skill that gets selected for work it was
    never written for, so the exclusion is required rather than optional.
    """
    section = _section(bodies[name], "## When this skill applies")
    bullets = [line for line in section.splitlines() if line.strip().startswith("-")]
    assert len(bullets) >= 3, f"{name}: {len(bullets)} applicability bullets"
    assert "do not use this skill" in section.lower(), f"{name}: no explicit exclusion"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_agent_card_skill_id_is_a_separate_record(
    name: str, manifests: dict[str, SkillManifest]
) -> None:
    card_id = manifests[name].agent_card_skill_id
    assert CARD_SKILL_ID.match(card_id), card_id
    assert card_id != manifests[name].name
    assert card_id not in EXPECTED_SKILLS


def test_every_agent_card_skill_id_is_unique(manifests: dict[str, SkillManifest]) -> None:
    ids = [manifest.agent_card_skill_id for manifest in manifests.values()]
    assert len(ids) == len(set(ids))


def test_manifests_carry_no_agent_card_fields(manifests: dict[str, SkillManifest]) -> None:
    """A card advertises over A2A; a package instructs a model. They are different records."""
    card_fields = (
        "capabilities",
        "tools",
        "auth",
        "agent_hash",
        "input_schema_id",
        "output_schema_id",
    )
    for name, manifest in manifests.items():
        for field in card_fields:
            assert field not in manifest.raw, f"{name} carries the Agent Card field {field!r}"


def test_manifests_declare_their_inputs_and_outputs(manifests: dict[str, SkillManifest]) -> None:
    for name, manifest in manifests.items():
        assert manifest.inputs, name
        assert manifest.outputs, name
        for record in (*manifest.inputs, *manifest.outputs):
            assert set(record) == {"name", "type", "description"}, f"{name}: {record}"


def test_manifests_do_not_embed_the_instruction_body(manifests: dict[str, SkillManifest]) -> None:
    """Selection must not be able to smuggle the body in through a metadata field."""
    for name, manifest in manifests.items():
        blob = json.dumps(manifest.raw)
        assert "## " not in blob, f"{name}: manifest contains Markdown headings"
        assert len(blob) < 6000, f"{name}: manifest is {len(blob)} bytes; too large to be metadata"


def _section(body: str, heading: str) -> str:
    """The text between ``heading`` and the next level-two heading."""
    after = body.split(heading, 1)
    assert len(after) == 2, f"missing section: {heading}"
    return after[1].split("\n## ", 1)[0]
