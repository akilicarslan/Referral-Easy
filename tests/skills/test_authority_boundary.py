"""Skills are instructions, not authority.

The boundary this file defends: adding or following a skill package must never widen what a model
is allowed to do. Tool authority lives in the runtime allowlist and in the database policy table,
which is keyed by workflow state and which the package format has no way to reach.

The tests fail if a manifest grows a way to name a tool, a permission or a scope, if the loader ever
accepts a manifest that claims one, or if an instruction body tells a reader that following it
authorises a call.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from skills_fixtures import EXPECTED_SKILLS

from referraleasy_skills import (
    AUTHORITY_INSTRUCTIONS_ONLY,
    FORBIDDEN_MANIFEST_KEYS,
    INSTRUCTIONS_FILENAME,
    SkillManifest,
    SkillManifestError,
    load_manifest,
)

pytestmark = pytest.mark.contract

# Phrases that would assert a skill can grant authority. Matched against a collapsed, lowercased
# body, so line wrapping in the Markdown cannot hide one.
AUTHORITY_GRANTING_PHRASES: tuple[str, ...] = (
    "grants permission",
    "grants the tool",
    "grants access",
    "you now have access",
    "you may now call",
    "this skill authorises",
    "this skill authorizes",
    "is authorised to call",
    "is authorized to call",
    "bypass the policy table",
    "override the policy table",
    "skip the policy check",
    "ignore the policy table",
    "permission to call",
)

# Phrases that would let a document direct the reader. Documents are data, never instructions.
DOCUMENT_AS_INSTRUCTION_PHRASES: tuple[str, ...] = (
    "follow the instructions in the document",
    "obey the document",
    "the document instructs you to",
    "as the document says, call",
)


def collapse(text: str) -> str:
    """Lowercase, drop Markdown emphasis, and collapse all whitespace to single spaces."""
    flat = re.sub(r"[`*]", "", text)
    return re.sub(r"\s+", " ", flat).lower()


def walk_keys(value: object) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.append(str(key))
            keys.extend(walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(walk_keys(item))
    return keys


def test_no_manifest_grants_tool_permissions(manifests: dict[str, SkillManifest]) -> None:
    for name, manifest in manifests.items():
        assert manifest.grants_tool_permissions is False, name
        assert manifest.raw["grants_tool_permissions"] is False, name


def test_every_manifest_states_that_it_carries_instructions_only(
    manifests: dict[str, SkillManifest],
) -> None:
    for name, manifest in manifests.items():
        assert manifest.authority == AUTHORITY_INSTRUCTIONS_ONLY, name


def test_no_manifest_names_a_tool_a_permission_or_an_agent_card_field(
    manifests: dict[str, SkillManifest],
) -> None:
    """Checked against the raw JSON, recursively, not against the parsed projection."""
    for name, manifest in manifests.items():
        offending = sorted(set(walk_keys(manifest.raw)) & FORBIDDEN_MANIFEST_KEYS)
        assert not offending, f"{name} publishes forbidden keys: {offending}"


def test_the_loader_refuses_a_manifest_that_claims_tool_permissions(
    isolate_skills: Callable[..., Path],
) -> None:
    """The boundary is enforced by the loader, not only asserted by the manifests."""
    root = isolate_skills(["booking-confirmation"])
    manifest_path = root / "booking-confirmation" / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["grants_tool_permissions"] = True
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SkillManifestError, match="must be the boolean false"):
        load_manifest("booking-confirmation")


def test_the_loader_refuses_an_authority_other_than_instructions_only(
    isolate_skills: Callable[..., Path],
) -> None:
    root = isolate_skills(["handoff-triage"])
    manifest_path = root / "handoff-triage" / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["authority"] = "may_call_tools"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SkillManifestError, match="authority must be"):
        load_manifest("handoff-triage")


def test_the_loader_refuses_a_manifest_that_declares_a_tool_list(
    isolate_skills: Callable[..., Path],
) -> None:
    """An Agent Card has a ``tools`` array. A package must not, or the two records would merge."""
    root = isolate_skills(["booking-confirmation"])
    manifest_path = root / "booking-confirmation" / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["tools"] = ["create_booking"]
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SkillManifestError, match="carries keys it may not"):
        load_manifest("booking-confirmation")


def test_no_body_claims_that_following_it_grant_authority(bodies: dict[str, str]) -> None:
    for name, body in bodies.items():
        flat = collapse(body)
        for phrase in AUTHORITY_GRANTING_PHRASES:
            assert phrase not in flat, f"{name} appears to grant authority: {phrase!r}"


def test_no_body_treats_a_document_as_an_instruction(bodies: dict[str, str]) -> None:
    for name, body in bodies.items():
        flat = collapse(body)
        for phrase in DOCUMENT_AS_INSTRUCTION_PHRASES:
            assert phrase not in flat, f"{name} treats document text as a directive: {phrase!r}"


def test_every_body_repoints_authority_to_the_policy_table(bodies: dict[str, str]) -> None:
    """Each body must say where authority actually lives, not merely stay silent about it."""
    for name, body in bodies.items():
        flat = collapse(body)
        assert "does not authorise a tool call" in flat, (
            f"{name} does not state that the package confers no tool authority"
        )
        assert "policy table" in flat, f"{name} does not name the policy table as the authority"


def test_every_package_has_a_body_to_instruct_with(skills_dir: Path) -> None:
    for name in EXPECTED_SKILLS:
        assert (skills_dir / name / INSTRUCTIONS_FILENAME).is_file(), name
