"""Where a replica resumes from (§7): a digest-verified checkpoint carried in the workflow payload.

§7 asks for two things that only make sense together: *a worker whose lease expired must not be able
to write its late result*, and *another replica resumes from the last verified checkpoint*. The second
is what makes the first survivable — a worker that dies mid-step does not take the work with it — and
it is also what makes the first dangerous, because a resumed workflow is exactly the situation in
which a stale worker's write would arrive looking plausible.

There is no `checkpoint` table in the control-state schema, and inventing a write path beside the
guarded one would be a second way into the same rows. So the checkpoint travels where the rest of the
worker-visible control state already travels: `workflow.payload`, under one key
(:data:`CHECKPOINT_KEY`). That choice buys the ownership guard for free and costs one property that
this module has to state plainly:

**The write is a compare-and-set, and a miss is the refusal.** The statement matches on `id`, on the
`owner_worker_id` the caller believes it holds, on the `generation` it read, and on a live lease.
When a replica has lost the workflow — its lease expired and another replica claimed it, which bumps
the generation — the `UPDATE` matches no row. Nothing is written, and the row keeps the *newer*
worker's checkpoint rather than being overwritten by a late one from a worker that no longer owns it.

The typed exception for that miss is raised here, not by the database, and the difference matters:
the database's own way of refusing a stale writer is `RE001` from `workflow_claim`/`workflow_advance`,
but a compare-and-set that matches nothing raises nothing — it updates zero rows. So the worker
re-reads the row and raises :class:`~referraleasy_domain.errors.StaleOwnershipError` (RE001) or
:class:`~referraleasy_domain.errors.HumanOwnedError` (RE003), whichever the row actually shows. The
tests pin both: the worker's exception *and* the fact that the database refuses the same write
independently when it is written without the guard (see `tests/workers/test_ownership.py`).

**The digest is ours.** `canonical_request_hash` exists to bind a *command* to its body and a
*patient's consent* to a payload; this digest exists to notice that a checkpoint in the payload is
not the checkpoint that was written — a half-written row, a hand-edited fixture, a key collision with
another writer. It is compared only against itself, so it is deliberately not the domain's hash: a
value shared with the approval machinery would tempt someone to compare the two.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from referraleasy_domain import Workflow
from referraleasy_domain.errors import HumanOwnedError, StaleOwnershipError
from referraleasy_workers.db import ConnectionLike, fetch_one, required_row

__all__ = [
    "CHECKPOINT_KEY",
    "HANDOFF_KEY",
    "Checkpoint",
    "CheckpointRead",
    "ResumePlan",
    "compute_digest",
    "make_checkpoint",
    "plan_resume",
    "read_checkpoint",
    "write_checkpoint",
    "write_payload",
]

#: One key, one checkpoint. A workflow has exactly one resume point; keeping a history here would
#: make "the last verified checkpoint" a question with several answers.
CHECKPOINT_KEY: Final = "checkpoint"

#: Where the handoff staging lives — the patient-visible summary, the consent timestamp, the reason
#: detail and the evidence lines a human will need. It is in the payload for the same reason the
#: checkpoint is: the `handoff` table has no columns for any of it (see
#: :mod:`referraleasy_workers.hitl`), and the payload is the one place automation may write that the
#: ownership guard already covers.
HANDOFF_KEY: Final = "handoff"

#: Reasons a resume did or did not happen. Strings rather than an enum because they are read by a
#: human looking at why a workflow restarted a step, and by the tests that assert which one.
ABSENT: Final = "no checkpoint"
VERIFIED: Final = "verified"
DIGEST_MISMATCH: Final = "digest does not match the checkpoint contents"
MALFORMED: Final = "checkpoint is not a well-formed record"
UNKNOWN_STEP: Final = "checkpoint names a step this pipeline does not have"
NEWER_GENERATION: Final = "checkpoint belongs to a newer generation than the workflow row"
LAST_STEP: Final = "checkpoint is the last step of the pipeline"


def compute_digest(*, step: str, state: str, state_version: int, data: Mapping[str, Any]) -> str:
    """sha256 over the canonical form of everything a checkpoint asserts.

    The step and the workflow position are inside the digest, not just the payload: a checkpoint
    whose data is intact but whose `step` was edited would otherwise resume at the wrong place while
    looking verified, which is worse than a corrupt one.
    """
    canonical = json.dumps(
        {
            "step": step,
            "state": state,
            "state_version": state_version,
            "data": dict(data),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """One completed step, and the workflow position it was completed at."""

    step: str
    state: str
    state_version: int
    generation: int
    digest: str
    data: Mapping[str, Any]
    written_at: str

    def as_payload(self) -> dict[str, Any]:
        """The exact JSON stored under :data:`CHECKPOINT_KEY`."""
        return {
            "step": self.step,
            "state": self.state,
            "state_version": self.state_version,
            "generation": self.generation,
            "digest": self.digest,
            "data": dict(self.data),
            "written_at": self.written_at,
        }

    @property
    def is_verified(self) -> bool:
        """Whether the stored digest still describes the stored contents."""
        return self.digest == compute_digest(
            step=self.step,
            state=self.state,
            state_version=self.state_version,
            data=self.data,
        )

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> Checkpoint | None:
        """Rebuild a checkpoint from JSON, or ``None`` when the record is not one.

        Returning `None` rather than raising is the caller-facing contract: a malformed checkpoint is
        a reason to restart a step, never a reason to fail a workflow. The distinction between "not a
        record" and "a record whose digest is wrong" is kept so the caller can say which happened.
        """
        try:
            step = raw["step"]
            state = raw["state"]
            state_version = raw["state_version"]
            generation = raw["generation"]
            digest = raw["digest"]
            data = raw["data"]
            written_at = raw["written_at"]
        except (KeyError, TypeError):
            return None
        if not all(isinstance(value, str) for value in (step, state, digest, written_at)):
            return None
        if not isinstance(state_version, int) or not isinstance(generation, int):
            return None
        if not isinstance(data, Mapping):
            return None
        return cls(
            step=step,
            state=state,
            state_version=state_version,
            generation=generation,
            digest=digest,
            data=dict(data),
            written_at=written_at,
        )


@dataclass(frozen=True, slots=True)
class CheckpointRead:
    """What the payload says, and why it says nothing when it says nothing."""

    checkpoint: Checkpoint | None
    reason: str

    @property
    def verified(self) -> bool:
        return self.checkpoint is not None and self.reason == VERIFIED


def make_checkpoint(
    workflow: Workflow,
    *,
    step: str,
    data: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> Checkpoint:
    """Build (and self-sign) a checkpoint for a step just completed at this workflow position."""
    written_at = (now or datetime.now(UTC)).isoformat()
    body = dict(data or {})
    return Checkpoint(
        step=step,
        state=workflow.state,
        state_version=workflow.state_version,
        generation=workflow.generation,
        digest=compute_digest(
            step=step, state=workflow.state, state_version=workflow.state_version, data=body
        ),
        data=body,
        written_at=written_at,
    )


def read_checkpoint(workflow: Workflow) -> CheckpointRead:
    """Read the checkpoint out of a workflow row, verifying its digest before trusting it."""
    raw = workflow.payload.get(CHECKPOINT_KEY)
    if not isinstance(raw, Mapping):
        return CheckpointRead(checkpoint=None, reason=ABSENT)
    checkpoint = Checkpoint.from_payload(raw)
    if checkpoint is None:
        return CheckpointRead(checkpoint=None, reason=MALFORMED)
    if not checkpoint.is_verified:
        return CheckpointRead(checkpoint=None, reason=DIGEST_MISMATCH)
    if checkpoint.generation > workflow.generation:
        # The payload describes a position this row has not reached. Trusting it would resume from a
        # future that this replica cannot prove happened.
        return CheckpointRead(checkpoint=None, reason=NEWER_GENERATION)
    return CheckpointRead(checkpoint=checkpoint, reason=VERIFIED)


def write_checkpoint(
    conn: ConnectionLike,
    *,
    workflow: Workflow,
    worker_id: str,
    checkpoint: Checkpoint,
) -> Workflow:
    """Store ``checkpoint`` if this worker still owns the workflow, and refuse politely if not.

    The guard lives in :func:`write_payload`; this is the checkpoint-shaped call to it. There is
    deliberately only one way to write a worker's control state into a workflow (§11's RE003 tripwire
    only works if every automation write goes through the same `WHERE`), so the handoff staging in
    :mod:`referraleasy_workers.hitl` uses the same function under a different key rather than a
    second statement.
    """
    return write_payload(
        conn,
        workflow=workflow,
        worker_id=worker_id,
        key=CHECKPOINT_KEY,
        value=checkpoint.as_payload(),
    )


def write_payload(
    conn: ConnectionLike,
    *,
    workflow: Workflow,
    worker_id: str,
    key: str,
    value: Mapping[str, Any],
) -> Workflow:
    """Store one key of the worker's control state, if this worker still owns the workflow.

    The `WHERE` clause is the concurrency control, exactly as it is in the domain's claim functions:
    owner, generation, no human, a live lease. The trigger that guards the table is the *second* line
    of defence rather than the first — the lease check here is what a stale worker hits, and it hits
    it by matching no row. A human owner is the third: `human_owner_id IS NULL` means an automation
    write simply cannot land while a ticket stands, which is §11's suspension made structural.
    """
    from psycopg.types.json import Jsonb

    row = fetch_one(
        conn,
        """
        UPDATE workflow
           SET payload = payload || jsonb_build_object(%s::text, %s::jsonb)
         WHERE id = %s
           AND owner_worker_id = %s
           AND generation = %s
           AND human_owner_id IS NULL
           AND lease_expires_at > now()
        RETURNING *
        """,
        (key, Jsonb(dict(value)), workflow.id, worker_id, workflow.generation),
    )
    if row is None:
        _refuse_late_write(conn, workflow=workflow, worker_id=worker_id)
    return Workflow.from_row(required_row(row, f"{key} write"))


def _refuse_late_write(conn: ConnectionLike, *, workflow: Workflow, worker_id: str) -> None:
    """Say which rule stopped this write, by reading the row the write did not match.

    Two are worth telling apart, because the caller's next move differs: a human owner means
    automation is suspended until the ticket is resolved (§11), while a lost lease means another
    replica holds the work now and the correct response is to stop rather than to re-claim.
    """
    current = fetch_one(conn, "SELECT * FROM workflow WHERE id = %s", (workflow.id,))
    if current is not None and current["human_owner_id"] is not None:
        raise HumanOwnedError(
            f"a human owns workflow {workflow.id}; automation writes are suspended",
            sqlstate="RE003",
        )
    raise StaleOwnershipError(
        f"write refused for worker {worker_id} on workflow {workflow.id}: the lease expired or "
        f"another worker holds generation {current['generation'] if current else '?'}",
        sqlstate="RE001",
    )


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """Where a replica starts, and the reason it starts there."""

    resumed: bool
    reason: str
    checkpoint: Checkpoint | None
    next_step: str
    steps_remaining: tuple[str, ...]

    @property
    def is_finished(self) -> bool:
        return self.next_step == ""


def plan_resume(workflow: Workflow, *, steps: Sequence[str]) -> ResumePlan:
    """Decide the next step for a replica taking over this workflow.

    A checkpoint is an optimisation with a safety property, not a source of truth: anything the
    payload cannot prove — absent, malformed, digest-broken, naming a step this pipeline does not
    have, or describing a position the row has not reached — falls back to starting at the first
    step. Restarting is always safe here because every step is written to be idempotent or to be
    guarded by the workflow's own tokens; trusting an unproven checkpoint is not.
    """
    read = read_checkpoint(workflow)
    if read.checkpoint is None:
        return ResumePlan(
            resumed=False,
            reason=read.reason,
            checkpoint=None,
            next_step=steps[0] if steps else "",
            steps_remaining=tuple(steps),
        )

    checkpoint = read.checkpoint
    if checkpoint.step not in steps:
        return ResumePlan(
            resumed=False,
            reason=UNKNOWN_STEP,
            checkpoint=checkpoint,
            next_step=steps[0] if steps else "",
            steps_remaining=tuple(steps),
        )

    remaining = tuple(steps[steps.index(checkpoint.step) + 1 :])
    if not remaining:
        return ResumePlan(
            resumed=False,
            reason=LAST_STEP,
            checkpoint=checkpoint,
            next_step="",
            steps_remaining=(),
        )
    return ResumePlan(
        resumed=True,
        reason=VERIFIED,
        checkpoint=checkpoint,
        next_step=remaining[0],
        steps_remaining=remaining,
    )
