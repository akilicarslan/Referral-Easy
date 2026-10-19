"""Leases, generations and the refusal of a late write (§7).

Ownership of a workflow is a row, not an object: `owner_worker_id`, `lease_expires_at` and a
monotonic `generation`. This module is the small amount of Python that a worker needs in order to
hold that row *and to lose it correctly*.

Three properties it exists to provide.

**A claim is a compare-and-set on the generation the caller read.** The database refuses a claim that
is not the current generation (`RE001`), so two replicas cannot both believe they own the work, and a
replica that was asleep while its lease expired cannot simply re-assert ownership: it must re-read the
row and claim at the generation it now sees.

**A step renews the lease by moving the workflow.** There is no separate "renew my lease" call in the
schema, and that is deliberate rather than missing: a renew-only path would let a worker hold a lease
for as long as it liked while doing nothing with it, and every other worker would wait behind a lease
that meant nothing. A lease is sized for the step it guards (`WORKER_LEASE_SECONDS`) and extended by
advancing, which is also what makes progress and ownership the same act.

**A late write is refused, and the refusal is typed.** `advance` and `write_checkpoint` both present
the tokens the caller read; when the lease has expired and another replica has taken over, the write
is refused with :class:`~referraleasy_domain.errors.StaleOwnershipError` (RE001). The worker's job is
then to *stop*, not to re-claim: the work belongs to whoever holds the newer generation, and
re-claiming would put two workers in the same step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from referraleasy_domain import PolicyTable, Workflow, WorkflowStore
from referraleasy_domain.workflow import DEFAULT_ADVANCE_LEASE_SECONDS, DEFAULT_CLAIM_LEASE_SECONDS
from referraleasy_workers.checkpoint import Checkpoint, write_checkpoint
from referraleasy_workers.db import ConnectionLike

__all__ = ["DEFAULT_ADVANCE_LEASE_SECONDS", "DEFAULT_CLAIM_LEASE_SECONDS", "OwnedWorkflow"]


class OwnedWorkflow:
    """A workflow this worker currently owns, with the tokens every write must present.

    The row is re-read on every write (`advance`/`write_checkpoint` return the fresh row and the
    object keeps it), because the generation and the state version are *the* concurrency tokens: a
    cached copy is a stale copy with extra steps.
    """

    __slots__ = ("_conn", "_store", "_workflow", "worker_id")

    def __init__(self, conn: ConnectionLike, worker_id: str, workflow: Workflow) -> None:
        self._conn = conn
        self._store = WorkflowStore(conn)
        self.worker_id = worker_id
        self._workflow = workflow

    @classmethod
    def claim(
        cls,
        conn: ConnectionLike,
        workflow_id: UUID | str,
        worker_id: str,
        *,
        expected_generation: int = 0,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> Self:
        """Take the workflow, or be refused (RE001 / RE003) by the database.

        ``expected_generation`` is the generation the caller *read* — 0 meaning "never claimed". This
        wrapper adds nothing to the database's rule; it exists so a worker passes the tokens in one
        place instead of at every call site.
        """
        row = WorkflowStore(conn).claim(
            workflow_id,
            worker_id,
            expected_generation=expected_generation,
            lease_seconds=lease_seconds,
        )
        return cls(conn, worker_id, row)

    @property
    def row(self) -> Workflow:
        return self._workflow

    @property
    def id(self) -> UUID:
        """The workflow's id, as the domain types it.

        Annotated `UUID` rather than `object`: everything that needs a workflow id — the command id
        derivation, the handoff request, a claim — wants *this* type, and `object` was a hole in the
        type check wide enough to hide a wrong value behind it.
        """
        return self._workflow.id

    @property
    def state(self) -> str:
        return self._workflow.state

    @property
    def generation(self) -> int:
        return self._workflow.generation

    @property
    def state_version(self) -> int:
        return self._workflow.state_version

    def lease_expired(self, *, now: datetime | None = None) -> bool:
        """Whether this worker's lease has run out — a *report*, never a precondition.

        Informational on purpose. Nothing here decides to skip a write because the lease looks
        expired: the guard decides, by refusing. A worker that checked this first would be trusting a
        row it read earlier, which is the mistake the compare-and-set exists to make impossible.
        """
        expires = self._workflow.lease_expires_at
        if expires is None:
            return True
        return expires <= (now or datetime.now(UTC))

    def refresh(self) -> Workflow:
        """Re-read the row from the database (after something else may have written it)."""
        current = self._store.load(self._workflow.id)
        if current is None:  # pragma: no cover - the row was deleted out of band
            raise LookupError(f"workflow {self._workflow.id} no longer exists")
        self._workflow = current
        return current

    def advance(
        self, to_state: str, *, lease_seconds: int = DEFAULT_ADVANCE_LEASE_SECONDS
    ) -> Workflow:
        """Move the workflow, presenting both tokens read from the current row.

        Refusals are the database's, and they are typed by SQLSTATE rather than by this class:

        * `RE001` when this worker no longer owns the workflow — the lease expired, another replica
          claimed it, or the state moved under it (`workflow_advance`'s `WHERE` checks all three).
        * `RE002` when the edge is not in the transition table.
        * `RE001` again when a **human** owns it: `workflow_advance` excludes human-owned rows in the
          same `WHERE`, so the row does not match and the function reports a stale claim rather than
          the suspension. This is a wart of that function, pinned by
          `tests/workers/test_ownership.py::test_a_human_owner_suspends_automation_writes_and_names_the_reason`
          rather than papered over here: the suspension is still enforced, and
          :meth:`write_checkpoint` is the path that *names* it, because it re-reads the row it failed
          to match and raises `RE003` for a human owner.

        None of those are re-implemented here.
        """
        row = self._store.advance(
            self._workflow.id,
            self.worker_id,
            expected_generation=self._workflow.generation,
            expected_state_version=self._workflow.state_version,
            to_state=to_state,
            lease_seconds=lease_seconds,
        )
        self._workflow = row
        return row

    def write_checkpoint(self, checkpoint: Checkpoint) -> Workflow:
        """Store a checkpoint for a completed step, if this worker still owns the workflow."""
        row = write_checkpoint(
            self._conn, workflow=self._workflow, worker_id=self.worker_id, checkpoint=checkpoint
        )
        self._workflow = row
        return row

    def assert_policy(self, agent_role: str, tool_name: str) -> None:
        """Ask the database to refuse when this role may not do this in the current state.

        Called immediately before every call that leaves the process, against the state *this worker
        last read*. The check is not what makes the call safe — the table is — but a worker that
        never asks would be the one place in the system where §4's matrix was not consulted.
        """
        PolicyTable(self._conn).assert_allowed(agent_role, tool_name, self._workflow.state)
