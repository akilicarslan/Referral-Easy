"""Shared fixtures for the migration test suite.

Two kinds of test live in this directory:

* **Unit tests** (`test_runner_unit.py`) inspect the SQL files themselves. They
  need no database and no driver, and they always run -- a migration that nobody
  can even parse should fail in CI before anyone starts a container.

* **Integration tests** exercise the constraints against a real PostgreSQL.
  They are marked `integration` and skip unless `TEST_DATABASE_URL` is set:

      docker run --rm -d --name re-mig-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=referraleasy \\
        -p 55432:5432 postgres:16-alpine
      TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/referraleasy \\
        uv run pytest migrations -v -m integration

  These tests are the point of the exercise. A constraint that has never
  refused a write is a comment, not a constraint.
"""

from __future__ import annotations

import pytest
from migration_fixtures import (
    TEST_DATABASE_URL,
    _load_runner,
    reset,
)


def pytest_configure(config: pytest.Config) -> None:
    if not TEST_DATABASE_URL:
        config.addinivalue_line(
            "markers",
            "integration: needs a live PostgreSQL in TEST_DATABASE_URL",
        )


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


def _require_driver() -> None:
    """Fail with the fix, not with a bare ImportError.

    The driver is deliberately not a hard dependency of the unit tests, so a
    missing psycopg is a normal state -- just one that should say how to
    resolve itself.
    """
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "the integration tests need psycopg (v3), which is not installed in "
            "this environment.\n"
            "Either add it to the workspace:\n"
            "    uv add --dev 'psycopg[binary]'\n"
            "or run these tests with it supplied for the one command:\n"
            "    uv run --with 'psycopg[binary]' python -m pytest migrations -m integration"
        ) from exc


@pytest.fixture(scope="session")
def database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set; integration tests need a database")
    _require_driver()
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """Apply every migration once per session, then hand the URL to the tests.

    `_load_runner` is imported rather than defined here because the helper block moved to
    `migration_fixtures.py`; the module is loaded by path, since `migrations/run.py` is a script
    rather than an importable package member.
    """
    runner = _load_runner()
    runner.apply_migrations(database_url, reporter=lambda *a, **k: None)
    return database_url


@pytest.fixture
def db(migrated_database: str):
    """A connection with an empty data set, rows returned as dicts.

    Not autocommit: a test that needs a row lock held across statements (the
    SKIP LOCKED test, the claim races) gets one for free, and teardown is a
    rollback.
    """
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(migrated_database, row_factory=dict_row) as conn:
        reset(conn)
        yield conn
        conn.rollback()


@pytest.fixture
def connect_factory(migrated_database: str):
    """Factory for extra independent connections. Races need more than one."""
    import psycopg
    from psycopg.rows import dict_row

    opened: list = []

    def _connect(*, autocommit: bool = False):
        conn = psycopg.connect(migrated_database, autocommit=autocommit, row_factory=dict_row)
        opened.append(conn)
        return conn

    yield _connect

    for conn in opened:
        try:
            conn.rollback()
            conn.close()
        except Exception:  # pragma: no cover - best-effort teardown
            pass
