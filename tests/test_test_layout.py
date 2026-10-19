"""Invariants about the test tree itself.

One rule lives here, and it exists because it was broken twice in one session by two different
people, in two different directories, and each time the suite stayed green.

**No test module may import from `conftest` by name.**

pytest puts every `conftest.py` it collects onto `sys.path` under the bare name `conftest`, so
`sys.modules["conftest"]` is whichever one pytest imported *last*. An import of the form

    from conftest import EXPECTED_SKILLS

therefore resolves to an arbitrary directory's conftest, decided by the order the directories were
given on the command line. The same command in a different order fails at *collection*:

    pytest tests/skills tests/agent_runtime   -> ImportError: cannot import name 'EXPECTED_SKILLS'
                                                 from tests/agent_runtime/conftest.py
    pytest tests/agent_runtime tests/skills   -> 421 passed

The default `pytest` run is alphabetical, so `tests/agent_runtime` happened to sort before
`tests/skills` and the bug was invisible — the suite was green by luck, not by construction. That is
the worst shape a bug can have here: it cannot be seen by running the tests, and the failure it
produces points at the wrong file.

The fix is a uniquely named module (`skills_fixtures.py`, `agent_runtime_doubles.py`) whose name
cannot collide. This test is what keeps the fix from being un-made, and it is checked by AST rather
than by regex so that a mention inside a docstring — like the ones above — is not a false positive.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

#: Every directory that holds test modules. `migrations/tests` was missing until the 124 tests in it
#: were found to be collected by nothing: they had their own `conftest` with importable helpers, and
#: `from conftest import ...` from outside `tests/` resolves to whichever conftest pytest imported
#: last — so the directory only worked when it was the only thing on the command line. Both rules
#: below apply to it exactly as they do to `tests/`.
TEST_ROOTS = (TESTS_DIR, REPO_ROOT / "migrations" / "tests")

#: A module that may legitimately be imported by name from a test tree. Named explicitly rather than
#: pattern-matched, so adding a second exception is a deliberate edit and not an accident.
BARE_NAME_IMPORTS = {"conftest"}


def _test_modules() -> list[Path]:
    return sorted(p for root in TEST_ROOTS for p in root.rglob("test_*.py") if p.is_file())


def _shown(path: Path) -> str:
    """A path as a reader would type it: relative to the repository root."""
    return str(path.relative_to(REPO_ROOT))


def _imports_module_by_bare_name(path: Path) -> list[tuple[int, str]]:
    """Every `import conftest` / `from conftest import ...` in `path`, as (line, name).

    Only real import statements count: a docstring that spells the pattern out is text, not code,
    and the other test modules document this rule extensively.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in BARE_NAME_IMPORTS:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found.extend(
                (alias.lineno, alias.name)
                for alias in node.names
                if alias.name in BARE_NAME_IMPORTS
            )
    return found


def test_the_tree_actually_has_test_modules() -> None:
    """Anti-vacuity: the scan below is worthless if it finds nothing to scan."""
    modules = _test_modules()
    assert len(modules) > 20, f"only {len(modules)} test modules found; are TEST_ROOTS right?"


def test_every_declared_test_root_exists_and_has_modules() -> None:
    """A root that silently matched nothing would make both rules below vacuous for that directory.

    This is the check that would have caught `migrations/tests` being outside the scan: not a failing
    assertion, but a *missing* one — the rules were all passing over a directory they never looked at.
    """
    for root in TEST_ROOTS:
        assert root.is_dir(), f"{_shown(root)} is not a directory"
        found = [p for p in root.rglob("test_*.py") if p.is_file()]
        assert found, f"{_shown(root)} holds no test modules, so neither rule covers it"


def test_no_test_module_imports_a_conftest_by_name() -> None:
    """The rule. See this module's docstring for why it exists."""
    offenders = [
        f"{_shown(path)}:{line} imports '{name}'"
        for path in _test_modules()
        for line, name in _imports_module_by_bare_name(path)
    ]
    assert offenders == [], (
        "A test module imports a conftest by bare name, which resolves to whichever conftest pytest "
        "imported last and breaks under a different collection order. Move the shared names into a "
        "uniquely named module in the same directory (see tests/skills/skills_fixtures.py) and "
        "import from there:\n  " + "\n  ".join(offenders)
    )


def _modules_by_basename() -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = {}
    for path in _test_modules():
        grouped.setdefault(path.name, []).append(path)
    return {name: paths for name, paths in grouped.items() if len(paths) > 1}


def test_no_two_test_modules_share_a_basename() -> None:
    """pytest's default import mode imports test modules **by basename**, not by path.

    Two files called `test_policy_table.py` in different directories therefore collide, and pytest
    refuses to collect either — with `import file mismatch ... which is not the same as the test file
    we want to collect`. That is a **hard stop for the entire run**, not a failure in the two files:
    one duplicate name blocks every other test in the repository.

    This happened once already, between `tests/domain/` and `tests/gateway/`, and the tempting
    "fix" — deleting the stale `__pycache__` it points at — works only until the next run and leaves
    the real collision in place. A unique basename is a one-word change; this test makes it a
    deliberate one.

    The alternative is `--import-mode=importlib`, which removes the collision class entirely but
    stops pytest putting test directories on `sys.path` — and several suites here rely on that for
    their shared doubles modules (`skills_fixtures`, `agent_runtime_doubles`, `_spike_env`). Renaming
    is the smaller change.
    """
    duplicates = _modules_by_basename()
    assert duplicates == {}, (
        "Two or more test modules share a basename, which stops pytest collecting the whole suite:\n"
        + "\n".join(
            f"  {name}:\n" + "\n".join(f"    {_shown(p)}" for p in paths)
            for name, paths in sorted(duplicates.items())
        )
        + "\nRename one of them."
    )


def test_the_basename_scan_would_notice_a_duplicate(tmp_path: Path) -> None:
    """Prove the check above can fail — a guard nobody has seen fire is a guess."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for directory in ("a", "b"):
        (tmp_path / directory / "test_duplicate.py").write_text("", encoding="utf-8")

    grouped: dict[str, list[Path]] = {}
    for path in sorted(tmp_path.rglob("test_*.py")):
        grouped.setdefault(path.name, []).append(path)

    assert [name for name, paths in grouped.items() if len(paths) > 1] == ["test_duplicate.py"]


@pytest.mark.parametrize(
    "source",
    [
        "from conftest import X\n",
        "import conftest\n",
        "import conftest, os\n",
    ],
)
def test_the_detector_catches_every_spelling(tmp_path: Path, source: str) -> None:
    """A guard that cannot fail is not a guard — prove it fires on each real spelling."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _imports_module_by_bare_name(probe), f"detector missed: {source!r}"


def test_the_detector_ignores_mentions_in_docstrings_and_strings(tmp_path: Path) -> None:
    """And prove it does *not* fire on prose, so the rule can be documented without tripping it."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        '"""A note that says from conftest import X out loud."""\n'
        'MESSAGE = "from conftest import X"\n',
        encoding="utf-8",
    )
    assert _imports_module_by_bare_name(probe) == []
