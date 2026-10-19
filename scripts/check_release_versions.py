#!/usr/bin/env python
"""CI entry point for §13's version-bump gate (Phase 3.5).

    checks that no artifact changed without its declared version increasing, and that
    every version bump is reported for evaluation.

**Exit codes** — a CI job must be able to tell "the change is wrong" apart from "the check
is broken", because a check that reports both the same way is routed around the first time
the environment is missing a variable:

====  ==================================================================================
0     the gate passed
1     the gate failed: an artifact changed and its **version did not increase**. §13:
      *"CI fails when an affected agent's version did not increase. Even a PATCH
      counts as a behaviour change and runs the evaluation."*
2     usage error: the arguments do not name a command this script has
3     the gate could not run: a missing artifact, an unset environment value, an
      incomplete §13 manifest, or no baseline release to compare against. **Not 0** — a
      missing baseline is not a pass. ``--initial`` says so explicitly for the first
      release of a new registry.
====  ==================================================================================

Usage::

    uv run --no-sync python scripts/check_release_versions.py check
    uv run --no-sync python scripts/check_release_versions.py check \\
        --repo-root . --baseline registry/releases/latest.json
    uv run --no-sync python scripts/check_release_versions.py build \\
        --git-sha "$RELEASE_GIT_SHA" --image-digest "$RELEASE_IMAGE_DIGEST"

``--repo-root`` is the checkout to describe and defaults to the directory this script lives
in, so a CI job that has checked out the repository passes nothing. ``--baseline`` is the
release to compare against and defaults to ``registry/releases/latest.json``. ``--built-at``
fixes the build time, which is what makes two runs over one tree produce byte-identical
files: a gate whose report changes on every run cannot be diffed, and a diff is how a
reviewer sees what a release actually changed.

The logic lives in ``referraleasy_release_registry`` (``gate.py``, ``manifest.py``,
``cli.py``) and is tested by ``tests/release``; this file is the invocation. It carries the
same ``sys.path`` bridge as ``tests/release/release_fixtures.py`` — inserted only when the
package is not importable, and idempotently. The bridge was written because
``packages/release-registry`` was not a member of ``[tool.uv.workspace]``; it is one now
(and appears in the root dependencies and ``PYTHON_SOURCE_ROOTS``), so with a synced
virtualenv the bridge resolves to nothing and does nothing. It is kept because a plain
checkout without ``uv sync`` is still a way to arrive here.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SRC = REPO_ROOT / "packages" / "release-registry" / "src"


def _bridge_release_registry_package() -> None:
    """Make ``referraleasy_release_registry`` importable from a plain checkout.

    The bridge adds the directory that *contains* the import package and nothing else, so a
    module reaching outside the package still fails, and it is skipped when the package is
    already installed. The same pattern is documented in ``tests/agents/conftest.py``.
    """
    if PACKAGE_SRC.is_dir() and str(PACKAGE_SRC) not in sys.path:
        sys.path.insert(0, str(PACKAGE_SRC))


def main() -> int:
    _bridge_release_registry_package()
    from referraleasy_release_registry.cli import main as cli_main

    return cli_main(sys.argv[1:], repo_root=REPO_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
