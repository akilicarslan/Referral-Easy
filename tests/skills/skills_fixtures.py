"""Shared constants and path setup for the skill package tests.

This is a plain module with a unique name, not `conftest.py`, and that is the whole point.

pytest puts every `conftest.py` it collects onto `sys.path` under the bare name `conftest`. With two
test directories each having one, `sys.modules["conftest"]` is whichever pytest imported **last**, so
a test module doing `from conftest import ...` gets an arbitrary one of them depending on the order
directories were given on the command line:

    pytest tests/skills tests/agent_runtime     -> ImportError, cannot import name 'EXPECTED_SKILLS'
                                                   from tests/agent_runtime/conftest.py
    pytest tests/agent_runtime tests/skills     -> 421 passed

The suite was green only because `tests/agent_runtime` sorts before `tests/skills` and the default
run is alphabetical. A uniquely named module cannot be shadowed, so the import means what it says
regardless of collection order. `tests/agent_runtime/agent_runtime_doubles.py` exists for the same
reason and carries the same explanation.

Keep importing from here rather than from `conftest`.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# ``packages/skills`` is a uv workspace member, so normally it is importable as an editable install.
# The source tree is added to the path as well, so that this suite also runs from a plain checkout -
# the loader is stdlib-only and has no build step, so nothing about the test run should depend on
# the shared virtualenv having been synced.
_SKILLS_SRC = REPO_ROOT / "packages" / "skills" / "src"
if _SKILLS_SRC.is_dir() and str(_SKILLS_SRC) not in sys.path:
    sys.path.insert(0, str(_SKILLS_SRC))

#: The eight packages §6 defines, by exact package name.
EXPECTED_SKILLS: tuple[str, ...] = (
    "booking-confirmation",
    "constraint-negotiation",
    "coverage-interpretation",
    "directory-grounding",
    "handoff-triage",
    "referral-intake",
    "referral-readiness",
    "uncertain-booking-reconciliation",
)

#: Package to owning agent role, exactly as §6 assigns them.
EXPECTED_OWNER_AGENT: dict[str, str] = {
    "referral-intake": "referral_intake",
    "coverage-interpretation": "coverage_network",
    "constraint-negotiation": "provider_matching",
    "directory-grounding": "provider_matching",
    "referral-readiness": "referral_readiness",
    "booking-confirmation": "scheduling",
    "uncertain-booking-reconciliation": "scheduling",
    "handoff-triage": "recovery_handoff",
}

SKILLS_DIR = REPO_ROOT / "packages" / "skills"

#: A body shorter than this is a stub, not an instruction.
MIN_BODY_WORDS = 450
MIN_BODY_CHARS = 2500


def marked_files(root: Path, filename: str) -> list[Path]:
    """Files called ``filename`` anywhere under ``root``."""
    return sorted(path for path in root.rglob(filename) if path.is_file())
