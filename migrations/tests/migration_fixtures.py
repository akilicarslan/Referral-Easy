"""Shared helpers for the migration test suite.

**Not a `conftest`.** These names used to live in one, and `from conftest import ...` resolves to
whichever `conftest.py` pytest imported *last* — so these 124 tests could only be collected when
their directory was the only thing on the command line. Adding `migrations` to `testpaths` made that
visible immediately: every module failed at collection with

    ImportError: cannot import name 'SHA_A' from 'conftest' (tests/workers/conftest.py)

The fix is the one this repository already prescribes for the same bug in `tests/`: a uniquely named
module whose name cannot collide. `tests/test_test_layout.py` guards the rule for `tests/`; the same
check now runs over this directory too.

The pytest hooks and the database fixtures stay in `conftest.py` — those are the parts pytest has to
find in a conftest. Everything imported *by name* is here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = TESTS_DIR.parent
REPO_ROOT = MIGRATIONS_DIR.parent
RUNNER_PATH = MIGRATIONS_DIR / "run.py"

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

#: Reset between tests. `schema_migrations` is deliberately absent: re-applying
#: migrations per test would make the suite slow, and the ledger is not test
#: state. The vocabulary and policy-seed tables are absent too -- they are part
#: of the schema, and a test that deleted them would be testing a different
#: database than the one that ships.
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

#: Tables whose purpose is to refuse deletion. Truncating them in a test fixture
#: is a deliberate act, which is why it is spelled out here instead of hidden
#: inside a generic "reset" helper. The guards themselves are tested directly.
GUARDED_TABLES = ("audit", "evidence", "release_registry", "release_event")


def _load_runner():
    spec = importlib.util.spec_from_file_location("referraleasy_migrations", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


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


# ---------------------------------------------------------------------------
# Concurrency helper
# ---------------------------------------------------------------------------


def run_concurrently(*fns, timeout: float = 30.0):
    """Run callables in parallel, released together by a barrier.

    Returns `(results, errors)`, index-aligned with `fns`. The barrier is what
    makes a race a race: without it the threads tend to serialise and the test
    proves nothing.
    """
    results = [None] * len(fns)
    errors: list[BaseException | None] = [None] * len(fns)
    barrier = threading.Barrier(len(fns))

    def wrap(index: int, fn) -> None:
        try:
            barrier.wait(timeout=timeout)
            results[index] = fn()
        except BaseException as exc:
            errors[index] = exc

    threads = [threading.Thread(target=wrap, args=(i, fn), daemon=True) for i, fn in enumerate(fns)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    return results, errors


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

SHA_A = "a" * 64
SHA_B = "b" * 64


def make_workflow(
    conn,
    *,
    ticket_ref: str = "T-1001",
    patient_ref: str = "P-77",
    state: str = "INTAKE",
    payload: dict | None = None,
) -> str:
    """Insert a workflow directly in `state` (seeding, not transitioning)."""
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow (ticket_ref, patient_ref, state, payload)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (ticket_ref, patient_ref, state, Jsonb(payload or {})),
        )
        return str(cur.fetchone()["id"])


def claim(
    conn,
    workflow_id: str,
    worker: str = "w-1",
    *,
    expected_generation: int = 0,
    lease_seconds: int = 60,
) -> dict:
    """Call workflow_claim and return the resulting workflow row."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM workflow_claim(%s, %s, %s, %s)",
            (workflow_id, worker, expected_generation, lease_seconds),
        )
        return cur.fetchone()


def advance(
    conn,
    workflow_id: str,
    to_state: str,
    *,
    worker: str = "w-1",
    expected_generation: int,
    expected_state_version: int,
    lease_seconds: int = 60,
) -> dict:
    """Call workflow_advance and return the resulting workflow row."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM workflow_advance(%s, %s, %s, %s, %s, %s)",
            (
                workflow_id,
                worker,
                expected_generation,
                expected_state_version,
                to_state,
                lease_seconds,
            ),
        )
        return cur.fetchone()


def walk(conn, workflow_id: str, path: list[str], *, worker: str = "w-1"):
    """Claim a workflow, then move it along `path`. Returns the final row."""
    row = claim(conn, workflow_id, worker, expected_generation=0)
    for target in path:
        row = advance(
            conn,
            workflow_id,
            target,
            worker=worker,
            expected_generation=row["generation"],
            expected_state_version=row["state_version"],
        )
    return row


def record_evidence(
    conn,
    *,
    evidence_id: str = "EV-1",
    fresh_for: str = "1 hour",
    authoritative: bool = True,
    observed_at: str = "now()",
    kind: str = "consent",
) -> str:
    """Insert an evidence row and return its primary key.

    `observed_at` is injected as SQL on purpose: the tests need to fabricate an
    already-stale record, which is precisely what the freshness rule is about.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO evidence (evidence_id, kind, source_system, source_id,
                                  authoritative, observed_at, fresh_for, content_hash)
            VALUES (%s, %s, 'consent-service', 'src-1', %s, {observed_at}, interval '{fresh_for}', %s)
            RETURNING id
            """,
            (evidence_id, kind, authoritative, SHA_A),
        )
        return str(cur.fetchone()["id"])


def grant_approval(
    conn,
    workflow_id: str,
    *,
    patient_ref: str = "P-77",
    payload_hash: str = SHA_A,
    state_version: int,
    evidence_id: str,
    expires_in: str = "1 hour",
) -> str:
    """Record an approval bound to the given epoch and payload hash."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO approval (workflow_id, patient_ref, evidence_id, state_version,
                                  booking_payload_hash, channel, approved_by, expires_at)
            VALUES (%s, %s, %s, %s, %s, 'patient_self_service', 'patient', now() + %s::interval)
            RETURNING id
            """,
            (workflow_id, patient_ref, evidence_id, state_version, payload_hash, expires_in),
        )
        return str(cur.fetchone()["id"])


def assert_rejects(conn, sql: str, params=(), *, sqlstate: str, match: str | None = None):
    """Assert that a statement is refused with the given SQLSTATE.

    Asserting on the SQLSTATE rather than the message keeps the test tied to the
    contract (`RE001` means "a stale writer tried to write") instead of to
    wording that a future edit will improve.

    The transaction is rolled back afterwards, because PostgreSQL aborts it --
    that is precisely the behaviour under test.
    """
    import psycopg

    with pytest.raises(psycopg.Error) as excinfo, conn.cursor() as cur:
        cur.execute(sql, params)
    conn.rollback()

    assert excinfo.value.sqlstate == sqlstate, (
        f"expected SQLSTATE {sqlstate}, got {excinfo.value.sqlstate}: {excinfo.value}"
    )
    if match is not None:
        assert match in str(excinfo.value), f"{match!r} not in {excinfo.value}"


def as_actor(conn, kind: str, actor_id: str | None = None) -> None:
    """Set the transaction-local actor context, the way the handoff functions do."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('referraleasy.actor_kind', %s, true)", (kind,))
        if actor_id is not None:
            cur.execute("SELECT set_config('referraleasy.actor_id', %s, true)", (actor_id,))


def set_payload_hash(conn, workflow_id: str, payload_hash: str) -> None:
    """Attach the booking payload hash a workflow is currently consenting to."""
    with conn.cursor() as cur:
        cur.execute(
            # The explicit ::text cast matters: jsonb_build_object takes variadic
            # "any" arguments, so PostgreSQL cannot infer a parameter's type here.
            "UPDATE workflow SET payload = payload"
            " || jsonb_build_object('booking_payload_hash', %s::text) WHERE id = %s",
            (payload_hash, workflow_id),
        )
