#!/usr/bin/env python3
"""`make seed` — prove the demo's data is loaded, and that what is loaded is the corpus on disk.

**This script writes nothing, and that is the finding rather than a shortcut.**

The Phase 0 stub said it would "load synthetic fixtures into PostgreSQL and the mock hospital API".
Reading the system settled that it should not, in two halves.

*PostgreSQL needs no fixture rows at all.* The only tables holding shipped data are the reference
tables — `workflow_state`, `workflow_transition`, `agent_role`, `tool_registry`, `policy_rule` — and
the migrations own them: 003 and 005 introduce them, and 014 replaces the invented vocabulary with the
contract's and re-derives the policy table. `fixtures/` contains no vocabulary file, so there is
nothing on disk to load them from and no second authority to load them from. Every other table
(`workflow`, `session`, `task`, `approval`, `booking_request`, `handoff`, `outbox`, `audit`,
`evidence`, `release_registry`, `command_dedup`) is *runtime* state written while the demo runs;
seeding it would fabricate a history the demo is about to create. `tests/domain/conftest.py` reaches
the same conclusion from the other side — it resets the data tables between tests and deliberately
leaves the vocabulary tables alone, "they are part of the schema, and a test that deleted them would
be testing a different database than the one that ships".

*The mock API seeds itself.* `services/mock-api` is the stand-in that owns the external systems —
directory, coverage, documents, evidence, fault scenarios — and it loads the fixture corpus at
startup (`src/app.ts` -> `domain/seeding.ts:seedStore` -> `domain/fixture-seed.ts:seedFromFixtures`),
defaulting to `MOCK_API_SEED=auto`, which prefers the fixtures whenever they are present. It is
already idempotent (directory rows are replaced, slots are `INSERT OR IGNORE`d so a restart cannot
resurrect a slot a live booking holds) and it reports what it loaded on `/health`. A second loader
in Python could only disagree with it.

So the demo's data is produced by two loaders that are both already built, and the honest job left
for `make seed` is to *prove* they produced it — because the failure it guards against is silent:
a service running on the built-in dataset instead of the fixtures answers every request happily while
the scenario manifest's ids dangle, and a store holding six hospitals when the fixture file holds
seven produces a demo whose numbers are wrong in a way nothing else detects.

What is verified, and where each expectation comes from (nothing below is a number typed from
memory — every expectation is read from a file in this repository):

  PostgreSQL, against `DATABASE_URL`
    1. every `migrations/*.sql` file has a `schema_migrations` row, and no row names a file that is
       gone (derived from the migrations directory)
    2. `workflow_state` is exactly `WorkflowState` from
       `packages/contracts/schemas/common.json#/$defs/WorkflowState` (derived from the frozen schema)
    3. the reference tables hold the row counts their own `INSERT` blocks declare, read out of
       `migrations/014_align_domain_vocabulary.sql` (derived from the migration)
    4. the state machine is whole: every non-terminal state has an outgoing transition and every
       state is reachable from `INTAKE` (a truncated `workflow_transition` is otherwise a demo that
       dead-ends mid-journey with no error)

  The mock API, against `MOCK_API_URL`
    5. `/health` answers, reports `seed.mode: "fixtures"` and a `source_directory` that is this
       repository's `fixtures/` — `"builtin"` means a parallel dataset the manifest does not describe
    6. each of the ten per-type counts in the seed report equals the count in the fixture file it
       names (the loader read the corpus)
    7. the records are in the *store*, not merely in the loader: for each fixture file the API
       exposes as a list, the ids it serves are exactly the fixture ids (a store that has drifted
       from the corpus — stale rows, or slots an upsert deliberately left alone — is caught here and
       nowhere else)
    8. the service's own coherence report is clean: `fixture_load_errors` empty, and the scenario
       manifest found, complete, with no missing or unreferenced files (this covers
       `fixtures/fault-scenarios/` and `fixtures/scenarios.json` without re-implementing either)

Not covered, stated rather than implied:

  * Slots, network records and referral requirements are not exposed by the API as a plain list, so
    for those three the store is only checked in *count*, against the loader's own report, and not
    in content. They are the three where a same-count drift would pass.
  * Field-level drift at an unchanged id — a fixture value edited in place — is not detected here.
    Content is owned by `tests/contracts/test_fixture_conformance.py` (schema) and the mock API's own
    `test/fixtures-integration.test.ts` (behaviour); this script checks identity, not content.
  * `fixtures/patients/**`, `fixtures/workflow/**`, `fixtures/coverage/coverage_results.json`,
    `fixtures/directory/availability_results.json` and `fixtures/directory/provider_candidates.json`
    are **not seeded anywhere**, and their absence from this script is deliberate. They are authored
    expectations — what the workflow, the search and the coverage rules must *produce* — read by
    `tests/contracts/` and by `packages/rag`'s authority lookup, and referenced by id from
    `fixtures/scenarios.json`. Loading them into a database would make the demo assert its answers
    rather than compute them, which is the one thing ADR-0024 D4 exists to prevent.

Running it twice cannot change anything: it opens no write path. That is asserted by
`tests/scripts/`, which counts rows across two runs rather than trusting the claim.

Exit codes
    0  every dataset is loaded and matches the source it is derived from
    1  the environment is not runnable (reported by scripts/validate_env.py)
    2  the environment is fine, but a dataset is missing, partial, or does not match its source —
       including the case where the mock API is not running, because then the corpus is genuinely
       not loaded anywhere and saying "OK" would be the lie this script exists to prevent

Usage
    python scripts/seed.py                      # `make seed`
    python scripts/seed.py --database-url postgresql://... --mock-api-url http://localhost:4010
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# `packages/contracts` is a workspace member, so `uv run` installs it and this line is redundant.
# It is kept for the same reason `scripts/index_directory.py` keeps it: the file should still run in
# an environment that has not been re-synced, and it documents that the script only ever reaches the
# contract through its public loader rather than reading a schema path of its own.
sys.path.insert(0, str(REPO_ROOT / "packages" / "contracts" / "python" / "src"))

from referraleasy_contracts.schemas import load_schema  # noqa: E402

FIXTURES_DIR: Final = REPO_ROOT / "fixtures"
MIGRATIONS_DIR: Final = REPO_ROOT / "migrations"

#: The migration that re-derives the control-state vocabulary and is therefore the authority on what
#: the reference tables contain. 003 and 005 introduced them with an invented vocabulary that 014
#: replaces wholesale (it deletes and re-inserts, then prunes to its own inventory), so reading the
#: baseline from 014 and subsequent additive migrations keeps the check aligned with the schema.
VOCABULARY_MIGRATION: Final = MIGRATIONS_DIR / "014_align_domain_vocabulary.sql"

EXIT_OK: Final = 0
EXIT_ENV_INVALID: Final = 1
EXIT_NOT_SEEDED: Final = 2

#: Seconds. Short on purpose: a seed check that hangs is indistinguishable from one that failed.
HTTP_TIMEOUT: Final = 10.0

#: The entry state of §4's flow. `INTAKE` is where every workflow starts, so it is where a
#: reachability check has to start too.
ENTRY_STATE: Final = "INTAKE"


class SeedFailure(Exception):
    """An expectation could not be evaluated at all.

    Distinct from a check that *failed*: this is "the script cannot tell", and it is raised rather
    than returned so that a check whose source has moved fails loudly instead of passing vacuously.
    """


@dataclass(frozen=True)
class Check:
    """One verified property, with the evidence that produced the verdict."""

    name: str
    ok: bool
    detail: str


# ---------------------------------------------------------------------------
# Fixture reading
# ---------------------------------------------------------------------------


def read_fixture_records(relative_path: str) -> list[dict[str, Any]]:
    """The records of one fixture file.

    Strict about the shape because ADR-0024 D1 is strict about it: one schema definition per file,
    "no envelopes, no wrapper objects". A file that has grown a wrapper is a change to that decision
    and should fail here rather than be tolerated by a permissive reader.
    """
    path = FIXTURES_DIR / relative_path
    if not path.is_file():
        raise SeedFailure(f"missing fixture file {path.relative_to(REPO_ROOT)}")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SeedFailure(f"{relative_path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise SeedFailure(
            f"{relative_path} holds a {type(parsed).__name__}, not the bare array ADR-0024 D1 "
            "requires"
        )
    for index, record in enumerate(parsed):
        if not isinstance(record, dict):
            raise SeedFailure(f"{relative_path}[{index}] is not an object")
    return parsed


def fixture_ids(relative_path: str, id_key: str) -> set[str]:
    """The `id_key` of every record in a fixture file, refusing a record without one.

    A record with no id would vanish from the comparison and read as a match, which is exactly the
    quiet drop the fixture contract forbids.
    """
    ids: set[str] = set()
    for index, record in enumerate(read_fixture_records(relative_path)):
        value = record.get(id_key)
        if not isinstance(value, str) or not value:
            raise SeedFailure(f"{relative_path}[{index}] has no string {id_key!r}")
        if value in ids:
            raise SeedFailure(f"{relative_path} repeats {id_key} {value!r}")
        ids.add(value)
    return ids


# ---------------------------------------------------------------------------
# What the mock API is expected to have loaded
# ---------------------------------------------------------------------------

#: `field in /health.seed` -> the fixture file whose length it must equal.
#:
#: This is the whole fixture corpus the mock API owns, except `fault-scenarios/` and
#: `scenarios.json`, which are not counted into the seed report and are covered by check 8 instead.
SEED_REPORT_FIXTURES: Final[tuple[tuple[str, str], ...]] = (
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
class ListProbe:
    """A fixture file the API exposes as a list, and how to ask it for every record.

    `group_by` exists for the endpoints that refuse to answer unfiltered: `GET /documents` requires a
    `patient_id`, so the requests are derived from the fixture's own distinct `patient_id` values.
    Deriving them from the file is the point — a hand-written set of ids would be a second fixture.
    """

    fixture: str
    endpoint: str
    envelope: str
    id_key: str
    group_by: str | None = None
    query_param: str | None = None


LIST_PROBES: Final[tuple[ListProbe, ...]] = (
    ListProbe("directory/locations.json", "/locations", "locations", "location_id"),
    ListProbe("directory/hospitals.json", "/hospitals", "hospitals", "hospital_id"),
    ListProbe("directory/providers.json", "/providers", "providers", "provider_id"),
    ListProbe("coverage/plans.json", "/plans", "plans", "plan_id"),
    ListProbe("evidence.json", "/evidence", "records", "evidence_id"),
    ListProbe(
        "coverage/evidence_snapshots.json", "/evidence-snapshots", "snapshots", "snapshot_id"
    ),
    ListProbe(
        "documents/documents.json",
        "/documents",
        "documents",
        "document_id",
        "patient_id",
        "patient_id",
    ),
)

#: The reference tables 014 owns, in the order it declares them.
REFERENCE_TABLES: Final[tuple[str, ...]] = (
    "workflow_state",
    "workflow_transition",
    "agent_role",
    "tool_registry",
    "policy_rule",
)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _read_dotenv() -> dict[str, str]:
    """`.env` layered under the process environment, without a dependency.

    Deliberately does not write into `os.environ`: a script that mutates the process environment
    leaks that change into everything it launches afterwards. This is the same reader
    `scripts/health.py` uses, for the same reason.
    """
    values: dict[str, str] = {}
    for path in (REPO_ROOT / ".env", REPO_ROOT / ".env.example"):
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in values:
                values[key] = value
    return {**values, **{k: v for k, v in os.environ.items() if v}}


def run_env_validation() -> int:
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "validate_env.py")],
        check=False,
        cwd=REPO_ROOT,
    ).returncode


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


def _count_values_rows(sql: str, table: str) -> int:
    """How many row tuples a table's `INSERT ... VALUES` block contains.

    Counted by walking the block with a small scanner that tracks parenthesis depth and whether it
    is inside a string literal, rather than by counting lines. Counting lines is the obvious thing
    and it is wrong: 014 wraps long `note` values onto a continuation line, and a row written that
    way is still one row. The scanner sees the real structure, so a tuple split across three lines
    counts once and a parenthesis inside a note (`'§3: ... (§4)'`) counts not at all.

    Every way the block can fail to parse raises instead of returning a number — a low count would
    otherwise read as "the database has extra rows" and send whoever reads the failure looking in
    the wrong place entirely.
    """
    lines = sql.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(rf"^\s*INSERT INTO {table}\b", line, re.I)
        ),
        None,
    )
    if start is None:
        raise SeedFailure(
            f"{VOCABULARY_MIGRATION.name} has no `INSERT INTO {table}` block; the reference count "
            "for it cannot be derived, and a count that cannot be derived is not a check"
        )
    if not re.search(r"\bVALUES\b", lines[start], re.IGNORECASE):
        raise SeedFailure(
            f"the `INSERT INTO {table}` block in {VOCABULARY_MIGRATION.name} does not open with "
            "`... VALUES` on one line, so its rows cannot be counted"
        )

    depth = 0
    in_string = False
    rows = 0
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if depth == 0 and not in_string:
            if not stripped or stripped.startswith("--"):
                continue  # a whole-line comment: between row groups, never inside a row
            if stripped.startswith(";") or re.match(r"^\s*ON CONFLICT\b", stripped, re.IGNORECASE):
                break  # the end of the statement
            if not stripped.startswith("("):
                raise SeedFailure(
                    f"the `INSERT INTO {table}` block in {VOCABULARY_MIGRATION.name} has an "
                    f"unrecognised line {line.strip()[:60]!r}; its rows can no longer be counted"
                )
        index = 0
        terminated = False
        while index < len(line):
            char = line[index]
            if in_string:
                if char == "'":
                    # '' is an escaped quote inside the literal, not the end of it.
                    if line[index + 1 : index + 2] == "'":
                        index += 2
                        continue
                    in_string = False
            elif char == "'":
                in_string = True
            elif char == "(":
                depth += 1
                if depth == 1:
                    rows += 1
            elif char == ")":
                depth -= 1
            elif char == ";" and depth == 0:
                # The statement terminator, which the last row often shares a line with.
                terminated = True
                break
            index += 1
        if terminated:
            break

    if rows == 0:
        raise SeedFailure(
            f"the `INSERT INTO {table}` block in {VOCABULARY_MIGRATION.name} yielded 0 rows; its "
            "shape changed and the count can no longer be derived from it"
        )
    return rows


def expected_reference_counts() -> dict[str, int]:
    """Count the vocabulary baseline and the rows added by later migrations.

    A hand-maintained copy of these numbers would be a second source of truth, and the two would
    drift the first time either moved; the migration is the file that decides what the tables
    contain, so it is the file that is read. Nothing here is a number typed from memory.
    """
    if not VOCABULARY_MIGRATION.is_file():
        raise SeedFailure(f"missing {VOCABULARY_MIGRATION.relative_to(REPO_ROOT)}")
    sql = VOCABULARY_MIGRATION.read_text(encoding="utf-8")
    counts = {table: _count_values_rows(sql, table) for table in REFERENCE_TABLES}
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name <= VOCABULARY_MIGRATION.name:
            continue
        additions = migration.read_text(encoding="utf-8")
        for table in REFERENCE_TABLES:
            if re.search(rf"^\s*INSERT INTO {table}\b", additions, re.I | re.M):
                # Later inserts add distinct keys. The database comparison below detects overlap
                # or a non-additive migration instead of accepting a hand-maintained row count.
                counts[table] += _count_values_rows(additions, table)
    return counts


def _connect(dsn: str):
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - the root dev group installs it
        raise SeedFailure("psycopg is not installed; the database checks cannot run") from exc
    try:
        # Autocommit, because every check below is an independent read. Shared implicit transaction
        # would make the first refused statement poison the rest: PostgreSQL aborts the transaction
        # and every later check reports `InFailedSqlTransaction` instead of its own verdict, burying
        # the one failure that mattered under three lines of noise about the connection.
        return psycopg.connect(dsn, connect_timeout=int(HTTP_TIMEOUT), autocommit=True)
    except Exception as exc:
        raise SeedFailure(
            f"cannot reach the database: {type(exc).__name__}: {str(exc)[:160]}"
        ) from exc


def check_migrations_applied(connection: Any) -> Check:
    """Every migration file is in `schema_migrations`, and the ledger matches the files.

    All three directions matter. A migration that was never applied means the demo runs against a
    schema nobody described. A ledger row with no file behind it makes the ordering of everything
    after it a guess. And a checksum that no longer matches means the file was edited *after* it was
    applied, so the database is neither the old schema nor the new one — the ledger stores
    `sha256` of the file's UTF-8 text (`migrations/run.py` computes the same digest, and refuses to
    apply a migration whose digest has moved), so this is the same comparison the runner makes, asked
    at a moment when nobody is about to run it.
    """
    on_disk = {path.name: path for path in sorted(MIGRATIONS_DIR.glob("*.sql"))}
    if not on_disk:
        raise SeedFailure(f"no *.sql files under {MIGRATIONS_DIR.relative_to(REPO_ROOT)}")

    # Asked before the ledger is read, because "the ledger does not exist" is the most likely thing
    # to be true on a fresh clone and it deserves the fix rather than a driver message about a
    # missing relation.
    if connection.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0] is None:
        raise SeedFailure(
            "the database has no schema_migrations table, so no migration has ever been applied "
            "to it: run `make migrate` first"
        )

    rows = {
        row[0]: row[1]
        for row in connection.execute("SELECT filename, checksum FROM schema_migrations").fetchall()
    }

    not_applied = sorted(set(on_disk) - set(rows))
    unknown = sorted(set(rows) - set(on_disk))
    edited = sorted(
        name
        for name, path in on_disk.items()
        if name in rows
        and hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        != rows[name]
    )

    detail = f"{len(rows)}/{len(on_disk)} applied"
    if not_applied:
        detail += f"; not applied: {', '.join(not_applied)}"
    if unknown:
        detail += f"; ledger names files that no longer exist: {', '.join(unknown)}"
    if edited:
        detail += f"; edited after being applied: {', '.join(edited)}"
    return Check("migrations applied", not (not_applied or unknown or edited), detail)


def check_vocabulary_matches_contract(connection: Any) -> Check:
    """`workflow_state` is exactly the frozen contract's `WorkflowState` enum.

    This is the one vocabulary whose authority is machine-readable. 014 exists precisely because the
    persisted states had drifted from the contract, so the property it fixed is the property worth
    re-checking — and it catches a table pruned by hand, where every state that disappeared turns
    silently into a default-deny at the policy gate.
    """
    definition = load_schema("common")["$defs"]["WorkflowState"]
    declared = {str(code) for code in definition["enum"]}
    persisted = {row[0] for row in connection.execute("SELECT code FROM workflow_state").fetchall()}
    missing = sorted(declared - persisted)
    extra = sorted(persisted - declared)
    detail = f"{len(persisted)} states == common.json#/$defs/WorkflowState ({len(declared)} codes)"
    if missing:
        detail += f"; absent from the database: {', '.join(missing)}"
    if extra:
        detail += f"; not in the contract: {', '.join(extra)}"
    return Check("workflow_state == contract", not missing and not extra, detail)


def check_reference_counts(connection: Any, expected: dict[str, int]) -> Check:
    """The reference tables hold the rows the migration declares, table by table."""
    wrong: list[str] = []
    detail: list[str] = []
    for table, wanted in expected.items():
        # The table name is a module constant (`REFERENCE_TABLES`), never user input; the count is
        # the only value that could be a parameter and it is not one.
        actual = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        detail.append(f"{table}={actual}/{wanted}")
        if actual != wanted:
            wrong.append(table)
    text = ", ".join(detail)
    if wrong:
        text += f" -- {', '.join(wrong)} does not match the migration"
    return Check("reference row counts", not wrong, text)


def check_state_machine_is_whole(connection: Any) -> Check:
    """Every non-terminal state can be left, and every state can be arrived at from `INTAKE`.

    Neither is a foreign key: the constraints in 003 refuse a transition naming an unknown state,
    but nothing refuses a state that *no* transition reaches or leaves. Such a state is a demo that
    dead-ends with no error, which is the failure mode this check was written for.
    """
    states = {row[0] for row in connection.execute("SELECT code FROM workflow_state").fetchall()}
    terminal = {
        row[0]
        for row in connection.execute("SELECT code FROM workflow_state WHERE terminal").fetchall()
    }
    transitions = {
        (row[0], row[1])
        for row in connection.execute(
            "SELECT from_state, to_state FROM workflow_transition"
        ).fetchall()
    }

    dead_ends = sorted(
        state
        for state in states - terminal
        if not any(source == state for source, _ in transitions)
    )

    reached = {ENTRY_STATE}
    frontier = deque([ENTRY_STATE])
    while frontier:
        current = frontier.popleft()
        for source, target in transitions:
            if source == current and target not in reached:
                reached.add(target)
                frontier.append(target)
    unreachable = sorted(states - reached)

    problems: list[str] = []
    if dead_ends:
        problems.append(f"non-terminal states with no outgoing transition: {', '.join(dead_ends)}")
    if unreachable:
        problems.append(f"states unreachable from {ENTRY_STATE}: {', '.join(unreachable)}")
    detail = (
        f"{len(states)} states, {len(transitions)} transitions, all reachable from {ENTRY_STATE},"
        " no dead ends"
    )
    if problems:
        detail = "; ".join(problems)
    return Check("state machine is whole", not problems, detail)


# ---------------------------------------------------------------------------
# The mock API
# ---------------------------------------------------------------------------


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise SeedFailure(f"GET {url} answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SeedFailure(f"GET {url} failed: {type(exc).__name__}") from exc


def check_mock_api_seeded_from_fixtures(health: dict[str, Any], fixtures_dir: Path) -> Check:
    """`/health` says the service is serving *a* fixtures directory, not its embedded dataset.

    `seed.mode: "builtin"` is the failure this exists for. The built-in dataset is coherent on its
    own, answers every request, and shares no id with `fixtures/scenarios.json` — so the demo would
    run and every scenario would reference a hospital that does not exist. The service reports
    `source_directory: null` in that mode, so the mode and the presence of a source are what
    distinguish the two.

    **The absolute path is not compared, and that is a correction.** The mock API runs in a container
    whose image carries the fixtures at `/app/fixtures` — there is no volume mount, so its path and
    this checkout's path name the same corpus from two different filesystems. Comparing them made
    `make seed` fail against the *shipped* configuration, which is the configuration the demo runs
    in, while every content check beside this one passed. What the previous comparison was reaching
    for is "is it serving our corpus", and that is asked properly by
    `check_seed_report_matches_fixtures`, `check_store_matches_fixtures` and the service's own
    coherence report, all of which read the files this checkout has. What is kept here is the part
    that is genuinely about this script's job: the directory is named `fixtures`, and the mode says
    the loader read one.
    """
    seed = health.get("seed")
    if not isinstance(seed, dict):
        raise SeedFailure(
            "/health reported no `seed` block, so which dataset is being served cannot be read"
        )
    mode = seed.get("mode")
    source = seed.get("source_directory")
    problems: list[str] = []
    if mode != "fixtures":
        problems.append(f'seed.mode is {mode!r}, not "fixtures"')
    if source is None:
        problems.append(
            "seed.source_directory is null, which is what the embedded dataset reports — the "
            "service is not serving a fixtures directory at all"
        )
    elif Path(str(source)).name != fixtures_dir.name:
        problems.append(
            f"seed.source_directory is {source!r}, whose directory is not named "
            f"{fixtures_dir.name!r}; this is a corpus from somewhere else"
        )
    detail = f"mode={mode!r} source={source!r} (this checkout: {fixtures_dir})"
    if problems:
        detail = "; ".join(problems)
    return Check("mock API serves the fixtures", not problems, detail)


def check_seed_report_matches_fixtures(health: dict[str, Any]) -> Check:
    """Each count in the seed report equals the length of the fixture file it names.

    The report comes from the loader, so this says the loader *read* the corpus. Whether the store
    then holds it is `check_store_matches_fixtures`, which is a different question and is asked
    separately on purpose.
    """
    seed = health.get("seed")
    if not isinstance(seed, dict):
        raise SeedFailure("/health reported no `seed` block")
    wrong: list[str] = []
    detail: list[str] = []
    for field, fixture in SEED_REPORT_FIXTURES:
        wanted = len(read_fixture_records(fixture))
        reported = seed.get(field)
        detail.append(f"{field}={reported}/{wanted}")
        if reported != wanted:
            wrong.append(field)
    text = ", ".join(detail)
    if wrong:
        text = f"{', '.join(wrong)} does not match the fixture file -- " + text
    return Check("seed report == fixture files", not wrong, text)


def _probe_urls(probe: ListProbe, base_url: str) -> list[str]:
    """The URLs one probe needs, with any grouping derived from its own fixture file."""
    root = base_url.rstrip("/") + probe.endpoint
    if probe.group_by is None or probe.query_param is None:
        return [root]
    values: list[str] = []
    for record in read_fixture_records(probe.fixture):
        value = record.get(probe.group_by)
        if not isinstance(value, str) or not value:
            raise SeedFailure(f"{probe.fixture} has a record with no string {probe.group_by!r}")
        if value not in values:
            values.append(value)
    return [f"{root}?{urllib.parse.urlencode({probe.query_param: value})}" for value in values]


def check_store_matches_fixtures(base_url: str) -> Check:
    """The ids the API serves are exactly the fixture ids, for every listable fixture file.

    This is the check the loader's own report cannot make. `seedFromFixtures` returns the counts it
    read, not the counts it wrote, and it writes slots with `INSERT OR IGNORE` — deliberately, so a
    restart cannot resurrect a slot a live booking holds. The consequence is that a slot deleted from
    the fixtures stays in the store, and one edited in place never lands. Comparing *ids* rather than
    counts is what makes that visible: a same-count drift is invisible to a count.
    """
    problems: list[str] = []
    detail: list[str] = []
    for probe in LIST_PROBES:
        wanted = fixture_ids(probe.fixture, probe.id_key)
        served: set[str] = set()
        for url in _probe_urls(probe, base_url):
            payload = _get_json(url)
            if not isinstance(payload, dict) or not isinstance(payload.get(probe.envelope), list):
                raise SeedFailure(f"GET {url} did not answer a {probe.envelope!r} list")
            for record in payload[probe.envelope]:
                if not isinstance(record, dict) or not isinstance(record.get(probe.id_key), str):
                    raise SeedFailure(
                        f"GET {url} returned a record without a string {probe.id_key!r}"
                    )
                served.add(record[probe.id_key])
        missing = sorted(wanted - served)
        extra = sorted(served - wanted)
        detail.append(f"{probe.envelope}={len(served)}/{len(wanted)}")
        if missing:
            problems.append(
                f"{probe.envelope}: {len(missing)} fixture id(s) not served, e.g. {missing[0]}"
            )
        if extra:
            problems.append(
                f"{probe.envelope}: {len(extra)} served id(s) not in the fixtures, e.g. {extra[0]}"
            )
    text = ", ".join(detail)
    if problems:
        text = "; ".join(problems) + " -- " + text
    return Check("store == fixture ids", not problems, text)


def check_service_coherence(health: dict[str, Any]) -> Check:
    """The service's own report of its dataset is clean.

    `fixture_load_errors` and `scenario_manifest` are computed by the mock API by cross-checking
    `fixtures/scenarios.json` against the files on disk, so asserting them covers the fault-scenario
    corpus and the manifest without this script re-implementing either.
    """
    errors = health.get("fixture_load_errors")
    if not isinstance(errors, list):
        raise SeedFailure("/health reported no `fixture_load_errors` list")
    manifest = health.get("scenario_manifest")
    if not isinstance(manifest, dict):
        raise SeedFailure("/health reported no `scenario_manifest` object")
    missing = manifest.get("missing_files") or []
    unreferenced = manifest.get("unreferenced_files") or []

    problems: list[str] = []
    if errors:
        problems.append(f"fixture_load_errors: {errors}")
    if not manifest.get("found"):
        problems.append("scenarios.json was not found")
    if not manifest.get("complete"):
        problems.append("the scenario manifest is not complete")
    if missing:
        problems.append(f"files the manifest names but that are absent: {missing}")
    if unreferenced:
        problems.append(f"files no scenario references: {unreferenced}")
    detail = "no load errors; scenarios.json found, complete, with no missing or unreferenced files"
    if problems:
        detail = "; ".join(problems)
    return Check("mock API corpus coherent", not problems, detail)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _report(checks: list[Check]) -> None:
    width = max(len(check.name) for check in checks)
    for check in checks:
        print(f"  {'PASS' if check.ok else 'FAIL'}  {check.name:<{width}}  {check.detail}")


def _check(checks: list[Check], name: str, operation: Callable[[], Check]) -> None:
    """Run one check, turning a failure into a failed check rather than an abort.

    A `SeedFailure` is an expected condition and is reported as written. Anything else is caught
    too, and that is deliberate — it follows `scripts/health.py`, which catches broadly around its
    PostgreSQL probe for the same reason: the alternative is enumerating driver exceptions in a list
    that goes stale, and a verifier that tracebacks tells the reader nothing about the data. It
    reports the exception's type and text, so a genuine bug still arrives with its name attached.
    """
    try:
        checks.append(operation())
    except SeedFailure as exc:
        checks.append(Check(name, False, str(exc)))
    except Exception as exc:  # the type and text are both reported, so nothing is swallowed
        checks.append(Check(name, False, f"{type(exc).__name__}: {str(exc)[:200]}"))


def _verify_postgres(checks: list[Check], database_url: str, expected: dict[str, int]) -> None:
    if not database_url:
        checks.append(Check("database reachable", False, "DATABASE_URL is not set"))
        return
    try:
        connection = _connect(database_url)
    except SeedFailure as exc:
        checks.append(Check("database reachable", False, str(exc)))
        return
    with connection:
        _check(checks, "migrations applied", lambda: check_migrations_applied(connection))
        _check(
            checks,
            "workflow_state == contract",
            lambda: check_vocabulary_matches_contract(connection),
        )
        _check(
            checks,
            "reference row counts",
            lambda: check_reference_counts(connection, expected),
        )
        _check(
            checks,
            "state machine is whole",
            lambda: check_state_machine_is_whole(connection),
        )


def _verify_mock_api(checks: list[Check], mock_api_url: str) -> None:
    if not mock_api_url:
        checks.append(Check("mock API reachable", False, "MOCK_API_URL is not set"))
        return
    try:
        health = _get_json(mock_api_url.rstrip("/") + "/health")
        if not isinstance(health, dict):
            raise SeedFailure("/health did not answer a JSON object")
    except SeedFailure as exc:
        # Not a lesser failure than a wrong dataset: the corpus is loaded by the service at startup,
        # so a service that is not running is a corpus that is not loaded, and reporting "OK" here
        # would be the exact lie this script replaces.
        checks.append(
            Check(
                "mock API reachable",
                False,
                f"{exc}. The fixtures are loaded by the service when it starts, so if it is not "
                "running nothing is loaded: `make up`, then re-run `make seed`.",
            )
        )
        return

    _check(
        checks,
        "mock API serves the fixtures",
        lambda: check_mock_api_seeded_from_fixtures(health, FIXTURES_DIR),
    )
    _check(
        checks,
        "seed report == fixture files",
        lambda: check_seed_report_matches_fixtures(health),
    )
    _check(
        checks,
        "store == fixture ids",
        lambda: check_store_matches_fixtures(mock_api_url),
    )
    _check(
        checks,
        "mock API corpus coherent",
        lambda: check_service_coherence(health),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the demo's data is loaded and matches its sources."
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL DSN to check (default: DATABASE_URL from .env / the environment)",
    )
    parser.add_argument(
        "--mock-api-url",
        default=None,
        help="base URL of the mock API to check (default: MOCK_API_URL from .env)",
    )
    args = parser.parse_args(argv)

    print("referraleasy seed")
    print("=" * 72)
    if run_env_validation() != 0:
        print(
            "\nSTOPPED: the environment is not runnable, so no dataset state could mean anything.",
            file=sys.stderr,
        )
        return EXIT_ENV_INVALID

    env = _read_dotenv()
    database_url = args.database_url or env.get("DATABASE_URL", "")
    mock_api_url = args.mock_api_url or env.get("MOCK_API_URL", "")

    try:
        expected_counts = expected_reference_counts()
    except SeedFailure as exc:
        # Not a failed check: the *expectations* could not be built, so nothing verified afterwards
        # would mean anything. A dataset whose contents cannot even be described is worse than one
        # dataset being wrong, and this must not be reported as a partial pass.
        print(f"\nSTOPPED: {exc}", file=sys.stderr)
        return EXIT_NOT_SEEDED

    # Every expectation this run compares against, printed before the verdicts so a reader can see
    # where each number came from rather than taking the summary on trust.
    print("\nExpectations, read from this repository rather than restated here:")
    print(f"  migrations       {MIGRATIONS_DIR.relative_to(REPO_ROOT)}/*.sql")
    print(f"  vocabulary       {VOCABULARY_MIGRATION.relative_to(REPO_ROOT)} + later additions")
    print("  states           packages/contracts/schemas/common.json#/$defs/WorkflowState")
    print(
        "  reference rows   "
        + ", ".join(f"{table}={count}" for table, count in expected_counts.items())
    )
    print(
        f"  fixtures         {FIXTURES_DIR.relative_to(REPO_ROOT)}/"
        f" ({len(SEED_REPORT_FIXTURES)} of its files feed the mock API)"
    )

    checks: list[Check] = []

    print("\nPostgreSQL")
    start = len(checks)
    _verify_postgres(checks, database_url, expected_counts)
    _report(checks[start:])

    print("\nMock API")
    start = len(checks)
    _verify_mock_api(checks, mock_api_url)
    _report(checks[start:])

    failed = [check for check in checks if not check.ok]
    print()
    if failed:
        # Flushed first so the verdicts above and the failures below cannot interleave out of order
        # when the two streams are captured together, which is how this script gets read in CI.
        sys.stdout.flush()
        # Named individually, because "2 checks failed" is not an actionable sentence and each of
        # these has a different fix.
        print(f"{len(failed)} of {len(checks)} check(s) failed:", file=sys.stderr)
        for check in failed:
            print(f"  {check.name}: {check.detail}", file=sys.stderr)
        print(
            "\nThe demo's numbers are computed from this data, so a failure here is a wrong demo "
            "rather than a missing one. Nothing was written by this script.",
            file=sys.stderr,
        )
        return EXIT_NOT_SEEDED

    print(f"All {len(checks)} check(s) passed. Nothing needed writing: the migrations own the")
    print("reference tables, and the mock API loads the fixtures itself at startup.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
