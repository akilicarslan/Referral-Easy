"""Helpers shared by the `tests/scripts` suite.

Uniquely named, and that is not decoration. pytest puts every `conftest.py` it collects on
`sys.path` under the bare name `conftest`, so `from conftest import X` resolves to whichever
directory's conftest pytest imported *last* — which depends on the order the directories were given
on the command line. `tests/test_test_layout.py` asserts no test module does that, and the fix it
prescribes is a uniquely named module beside the tests (the same way `tests/skills/skills_fixtures.py`
and `tests/gateway/gateway_fixtures.py` are named).

Three things live here:

* **A scratch database.** The checks under test are aimed at a database, and proving that a missing
  reference row is *reported* means having a database where a row is genuinely missing. Deleting one
  out of the shared test database would be a test that damages the thing it tests, and
  `tests/domain/conftest.py` deliberately never resets the vocabulary tables for exactly that reason.
  So each such test gets its own database on the same server, created and dropped around the test.
  It reaches PostgreSQL the same way `tests/domain/conftest.py` does — `TEST_DATABASE_URL`, and the
  migrations applied through `migrations/run.py`, which is the one runner this repository has.

* **A fixture-serving stub for the mock hospital API.** The stdout side of `make seed` talks HTTP to
  `services/mock-api`, and the service is loaded from the *real* fixture files. The stub serves those
  same files verbatim under the envelopes `services/mock-api/README.md` documents, so the checks can
  be exercised against real fixture content without a running service — including the cases that
  matter, which is every case where a record is *absent*. What this does **not** prove is that the
  real service behaves as the stub does; that is what the run in the change's own evidence shows.

* **The module loaders.** `migrations/run.py` and `scripts/seed.py` are files, not importable
  packages, so both are loaded by path — the same technique `scripts/migrate.py` and
  `tests/domain/conftest.py` already use.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed.py"
DEMO_SCRIPT = REPO_ROOT / "scripts" / "demo.py"
HEALTH_SCRIPT = REPO_ROOT / "scripts" / "health.py"
RUNNER_PATH = REPO_ROOT / "migrations" / "run.py"

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def load_module(name: str, path: Path) -> Any:
    """Import a Python file by path.

    Registered in `sys.modules` before execution because the migration runner is typed with
    dataclasses, and a dataclass resolves its defining module through `sys.modules` while the class
    body is being processed.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def demo_module() -> Any:
    """`scripts/demo.py`, importable so its step sequence can be driven without docker.

    The orchestrator's own behaviour — that a failed step stops the run, that a skipped step says
    skipped, that the entry-point list comes from the probe — is testable here by replacing the two
    things it shells out to. What is *not* tested here is the lifecycle itself; that is what
    `make demo` is, and the CI job runs it against a runner with nothing up.
    """
    return load_module("referraleasy_demo_script", DEMO_SCRIPT)


def health_module() -> Any:
    """`scripts/health.py`, whose probe the demo reuses rather than reimplementing."""
    return load_module("referraleasy_health_script", HEALTH_SCRIPT)


def seed_module() -> Any:
    """`scripts/seed.py`, importable so its checks can be run one at a time.

    Holding the module does not run it: `main()` is behind `if __name__ == "__main__"`, and the
    subprocess tests are what execute it end to end.
    """
    return load_module("referraleasy_seed_script", SEED_SCRIPT)


def apply_migrations(database_url: str) -> None:
    load_module("referraleasy_migrations", RUNNER_PATH).apply_migrations(
        database_url, reporter=lambda *a, **k: None
    )


def _admin_connection(database_url: str):
    import psycopg

    return psycopg.connect(database_url, autocommit=True)


@contextmanager
def scratch_database(database_url: str, name: str = "referraleasy_seed_test") -> Iterator[str]:
    """A database of its own on the same server, dropped afterwards either way.

    `CREATE DATABASE` cannot run inside a transaction, hence the autocommit admin connection. The
    name is fixed rather than random so that a crashed run leaves one obviously-named database to
    drop, instead of accumulating anonymous ones — and it is dropped up front too, for the same
    reason.
    """
    server = database_url.rsplit("/", 1)[0]
    scratch = f"{server}/{name}"
    with _admin_connection(database_url) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield scratch
    finally:
        with _admin_connection(database_url) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@dataclass(frozen=True)
class TableCounts:
    """Row counts for every table in a database, so two states can be compared as a whole."""

    counts: dict[str, int]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TableCounts) and self.counts == other.counts

    def describe(self) -> str:
        return ", ".join(f"{table}={count}" for table, count in sorted(self.counts.items()))


def table_row_counts(database_url: str) -> TableCounts:
    """Count every base table in the public schema.

    Every table, not a chosen list: "the second run left the same state as the first" is only worth
    asserting if nothing is exempt from it. A list would be a place for the one table that changed
    to hide.
    """
    with _admin_connection(database_url) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                " ORDER BY table_name"
            ).fetchall()
        ]
        return TableCounts(
            {
                table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                for table in tables
            }
        )


# ---------------------------------------------------------------------------
# A stub for the mock hospital API
# ---------------------------------------------------------------------------

#: `(field in /health.seed)` -> `(fixture file, its record list)`. The shape is the mock API's own
#: `SeedSummary` (`services/mock-api/src/domain/seed.ts`) and the file mapping is
#: `src/domain/fixture-seed.ts`. Written out rather than imported so that the stub is an independent
#: statement of the service's contract: if the service moves a field, these tests fail and say so,
#: which is the point of a test.
SEED_REPORT_FIELDS: tuple[tuple[str, str], ...] = (
    ("locations", "directory/locations.json"),
    ("hospitals", "directory/hospitals.json"),
    ("providers", "directory/providers.json"),
    ("slots", "directory/slots.json"),
    ("plans", "coverage/plans.json"),
    ("networkRecords", "coverage/network_records.json"),
    ("referralRequirements", "coverage/referral_requirements.json"),
    ("documents", "documents/documents.json"),
    ("evidence", "evidence.json"),
    ("coverageSnapshots", "coverage/evidence_snapshots.json"),
)


@dataclass(frozen=True)
class Route:
    """One list endpoint, as `services/mock-api/README.md` documents it."""

    endpoint: str
    fixture: str
    envelope: str
    id_key: str
    #: Fixture field whose distinct values become one request each, for endpoints that refuse an
    #: unfiltered request (`GET /documents` requires a `patient_id`).
    group_by: str | None = None


ROUTES: tuple[Route, ...] = (
    Route("/locations", "directory/locations.json", "locations", "location_id"),
    Route("/hospitals", "directory/hospitals.json", "hospitals", "hospital_id"),
    Route("/providers", "directory/providers.json", "providers", "provider_id"),
    Route("/plans", "coverage/plans.json", "plans", "plan_id"),
    Route("/evidence", "evidence.json", "records", "evidence_id"),
    Route("/evidence-snapshots", "coverage/evidence_snapshots.json", "snapshots", "snapshot_id"),
    Route("/documents", "documents/documents.json", "documents", "document_id", "patient_id"),
)


def fixture_records(relative_path: str) -> list[dict[str, Any]]:
    parsed = json.loads((FIXTURES_DIR / relative_path).read_text(encoding="utf-8"))
    assert isinstance(parsed, list), f"{relative_path} is not a bare array"
    return parsed


def seed_report() -> dict[str, Any]:
    """A `seed` block derived from the fixture files, so it can never disagree with them by accident."""
    return {
        "mode": "fixtures",
        "source_directory": str(FIXTURES_DIR),
        **{field: len(fixture_records(path)) for field, path in SEED_REPORT_FIELDS},
    }


@dataclass
class StubConfig:
    """How the stub should misbehave, when a test wants it to."""

    #: Primary ids to leave out of every route. The "a row is missing" case.
    drop_ids: set[str] = field(default_factory=set)
    #: Extra records to serve, by endpoint. The "the store has something the fixtures do not" case.
    extra_records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: `seed.mode` to report. `"builtin"` is the failure the seed check exists for.
    seed_mode: str = "fixtures"
    #: `seed.source_directory` to report.
    source_directory: str | None = None
    #: Extra fields merged into the `/health` payload.
    health_overrides: dict[str, Any] = field(default_factory=dict)


def _build_health(config: StubConfig) -> dict[str, Any]:
    seed = seed_report()
    seed["mode"] = config.seed_mode
    if config.source_directory is not None:
        seed["source_directory"] = config.source_directory
    health: dict[str, Any] = {
        "status": "ok",
        "service": "mock-api",
        "seed": seed,
        "fixture_load_errors": [],
        "scenario_manifest": {
            "found": True,
            "complete": True,
            "missing_files": [],
            "unreferenced_files": [],
        },
    }
    health.update(config.health_overrides)
    return health


class _StubHandler(BaseHTTPRequestHandler):
    config: StubConfig

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path == "/health":
            self._send(_build_health(self.config))
            return

        route = next((r for r in ROUTES if r.endpoint == path), None)
        if route is None:
            self._send({"error": f"no stub route for {path}"}, status=404)
            return

        filters = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
        records = [
            record
            for record in fixture_records(route.fixture)
            if record[route.id_key] not in self.config.drop_ids
            and (route.group_by is None or filters.get(route.group_by) == record[route.group_by])
        ]
        records.extend(self.config.extra_records.get(route.endpoint, []))
        self._send({route.envelope: records, "count": len(records)})

    def _send(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Silence. The stub is not the subject of the test and its access log is not evidence."""


@contextmanager
def fake_mock_api(config: StubConfig | None = None) -> Iterator[str]:
    """Serve the fixture corpus on an ephemeral port and yield its base URL.

    Bound to port 0 so two tests can run at once, and 127.0.0.1 so nothing is reachable off the
    machine — the fixtures are synthetic and this stays that way.
    """
    handler = type("_BoundStubHandler", (_StubHandler,), {"config": config or StubConfig()})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Running the script
# ---------------------------------------------------------------------------


def run_seed(database_url: str, mock_api_url: str) -> subprocess.CompletedProcess[str]:
    """`scripts/seed.py` as it is actually invoked, against the URLs the test controls.

    `sys.executable` rather than `uv run`: the subprocess should be the interpreter pytest is already
    running in, with the packages already installed, so the test measures the script rather than
    whatever a fresh resolver would have chosen.
    """
    return subprocess.run(
        [
            sys.executable,
            str(SEED_SCRIPT),
            "--database-url",
            database_url,
            "--mock-api-url",
            mock_api_url,
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=180,
        check=False,
    )


def describe(result: subprocess.CompletedProcess[str]) -> str:
    """Both streams, so a failing assertion carries the evidence instead of a bare exit code."""
    return f"exit {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
