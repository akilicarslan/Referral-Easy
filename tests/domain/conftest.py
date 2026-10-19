"""Shared fixtures for the domain-client tests.

Two kinds of test live in this directory, and the split is deliberate.

* **Unit tests** need neither a database nor a driver. They cover what is pure Python: parsing an
  `owner:action` reference, canonical request hashing, and SQLSTATE -> exception translation, which
  is exercised against a fake connection that raises a driver-shaped error carrying a `sqlstate`.

* **Integration tests** are marked `integration` and skip unless `TEST_DATABASE_URL` is set:

      docker run --rm -d --name re-domain \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=referraleasy \\
        -p 55450:5432 postgres:16-alpine
      DATABASE_URL=postgresql://postgres:test@localhost:55450/referraleasy \\
        uv run python scripts/migrate.py
      TEST_DATABASE_URL=postgresql://postgres:test@localhost:55450/referraleasy \\
        uv run pytest tests/domain -v

  (No `PYTHONPATH` is needed: the root `pyproject.toml` lists every workspace member as a
  dependency, so `uv sync` installs them. If an import of `referraleasy_domain` ever fails here,
  the workspace is out of sync — run `uv sync --all-packages`, do not reach for `PYTHONPATH`.)

  These integration tests are the point of the exercise. This package's central claim is that *the
  database* owns the rules and the client only translates its refusals — a claim that can only be
  tested against a real database. A client test that mocks the refusals proves nothing about the
  rules.

**The connection uses the default row factory (tuples), on purpose.** The stores read
`cursor.description` and build their own dicts, so they work whatever factory the caller configured;
testing with tuples is what shows that, rather than testing a configuration the store happens to
need. `test_store_plumbing.py` covers both directions.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent.parent
RUNNER_PATH = REPO_ROOT / "migrations" / "run.py"

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

#: Reset between tests. `schema_migrations` is deliberately absent: the ledger is not test state.
#: The vocabulary and policy-seed tables are absent too -- they are part of the schema, and a test
#: that deleted them would be testing a different database than the one that ships.
RESET_TABLES = (
    "audit",
    "release_event",
    "release_registry",
    "handoff",
    "booking_request",
    "approval",
    "command_dedup",
    "outbox",
    "task",
    "session",
    "workflow",
    "evidence",
)

#: Tables whose purpose is to refuse deletion, so emptying them is a deliberate act.
GUARDED_TABLES = ("audit", "evidence", "release_registry", "release_event")

#: A legal route from INTAKE to AWAITING_FINAL_APPROVAL. Every pair is a `workflow_transition` row,
#: which is why the walk is written out rather than clever: an invented shortcut would be refused.
PATH_TO_FINAL_APPROVAL: tuple[str, ...] = (
    "COVERAGE_CHECK",
    "PROVIDER_MATCHING",
    "AWAITING_HOSPITAL_CHOICE",
    "AWAITING_DOCTOR_CHOICE",
    "READINESS_CHECK",
    "AWAITING_SLOT_CHOICE",
    "AWAITING_FINAL_APPROVAL",
)


def _load_runner():
    spec = importlib.util.spec_from_file_location("referraleasy_migrations", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _require_driver() -> None:
    """Fail with the fix, not with a bare ImportError."""
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "the integration tests need psycopg (v3), which is not installed.\n"
            "    uv add --dev 'psycopg[binary]'\n"
            "or supply it for one command:\n"
            "    uv run --with 'psycopg[binary]' pytest tests/domain -m integration"
        ) from exc


@pytest.fixture(scope="session")
def database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set; integration tests need a database")
    _require_driver()
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """Apply every migration once per session, then hand the URL to the tests."""
    runner.apply_migrations(database_url, reporter=lambda *a, **k: None)
    return database_url


@pytest.fixture
def db(migrated_database: str):
    """A connection with an empty data set.

    Not autocommit: teardown is a rollback, and a test that wants to observe what a refusal did to
    the transaction gets to see it. Everything a test does therefore lands in one transaction, which
    is why a refusal is isolated with a savepoint rather than a rollback (see `expect_refusal`).
    """
    import psycopg

    with psycopg.connect(migrated_database) as conn:
        reset(conn)
        yield conn
        conn.rollback()


@pytest.fixture
def store(db):
    """A `WorkflowStore` bound to the test connection."""
    from referraleasy_domain import WorkflowStore

    return WorkflowStore(db)


@pytest.fixture
def seed(db) -> Seeder:
    """Seeding helpers bound to the test connection."""
    return Seeder(db)


@pytest.fixture
def refusals(db):
    """`with refusals(StaleOwnershipError): ...` -- assert a refusal and carry on.

    A fixture rather than an imported helper so the test modules never import from `conftest`,
    which is ambiguous once a second test directory has one.
    """

    @contextmanager
    def expect(expected: type[BaseException]) -> Iterator[None]:
        with expect_refusal(db, expected):
            yield

    return expect


def reset(conn) -> None:
    """Empty the data tables, stepping explicitly around the immutability guards."""
    conn.rollback()
    with conn.cursor() as cur:
        for table in GUARDED_TABLES:
            cur.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        cur.execute("TRUNCATE " + ", ".join(RESET_TABLES) + " RESTART IDENTITY CASCADE")
        for table in GUARDED_TABLES:
            cur.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
    conn.commit()


@contextmanager
def expect_refusal(conn, expected: type[BaseException]) -> Iterator[None]:
    """Assert that a call is refused, and leave the connection usable afterwards.

    A refused statement aborts its transaction, so a rollback has to happen before the test can do
    anything else. It is done as a **savepoint** rather than a rollback of the whole transaction,
    because rolling back would also discard the rows the test just seeded -- leaving the test unable
    to check, for example, that a refused transition left the workflow exactly where it was.
    """
    with pytest.raises(expected), conn.transaction():
        yield


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


class Seeder:
    """Connection-bound helpers that insert rows directly.

    Setup, not the thing under test: these write rows the way a fixture, a previous phase or another
    service would have left them. Every call this package is responsible for goes through a store.

    `state` is inserted rather than transitioned on purpose. A test about transitions must not
    depend on the transitions it is not testing, and the guard would refuse a shortcut anyway.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def workflow(
        self,
        *,
        ticket_ref: str = "T-1001",
        patient_ref: str = "P-77",
        state: str = "INTAKE",
        payload: dict | None = None,
    ) -> str:
        """Insert a workflow in `state` and return its id."""
        from psycopg.types.json import Jsonb

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow (ticket_ref, patient_ref, state, payload)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (ticket_ref, patient_ref, state, Jsonb(payload or {})),
            )
            return str(cur.fetchone()[0])

    def set_booking_payload_hash(self, workflow_id: Any, payload_hash: str) -> None:
        """Attach the booking payload hash a workflow is currently consenting to.

        No function does this: the hash travels on `workflow.payload`, and whoever assembles the
        booking payload writes it there. The `::text` cast is required because `jsonb_build_object`
        takes variadic "any" arguments, so PostgreSQL cannot infer a parameter's type.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow SET payload = payload"
                " || jsonb_build_object('booking_payload_hash', %s::text) WHERE id = %s",
                (payload_hash, workflow_id),
            )

    def evidence(
        self,
        *,
        evidence_id: str = "EV-1",
        fresh_for: str = "1 hour",
        kind: str = "consent",
    ) -> str:
        """Insert an authoritative, fresh evidence row and return its primary key."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO evidence (evidence_id, kind, source_system, source_id,
                                      authoritative, observed_at, fresh_for, content_hash)
                VALUES (%s, %s, 'consent-service', 'src-1', true, now(), %s::interval, %s)
                RETURNING id
                """,
                (evidence_id, kind, fresh_for, "a" * 64),
            )
            return str(cur.fetchone()[0])

    def approval(
        self,
        workflow_id: Any,
        *,
        state_version: int,
        payload_hash: str,
        evidence_id: str,
        patient_ref: str = "P-77",
        expires_in: str = "1 hour",
    ) -> str:
        """Record a patient approval bound to the given epoch and payload hash.

        `expires_in` has to be positive: `approval_expiry_after_approval` requires
        `expires_at > approved_at`, and `approval_no_rebinding` then makes `expires_at` immutable --
        so consent cannot be back-dated into still being valid, and a lapsed approval cannot be
        fabricated in one statement either. A test that needs a lapsed one grants a very short expiry
        and crosses a transaction boundary (see `test_booking_gate`), because `now()` is the
        transaction timestamp.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approval (workflow_id, patient_ref, evidence_id, state_version,
                                      booking_payload_hash, channel, approved_by, expires_at)
                VALUES (%s, %s, %s, %s, %s, 'patient_self_service', 'patient',
                        now() + %s::interval)
                RETURNING id
                """,
                (workflow_id, patient_ref, evidence_id, state_version, payload_hash, expires_in),
            )
            return str(cur.fetchone()[0])

    def handoff(
        self,
        workflow_id: Any,
        *,
        ticket_ref: str = "T-1001",
        reason: str = "no matching provider",
    ) -> str:
        """Insert an open handoff ticket for a workflow."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO handoff (workflow_id, ticket_ref, reason)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (workflow_id, ticket_ref, reason),
            )
            return str(cur.fetchone()[0])

    def outbox(
        self,
        *,
        dedupe_key: str = "notify-1",
        kind: str = "notify_patient",
        max_attempts: int = 5,
        attempt_count: int = 0,
    ) -> str:
        """Queue one notification."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO outbox (dedupe_key, kind, max_attempts, attempt_count)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (dedupe_key, kind, max_attempts, attempt_count),
            )
            return str(cur.fetchone()[0])

    # -- composite setups ---------------------------------------------------

    def walk(self, workflow_id: Any, states: Sequence[str], *, worker: str = "w-1"):
        """Claim a workflow and advance it along `states`, through the store.

        Every step re-reads the generation and state_version from the previous call's row, which is
        the only way to drive an optimistic-concurrency API honestly.
        """
        from referraleasy_domain import WorkflowStore

        store = WorkflowStore(self._conn)
        row = store.claim(workflow_id, worker, expected_generation=0)
        for state in states:
            row = store.advance(
                row.id,
                worker,
                expected_generation=row.generation,
                expected_state_version=row.state_version,
                to_state=state,
            )
        return row

    def to_final_approval(self, *, patient_ref: str = "P-77", worker: str = "w-1"):
        """A workflow waiting for the patient's approval, claimed and leased to `worker`."""
        workflow_id = self.workflow(patient_ref=patient_ref)
        return self.walk(workflow_id, PATH_TO_FINAL_APPROVAL, worker=worker)

    def consent_ready(
        self,
        *,
        patient_ref: str = "P-77",
        worker: str = "w-1",
        expires_in: str = "1 hour",
    ):
        """A workflow waiting for approval, with an approval bound to its payload.

        Returns `(workflow, approval_id, payload_hash)`. The approval is bound to the workflow's
        *current* `state_version`, which is the version the transition into `BOOKING_IN_FLIGHT`
        checks -- so this is the state of the world in which consent has just been captured and the
        booking is about to be attempted. `expires_in` is the window the patient left; granting a
        very short one is how a test stages consent that lapses.
        """
        from referraleasy_domain import canonical_booking_payload_hash

        payload_hash = canonical_booking_payload_hash({"slot_id": "S-1", "doctor_id": "D-9"})
        workflow = self.to_final_approval(patient_ref=patient_ref, worker=worker)
        self.set_booking_payload_hash(workflow.id, payload_hash)

        evidence_id = self.evidence()
        approval_id = self.approval(
            workflow.id,
            state_version=workflow.state_version,
            payload_hash=payload_hash,
            evidence_id=evidence_id,
            patient_ref=patient_ref,
            expires_in=expires_in,
        )
        return workflow, approval_id, payload_hash
