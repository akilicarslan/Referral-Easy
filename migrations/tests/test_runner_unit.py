"""Tests that always run: no database, no driver, no container.

A deployment pipeline that only finds out a migration is unparseable when it has
already started applying it is a pipeline that fails in the worst place. These
tests check the migration set as an artifact: numbered, ordered, well-formed.
"""

from __future__ import annotations

import pytest
from migration_fixtures import MIGRATIONS_DIR, runner

# ---------------------------------------------------------------------------
# The migration set itself
# ---------------------------------------------------------------------------


def test_every_file_is_numbered_without_gaps():
    migrations = runner.discover()

    versions = [m.version for m in migrations]
    assert versions == sorted(versions), "migrations must be discovered in version order"
    assert versions[0] == 1, "numbering starts at 001"
    assert versions == list(range(1, len(versions) + 1)), (
        f"numbering has gaps or duplicates: {versions}"
    )


def test_filenames_follow_the_naming_rule():
    for migration in runner.discover():
        assert migration.filename == f"{migration.version:03d}_{migration.name}.sql"
        assert migration.name.islower(), f"{migration.filename}: name must be lower_snake_case"


def test_every_migration_is_well_formed():
    problems = []
    for migration in runner.discover():
        for problem in runner.validate_sql_file(migration.path):
            problems.append(f"{migration.filename}: {problem}")
    assert not problems, "malformed SQL files:\n  " + "\n  ".join(problems)


def test_every_migration_has_a_header_comment():
    """Each file should say what it is for. The DDL is the spec; a file without
    a rationale is a file nobody can safely change."""
    missing = []
    for migration in runner.discover():
        head = migration.sql.lstrip()
        if not head.startswith("--"):
            missing.append(migration.filename)
    assert not missing, f"missing a leading comment block: {missing}"


def test_checksums_are_stable_and_distinct():
    first = runner.discover()
    second = runner.discover()

    assert [m.checksum for m in first] == [m.checksum for m in second]
    assert len({m.checksum for m in first}) == len(first), "two migrations have identical content"


def test_ledger_columns_match_the_ledger_ddl():
    """The runner writes to schema_migrations; 001 creates it. If they drift,
    the first migration of a fresh database is what breaks."""
    ddl = (MIGRATIONS_DIR / "001_schema_migrations.sql").read_text(encoding="utf-8")
    for column in runner.LEDGER_COLUMNS:
        assert column in ddl, f"ledger column {column!r} is missing from 001"


# ---------------------------------------------------------------------------
# Numbering failures really do stop the run
# ---------------------------------------------------------------------------


def _write(directory, name: str, body: str = "SELECT 1;\n") -> None:
    (directory / name).write_text(body, encoding="utf-8")


def test_a_gap_in_numbering_is_refused(tmp_path):
    _write(tmp_path, "001_first.sql")
    _write(tmp_path, "003_third.sql")

    with pytest.raises(runner.MigrationError, match="gaps"):
        runner.discover(tmp_path)


def test_a_duplicate_version_is_refused(tmp_path):
    _write(tmp_path, "001_first.sql")
    _write(tmp_path, "002_second.sql")
    # Same version, different name: two files claiming to be migration 002.
    (tmp_path / "002_other.sql").write_text("SELECT 1;\n", encoding="utf-8")

    with pytest.raises(runner.MigrationError, match="duplicate"):
        runner.discover(tmp_path)


def test_an_unrecognised_filename_is_refused(tmp_path):
    _write(tmp_path, "001_first.sql")
    _write(tmp_path, "notes.sql")

    with pytest.raises(runner.MigrationError, match="unrecognised"):
        runner.discover(tmp_path)


def test_a_missing_terminator_is_reported(tmp_path):
    _write(tmp_path, "001_first.sql", "CREATE TABLE t (id int)\n")

    problems = runner.validate_sql_file(tmp_path / "001_first.sql")
    assert any("not terminated" in problem for problem in problems)


# ---------------------------------------------------------------------------
# The statement splitter
# ---------------------------------------------------------------------------


def test_splitter_is_not_fooled_by_a_semicolon_in_a_string():
    statements = runner.split_statements("INSERT INTO t VALUES ('a;b'); SELECT 2;")
    assert len(statements) == 2
    assert statements[0].endswith("'a;b')")


def test_splitter_is_not_fooled_by_a_semicolon_in_a_comment():
    sql = "SELECT 1; -- a comment; with a semicolon\nSELECT 2;"
    assert len(runner.split_statements(sql)) == 2


def test_splitter_is_not_fooled_by_a_semicolon_in_a_dollar_quoted_body():
    sql = """
    CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$
        SELECT 1; -- this semicolon is inside the body
    $$;
    SELECT f();
    """
    statements = runner.split_statements(sql)
    assert len(statements) == 2, statements
    assert statements[0].rstrip().endswith("$$")


def test_splitter_handles_tagged_dollar_quotes():
    sql = "CREATE FUNCTION g() RETURNS int LANGUAGE sql AS $fn$ SELECT 1; $fn$;"
    assert len(runner.split_statements(sql)) == 1


def test_splitter_handles_doubled_quotes():
    sql = "SELECT 'it''s; fine';"
    assert len(runner.split_statements(sql)) == 1


def test_splitter_handles_nested_block_comments():
    sql = "/* outer /* inner; */ still a comment; */ SELECT 1;"
    assert len(runner.split_statements(sql)) == 1


def test_an_unterminated_dollar_quote_is_an_error():
    with pytest.raises(ValueError, match="unterminated dollar"):
        runner.split_statements("CREATE FUNCTION f() AS $$ SELECT 1;")


# ---------------------------------------------------------------------------
# Drift detection is pure logic, so it can be tested without a database
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, filename: str, checksum: str) -> None:
        self.filename = filename
        self.checksum = checksum


def test_drift_is_refused_when_an_applied_file_changed():
    migrations = runner.discover()
    applied = {m.version: _Row(m.filename, "0" * 64) for m in migrations}

    with pytest.raises(runner.MigrationError, match="changed after being applied"):
        runner.check_drift(migrations, applied)


def test_drift_is_refused_when_an_applied_file_disappeared():
    migrations = runner.discover()
    applied = {m.version: _Row(m.filename, m.checksum) for m in migrations}
    applied[999] = _Row("999_ghost.sql", "0" * 64)

    with pytest.raises(runner.MigrationError, match="missing from disk"):
        runner.check_drift(migrations, applied)


def test_drift_check_passes_for_an_untouched_set():
    migrations = runner.discover()
    applied = {m.version: _Row(m.filename, m.checksum) for m in migrations}
    runner.check_drift(migrations, applied)
