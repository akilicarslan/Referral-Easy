"""Integration tests for workflow ownership and transitions (§7).

Every refusal asserted here is raised by the database, not by the store. What the store adds — and
what these tests pin — is that the caller receives it as a typed exception carrying its SQLSTATE,
rather than as a message string it would have to parse.

The invariant under test throughout: **a stale worker cannot write.** Expired lease, superseded
generation, or a state that moved under the caller — all three are refused rather than merged.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

import pytest

from referraleasy_domain import IllegalTransitionError, StaleOwnershipError

pytestmark = pytest.mark.integration


class State(StrEnum):
    """Stands in for the contract `WorkflowState`, which is a `StrEnum` too."""

    COVERAGE_CHECK = "COVERAGE_CHECK"


def test_claim_takes_ownership_and_bumps_the_generation(seed, store) -> None:
    workflow_id = seed.workflow()

    claimed = store.claim(workflow_id, "w-1", expected_generation=0)

    assert claimed.id == UUID(workflow_id)
    assert claimed.owner_worker_id == "w-1"
    # Claiming is an ownership change, so it is a new epoch: generation 0 -> 1.
    assert claimed.generation == 1
    assert claimed.state == "INTAKE"
    assert claimed.state_version == 1
    assert claimed.lease_expires_at is not None
    assert claimed.is_human_owned is False


def test_advance_moves_the_state_and_bumps_both_counters(seed, store) -> None:
    claimed = store.claim(seed.workflow(), "w-1", expected_generation=0)

    advanced = store.advance(
        claimed.id,
        "w-1",
        expected_generation=claimed.generation,
        expected_state_version=claimed.state_version,
        to_state="COVERAGE_CHECK",
    )

    assert advanced.state == "COVERAGE_CHECK"
    # state_version is the transition counter; generation is the ownership epoch. Both move.
    assert advanced.state_version == claimed.state_version + 1
    assert advanced.generation == claimed.generation + 1
    assert advanced.owner_worker_id == "w-1"


def test_a_str_enum_target_state_reaches_the_database_as_a_plain_string(seed, store) -> None:
    claimed = store.claim(seed.workflow(), "w-1", expected_generation=0)

    advanced = store.advance(
        claimed.id,
        "w-1",
        expected_generation=claimed.generation,
        expected_state_version=claimed.state_version,
        to_state=State.COVERAGE_CHECK,
    )

    assert advanced.state == "COVERAGE_CHECK"


def test_a_superseded_generation_cannot_claim(seed, store, refusals) -> None:
    workflow_id = seed.workflow()
    store.claim(workflow_id, "w-1", expected_generation=0)

    # The row is at generation 1 now, so an assertion of 0 is a stale read.
    with refusals(StaleOwnershipError):
        store.claim(workflow_id, "w-2", expected_generation=0)


def test_a_worker_that_does_not_own_the_workflow_cannot_write_it(seed, store, refusals) -> None:
    """The headline §7 rule: ownership is required to advance, not merely assumed."""
    claimed = store.claim(seed.workflow(), "w-1", expected_generation=0)

    with refusals(StaleOwnershipError):
        store.advance(
            claimed.id,
            "w-2",  # a different worker
            expected_generation=claimed.generation,
            expected_state_version=claimed.state_version,
            to_state="COVERAGE_CHECK",
        )


def test_an_expired_lease_is_reclaimable(seed, store) -> None:
    """An expired lease frees the work; a live one does not.

    The lease is fabricated as already expired with a negative duration, because within a single
    transaction `now()` does not advance -- waiting would not make the lease lapse, it would only
    make the test slow.
    """
    workflow_id = seed.workflow()
    first = store.claim(workflow_id, "w-1", expected_generation=0, lease_seconds=-1)

    second = store.claim(workflow_id, "w-2", expected_generation=first.generation)

    assert second.owner_worker_id == "w-2"
    assert second.generation == first.generation + 1


def test_a_live_lease_is_not_stealable(seed, store, refusals) -> None:
    workflow_id = seed.workflow()
    held = store.claim(workflow_id, "w-1", expected_generation=0, lease_seconds=300)

    with refusals(StaleOwnershipError):
        store.claim(workflow_id, "w-2", expected_generation=held.generation)


def test_advance_requires_the_state_version_the_caller_read(seed, store, refusals) -> None:
    """A wrong state_version fails the compare-and-set, so it arrives as a stale write (RE001).

    Worth pinning: the caller does not get an "illegal transition" here, because nothing reached the
    state machine. The row had already moved on, which is a different fact.
    """
    claimed = store.claim(seed.workflow(), "w-1", expected_generation=0)

    with refusals(StaleOwnershipError):
        store.advance(
            claimed.id,
            "w-1",
            expected_generation=claimed.generation,
            expected_state_version=claimed.state_version + 5,
            to_state="COVERAGE_CHECK",
        )


def test_an_edge_the_machine_does_not_have_is_refused(seed, store, refusals) -> None:
    """`INTAKE -> READINESS_CHECK` is not a row in `workflow_transition`, and readiness is not
    reachable before a doctor is known (§4)."""
    claimed = store.claim(seed.workflow(), "w-1", expected_generation=0)

    with refusals(IllegalTransitionError):
        store.advance(
            claimed.id,
            "w-1",
            expected_generation=claimed.generation,
            expected_state_version=claimed.state_version,
            to_state="READINESS_CHECK",
        )


def test_a_refused_transition_leaves_the_workflow_exactly_where_it_was(
    seed, store, refusals
) -> None:
    """A refusal is atomic, and the store's caller can inspect the row afterwards.

    This also pins the fixture: the savepoint that isolates the refusal must not have discarded the
    seeded work.
    """
    claimed = store.claim(seed.workflow(), "w-1", expected_generation=0)

    with refusals(IllegalTransitionError):
        store.advance(
            claimed.id,
            "w-1",
            expected_generation=claimed.generation,
            expected_state_version=claimed.state_version,
            to_state="READINESS_CHECK",
        )

    reloaded = store.load(claimed.id)
    assert reloaded is not None
    assert reloaded.state == "INTAKE"
    assert reloaded.state_version == claimed.state_version
    assert reloaded.generation == claimed.generation


def test_load_reads_what_the_last_call_wrote(seed, store) -> None:
    claimed = store.claim(seed.workflow(), "w-1", expected_generation=0)
    advanced = store.advance(
        claimed.id,
        "w-1",
        expected_generation=claimed.generation,
        expected_state_version=claimed.state_version,
        to_state="COVERAGE_CHECK",
    )

    reloaded = store.load(claimed.id)

    assert reloaded is not None
    assert reloaded.state == advanced.state
    assert reloaded.generation == advanced.generation


def test_load_returns_none_for_a_workflow_that_does_not_exist(store) -> None:
    # Missing is a normal answer rather than an invented exception: the database defines no
    # "not found" refusal, so neither does the client.
    assert store.load(uuid4()) is None


def test_the_whole_route_to_final_approval_is_walkable(seed) -> None:
    """The seeded transition rows really do connect INTAKE to AWAITING_FINAL_APPROVAL.

    A guard-rail on the fixtures: if the path in `conftest` were wrong, every booking test would
    fail for a reason that had nothing to do with booking.
    """
    workflow = seed.to_final_approval()

    assert workflow.state == "AWAITING_FINAL_APPROVAL"
    assert workflow.state_version == 8  # INTAKE at 1, plus seven transitions
    assert workflow.generation == 8  # one claim, plus seven advances
