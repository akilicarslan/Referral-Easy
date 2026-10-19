"""Test configuration for `tests/agents`: the path bridge the workspace cannot give us.

`services/agents/*` is **not** a member of `[tool.uv.workspace]` in the root
`pyproject.toml` — the glob is present but commented out, and this workstream may
not edit that file. So `referraleasy_agents` is not installed into the shared venv
and imports must be bridged here.

The bridge is deliberately narrow: it inserts `services/agents` (the directory that
*contains* the import package) and nothing else, so a test that reaches outside the
package's own modules still fails. It is also idempotent and first, so a future
`uv sync` that installs the package for real changes nothing.

**Reported as a finding, not worked around silently:** the correct fix is a human
adding `"services/agents/*"` to `[tool.uv.workspace].members` plus a
`[tool.uv.sources]` entry. Until then, `tests/agents` is the only place that can
import the package, and `uv run --no-sync pytest tests/agents` works while
`uv run --no-sync python -c "import referraleasy_agents"` does not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: `tests/agents/conftest.py` -> repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The directory that makes `import referraleasy_agents` resolve.
AGENTS_SRC = REPO_ROOT / "services" / "agents"


def _bridge_agents_package() -> None:
    entry = str(AGENTS_SRC)
    if entry not in sys.path:
        sys.path.insert(0, entry)


_bridge_agents_package()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The repository root, so a test can read the migration or a skill manifest."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def agents_src() -> Path:
    """`services/agents`, where the import package lives."""
    return AGENTS_SRC
