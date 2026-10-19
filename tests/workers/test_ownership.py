"""A lease is a row, losing it is a refusal, and the database is what refuses (§7, §11).

§7 asks for two things about ownership that sound like one thing:

* a worker whose lease expired must not be able to write its late result, and
* another replica taking the work over must be able to *tell* that it did.

Both are only testable with two real connections, so every test here uses `db` (replica A) and `db2`
(replica B) — two backends, two transactions, two claims — and commits where a replica would commit.
A test that faked replica B in-process would prove something about a Python object and nothing about a
lease, a generation or a lost claim, which is the whole subject.

Every test also pins *which layer* refuses. Three are in play and they are not interchangeable:

* `workflow_claim` / `workflow_advance` — SQL functions that compare-and-set and raise `RE001`.
* `workflow_guard` — the `BEFORE UPDATE` trigger, which refuses a stale generation or an ownership
  change without a generation bump (`RE001`) and any write to a human-owned workflow by an automation
  actor (`RE003`).
* this package's `write_payload` — a compare-and-set that matches no row and then *re-reads* to raise
  the typed refusal, because an `UPDATE` that matches nothing raises nothing.

The last one is the only refusal in the system that this package produces rather than reports, so it
is the one that most needs a test that names it — and a second test showing the database refuses the
same write anyway, with no Python in the way.
"""

from __future__ import annotations

from typing import Any

import pytest
from worker_doubles import (
    OPERATOR,
    Seeder,
    expect_database_refusal,
    expect_refusal,
    expire_lease,
    queue_and_claim_ticket,
    workflow_row,
)

from referraleasy_domain import Workflow, WorkflowStore
from referraleasy_domain.errors import HumanOwnedError, StaleOwnershipError
from referraleasy_workers.checkpoint import make_checkpoint, read_checkpoint
from referraleasy_workers.hitl import HumanQueue
from referraleasy_workers.ownership import OwnedWorkflow

#: The two replicas. (The operator is `worker_doubles.OPERATOR`, imported above, because three test
#: modules now hand a workflow to a human and they must not disagree about who.) Constants rather than
#: literals because the assertions are about *whose* write landed, and a typo in one of them would read
#: as a passing test about a third worker.
REPLICA_A = "w-a"
REPLICA_B = "w-b"


# ---------------------------------------------------------------------------
# A claim is a compare-and-set
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_two_replicas_cannot_both_own_one_workflow(db: Any, db2: Any, seed: Seeder) -> None:
    """Exactly one claim wins, and the loser is told so by the database (§7).

    The losing claim is attempted twice on purpose: once at the generation it read (stale) and once at
    the current generation, which loses anyway because the holder's lease is live. Those are the two
    halves of `workflow_claim`'s `WHERE`, and a claim that only checked the generation would let a
    second replica in on every retry.
    """
    workflow_id = seed.workflow()
    db.commit()

    replica_a = OwnedWorkflow.claim(db, workflow_id, REPLICA_A, expected_generation=0)
    assert replica_a.generation == 1
    db.commit()

    with expect_refusal(db2, StaleOwnershipError):
        WorkflowStore(db2).claim(workflow_id, REPLICA_B, expected_generation=0)
    with expect_refusal(db2, StaleOwnershipError):
        WorkflowStore(db2).claim(workflow_id, REPLICA_B, expected_generation=replica_a.generation)

    row = workflow_row(db2, workflow_id)
    assert row["owner_worker_id"] == REPLICA_A
    assert row["generation"] == replica_a.generation


@pytest.mark.integration
def test_a_generation_only_ever_moves_forward(db: Any, db2: Any, seed: Seeder) -> None:
    """Each claim is a new epoch. A claim from the past and a claim from the future are both refused.

    Both refusals matter differently: a stale generation means somebody else took the work, and a
    generation the row has not reached means the caller is working from a position that has not
    happened. Between them they are why a claim cannot be a blind retry.
    """
    workflow_id = seed.workflow()
    db.commit()

    generations = []
    for worker in (REPLICA_A, REPLICA_B, REPLICA_A):
        if generations:
            # The first claim has nothing to take over: the row is unowned, and it is a *lease*
            # expiry that makes the second and third claims legal.
            expire_lease(db2, workflow_id)
        row = WorkflowStore(db2).claim(
            workflow_id, worker, expected_generation=max(generations, default=0)
        )
        db2.commit()
        generations.append(row.generation)

    assert generations == [1, 2, 3], "a claim is a new epoch, not a reuse of the last one"

    with expect_refusal(db, StaleOwnershipError):
        WorkflowStore(db).claim(workflow_id, REPLICA_A, expected_generation=2)
    with expect_refusal(db, StaleOwnershipError):
        WorkflowStore(db).claim(workflow_id, REPLICA_A, expected_generation=4)
    assert workflow_row(db, workflow_id)["generation"] == 3


# ---------------------------------------------------------------------------
# The late write
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_worker_whose_lease_expired_cannot_write_its_late_result(
    db: Any, db2: Any, seed: Seeder
) -> None:
    """The claim §7 is really about: the work was taken over, and the old worker comes back.

    Replica A completes "send the booking" and then stops hearing from the world long enough for its
    lease to lapse; replica B takes the workflow over and completes the same step its own way. A's
    result arrives late — and both ways it can arrive (a state move and a checkpoint write) are
    refused, with the workflow left exactly as B wrote it. That last assertion is the one that matters:
    a refusal that still applied the write would be worse than no refusal at all.
    """
    workflow_id = seed.workflow()
    db.commit()

    replica_a = OwnedWorkflow.claim(db, workflow_id, REPLICA_A, expected_generation=0)
    replica_a.write_checkpoint(
        make_checkpoint(replica_a.row, step="send_booking", data={"by": REPLICA_A})
    )
    db.commit()

    expire_lease(db2, workflow_id)
    replica_b = OwnedWorkflow.claim(
        db2, workflow_id, REPLICA_B, expected_generation=replica_a.generation
    )
    replica_b.write_checkpoint(
        make_checkpoint(replica_b.row, step="send_booking", data={"by": REPLICA_B})
    )
    db2.commit()
    assert replica_b.generation == replica_a.generation + 1

    late_checkpoint = make_checkpoint(replica_a.row, step="send_booking", data={"by": REPLICA_A})

    with expect_refusal(db, StaleOwnershipError):
        replica_a.advance("COVERAGE_CHECK")
    with expect_refusal(db, StaleOwnershipError):
        replica_a.write_checkpoint(late_checkpoint)

    row = workflow_row(db, workflow_id)
    stored = read_checkpoint(Workflow.from_row(row))
    assert stored.checkpoint is not None
    assert stored.checkpoint.data == {"by": REPLICA_B}, "the late write did not land"
    assert row["state"] == replica_b.state
    assert row["generation"] == replica_b.generation
    assert row["owner_worker_id"] == REPLICA_B


@pytest.mark.integration
def test_the_database_refuses_the_same_two_writes_with_no_python_in_the_way(
    db: Any, db2: Any, seed: Seeder
) -> None:
    """The trigger, not this package, is what makes a stale writer impossible.

    The same two refusals as above, issued as raw SQL — no `OwnedWorkflow`, no `write_payload`, no
    `_refuse_late_write` — and refused by `workflow_guard` for the same reason: ownership is a
    compare-and-set in the row, not a convention in a client.
    """
    workflow_id = seed.workflow()
    db.commit()

    replica_a = OwnedWorkflow.claim(db, workflow_id, REPLICA_A, expected_generation=0)
    db.commit()
    expire_lease(db2, workflow_id)
    WorkflowStore(db2).claim(workflow_id, REPLICA_B, expected_generation=replica_a.generation)
    db2.commit()

    # The same ownership change `workflow_claim` makes, minus the generation bump.
    with expect_database_refusal(db, "RE001"):
        db.execute(
            "UPDATE workflow SET owner_worker_id = %s WHERE id = %s", (REPLICA_A, workflow_id)
        )
    # The same generation the stale worker believed in, written back.
    with expect_database_refusal(db, "RE001"):
        db.execute(
            "UPDATE workflow SET generation = %s WHERE id = %s", (replica_a.generation, workflow_id)
        )

    assert workflow_row(db, workflow_id)["owner_worker_id"] == REPLICA_B


@pytest.mark.integration
def test_a_stale_replica_cannot_take_the_work_back_by_re_reading(
    db: Any, db2: Any, seed: Seeder
) -> None:
    """Re-reading the row is not re-taking it: the tokens are fresh, and the write is still refused.

    This is the mistake `ownership.py`'s module docstring calls out by name. A worker that lost its
    lease `refresh()`es, sees the new generation, and writes with *those* tokens — and
    `workflow_advance` refuses anyway, because the row's `owner_worker_id` is the other replica. If
    this test ever fails, ownership has become a matter of what a worker believes.
    """
    workflow_id = seed.workflow()
    db.commit()

    replica_a = OwnedWorkflow.claim(db, workflow_id, REPLICA_A, expected_generation=0)
    db.commit()
    expire_lease(db2, workflow_id)
    WorkflowStore(db2).claim(workflow_id, REPLICA_B, expected_generation=replica_a.generation)
    db2.commit()

    current = replica_a.refresh()
    assert current.generation == 2
    assert current.owner_worker_id == REPLICA_B

    with expect_refusal(db, StaleOwnershipError):
        replica_a.advance("COVERAGE_CHECK")

    row = workflow_row(db, workflow_id)
    assert row["state"] == "INTAKE"
    assert row["state_version"] == 1


# ---------------------------------------------------------------------------
# §11: a human stops automation
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_human_owner_suspends_automation_writes_and_names_the_reason(
    db: Any, db2: Any, seed: Seeder
) -> None:
    """Claiming a ticket is what suspends automation; the payload path says which rule stopped it.

    Three layers, two answers, and the test pins both because they mean different things to a caller:
    the payload path raises `RE003` ("a human owns this workflow; wait for the resolution"), and
    `advance` raises `RE001` (the SQL function excludes human-owned rows in its own `WHERE`, so it
    cannot tell a suspended workflow from a lease that lapsed). A raw write with no Python involved is
    refused by the trigger with `RE003`. The workflow's row is asserted unchanged after all four —
    a suspension that still let one of them through would be the §11 failure the tripwire exists for.
    """
    workflow_id = seed.workflow()
    workflow = seed.walk(workflow_id, ("COVERAGE_CHECK", "PROVIDER_MATCHING"), worker=REPLICA_A)
    replica_a = OwnedWorkflow(db, REPLICA_A, workflow)
    before = workflow_row(db, workflow_id)
    record = queue_and_claim_ticket(db, workflow_id)
    db.commit()

    assert record.claimed_by == OPERATOR
    owned = workflow_row(db, workflow_id)
    assert owned["human_owner_id"] == OPERATOR
    assert owned["owner_worker_id"] is None
    assert owned["generation"] == before["generation"] + 1

    with expect_refusal(db, HumanOwnedError):
        replica_a.write_checkpoint(make_checkpoint(replica_a.row, step="send_booking"))
    with expect_refusal(db, StaleOwnershipError):
        replica_a.advance("READINESS_CHECK")

    # The database's own refusal, with no Python guard involved, for the same two writes.
    with expect_database_refusal(db, "RE003"):
        db.execute("UPDATE workflow SET state = 'CANCELLED' WHERE id = %s", (workflow_id,))
    with expect_database_refusal(db, "RE003"):
        db.execute(
            """UPDATE workflow SET payload = payload || '{"late": true}'::jsonb WHERE id = %s""",
            (workflow_id,),
        )

    after = workflow_row(db, workflow_id)
    assert after["state"] == before["state"]
    assert after["state_version"] == before["state_version"]
    assert dict(after["payload"]) == dict(before["payload"])


@pytest.mark.integration
def test_releasing_the_ticket_gives_automation_back(db: Any, db2: Any, seed: Seeder) -> None:
    """The suspension is a state, not a one-way door: an operator who walks away frees the workflow.

    `handoff_release` clears `human_owner_id` and bumps the generation, leaving the workflow unowned —
    so the next claim is a new epoch and the worker that takes it writes normally. Without this the
    suspension would leak in the direction of "nobody may ever automate this workflow again".
    """
    workflow_id = seed.workflow()
    seed.walk(workflow_id, ("COVERAGE_CHECK",), worker=REPLICA_A)
    queue = HumanQueue(db)
    record = queue_and_claim_ticket(db, workflow_id)
    db.commit()

    released = queue.release(record.id, operator_id=OPERATOR)
    db.commit()
    assert released.state == "open"
    assert released.claimed_by is None
    assert workflow_row(db, workflow_id)["human_owner_id"] is None

    current = workflow_row(db, workflow_id)
    claim = OwnedWorkflow.claim(
        db2, workflow_id, REPLICA_B, expected_generation=current["generation"]
    )
    db2.commit()
    assert claim.generation == current["generation"] + 1

    claim.advance("PROVIDER_MATCHING")
    db2.commit()
    assert workflow_row(db, workflow_id)["state"] == "PROVIDER_MATCHING"


@pytest.mark.integration
def test_human_ownership_is_on_the_row_so_a_worker_that_re_reads_sees_it_before_it_writes(
    db: Any, db2: Any, seed: Seeder
) -> None:
    """The tripwire is the row, so a replica that re-reads sees the suspension before it writes.

    §11's suspension lives in `workflow.human_owner_id` and nowhere else: there is no in-memory flag to
    consult and none to forget to consult. A worker that claimed *before* the handoff holds a cached
    row that says nothing about a human (that is what a cached row is), and one `refresh()` is what
    turns the suspension from a rule into something the worker can read — which is why every write in
    this package re-reads the row it is writing to rather than trusting one it already has.
    """
    workflow_id = seed.workflow()
    worker = OwnedWorkflow.claim(db, workflow_id, REPLICA_A, expected_generation=0)
    db.commit()

    queue_and_claim_ticket(db2, workflow_id)
    db2.commit()

    assert worker.row.human_owner_id is None, "the worker's cached row is exactly that: cached"
    refreshed = worker.refresh()
    assert refreshed.human_owner_id == OPERATOR
    assert refreshed.owner_worker_id is None

    with expect_refusal(db, HumanOwnedError):
        worker.write_checkpoint(make_checkpoint(worker.row, step="send_booking"))


# ---------------------------------------------------------------------------
# What the fixtures cannot fake
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_two_connections_are_two_backends(db: Any, db2: Any) -> None:
    """A guard for the fixtures: if `db2` ever became the same connection, every test above is theatre.

    Compared by backend PID, which is what a second connection *is*. A shared connection would let a
    "losing" claim see the other claim's uncommitted row and pass for the wrong reason.
    """
    with db.cursor() as cur:
        cur.execute("SELECT pg_backend_pid() AS pid")
        first = cur.fetchone()["pid"]
    with db2.cursor() as cur:
        cur.execute("SELECT pg_backend_pid() AS pid")
        second = cur.fetchone()["pid"]

    assert first != second
