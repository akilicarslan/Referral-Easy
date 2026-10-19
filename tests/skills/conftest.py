"""Fixtures for the skill package tests.

Fixtures only. The shared constants and the `sys.path` setup live in `skills_fixtures.py`, and test
modules must import them from there rather than from here — importing from a `conftest` module by
name is order-dependent and silently picks up another directory's conftest. That file explains it.

The content assertions read the packages from the source tree directly rather than through
``skills_root()``, so they cannot be affected by the environment override that the loader-behaviour
tests use to point at temporary copies.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from skills_fixtures import (
    EXPECTED_SKILLS,
    SKILLS_DIR,
)

from referraleasy_skills import (
    INSTRUCTIONS_FILENAME,
    SkillManifest,
    load_manifest,
)


@pytest.fixture(scope="session")
def skills_dir() -> Path:
    assert SKILLS_DIR.is_dir(), f"skills directory is missing: {SKILLS_DIR}"
    return SKILLS_DIR


@pytest.fixture(scope="session")
def manifests(skills_dir: Path) -> dict[str, SkillManifest]:
    return {name: load_manifest(name) for name in EXPECTED_SKILLS}


@pytest.fixture(scope="session")
def bodies(skills_dir: Path) -> dict[str, str]:
    return {
        name: (skills_dir / name / INSTRUCTIONS_FILENAME).read_text(encoding="utf-8")
        for name in EXPECTED_SKILLS
    }


@pytest.fixture
def isolate_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skills_dir: Path
) -> Callable[..., Path]:
    """Copy named packages into a temporary skills root and point the loader at it.

    ``drop`` names files to leave out, which is how the "a manifest loads without the instruction
    body" test constructs a tree that has a manifest and no ``SKILL.md``.

    Each call gets its own root, so a test that isolates several packages in a loop sees one root
    per iteration rather than the accumulation of all of them.
    """

    def _isolate(names: Iterable[str], *, drop: Iterable[str] = ()) -> Path:
        root = tmp_path / f"skills-{len(list(tmp_path.glob('skills-*')))}"
        root.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns(*drop) if tuple(drop) else None
        for name in names:
            shutil.copytree(skills_dir / name, root / name, ignore=ignore)
        monkeypatch.setenv("REFERRALEASY_SKILLS_ROOT", str(root))
        return root

    return _isolate
