"""The eval replay drives real journeys and its records score (§14).

Runs two scenarios through `scripts/eval_replay.py` exactly as `make eval` does — each on its own
isolated mock API — and scores the records with the committed rules. The point is not the scores
themselves (those are the report's job) but that the replay produces a *scoreable* record from a
real run: an outcome, the selection, the bookings bound to their approval, and the agents' own tool
calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from referraleasy_eval.capture import DEFAULT_VARIANT, record_for
from referraleasy_eval.dataset import load_dataset
from referraleasy_eval.rules import RULES

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_replay  # type: ignore[import-not-found]  # noqa: E402


def _release_id() -> str:
    return "rel-20261016-8e00275"


def test_the_standard_scenario_replays_to_an_approved_booking() -> None:
    observation = eval_replay.observe(_release_id(), "standard_booking", DEFAULT_VARIANT)
    assert observation.error is None, observation.error
    assert observation.final_state == "BOOKED"
    # §13: the conversation was pinned by the gateway's router on its first dispatch.
    assert observation.payload["release_pin"]["release_manifest_id"] == _release_id()
    case = next(
        c
        for c in load_dataset().cases
        if c.case_id == "standard_booking/reaches_a_confirmed_booking"
    )
    record = record_for(case, observation)
    assert record.outcome == "completed"
    assert len(record.bookings) == 1
    assert record.bookings[0].approval_id in record.live_approval_ids
    assert [c.name for c in record.tool_calls].count("create_booking") == 1
    for rule in ("outcome_correctness", "unapproved_booking", "double_booking"):
        assert RULES[rule](case, record).score == 1.0, rule


def test_a_lost_commit_response_is_observed_as_an_unknown_outcome() -> None:
    observation = eval_replay.observe(_release_id(), "timeout_after_commit", DEFAULT_VARIANT)
    assert observation.error is None, observation.error
    assert observation.final_state == "BOOKING_UNVERIFIED"
    assert observation.bookings and all(b["state"] != "CONFIRMED" for b in observation.bookings)
