"""Fixtures for the §13 release registry tests.

Fixtures only. The constants, the builders and the ``sys.path`` bridge live in
``release_fixtures.py``; import them from there rather than from here, for the reason that
module explains and ``tests/test_test_layout.py`` enforces.

``manifest`` is session-scoped and expensive on purpose: building it walks the seven agent
service entry points, reads nineteen tool handlers, loads eight skill packages and parses
the two sub-agent builders. Doing that once means every test that merely *reads* §13's
manifest shares one, and the tests that must change something build their own — which is
exactly the split the module docstrings below follow.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from release_fixtures import (
    REPO_ROOT,
    ScratchRepo,
    build_test_manifest,
    scratch_repo,
)

from referraleasy_contracts.generated.workflow import ReleaseManifest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    assert (REPO_ROOT / "registry" / "versions.json").is_file(), (
        "the §13 version ledger is missing; the manifest cannot be built without it"
    )
    return REPO_ROOT


@pytest.fixture(scope="session")
def manifest(repo_root: Path) -> ReleaseManifest:
    """One manifest for the whole session, built from the real tree."""
    return build_test_manifest(root=repo_root)


@pytest.fixture
def scratch(tmp_path: Path) -> Iterator[ScratchRepo]:
    """A throwaway copy of the trees the build reads.

    Function-scoped, not session-scoped: every test that uses it *mutates* it, and a shared
    scratch tree would let one test's mutation become another test's baseline.
    """
    yield scratch_repo(tmp_path)
