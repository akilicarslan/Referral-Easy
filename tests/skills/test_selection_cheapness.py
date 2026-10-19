"""Selection is cheap, and it is separate from loading.

Skills are selected, not all loaded. A selector runs on every turn against every candidate, so
reading eight instruction bodies to decide which one to read is both the cost the format exists to
avoid and a subtler problem: it makes the selection decision depend on content that was supposed to
be loaded only after the decision.

The loader reads files through ``Path.read_text`` and nothing else, which is what lets the spy tests
below be exact rather than approximate.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from skills_fixtures import EXPECTED_SKILLS

from referraleasy_skills import (
    INSTRUCTIONS_FILENAME,
    SkillManifestError,
    list_manifests,
    list_skills,
    load_manifest,
    load_skill,
    select,
)

pytestmark = pytest.mark.contract

LOADER_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "skills"
    / "src"
    / "referraleasy_skills"
    / "loader.py"
)


def test_a_manifest_loads_with_no_instruction_body_present(
    isolate_skills: Callable[..., Path],
) -> None:
    """The instruction body is deleted from disk; selection must not notice."""
    isolate_skills(EXPECTED_SKILLS, drop=[INSTRUCTIONS_FILENAME])

    for name in EXPECTED_SKILLS:
        manifest = load_manifest(name)
        assert manifest.name == name
        assert manifest.summary
        assert manifest.when_to_use


def test_listing_and_selecting_work_with_no_instruction_body_present(
    isolate_skills: Callable[..., Path],
) -> None:
    isolate_skills(EXPECTED_SKILLS, drop=[INSTRUCTIONS_FILENAME])

    assert list_skills() == sorted(EXPECTED_SKILLS)
    assert len(list_manifests()) == len(EXPECTED_SKILLS)
    assert "booking-confirmation" in {manifest.name for manifest in select("confirm booking")}


def test_full_load_does_require_the_instruction_body(
    isolate_skills: Callable[..., Path],
) -> None:
    """The split is real: the body is genuinely required, just not for selection."""
    isolate_skills(["booking-confirmation"], drop=[INSTRUCTIONS_FILENAME])

    assert load_manifest("booking-confirmation").name == "booking-confirmation"
    with pytest.raises(SkillManifestError, match=r"no SKILL\.md"):
        load_skill("booking-confirmation")


def test_manifest_loading_never_reads_a_skill_md(
    monkeypatch: pytest.MonkeyPatch, skills_dir: Path
) -> None:
    read_names: list[str] = []
    original = Path.read_text

    def spy(self: Path, *args: object, **kwargs: object) -> str:
        read_names.append(self.name)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", spy)

    for name in EXPECTED_SKILLS:
        load_manifest(name)
    list_manifests()
    select("coverage network check")

    assert read_names, "expected the manifests to be read at all"
    assert INSTRUCTIONS_FILENAME not in read_names, "selection read an instruction body"


def test_the_selector_can_run_from_manifests_alone(
    monkeypatch: pytest.MonkeyPatch, skills_dir: Path
) -> None:
    """Reading anything other than manifest.json during selection is a defect, so nothing else is
    permitted to be readable."""
    original = Path.read_text

    def refuse_anything_but_manifests(self: Path, *args: object, **kwargs: object) -> str:
        if self.name != "manifest.json":
            raise AssertionError(f"selection tried to read {self.name}")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", refuse_anything_but_manifests)

    manifests = list_manifests()
    assert len(manifests) == len(EXPECTED_SKILLS)
    assert select(limit=3)


def test_a_package_loads_on_its_own(isolate_skills: Callable[..., Path]) -> None:
    """No package depends on a sibling being present."""
    for name in EXPECTED_SKILLS:
        isolate_skills([name])
        assert list_skills() == [name]
        manifest = load_manifest(name)
        assert manifest.name == name
        assert load_skill(name).instructions.strip()


def test_a_missing_reference_is_refused_at_selection_time(
    isolate_skills: Callable[..., Path],
) -> None:
    """A package that cites a file it does not ship must fail before it is selected, not after."""
    root = isolate_skills(["coverage-interpretation"])
    reference = root / "coverage-interpretation" / "references" / "conflict-worked-example.md"
    reference.unlink()

    with pytest.raises(SkillManifestError, match="cites a missing reference"):
        load_manifest("coverage-interpretation")


def test_select_filters_by_owning_agent_role() -> None:
    scheduling = select(owner_agent="scheduling")
    assert {manifest.name for manifest in scheduling} == {
        "booking-confirmation",
        "uncertain-booking-reconciliation",
    }

    matching = select(owner_agent="provider_matching")
    assert {manifest.name for manifest in matching} == {
        "constraint-negotiation",
        "directory-grounding",
    }


def test_select_filters_by_tag() -> None:
    tagged = select(tag="consent")
    assert {"constraint-negotiation", "booking-confirmation"} <= {
        manifest.name for manifest in tagged
    }


def test_select_is_deterministic() -> None:
    """The effective skill list is written to the trace, so its order cannot wobble."""
    first = [manifest.name for manifest in select("booking")]
    second = [manifest.name for manifest in select("booking")]
    assert first == second


def test_select_without_a_query_returns_everything_in_a_stable_order() -> None:
    names = [manifest.name for manifest in select()]
    assert names == sorted(EXPECTED_SKILLS)


def test_select_respects_its_limit() -> None:
    assert len(select(limit=2)) == 2


def test_every_manifest_carries_what_a_selector_needs(manifests: dict[str, object]) -> None:
    for name, manifest in manifests.items():
        assert manifest.summary, name  # type: ignore[attr-defined]
        assert len(manifest.when_to_use) >= 3, name  # type: ignore[attr-defined]
        assert manifest.owner_agent, name  # type: ignore[attr-defined]
        assert manifest.tags, name  # type: ignore[attr-defined]


def test_the_loader_imports_nothing_outside_the_standard_library() -> None:
    """The selector runs on every turn. A heavy import here is a cost paid on every one of them."""
    source = LOADER_SOURCE.read_text(encoding="utf-8")
    roots = {
        match.group(1).split(".")[0]
        for match in re.finditer(r"^(?:import|from)\s+([A-Za-z_][\w.]*)", source, re.MULTILINE)
    }
    non_stdlib = sorted(root for root in roots if root not in sys.stdlib_module_names)
    assert not non_stdlib, f"loader imports outside the standard library: {non_stdlib}"


def test_the_skills_package_declares_no_runtime_dependencies() -> None:
    pyproject = LOADER_SOURCE.parents[2] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert re.search(r"^dependencies\s*=\s*\[\s*\]", text, re.MULTILINE), (
        "packages/skills must stay dependency-free"
    )


def test_synthetic_values_only(manifests: dict[str, object]) -> None:
    """Nothing in a package may look like a real patient identifier or a credential."""
    import json

    for name, manifest in manifests.items():
        blob = json.dumps(manifest.raw)  # type: ignore[attr-defined]
        for pattern in (r"\bsk-[A-Za-z0-9]", r"\bAIza[0-9A-Za-z_-]{10,}", r"\bBearer\s"):
            assert not re.search(pattern, blob), f"{name}: possible secret in manifest"
