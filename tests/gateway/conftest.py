"""Fixtures for the gateway suite.

Fixtures only. The constants, the `sys.path` setup and the `Demo` driver live in
`gateway_fixtures.py`, and test modules must import them from there rather than from here — a
`conftest` module is reachable under a name six directories share (see that file).

Two app fixtures exist because the shipped profile and the *testable* write path are different
configurations, and pretending otherwise would hide the phase's central claim:

* `client` — what `create_app()` builds, which is the demo profile: layer 1 real, layers 2 and 3
  unconfigured, so a booking write is refused. Most tests use this one, because it is the thing that
  ships.
* `configured_client` — the same app with real layers behind the two model seams, so a booking can
  actually be written and "exactly one booking row" is observable. It exists so that the refusal is
  not confused with an absence: the write path works, and it is closed by configuration.

**Everything below is parameterised over the control-state profile**, so a test written once runs
against `InMemoryControlState` and against `PostgresControlState` on a real server and has to pass
both times. That is the point of the parameter, not a convenience: the two implementations are the
same port, and the only way to know they answer the same is to ask them the same questions.

Where a test cannot pass under both, it says so and says why — it does not get a weaker assertion.
`tests/gateway/test_postgres_control_state.py` collects those cases, and it is written as tests
rather than as a comment so that the boundary between the profiles cannot quietly move.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway_fixtures import (
    BACKEND_PARAMS,
    MEMORY_ONLY_MARKER,
    TEST_DATABASE_URL,
    TEST_WORKER_ID,
    Demo,
    allow_all_pipeline,
    demo_settings,
    require_migrated_database,
    reset_database,
)

from referraleasy_gateway.app import create_app
from referraleasy_gateway.config import GatewaySettings
from referraleasy_gateway.control import (
    InMemoryControlState,
    PostgresControlState,
    SeedableControlState,
)
from referraleasy_gateway.guardrails import GuardrailPipeline
from referraleasy_gateway.timing import ManualClock

#: 3 ms per clock read. The clock is a *measurement* seam: with a read cost, a request that reads it
#: several times reports a progress latency strictly smaller than its final one, which is §12's
#: separation observable without a sleep.
READ_COST_MS = 3.0

# `MEMORY_ONLY_MARKER` comes from `gateway_fixtures` rather than being defined here, because
# `test_postgres_control_state.py` holds the guard that keeps the set of marked tests exactly the
# documented one and a test module may not import from a `conftest`. The marker itself is registered
# below rather than in `pyproject.toml`, which this suite does not own.


def pytest_configure(config: pytest.Config) -> None:
    """Register this suite's marker, so `--strict-markers` (which is on) can see it.

    A conftest's `pytest_configure` runs while the conftest is being loaded — before collection and
    before any marker validation — which is what makes registering here work rather than merely
    appear to.
    """
    config.addinivalue_line(
        "markers",
        f"{MEMORY_ONLY_MARKER}: the test cannot pass in the postgres profile. Its docstring names "
        "which guarantee the schema enforces differently, and its name is in MEMORY_ONLY in "
        "test_postgres_control_state.py, which fails if a test is marked without being documented or "
        "documented while still running unmarked.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Run a `memory_only` test in the memory profile only — deselecting, never skipping.

    Deselection rather than a skip, and that is the whole point: CI's database job fails the build
    when anything skips, because a database-backed test that did not run must not be able to look
    like one that passed. A test that cannot pass in a profile is not *in* that profile at all, and
    the memory half of it still runs in the job that has no database, where it is the only half
    there is.

    The parameter is read by name rather than by matching the node id, so a test keeps its exclusion
    if it is renamed.
    """

    def unportable(item: pytest.Item) -> bool:
        if item.get_closest_marker(MEMORY_ONLY_MARKER) is None:
            return False
        callspec = getattr(item, "callspec", None)
        return callspec is not None and callspec.params.get("backend") == "postgres"

    removed = [item for item in items if unportable(item)]
    if removed:
        config.hook.pytest_deselected(items=removed)
        items[:] = [item for item in items if not unportable(item)]


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Prove the database is there and migrated, once per session, before anything uses it.

    Session-scoped and autouse so the failure is one clear error at the start rather than a
    connection error from whichever test happened to run first.
    """
    if TEST_DATABASE_URL:
        require_migrated_database(TEST_DATABASE_URL)


@pytest.fixture(params=BACKEND_PARAMS)
def backend(request: pytest.FixtureRequest) -> str:
    """Which control-state profile this test runs against, and an empty database for it.

    The reset happens here rather than in `control` so that every fixture reading `settings` or
    building an app sees a database that is already empty. It runs before the app is built, which is
    the only ordering that cannot leave a row from the previous test visible to this one.
    """
    if request.param == "postgres":
        assert TEST_DATABASE_URL  # BACKEND_PARAMS only offers postgres when it is set
        reset_database(TEST_DATABASE_URL)
    return str(request.param)


@pytest.fixture
def settings(backend: str) -> GatewaySettings:
    """The demo profile, pointed at the database when the backend is `postgres`."""
    if backend == "postgres":
        assert TEST_DATABASE_URL
        return demo_settings(GATEWAY_CONTROL_BACKEND="postgres", DATABASE_URL=TEST_DATABASE_URL)
    return demo_settings()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(read_cost_ms=READ_COST_MS)


@pytest.fixture
def control(backend: str) -> SeedableControlState:
    """The store under test: a fresh one, so no test can see another one's workflow.

    A `PostgresControlState` is stateless — a DSN and a worker id — so this instance being the same
    object the app uses is a convenience rather than a shared cache. What *is* shared, deliberately,
    is the worker id: `advance` claims the row under it, and a test that moved the identity between
    a seed and a transition would be testing a second worker.
    """
    if backend == "postgres":
        assert TEST_DATABASE_URL
        return PostgresControlState(TEST_DATABASE_URL, worker_id=TEST_WORKER_ID)
    return InMemoryControlState()


@pytest.fixture
def app(settings: GatewaySettings, clock: ManualClock, control: SeedableControlState) -> FastAPI:
    return create_app(settings=settings, clock=clock, control=control)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def demo(client: TestClient) -> Demo:
    """One patient session and one coordinator session, both over the HTTP surface."""
    return Demo(client)


@pytest.fixture
def configured_pipeline(clock: ManualClock) -> GuardrailPipeline:
    """The pipeline a *complete* screening needs: all three layers, two of them as doubles."""
    return allow_all_pipeline(clock)


@pytest.fixture
def configured_app(
    settings: GatewaySettings,
    clock: ManualClock,
    control: SeedableControlState,
    configured_pipeline: GuardrailPipeline,
) -> FastAPI:
    return create_app(
        settings=settings, clock=clock, control=control, guardrails=configured_pipeline
    )


@pytest.fixture
def configured_client(configured_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(configured_app) as test_client:
        yield test_client


@pytest.fixture
def configured_demo(configured_client: TestClient) -> Demo:
    return Demo(configured_client)
