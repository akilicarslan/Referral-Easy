#!/usr/bin/env python3
"""Forward-only SQL migration runner for ReferralEasy.

Design notes, in order of how much they matter:

* **Forward-only.** A migration that has been applied is never re-run and never
  edited. The ledger stores a sha256 of the file; if a file changes after being
  applied, the runner stops rather than guessing which version of the truth the
  database is holding.

* **No ORM, no dependency beyond psycopg.** The DDL in this directory is meant
  to be read by a human: the constraints are the specification. A migration tool
  that hides the SQL would defeat that, so this runner does the two things a
  migration tool actually has to do -- apply files in order, and remember which
  ones ran -- and nothing else.

* **Statements are split here, not by the driver.** A tiny quote-aware lexer
  walks the file so that a `;` inside a string, a comment, or a `$$`-quoted
  function body does not end a statement. The same lexer backs the
  well-formedness check, which is why that check can run without a database.

* **psycopg is imported lazily** so that `import run` -- and therefore the
  no-database test suite -- works in an environment with no driver installed.

Usage
-----
    uv run python migrations/run.py --status
    uv run python migrations/run.py --database-url postgresql://...
    uv run python migrations/run.py --dry-run

`scripts/migrate.py` is a thin CLI wrapper around this module.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent
FILENAME_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
LEDGER_TABLE = "schema_migrations"

#: Ledger DDL, byte-identical in intent to 001_schema_migrations.sql. The runner
#: does not create the table itself (see `applied_versions`): migration 001 does,
#: and creating it twice would be two places to keep in sync.
LEDGER_COLUMNS = ("version", "name", "filename", "checksum", "applied_at", "duration_ms")


# ---------------------------------------------------------------------------
# SQL lexing
# ---------------------------------------------------------------------------

#: End states of the lexer, used to report *why* a file is malformed.
_UNTERMINATED_DOLLAR = "unterminated dollar-quoted block"
_UNTERMINATED_STRING = "unterminated string literal"
_UNTERMINATED_COMMENT = "unterminated block comment"


def split_statements(sql: str) -> list[str]:
    """Split SQL into statements without being fooled by embedded semicolons.

    Handles, in this precedence order: dollar quoting (``$$`` and ``$tag$``),
    single-quoted strings (with ``''`` escapes), double-quoted identifiers,
    line comments, and nestable block comments.

    Raises `ValueError` when the text ends inside any of those constructs.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]

        # -- line comment ----------------------------------------------------
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            i = end
            continue

        # -- block comment (PostgreSQL allows nesting) ------------------------
        if sql.startswith("/*", i):
            depth = 1
            j = i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise ValueError(_UNTERMINATED_COMMENT)
            i = j
            continue

        # -- dollar quoting: $$ ... $$, $tag$ ... $tag$ ----------------------
        if ch == "$":
            tag_match = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if tag_match:
                tag = tag_match.group(0)
                close = sql.find(tag, i + len(tag))
                if close == -1:
                    raise ValueError(_UNTERMINATED_DOLLAR)
                buf.append(sql[i : close + len(tag)])
                i = close + len(tag)
                continue

        # -- quoted literals and identifiers ---------------------------------
        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    # A doubled quote is an escaped quote, not a terminator.
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    break
                if sql[j] == "\\" and quote == "'":
                    j += 2
                    continue
                j += 1
            if j >= n:
                raise ValueError(_UNTERMINATED_STRING)
            buf.append(sql[i : j + 1])
            i = j + 1
            continue

        # -- statement terminator --------------------------------------------
        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        # Trailing text with no terminator: a truncated file, or a missing `;`.
        raise ValueError(f"statement is not terminated by ';': {tail[:60]!r}")

    return statements


def validate_sql_text(sql: str) -> list[str]:
    """Return a list of well-formedness problems; empty means the file is fine."""
    problems: list[str] = []

    if not sql.strip():
        return ["file is empty"]

    for line in sql.splitlines():
        if line.rstrip() != line:
            problems.append(f"trailing whitespace on line: {line[:60]!r}")
            break

    try:
        statements = split_statements(sql)
    except ValueError as exc:
        return [*problems, str(exc)]

    if not statements:
        problems.append("file contains no statements")

    # A file that is only comments is almost certainly a mistake.
    stripped = re.sub(r"--[^\n]*", "", sql)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL).strip()
    if not stripped:
        problems.append("file contains only comments")

    return problems


def validate_sql_file(path: Path) -> list[str]:
    return validate_sql_text(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    filename: str
    path: Path
    sql: str
    checksum: str

    @property
    def statements(self) -> list[str]:
        return split_statements(self.sql)


def discover(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return migrations in version order, refusing gaps and duplicates."""
    found: dict[int, Path] = {}
    unexpected: list[str] = []

    for path in sorted(migrations_dir.iterdir()):
        if path.is_dir() or path.name.startswith("."):
            continue
        if path.suffix != ".sql":
            continue
        match = FILENAME_RE.match(path.name)
        if not match:
            unexpected.append(path.name)
            continue
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version:03d}: {found[version].name} and {path.name}"
            )
        found[version] = path

    if unexpected:
        raise MigrationError(
            "unrecognised file(s) in migrations/: "
            + ", ".join(unexpected)
            + " (expected NNN_lower_snake_case.sql)"
        )

    if not found:
        raise MigrationError(f"no migrations found in {migrations_dir}")

    versions = sorted(found)
    expected = list(range(1, versions[-1] + 1))
    if versions != expected:
        missing = sorted(set(expected) - set(versions))
        raise MigrationError(
            "migration numbering has gaps: missing " + ", ".join(f"{v:03d}" for v in missing)
        )

    migrations: list[Migration] = []
    for version in versions:
        path = found[version]
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=version,
                name=FILENAME_RE.match(path.name).group(2),
                filename=path.name,
                path=path,
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    return migrations


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


class MigrationError(RuntimeError):
    """Raised for any condition that must stop the run."""


@dataclass(frozen=True)
class LedgerRow:
    version: int
    name: str
    filename: str
    checksum: str
    applied_at: object
    duration_ms: int


def applied_versions(conn) -> dict[int, LedgerRow]:
    """Read the ledger, tolerating its absence on a virgin database."""
    from psycopg import errors, sql

    with conn.cursor() as cur:
        try:
            cur.execute(
                sql.SQL(
                    "SELECT version, name, filename, checksum, applied_at, duration_ms"
                    "  FROM {} ORDER BY version"
                ).format(sql.Identifier(LEDGER_TABLE))
            )
        except errors.UndefinedTable:
            conn.rollback()
            return {}
        rows = cur.fetchall()

    return {row[0]: LedgerRow(*row) for row in rows}


def check_drift(migrations: list[Migration], applied: dict[int, LedgerRow]) -> None:
    """Refuse to continue when an applied migration no longer matches the disk."""
    by_version = {m.version: m for m in migrations}
    problems: list[str] = []

    for version, row in sorted(applied.items()):
        migration = by_version.get(version)
        if migration is None:
            problems.append(
                f"version {version:03d} ({row.filename}) is applied but missing from disk"
            )
        elif migration.checksum != row.checksum:
            problems.append(
                f"{migration.filename} changed after being applied "
                f"(ledger {row.checksum[:12]}..., disk {migration.checksum[:12]}...)"
            )

    if problems:
        raise MigrationError(
            "refusing to run: migrations are forward-only\n  " + "\n  ".join(problems)
        )


def apply_migrations(
    database_url: str,
    *,
    to_version: int | None = None,
    dry_run: bool = False,
    reporter=print,
) -> list[Migration]:
    """Apply pending migrations in order. Returns the ones that ran."""
    import psycopg

    migrations = discover()
    pending: list[Migration] = []

    with psycopg.connect(database_url) as conn:
        applied = applied_versions(conn)
        check_drift(migrations, applied)
        # Close the read transaction so each migration below gets its own.
        conn.commit()

        pending = [m for m in migrations if m.version not in applied]
        if to_version is not None:
            pending = [m for m in pending if m.version <= to_version]

        if not pending:
            reporter("database is up to date; nothing to apply")
            return []

        for migration in pending:
            if dry_run:
                reporter(f"[dry-run] would apply {migration.filename}")
                continue

            started = time.monotonic()
            try:
                # One transaction per file: a migration either lands whole or
                # not at all, and its ledger row is written in the same
                # transaction as the DDL that it records.
                with conn.transaction(), conn.cursor() as cur:
                    for statement in migration.statements:
                        cur.execute(statement)
                    cur.execute(
                        "INSERT INTO schema_migrations"
                        " (version, name, filename, checksum, duration_ms)"
                        " VALUES (%s, %s, %s, %s, %s)",
                        (
                            migration.version,
                            migration.name,
                            migration.filename,
                            migration.checksum,
                            int((time.monotonic() - started) * 1000),
                        ),
                    )
            except Exception as exc:
                reporter(f"applying {migration.filename} ... FAILED")
                raise MigrationError(f"{migration.filename}: {type(exc).__name__}: {exc}") from exc
            reporter(f"applying {migration.filename} ... ok")

    return pending


def describe_status(database_url: str, reporter=print) -> int:
    """Print the ledger and the pending set. Returns an exit code."""
    import psycopg

    migrations = discover()
    with psycopg.connect(database_url) as conn:
        applied = applied_versions(conn)

    reporter(f"{'version':<8}{'filename':<40}{'applied at':<32}checksum")
    for migration in migrations:
        row = applied.get(migration.version)
        if row is None:
            reporter(f"{migration.version:<8}{migration.filename:<40}{'-- pending --':<32}")
        else:
            reporter(
                f"{migration.version:<8}{migration.filename:<40}"
                f"{row.applied_at.isoformat():<32}{row.checksum[:12]}..."
            )

    pending = [m for m in migrations if m.version not in applied]
    reporter("")
    reporter(f"{len(applied)} applied, {len(pending)} pending")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate",
        description="Apply ReferralEasy SQL migrations in order (forward-only).",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL DSN. Defaults to $DATABASE_URL.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the ledger without applying anything.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be applied, without touching the database.",
    )
    parser.add_argument(
        "--to",
        type=int,
        default=None,
        metavar="VERSION",
        help="Stop after this version (inclusive).",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Check every SQL file for well-formedness and numbering. No database needed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.validate_only:
            return _validate_cli()

        if not args.database_url:
            print(
                "error: no database URL. Pass --database-url or set $DATABASE_URL.",
                file=sys.stderr,
            )
            return 2

        if args.status:
            return describe_status(args.database_url)

        applied = apply_migrations(args.database_url, to_version=args.to, dry_run=args.dry_run)
        if not args.dry_run:
            print(f"{len(applied)} migration(s) applied")
        return 0
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _validate_cli() -> int:
    problems: list[str] = []
    try:
        migrations = discover()
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for migration in migrations:
        for problem in validate_sql_file(migration.path):
            problems.append(f"{migration.filename}: {problem}")

    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    print(
        f"{len(migrations)} migration file(s) valid: {migrations[0].version:03d}-{migrations[-1].version:03d}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
