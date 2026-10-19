"""Fixtures for the worker tests.

Fixtures only. Constants and doubles live in `worker_doubles.py`, and test modules must import them
from there rather than from here: importing a `conftest` by name is order-dependent and silently
picks up another directory's conftest. `worker_doubles.py` explains that at length,
`tests/test_test_layout.py` enforces it, and nothing in this directory does it.

Two connections, on purpose. :func:`db` is the test's connection and :func:`db2` is a **second, real
connection to the same database** — a separate backend, a separate transaction, a separate
`owner_worker_id`/claim. §7's requirements are about two replicas working one workflow, so the tests
that assert them use two connections and commit where a replica would commit; a test that faked the
second replica in-process would prove nothing about a lease, a generation or a lost claim.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from worker_doubles import (
    MIGRATIONS_RUNNER,
    SPEC_CAPS,
    FakeClock,
    ScriptedBookingApi,
    ScriptedSender,
    Seeder,
    expect_refusal,
    reset,
)

from referraleasy_workers.settings import WorkerSettings


def _database_env(url: str) -> dict[str, str]:
    """The worker environment, stated explicitly.

    Built as a dict rather than read from `os.environ`: a test that inherited the developer's shell
    would be testing the developer's shell. The caps are stated as the string values the runtime
    parses, because that is the path a deployment takes — and they are *derived* from
    `worker_doubles.SPEC_CAPS` rather than restated, so the environment this fixture hands the worker
    and the numbers the tests assert against cannot drift into two sets of "the spec's numbers".
    """
    return {
        "DATABASE_URL": url,
        "WORKER_ID": "w-1",
        "WORKER_LEASE_SECONDS": "600",
        "WORKER_POLL_SECONDS": "0.1",
        # The worker's own knobs carry the `WORKER_` prefix; the four URLs and the caps do not,
        # because they are read from the same environment the agents read (§8).
        "WORKER_OUTBOX_CLAIM_LIMIT": "10",
        "WORKER_OUTBOX_LEASE_SECONDS": "60",
        "WORKER_OUTBOX_BACKOFF_BASE_SECONDS": "5",
        "WORKER_OUTBOX_BACKOFF_CAP_SECONDS": "3600",
        "WORKER_RECONCILE_LOOKUP_BUDGET": "5",
        "BOOKING_API_URL": "http://booking.invalid/",
        "NOTIFICATION_URL": "http://notify.invalid/",
        "AGENT_ATTEMPT_TIMEOUT_MS": str(SPEC_CAPS.attempt_timeout_ms),
        "AGENT_MAX_ATTEMPTS": str(SPEC_CAPS.max_attempts),
        "TURN_DEADLINE_MS": str(SPEC_CAPS.turn_deadline_ms),
        "HTTP_TOOL_TIMEOUT_MS": str(SPEC_CAPS.http_tool_timeout_ms),
    }


@pytest.fixture(scope="session")
def database_url() -> str:
    """The test database URL, skipping (loudly) when it is not configured.

    The skip is the honest answer for a unit-test run with no database; the CI integration job sets
    the variable and then fails if anything under `-m integration` skips, which is what keeps this
    from being a way to go green while testing nothing.
    """
    import os

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; the worker tests need a real PostgreSQL")
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "the worker tests need psycopg (v3), which is not installed.\n"
            "    uv add --dev 'psycopg[binary]'"
        ) from exc
    return url


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """Apply every migration once per session, then hand the URL to the tests.

    The same runner the migrations' own tests and `scripts/migrate.py` use, loaded from the file
    rather than imported as a package: `migrations/` is not a workspace member, and a test helper
    that duplicated the runner's logic would be able to disagree with it.
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "referraleasy_migrations_for_workers", MIGRATIONS_RUNNER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.apply_migrations(database_url, reporter=lambda *args, **kwargs: None)
    return database_url


def _connect(url: str) -> Any:
    """One real connection, with the dict row factory — and two bounds that this database needs.

    The doubles in this directory read their rows by column name, and the domain's stores build
    dicts from `cursor.description` whatever factory is configured — so the named factory is a
    convenience for the tests rather than a requirement of the code under test. The domain's own
    tests use tuples for exactly that reason; here the fixture helpers are the heavier user.

    `TEST_DATABASE_URL` is shared with the three other suites in this repo, and each of them empties
    the same twelve tables between tests with `TRUNCATE`. A test's connection is deliberately not
    autocommit, so while a test is running it *holds* the locks on the rows it seeded, and a
    neighbour's `TRUNCATE` needs `ACCESS EXCLUSIVE` on all twelve tables — so the two wait on each
    other, in opposite directions, in two different processes. PostgreSQL will not break that cycle:
    the waiting side of one of the pairs is *idle in transaction*, which is not a lock wait at all,
    so the advertised deadlock detector never sees it and the run hangs rather than failing. The two
    settings make that state impossible instead of unlikely:

    * `lock_timeout` — a statement that cannot get its lock fails in fifteen seconds with a message
      that says so, rather than hanging until someone notices. Fifteen seconds is far longer than any
      statement in this suite legitimately waits.
    * `idle_in_transaction_session_timeout` — an *idle* transaction that holds locks is terminated by
      the server, which releases them and lets the neighbour's `TRUNCATE` finish. This is the half
      that fixes the livelock for the other suites as well as for this one: without it, a hung
      workers run blocks `tests/domain` and `tests/gateway` too, which is precisely backwards from
      what a shared fixture should do.
    """
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        url,
        row_factory=dict_row,
        options="-c lock_timeout=15000 -c idle_in_transaction_session_timeout=15000",
    )


@pytest.fixture
def db(migrated_database: str) -> Iterator[Any]:
    """The test's connection: empty data set, not autocommit.

    Not autocommit so that a refusal can be isolated with a savepoint rather than by rolling the whole
    transaction back — a rollback would discard the rows the test just seeded, leaving it unable to
    check that a refused write changed nothing.
    """
    with _connect(migrated_database) as conn:
        reset(conn)
        yield conn
        conn.rollback()


@pytest.fixture
def db2(migrated_database: str) -> Iterator[Any]:
    """A **second real connection** — the other replica.

    A distinct backend with its own transaction and its own view of the row, which is the only way to
    test a lease, a generation, a `FOR UPDATE SKIP LOCKED` claim or a `command_id` race honestly. It
    does *not* reset the database: :func:`db` already did, and a second `TRUNCATE` would delete the
    rows the test seeded between the two fixtures.
    """
    with _connect(migrated_database) as conn:
        yield conn
        conn.rollback()


@pytest.fixture
def seed(db: Any) -> Seeder:
    """Seeding helpers bound to the test's connection."""
    return Seeder(db)


@pytest.fixture
def seed2(db2: Any) -> Seeder:
    """Seeding helpers bound to the second connection (for rows the other replica writes)."""
    return Seeder(db2)


@pytest.fixture
def refusals(db: Any) -> Callable[[type[BaseException]], Any]:
    """`with refusals(StaleOwnershipError): ...` — assert a refusal and carry on."""

    @contextmanager
    def expect(expected: type[BaseException]) -> Iterator[None]:
        with expect_refusal(db, expected):
            yield

    return expect


@pytest.fixture
def settings(migrated_database: str) -> WorkerSettings:
    """Worker settings with the spec's numbers, in the worker's own vocabulary (`worker_id`)."""
    return WorkerSettings.from_env(_database_env(migrated_database))


@pytest.fixture
def api() -> ScriptedBookingApi:
    """A booking facility whose answers the test scripts."""
    return ScriptedBookingApi()


@pytest.fixture
def sender() -> ScriptedSender:
    """A notification sink whose answers the test scripts."""
    return ScriptedSender()


@pytest.fixture
def clock() -> FakeClock:
    """A clock the test drives. No test in this suite sleeps for real."""
    return FakeClock()
