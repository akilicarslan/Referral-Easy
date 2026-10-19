"""Fixtures for the `tests/scripts` suite.

Kept to fixtures only: the shared *names* live in `scripts_fixtures.py`, because a name imported
from `conftest` resolves to whichever directory's conftest pytest imported last (the rule
`tests/test_test_layout.py` enforces).

The two kinds of test in this directory split the same way they do in `tests/domain`:

* the mock-API checks are pure functions over a URL and a payload, so they run against the stub in
  `scripts_fixtures.py` with no service and no database, and are **not** marked `integration`;
* the database checks need PostgreSQL, so they are marked `integration` and gate on
  `TEST_DATABASE_URL`, exactly as `tests/domain/conftest.py` does.

That split is not cosmetic. CI's integration job provides PostgreSQL and **nothing else**, and it
fails the job if any integration test skips — so an integration test that needed the mock API service
would be a test that can never legitimately run there.
"""

from __future__ import annotations

import pytest
import scripts_fixtures as fx


@pytest.fixture(scope="session")
def database_url() -> str:
    if not fx.TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set; integration tests need a database")
    return fx.TEST_DATABASE_URL


@pytest.fixture
def scratch(database_url: str) -> str:
    """A freshly migrated database of this test's own, dropped when the test ends.

    Migrated rather than assumed: the checks under test ask whether the migrations did their job, so
    a test that borrowed somebody else's already-migrated database would be asking a different
    question.
    """
    with fx.scratch_database(database_url) as url:
        fx.apply_migrations(url)
        yield url
