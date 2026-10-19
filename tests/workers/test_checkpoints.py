"""What a second replica resumes from, and everything that makes it start over instead (§7).

§7's two ownership requirements only make sense together. "A worker whose lease expired must not write
its late result" is survivable *because* "another replica resumes from the last verified checkpoint" —
and the resume is what makes the late write dangerous, since a resumed workflow is exactly the
situation in which a stale worker's write would arrive looking plausible.

So this module is about the second half, and its shape is a list of reasons to **not** trust a
checkpoint. A checkpoint lives in `workflow.payload`, which means the ownership guard covers the write
for free — and also means it is just JSON, so a half-written row, a hand-edited fixture or a payload
key collision could produce a record that *looks* like a position. The digest and the four reasons in
`plan_resume` are what turn that into either a resume or a restart, and restarting is always safe:
every step is idempotent or guarded by the workflow's own tokens, so a restart costs an attempt and a
resume-from-a-lie costs correctness.

The algebra is pure and tested as such. The durability claim — that the checkpoint outlives the worker
that wrote it, and that the *other* replica reads it — needs two real connections and is tested with
them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from worker_doubles import Seeder, workflow_row

from referraleasy_domain import Workflow
from referraleasy_workers import checkpoint as checkpoint_module
from referraleasy_workers.checkpoint import (
    ABSENT,
    CHECKPOINT_KEY,
    DIGEST_MISMATCH,
    HANDOFF_KEY,
    LAST_STEP,
    MALFORMED,
    NEWER_GENERATION,
    UNKNOWN_STEP,
    VERIFIED,
    Checkpoint,
    CheckpointRead,
    compute_digest,
    make_checkpoint,
    plan_resume,
    read_checkpoint,
    write_checkpoint,
)
from referraleasy_workers.ownership import OwnedWorkflow

#: The booking leg, in the same order and with the same names `service.BOOKING_STEPS` uses. Imported
#: as a literal rather than from the service so that a step renamed in one place and not the other
#: fails the resume tests instead of silently agreeing with them.
STEPS: tuple[str, ...] = ("send_booking", "confirm_outcome")

SEND = STEPS[0]


def workflow(**overrides: Any) -> Workflow:
    """A `Workflow` value for the pure tests, with only the fields the checkpoint reads.

    Built through `Workflow.from_row`, the same constructor the stores use, so a column renamed in the
    domain fails here rather than being silently ignored: the checkpoint's own fields are all in this
    mapping, and a missing one is a `KeyError`, not a default.
    """
    moment = datetime(2026, 10, 15, tzinfo=UTC)
    fields: dict[str, Any] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "ticket_ref": "T-1001",
        "patient_ref": "P-77",
        "specialty": "cardiology",
        "owner_worker_id": "w-a",
        "state": "BOOKING_IN_FLIGHT",
        "state_version": 7,
        "generation": 3,
        "lease_expires_at": moment,
        "human_owner_id": None,
        "human_claimed_at": None,
        "payload": {},
        "created_at": moment,
        "updated_at": moment,
    }
    fields.update(overrides)
    return Workflow.from_row(fields)


def signed(
    workflow_row_: Workflow, *, step: str = SEND, data: dict[str, Any] | None = None
) -> Checkpoint:
    return make_checkpoint(workflow_row_, step=step, data=data or {"outcome": "created"})


def with_checkpoint(workflow_row_: Workflow, checkpoint: Checkpoint | Any) -> Workflow:
    """The same row with one payload key set. The row is frozen, so this is a new value, not a mock."""
    raw = checkpoint if not isinstance(checkpoint, Checkpoint) else checkpoint.as_payload()
    return replace(workflow_row_, payload={CHECKPOINT_KEY: raw})


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_verified_checkpoint_resumes_at_the_next_step() -> None:
    """The point of the whole module: `send_booking` is done, so the next replica confirms it."""
    plan = plan_resume(with_checkpoint(workflow(), signed(workflow())), steps=STEPS)

    assert plan.resumed is True
    assert plan.reason == VERIFIED
    assert plan.next_step == "confirm_outcome"
    assert plan.steps_remaining == ("confirm_outcome",)
    assert plan.is_finished is False


def test_a_checkpoint_for_the_last_step_means_there_is_nothing_left() -> None:
    """Not a resume: the pipeline has no next step, and saying so is different from starting over."""
    row = workflow()
    plan = plan_resume(with_checkpoint(row, signed(row, step="confirm_outcome")), steps=STEPS)

    assert plan.resumed is False
    assert plan.reason == LAST_STEP
    assert plan.is_finished is True
    assert plan.steps_remaining == ()


def test_a_workflow_with_no_checkpoint_starts_at_the_first_step() -> None:
    plan = plan_resume(workflow(), steps=STEPS)

    assert plan.resumed is False
    assert plan.reason == ABSENT
    assert plan.checkpoint is None
    assert plan.next_step == SEND
    assert plan.steps_remaining == STEPS


# ---------------------------------------------------------------------------
# Every reason not to trust one
# ---------------------------------------------------------------------------


def test_an_edited_checkpoint_is_not_a_position() -> None:
    """The digest covers what the checkpoint *asserts*, not just its data.

    Editing the step while leaving the data intact is the interesting attack: the payload still parses,
    the outcome field is still the one the facility sent, and a resume would skip a step that never
    ran — or, worse, resume into one that already did. The digest binds step, state and state_version
    together with the data for exactly this reason.
    """
    row = workflow()
    checkpoint = signed(row)
    tampered = {**checkpoint.as_payload(), "step": "confirm_outcome"}

    read = read_checkpoint(with_checkpoint(row, tampered))
    plan = plan_resume(with_checkpoint(row, tampered), steps=STEPS)

    assert read.checkpoint is None
    assert read.reason == DIGEST_MISMATCH
    assert plan.resumed is False
    assert plan.reason == DIGEST_MISMATCH
    assert plan.next_step == SEND


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: {**body, "state_version": body["state_version"] + 1},
        lambda body: {**body, "state": "BOOKED"},
        lambda body: {**body, "data": {**body["data"], "outcome": "timeout_after_commit"}},
    ],
    ids=["state_version", "state", "data"],
)
def test_the_digest_covers_every_field_of_the_claim(mutate: Any) -> None:
    """One field changed and nothing else: no resume. Listed per field, because each one is a claim
    the pipeline acts on — which step ran, where the workflow was, and what the facility answered."""
    row = workflow()
    body = signed(row).as_payload()

    read = read_checkpoint(with_checkpoint(row, mutate(body)))

    assert read.checkpoint is None
    assert read.reason == DIGEST_MISMATCH


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("not even a mapping", ABSENT, id="a string under the key is not a record"),
        pytest.param({"step": SEND}, MALFORMED, id="missing everything but the step"),
        pytest.param(
            {
                "step": SEND,
                "state": "BOOKING_IN_FLIGHT",
                "state_version": "seven",
                "generation": 3,
                "digest": "0" * 64,
                "data": {},
                "written_at": "2026-10-15T00:00:00+00:00",
            },
            MALFORMED,
            id="state_version is not an integer",
        ),
        pytest.param(
            {
                "step": SEND,
                "state": "BOOKING_IN_FLIGHT",
                "state_version": 7,
                "generation": 3,
                "digest": "0" * 64,
                "data": "the data is a string",
                "written_at": "2026-10-15T00:00:00+00:00",
            },
            MALFORMED,
            id="data is not a mapping",
        ),
        pytest.param(
            {
                "step": SEND,
                "state": "BOOKING_IN_FLIGHT",
                "state_version": 7,
                "generation": 3,
                "digest": "0" * 64,
                "data": {},
                "written_at": 20261015,
            },
            MALFORMED,
            id="written_at is not a string",
        ),
    ],
)
def test_a_malformed_record_is_a_restart_never_an_exception(raw: Any, expected: str) -> None:
    """Reading a payload is not a trust decision, so it cannot be a failure mode either.

    A malformed checkpoint is JSON somebody else wrote, and the honest answer is to start over rather
    than to fail a workflow because a payload looked odd. Tested as a property over shapes rather than
    as a single example: `from_payload` has six `isinstance` checks and each of them could raise
    instead of returning `None`.

    The two reasons are kept apart on purpose, and the first row is the line between them: a payload
    key holding something that is not a mapping was never a checkpoint (`ABSENT`), while a mapping that
    is missing fields or has a field of the wrong type is a broken *record* (`MALFORMED`).
    """
    row = with_checkpoint(workflow(), raw)
    read = read_checkpoint(row)
    plan = plan_resume(row, steps=STEPS)

    assert read.checkpoint is None
    assert read.reason == expected
    assert plan.resumed is False
    assert plan.next_step == SEND


def test_a_key_collision_is_not_a_resume_point() -> None:
    """Somebody else's payload under our key: `{"checkpoint": {...}}` is only ours if it is a record.

    The realistic version of this is another writer using the same key for something else — the module
    docstring's "a key collision with another writer". The digest is what makes it cheap to detect.
    """
    row = with_checkpoint(workflow(), {"note": "written by something that is not this pipeline"})

    read = read_checkpoint(row)

    assert read.checkpoint is None
    assert read.reason == MALFORMED


def test_a_checkpoint_naming_a_step_this_pipeline_does_not_have_is_unproven() -> None:
    """A renamed or removed step is a *config* change, and the checkpoint cannot know about it.

    Resuming into a step the pipeline no longer has would either skip work or fail halfway, so the
    checkpoint is treated as absent and the leg starts over. The reason is kept distinct from "absent"
    because an operator reading a log needs to know a stale checkpoint was *found* and rejected.
    """
    row = workflow()
    plan = plan_resume(
        with_checkpoint(row, signed(row, step="book_with_the_fax_machine")), steps=STEPS
    )

    assert plan.resumed is False
    assert plan.reason == UNKNOWN_STEP
    assert plan.next_step == SEND
    assert plan.checkpoint is not None, "the record was found; it was not trusted"


def test_a_checkpoint_from_a_generation_the_row_has_not_reached_is_unproven() -> None:
    """The payload describes a position this row cannot prove happened.

    This is what a rolled-back or restored row looks like: the payload is *ahead* of the row. Trusting
    it would resume from a future, so it is a restart — and the digest is intact, which is why this
    reason has to be checked separately from it.
    """
    fresh = workflow(generation=1, state_version=1)
    ahead = make_checkpoint(workflow(generation=6, state_version=9), step=SEND)
    row = with_checkpoint(fresh, ahead)

    assert ahead.is_verified is True, "the in-memory record is self-consistent"
    read = read_checkpoint(row)
    plan = plan_resume(row, steps=STEPS)

    assert read.checkpoint is None
    assert read.reason == NEWER_GENERATION
    assert plan.reason == NEWER_GENERATION
    assert plan.next_step == SEND


def test_a_checkpoint_older_than_the_row_is_still_a_resume_point() -> None:
    """The other direction is normal, not suspicious: the workflow moved on after the checkpoint.

    `send_booking` completed at generation 1 and the advance to `BOOKING_UNVERIFIED` bumped the row to
    2 — so a checkpoint *behind* the row is what a healthy run looks like, and refusing it would refuse
    every real resume.
    """
    row = workflow(generation=2, state_version=2)
    older = make_checkpoint(workflow(generation=1, state_version=1), step=SEND)
    plan = plan_resume(with_checkpoint(row, older), steps=STEPS)

    assert plan.resumed is True
    assert plan.reason == VERIFIED
    assert plan.next_step == "confirm_outcome"


def test_an_empty_step_list_resumes_to_nothing_rather_than_to_a_step() -> None:
    """A pipeline configured with no steps has no next step, and `next_step` says so with `""`."""
    plan = plan_resume(workflow(), steps=())

    assert plan.next_step == ""
    assert plan.is_finished is True
    assert plan.steps_remaining == ()


# ---------------------------------------------------------------------------
# Shape: one key, one way in
# ---------------------------------------------------------------------------


def test_there_is_one_key_for_the_checkpoint_and_one_for_the_handoff_staging() -> None:
    """Two keys, both written through the same guarded statement — the check is on the writer."""
    assert CHECKPOINT_KEY == "checkpoint"
    assert HANDOFF_KEY == "handoff"
    assert CHECKPOINT_KEY != HANDOFF_KEY


def test_a_checkpoint_is_written_through_the_one_guarded_payload_write(monkeypatch: Any) -> None:
    """`write_checkpoint` is `write_payload` under one key, and this is that claim, structurally.

    §11's tripwire and §7's late-write refusal both work by there being exactly one place that writes a
    worker's control state into a workflow row. A second statement — even a correct copy — would be a
    second place for the `WHERE` clause to drift, so this test fails if `write_checkpoint` ever stops
    going through `write_payload`, and it names the key and the contents it passes on the way.
    """
    calls: list[tuple[Any, Any, Any, str, Any]] = []

    def fake_write_payload(
        conn: Any, *, workflow: Any, worker_id: Any, key: str, value: Any
    ) -> Any:
        calls.append((conn, workflow, worker_id, key, value))
        return "the fresh row"

    monkeypatch.setattr(checkpoint_module, "write_payload", fake_write_payload)
    row = workflow()
    body = {"outcome": "created"}

    result = write_checkpoint(
        "conn", workflow=row, worker_id="w-a", checkpoint=signed(row, data=body)
    )

    assert result == "the fresh row"
    assert len(calls) == 1
    conn, passed_row, worker_id, key, value = calls[0]
    assert (conn, passed_row, worker_id) == ("conn", row, "w-a")
    assert key == CHECKPOINT_KEY
    assert value["step"] == SEND
    assert value["data"] == body
    assert value["digest"] == compute_digest(
        step=SEND, state=row.state, state_version=row.state_version, data=body
    )


def test_the_same_object_is_not_a_verified_checkpoint_twice_over() -> None:
    """`CheckpointRead.verified` is derived, not asserted: a caller cannot ask for one that is not."""
    row = workflow()
    good = read_checkpoint(with_checkpoint(row, signed(row)))
    bad = read_checkpoint(with_checkpoint(row, {**signed(row).as_payload(), "digest": "0" * 64}))

    assert good.verified is True
    assert bad.verified is False
    assert CheckpointRead(checkpoint=good.checkpoint, reason=ABSENT).verified is False


# ---------------------------------------------------------------------------
# Durability: the checkpoint outlives the worker that wrote it
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_other_replica_resumes_from_the_dead_workers_checkpoint(
    db: Any, db2: Any, seed: Seeder
) -> None:
    """The claim that needs two real connections: the work survives the worker that was doing it.

    Replica A claims, does `send_booking`, checkpoints it, and stops existing — no rollback, no
    unwinding, just no more commits. Replica B takes the workflow over after the lease expires and asks
    the *database* where to resume: it gets A's checkpoint, verified, naming `send_booking`, and its
    next step is `confirm_outcome`. B never sees A's objects; it sees one committed row.
    """
    workflow_id = seed.workflow()
    db.commit()

    replica_a = OwnedWorkflow.claim(db, workflow_id, "w-a", expected_generation=0)
    row = replica_a.write_checkpoint(
        make_checkpoint(
            replica_a.row, step=SEND, data={"outcome": "created", "booking_request_id": "B-1"}
        )
    )
    assert row.payload[CHECKPOINT_KEY]["data"]["booking_request_id"] == "B-1"
    db.commit()

    # A's lease lapses and B takes over.
    with db2.cursor() as cur:
        cur.execute(
            "UPDATE workflow SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (workflow_id,),
        )
    replica_b = OwnedWorkflow.claim(
        db2, workflow_id, "w-b", expected_generation=replica_a.generation
    )
    db2.commit()
    assert replica_b.generation == replica_a.generation + 1

    stored = Workflow.from_row(workflow_row(db2, workflow_id))
    plan = plan_resume(stored, steps=STEPS)

    assert plan.resumed is True, "a resume across replicas is the reason a crash is survivable"
    assert plan.reason == VERIFIED
    assert plan.checkpoint is not None
    assert plan.checkpoint.step == SEND
    assert plan.checkpoint.data["outcome"] == "created"
    assert plan.next_step == "confirm_outcome"


@pytest.mark.integration
def test_a_malformed_checkpoint_in_a_real_payload_is_a_restart_not_a_crash(
    db: Any, db2: Any, seed: Seeder
) -> None:
    """The same property as the pure tests, against a value that has been through `jsonb`.

    Worth a database test because the round trip is not identity: `jsonb` reorders keys, turns Python
    tuples into lists and drops types the driver guessed. The digest is computed over the *canonical*
    form of the data, so it has to survive that trip — and if it did not, every real resume would be a
    restart and no pure test would notice.
    """
    workflow_id = seed.workflow()
    db.commit()

    replica_a = OwnedWorkflow.claim(db, workflow_id, "w-a", expected_generation=0)
    replica_a.write_checkpoint(
        make_checkpoint(
            replica_a.row,
            step=SEND,
            data={"outcome": "created", "nested": {"b": [1, 2], "a": "x"}, "flag": True, "n": 3},
        )
    )
    db.commit()

    stored = Workflow.from_row(workflow_row(db, workflow_id))
    read = read_checkpoint(stored)
    assert read.verified is True, "the digest survives the jsonb round trip"
    assert read.checkpoint is not None
    assert read.checkpoint.data["nested"] == {"a": "x", "b": [1, 2]}
    assert plan_resume(stored, steps=STEPS).next_step == "confirm_outcome"

    # Same row, one hand-edit: an operator "fixing" the checkpoints's step in psql.
    with db2.cursor() as cur:
        cur.execute(
            "UPDATE workflow SET payload = jsonb_set(payload, '{checkpoint,step}', '\"confirm_outcome\"')"
            " WHERE id = %s",
            (workflow_id,),
        )
    db2.commit()

    edited = Workflow.from_row(workflow_row(db, workflow_id))
    assert read_checkpoint(edited).reason == DIGEST_MISMATCH
    assert plan_resume(edited, steps=STEPS).next_step == SEND


def test_the_resume_reasons_are_seven_distinct_sentences() -> None:
    """The reasons are read by an operator looking at why a workflow restarted a step.

    Two of them sharing a sentence would make "we found nothing" and "we found something we do not
    trust" the same message, which is the distinction this module exists to keep.
    """
    reasons = {
        ABSENT,
        VERIFIED,
        DIGEST_MISMATCH,
        MALFORMED,
        UNKNOWN_STEP,
        NEWER_GENERATION,
        LAST_STEP,
    }

    assert len(reasons) == 7
    assert VERIFIED not in (
        ABSENT,
        DIGEST_MISMATCH,
        MALFORMED,
        UNKNOWN_STEP,
        NEWER_GENERATION,
        LAST_STEP,
    )


def test_a_checkpoint_has_no_dict_of_its_own_to_drift() -> None:
    """`as_payload` is the only serialisation, and `from_payload` reads exactly its fields.

    A second spelling of the record (a `to_json`, a `.raw`) is how a stored checkpoint and a read one
    start disagreeing about a field name; this pins that the payload round-trips *exactly*, which is
    also what makes the digest comparison meaningful.
    """
    row = workflow()
    checkpoint = signed(row, data={"outcome": "created", "n": [1, {"a": 2}]})

    rebuilt = Checkpoint.from_payload(checkpoint.as_payload())
    assert rebuilt is not None

    assert rebuilt.as_payload() == checkpoint.as_payload()
    assert rebuilt.is_verified is True
    assert set(rebuilt.as_payload()) == {
        "step",
        "state",
        "state_version",
        "generation",
        "digest",
        "data",
        "written_at",
    }


def test_a_written_at_timestamp_is_part_of_the_record_and_not_of_the_digest() -> None:
    """When it was written is provenance; what it asserts is the digest.

    A clock difference between two replicas would otherwise invalidate every checkpoint the other one
    wrote, so the timestamp is stored and compared only to nothing.
    """
    row = workflow()
    first = make_checkpoint(
        row, step=SEND, data={"outcome": "created"}, now=datetime(2026, 10, 15, tzinfo=UTC)
    )
    second = make_checkpoint(
        row, step=SEND, data={"outcome": "created"}, now=datetime(2026, 10, 16, tzinfo=UTC)
    )

    assert first.digest == second.digest
    assert first.written_at != second.written_at
