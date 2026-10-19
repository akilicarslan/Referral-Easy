"""The checks `make seed` runs against PostgreSQL.

Marked `integration`: every test here needs a real database, and CI's integration job fails the run
if any integration test skips, so a skip in this file is a failure rather than a pass. That is the
right shape for these tests — the question they ask ("did the migrations leave the reference tables
in the state they declare?") only has an answer against a database.

Each test that needs the data to be *wrong* gets a database of its own from the `scratch` fixture,
created and dropped around the test. The alternative — deleting a row out of the shared test
database — is a test that damages the thing it tests, and `tests/domain/conftest.py` deliberately
leaves the vocabulary tables alone for the same reason.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
import scripts_fixtures as fx

pytestmark = pytest.mark.integration

seed = fx.seed_module()


def _connection(database_url: str):
    """Autocommit: these mutate a scratch database and the check under test opens its own connection."""
    return psycopg.connect(database_url, autocommit=True)


# ---------------------------------------------------------------------------
# The happy path — without this the failing cases prove nothing
# ---------------------------------------------------------------------------


def test_a_migrated_database_passes_every_postgres_check(scratch: str) -> None:
    with _connection(scratch) as connection:
        checks = [
            seed.check_migrations_applied(connection),
            seed.check_vocabulary_matches_contract(connection),
            seed.check_reference_counts(connection, seed.expected_reference_counts()),
            seed.check_state_machine_is_whole(connection),
        ]
    assert [(check.name, check.detail) for check in checks if not check.ok] == []


# ---------------------------------------------------------------------------
# A row that is missing must be reported, not passed over
# ---------------------------------------------------------------------------


def test_a_missing_reference_row_is_reported(scratch: str) -> None:
    """One `policy_rule` row removed out of 47.

    This is the case a non-empty check cannot see: the table still has rows, every foreign key still
    resolves, and the only trace is a count. It matters because `policy_rule` is the guardrail table —
    the rule that disappeared becomes a default-deny at the policy gate, so a tool the flow needs
    starts refusing mid-demo.
    """
    with _connection(scratch) as connection:
        before = connection.execute("SELECT count(*) FROM policy_rule").fetchone()[0]
        connection.execute(
            "DELETE FROM policy_rule WHERE (agent_role, tool_name, workflow_state) ="
            " (SELECT agent_role, tool_name, workflow_state FROM policy_rule"
            "  ORDER BY agent_role, tool_name, workflow_state LIMIT 1)"
        )
        after = connection.execute("SELECT count(*) FROM policy_rule").fetchone()[0]
        assert after == before - 1, (
            "the fixture did not remove a row, so this test would be vacuous"
        )

        check = seed.check_reference_counts(connection, seed.expected_reference_counts())

    assert not check.ok
    assert "policy_rule" in check.detail


def test_a_missing_workflow_state_is_reported(scratch: str) -> None:
    """A state removed from the vocabulary, with its rules and transitions so the FKs allow it.

    Two checks must fire, and the pair is the point: the count says a row is gone, and the contract
    comparison says *which* row the schema declares and the database no longer has.
    """
    with _connection(scratch) as connection:
        connection.execute("DELETE FROM policy_rule WHERE workflow_state = 'RECOVERY'")
        connection.execute(
            "DELETE FROM workflow_transition WHERE from_state = 'RECOVERY' OR to_state = 'RECOVERY'"
        )
        connection.execute("DELETE FROM workflow_state WHERE code = 'RECOVERY'")

        vocabulary = seed.check_vocabulary_matches_contract(connection)
        counts = seed.check_reference_counts(connection, seed.expected_reference_counts())

    assert not vocabulary.ok
    assert "RECOVERY" in vocabulary.detail
    assert not counts.ok
    assert "workflow_state" in counts.detail


def test_a_truncated_transition_table_is_reported(scratch: str) -> None:
    """No transitions at all.

    No foreign key refuses this: every removed row referenced states that still exist. The result is
    a workflow that cannot leave `INTAKE`, which no constraint and no count of the other tables would
    notice.
    """
    with _connection(scratch) as connection:
        connection.execute("DELETE FROM workflow_transition")
        check = seed.check_state_machine_is_whole(connection)

    assert not check.ok
    assert "no outgoing transition" in check.detail
    assert "unreachable" in check.detail


def test_a_database_with_no_migrations_says_what_to_run(database_url: str) -> None:
    """A fresh clone, `make seed` before `make migrate`.

    This is the most likely way to meet this script for the first time, so the message names the
    command that fixes it. It was a traceback until this test existed, which is why it is here:
    `relation "schema_migrations" does not exist` tells a reader what broke and not what to do.
    """
    with (
        fx.scratch_database(database_url, name="referraleasy_seed_unmigrated") as virgin,
        _connection(virgin) as connection,
        pytest.raises(seed.SeedFailure, match="make migrate"),
    ):
        seed.check_migrations_applied(connection)


def test_an_unmigrated_database_is_reported_by_the_cli_rather_than_crashing_it(
    database_url: str,
) -> None:
    """The same state, through the entry point an operator actually uses.

    The check raising is the right contract — "cannot tell" is not the same verdict as "wrong" — but
    it is only correct if the script catches it. A `SeedFailure` escaping to the top is a traceback
    and exit 1, which reads as "the environment is broken" for a database that merely needs
    `make migrate`.
    """
    with (
        fx.scratch_database(database_url, name="referraleasy_seed_unmigrated") as virgin,
        fx.fake_mock_api() as api_url,
    ):
        result = fx.run_seed(virgin, api_url)

    assert result.returncode == seed.EXIT_NOT_SEEDED, fx.describe(result)
    assert "make migrate" in result.stderr, fx.describe(result)
    assert "Traceback" not in result.stderr, fx.describe(result)


# ---------------------------------------------------------------------------
# The ledger is compared against the files, in both directions
# ---------------------------------------------------------------------------


def test_an_edited_migration_file_is_reported(scratch: str, tmp_path: Path) -> None:
    """The file changed after it was applied, so the database is neither the old nor the new schema.

    Staged by pointing the check at a directory holding one migration *by name* with different
    content, rather than by editing a real migration — a test must not damage the thing it tests, and
    `migrations/` belongs to the schema rather than to this suite.
    """
    real = sorted((fx.REPO_ROOT / "migrations").glob("*.sql"))[0]
    (tmp_path / real.name).write_text("-- not what was applied\n", encoding="utf-8")

    original = seed.MIGRATIONS_DIR
    seed.MIGRATIONS_DIR = tmp_path
    try:
        with _connection(scratch) as connection:
            check = seed.check_migrations_applied(connection)
    finally:
        seed.MIGRATIONS_DIR = original

    assert not check.ok
    assert f"edited after being applied: {real.name}" in check.detail


def test_a_migration_on_disk_that_was_never_applied_is_reported(
    scratch: str, tmp_path: Path
) -> None:
    """A file added but not migrated: the schema the demo would run against does not exist yet."""
    for path in sorted((fx.REPO_ROOT / "migrations").glob("*.sql")):
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "099_not_applied.sql").write_text("-- pending\n", encoding="utf-8")

    original = seed.MIGRATIONS_DIR
    seed.MIGRATIONS_DIR = tmp_path
    try:
        with _connection(scratch) as connection:
            check = seed.check_migrations_applied(connection)
    finally:
        seed.MIGRATIONS_DIR = original

    assert not check.ok
    assert "099_not_applied.sql" in check.detail


# ---------------------------------------------------------------------------
# The expectations themselves
# ---------------------------------------------------------------------------


def test_the_declared_counts_are_the_ones_the_migration_produced(scratch: str) -> None:
    """The parse and the database agree.

    They are two independent readings of the same migration — the file's text and PostgreSQL's
    interpretation of it — so agreement is evidence that the parser reads what it claims to.
    """
    declared = seed.expected_reference_counts()
    with _connection(scratch) as connection:
        for table, count in declared.items():
            actual = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            assert actual == count, f"{table}: migration declares {count}, database has {actual}"


def test_the_row_count_parser_counts_rows_rather_than_lines() -> None:
    """A synthetic block exercising the two shapes that defeat line counting.

    A row wrapped over several lines is still one row, and a parenthesis inside a string literal does
    not open a nested tuple — both occur in `014_align_domain_vocabulary.sql`, and counting lines
    instead of structure under-reports by exactly the number of wrapped rows.
    """
    sql = (
        "INSERT INTO example (a, b) VALUES\n"
        "    ('one', NULL),\n"
        "    -- a comment between rows\n"
        "    ('two', 'a note that mentions (§4) and (parens)'),\n"
        "    ('three',\n"
        "     'a note long enough to wrap'),\n"
        "    ('four', 'it''s escaped');\n"
        "INSERT INTO other (a) VALUES\n"
        "    ('not this block');\n"
    )
    assert seed._count_values_rows(sql, "example") == 4
    assert seed._count_values_rows(sql, "other") == 1


def test_a_block_the_parser_cannot_read_raises_rather_than_returning_zero() -> None:
    """A count that cannot be derived must not silently become a check that always passes."""
    with pytest.raises(seed.SeedFailure, match="no `INSERT INTO example` block"):
        seed._count_values_rows("INSERT INTO something_else (a) VALUES ('x');\n", "example")


# ---------------------------------------------------------------------------
# End to end, through the CLI
# ---------------------------------------------------------------------------


def test_the_cli_reports_a_missing_row_rather_than_passing(scratch: str) -> None:
    """The whole script, against a database whose guardrail table is one row short.

    Nothing else is wrong: the migrations are applied, the vocabulary matches the contract, and the
    API answers from the fixtures. The run must still fail, name the table, and exit 2 — a check that
    only fires inside a unit test has not been shown to be wired up.
    """
    with _connection(scratch) as connection:
        connection.execute(
            "DELETE FROM policy_rule WHERE (agent_role, tool_name, workflow_state) ="
            " (SELECT agent_role, tool_name, workflow_state FROM policy_rule"
            "  ORDER BY agent_role, tool_name, workflow_state LIMIT 1)"
        )

    with fx.fake_mock_api() as api_url:
        result = fx.run_seed(scratch, api_url)

    assert result.returncode == seed.EXIT_NOT_SEEDED, fx.describe(result)
    assert "policy_rule" in result.stderr, fx.describe(result)
    assert "migrations applied" not in result.stderr, "an unrelated check failed too"


def test_the_cli_exits_zero_on_a_database_that_matches_its_migration(scratch: str) -> None:
    """The control for the test above, and the reason it means anything.

    Both run the same script against the same kind of database; only one of them has a row removed.
    """
    with fx.fake_mock_api() as api_url:
        result = fx.run_seed(scratch, api_url)

    assert result.returncode == seed.EXIT_OK, fx.describe(result)
    assert "check(s) passed" in result.stdout, fx.describe(result)
