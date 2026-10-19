"""Settings, and the packaging fact that decides how this suite imports (§7).

Two things are tested here and they are unrelated except that both are about "what is true before
anything runs".

**The settings.** Every value is resolved from the environment by name, validated at construction,
and the ceilings are the agent runtime's rather than the worker's. That last part is the one worth a
test of its own: §8's numbers are `BudgetCaps`, `BudgetCaps.from_env` is the only reader of them, and
a worker that declared its own `max_attempts` would be the second place the budget lives.

**The import path.** `services/workers` is a uv workspace member, so `uv sync` installs
`referraleasy_workers` into the shared virtualenv and this suite imports it like any other package —
with no `sys.path` insert anywhere in the directory. The tests below pin that rather than assume it:
the three legs of the workspace wiring, the package resolving to the source tree, and the *absence*
of an insert, which is what keeps a packaging break loud. Until Phase 5 the wiring was missing and a
`worker_bootstrap.py` helper did the insert instead; both are gone together, and the scan test in that
section fails if the insert comes back without this file changing with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from worker_doubles import REPO_ROOT, WORKERS_SRC

from referraleasy_agent_runtime import BudgetCaps, ConfigurationError
from referraleasy_workers.settings import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_POLL_SECONDS,
    WorkerSettings,
    default_worker_id,
)

#: Read by every test here. Stated once so a test that means "this value" cannot mean another.
ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql://worker@db:5432/control",
    "WORKER_ID": "w-9",
    "WORKER_LEASE_SECONDS": "120",
    "WORKER_POLL_SECONDS": "1.5",
    "WORKER_OUTBOX_CLAIM_LIMIT": "4",
    "WORKER_OUTBOX_LEASE_SECONDS": "30",
    "WORKER_OUTBOX_BACKOFF_BASE_SECONDS": "7",
    "WORKER_OUTBOX_BACKOFF_CAP_SECONDS": "600",
    "WORKER_OUTBOX_MAX_BATCHES": "3",
    "WORKER_RECONCILE_LOOKUP_BUDGET": "4",
    "BOOKING_API_URL": "http://booking.example/",
    "BOOKING_API_TOKEN": "not-a-real-token",
    "NOTIFICATION_URL": "http://notify.example/",
    "NOTIFICATION_TOKEN": "not-a-real-token",
    "AGENT_ATTEMPT_TIMEOUT_MS": "3000",
    "AGENT_MAX_ATTEMPTS": "3",
    "TURN_DEADLINE_MS": "30000",
    "HTTP_TOOL_TIMEOUT_MS": "1500",
}


def test_every_worker_setting_comes_from_its_named_variable() -> None:
    """The whole object, resolved by name — including the ones with a `WORKER_` prefix."""
    settings = WorkerSettings.from_env(ENV)

    assert settings.worker_id == "w-9"
    assert settings.database_url == "postgresql://worker@db:5432/control"
    assert settings.lease_seconds == 120
    assert settings.poll_seconds == 1.5
    assert settings.outbox_claim_limit == 4
    assert settings.outbox_lease_seconds == 30
    assert settings.outbox_backoff_base_seconds == 7
    assert settings.outbox_backoff_cap_seconds == 600
    assert settings.outbox_max_batches == 3
    assert settings.reconcile_lookup_budget == 4
    assert settings.booking_api_url == "http://booking.example/"
    assert settings.notification_url == "http://notify.example/"


def test_the_ceilings_are_the_agent_runtimes_not_a_second_copy_of_them() -> None:
    """§8's numbers live in `BudgetCaps`. The worker reads them and declares none of its own.

    The assertion is structural: the resolved caps are a `BudgetCaps` built by the runtime's own
    `from_env`, and `WorkerSettings` has no field of its own that could drift from it. An
    `AGENT_MAX_ATTEMPTS=3` that meant one thing to an agent and another to a worker is exactly the
    drift this package exists to avoid.
    """
    settings = WorkerSettings.from_env(ENV)

    assert isinstance(settings.caps, BudgetCaps)
    assert settings.caps == BudgetCaps.from_env(ENV)
    assert settings.caps.max_attempts == 3
    assert settings.caps.attempt_timeout_ms == 3000
    assert settings.caps.turn_deadline_ms == 30000
    own_fields = set(WorkerSettings.__dataclass_fields__)
    assert "max_attempts" not in own_fields
    assert "attempt_timeout_ms" not in own_fields
    assert "turn_deadline_ms" not in own_fields


def test_defaults_apply_when_a_worker_knob_is_absent() -> None:
    """A truncated environment is a legitimate one: every worker knob has a stated default."""
    settings = WorkerSettings.from_env({})

    assert settings.lease_seconds == DEFAULT_LEASE_SECONDS
    assert settings.poll_seconds == DEFAULT_POLL_SECONDS
    assert settings.database_url is None
    assert settings.outbox_max_batches is None


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_lease_that_could_not_hold_anything_is_refused(value: str) -> None:
    """`WORKER_LEASE_SECONDS=0` would make every claim instantly stale.

    Clamping silently would produce a worker that reports every write as a lost lease — a failure
    that looks like a database problem and is a typo. The value is refused with the variable named.
    """
    with pytest.raises(ConfigurationError) as raised:
        WorkerSettings.from_env({**ENV, "WORKER_LEASE_SECONDS": value})
    assert "WORKER_LEASE_SECONDS" in str(raised.value)


def test_a_non_numeric_poll_interval_is_refused() -> None:
    with pytest.raises(ConfigurationError) as raised:
        WorkerSettings.from_env({**ENV, "WORKER_POLL_SECONDS": "soon"})
    assert "WORKER_POLL_SECONDS" in str(raised.value)


def test_an_empty_value_counts_as_absent() -> None:
    """An unset variable in a compose file arrives as an empty string, not as a missing key."""
    settings = WorkerSettings.from_env({**ENV, "WORKER_ID": "  ", "BOOKING_API_URL": ""})

    assert settings.worker_id == default_worker_id()
    assert settings.booking_api_url is None


@pytest.mark.parametrize(
    "method, variable",
    [
        ("require_database_url", "DATABASE_URL"),
        ("require_booking_api_url", "BOOKING_API_URL"),
        ("require_notification_url", "NOTIFICATION_URL"),
    ],
)
def test_a_missing_url_is_an_error_that_names_the_variable(method: str, variable: str) -> None:
    """Absent at import time is fine; absent at the moment it is needed is an error, naming it."""
    settings = WorkerSettings.from_env({})

    with pytest.raises(ConfigurationError) as raised:
        getattr(settings, method)()

    assert variable in str(raised.value)


def test_the_default_worker_id_names_the_process() -> None:
    """`host:pid` — the value that appears in `workflow.owner_worker_id` when a claim is refused."""
    worker_id = default_worker_id()

    host, _, pid = worker_id.rpartition(":")
    assert host, f"expected a host prefix in {worker_id!r}"
    assert pid.isdigit(), f"expected a numeric pid in {worker_id!r}"


# ---------------------------------------------------------------------------
# How this suite imports the package under test
# ---------------------------------------------------------------------------


def _root_pyproject() -> dict[str, Any]:
    """The root `pyproject.toml`, parsed. Parsed rather than grepped: the file has comments that
    talk about the members block, and a substring search would match the explanation instead of the
    declaration — which is precisely the failure mode a packaging test must not have."""
    import tomllib

    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_package_resolves_to_the_source_tree() -> None:
    """Not to a copy in site-packages, and not to nothing.

    Asserted as "the parent of the parent of the package is `services/workers/src`": that is true of
    a plain checkout *and* of the editable install the workspace performs, so this test does not have
    to change with how uv arranges the environment.
    """
    import referraleasy_workers

    package_dir = Path(referraleasy_workers.__file__).resolve().parent
    assert package_dir.parent == WORKERS_SRC, (
        f"referraleasy_workers resolved to {package_dir}, not to {WORKERS_SRC}. The workspace "
        "install is the only thing that puts the package on the path for this suite; repair the "
        "packaging rather than adding a sys.path insert."
    )


def test_services_workers_is_wired_into_the_workspace_three_ways() -> None:
    """The packaging gap this suite used to work around, asserted in the direction that matters now.

    `services/workers` was a package tree with no workspace wiring, so `uv sync` installed nothing
    for it and the tests inserted its `src` on `sys.path` themselves. That insert is gone, so all
    three legs of the wiring are load-bearing and none may be dropped: the member path, the
    dependency declaration (`uv sync` *removes* a package that is only a member), and the
    `[tool.uv.sources]` entry (without which uv refuses to resolve the bare member name). Dropping
    any one of them breaks `import referraleasy_workers` for every test in this directory — loudly,
    which is the point of asserting it here instead of relying on it.
    """
    root = _root_pyproject()

    assert "services/workers" in root["tool"]["uv"]["workspace"]["members"]
    assert "referraleasy-workers" in root["project"]["dependencies"]
    # `.get`, so that deleting the entry fails the assertion with this message rather than with a
    # `KeyError` from somewhere inside the parse.
    assert root["tool"]["uv"]["sources"].get("referraleasy-workers") == {"workspace": True}
    assert (WORKERS_SRC / "referraleasy_workers" / "__init__.py").is_file()


def _path_inserts(directory: Path) -> list[str]:
    """The names of the `*.py` files in ``directory`` that put something on ``sys.path``.

    The needles are assembled rather than written out, so that the module holding them cannot match
    itself and report a false positive. `PYTHONPATH` is deliberately not one of them: a docstring
    explaining why nobody needs it would trip the scan, and it is the insert (or a mutation of the
    list) that actually hides a packaging break.
    """
    needles = ("sys.path" + ".insert", "sys.path" + ".append")
    return sorted(
        path.name
        for path in directory.glob("*.py")
        if any(needle in path.read_text(encoding="utf-8") for needle in needles)
    )


def test_no_module_in_this_directory_puts_the_source_tree_on_sys_path() -> None:
    """The inverse of the test that used to live here, and the reason it was deleted.

    A path insert makes the suite pass against a virtualenv where the package was never installed,
    which is exactly the packaging break
    `test_services_workers_is_wired_into_the_workspace_three_ways` now catches. If a future change
    needs an insert again, delete this test together with it and repair the packaging instead: two
    ways of importing one package is how a loud failure becomes a silent one.
    """
    offenders = _path_inserts(Path(__file__).parent)

    assert offenders == [], (
        f"these modules put a path on sys.path: {offenders}. The package under test is a workspace "
        "member and does not need one; a test that adds one hides a broken `uv sync`."
    )


def test_the_path_insert_scan_would_notice_a_planted_one(tmp_path: Path) -> None:
    """Prove the check above can fail — a guard nobody has seen fire is a guess."""
    (tmp_path / "planted_insert.py").write_text(
        "import sys\n" + "sys.path" + ".insert(0, '/somewhere/else')\n", encoding="utf-8"
    )
    (tmp_path / "planted_append.py").write_text(
        "import sys\n" + "sys.path" + ".append('/somewhere/else')\n", encoding="utf-8"
    )
    (tmp_path / "innocent.py").write_text("import os\n", encoding="utf-8")

    assert _path_inserts(tmp_path) == ["planted_append.py", "planted_insert.py"]
