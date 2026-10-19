"""Progress latency and final latency, measured separately and never averaged (§12).

§12 asks for the two to be measured **separately**, and the reason is a product reason rather than a
metrics one: they answer different questions. Progress latency is "how long until the patient saw
something happen"; final latency is "how long until the patient had an outcome". A pipeline optimised
against one blended number can look fast while showing the patient nothing for twenty seconds, which
is the failure this split exists to make visible.

So the tests here are written against three properties, all of which fail if the two are collapsed:

1. each turn carries both readings, and the terminal event — the only event that has a final one —
   has `progress_elapsed_ms` null and the *first* progress reading inside its payload;
2. a turn that never showed the patient anything has no progress latency at all, which is not the same
   as a zero and must not be averaged as one;
3. the two means are computed over their own populations, so neither can be recovered from the other.

The clock is injected (`ManualClock`), so the numbers here are exact rather than approximate: a test
that has to sleep to observe a latency is slow, flaky, or both. Over the wire the app runs with a
3 ms-per-read clock (`conftest`), which is what makes "progress strictly smaller than final" checkable
without a single `sleep`.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from gateway_fixtures import Demo

from referraleasy_contracts.generated.common import ResultOutcome, WorkflowState
from referraleasy_gateway.commands import Turn
from referraleasy_gateway.events import EventLog
from referraleasy_gateway.timing import LatencyLedger, LatencySample, ManualClock, TurnClock

WORKFLOW = UUID(int=1)

#: §9's approved emergency answer, which is a turn that ends in a `terminal` event without a booking:
#: the shortest honest way to observe a final reading on the wire.
EMERGENCY = "I have chest pain and cannot breathe"


def _frames(response: Any) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def _turn(clock: ManualClock, ledger: LatencyLedger, log: EventLog, *, turn_id: str = "t1") -> Turn:
    return Turn(
        workflow_id=WORKFLOW,
        turn_id=turn_id,
        label="coordinator",
        log=log,
        clock=clock,
        latencies=ledger,
        synthetic=True,
    )


# ---------------------------------------------------------------------------
# One turn
# ---------------------------------------------------------------------------


def test_a_turn_carries_both_readings_and_the_terminal_event_carries_the_first_one() -> None:
    """The shape of §12's split on the wire, with exact numbers.

    `progress_elapsed_ms` is a *field* on any event that counts as progress, and it is null on the
    terminal event — the terminal carries the final reading in its payload instead, together with the
    turn's first progress reading. A client that only reads payloads therefore still gets both; the
    event-level field is null so that "this event is not progress" is not something a reader has to
    infer from the type.
    """
    clock = ManualClock()
    ledger = LatencyLedger()
    turn = _turn(clock, ledger, EventLog(synthetic=True))

    clock.advance_ms(120)
    progress_event = turn.progress(step="reading")
    assert progress_event.progress_elapsed_ms == 120
    assert progress_event.payload is not None

    clock.advance_ms(130)
    terminal = turn.terminal(outcome=ResultOutcome.completed)
    assert terminal.progress_elapsed_ms is None
    assert terminal.payload is not None
    assert terminal.payload["progress_elapsed_ms"] == 120
    assert terminal.payload["final_elapsed_ms"] == 250
    assert "progress_elapsed_ms" not in progress_event.payload, (
        "the reading travels on the event, not duplicated into a payload that differs per type"
    )

    turn.close()
    sample = ledger.samples[0]
    assert (sample.progress_ms, sample.final_ms) == (120, 250)
    assert sample.as_dict()["progress_elapsed_ms"] == 120
    assert sample.as_dict()["final_elapsed_ms"] == 250


def test_the_first_progress_reading_is_the_one_that_counts() -> None:
    """§12 measures "how long until the patient saw something happen", which is the *first* thing.

    A turn that emitted five progress events and took twelve seconds to emit the first has a
    twelve-second progress latency. Keeping the latest reading would let a chatty pipeline report a
    fast progress latency while the patient stared at nothing — the exact failure the split is for.
    """
    clock = ManualClock()
    turn_clock = TurnClock(turn_id="t1", workflow_id=str(WORKFLOW), clock=clock, synthetic=True)

    clock.advance_ms(120)
    assert turn_clock.progress_ms() == 120
    clock.advance_ms(130)
    assert turn_clock.progress_ms() == 250, (
        "the call still reports the reading at the moment it is made"
    )

    assert turn_clock.final_ms() == 250
    sample = turn_clock.sample()
    assert sample.progress_ms == 120, "the sample kept the first reading, not the last"
    with pytest.raises(RuntimeError):
        turn_clock.final_ms()
    with pytest.raises(RuntimeError):
        TurnClock(turn_id="t2", workflow_id=str(WORKFLOW), clock=clock, synthetic=True).sample()


def test_a_turn_that_showed_nothing_has_no_progress_latency_rather_than_a_zero() -> None:
    """ "Never showed the patient anything" and "showed them something instantly" are different facts.

    The refusal is checked twice — on a turn with no progress event at all, and on a ledger that has
    only such turns — because the tempting implementation (`0 if not samples else mean`) reports a
    zero that flatters us, and an average over `[0, 0]` is indistinguishable from real data.
    """
    clock = ManualClock()
    ledger = LatencyLedger()
    turn = _turn(clock, ledger, EventLog(synthetic=True), turn_id="silent")
    clock.advance_ms(40)
    turn.terminal(outcome=ResultOutcome.terminal_error)
    turn.close()

    assert ledger.samples[0].progress_ms is None
    assert ledger.progress_mean_ms is None
    assert ledger.final_mean_ms == 40.0
    assert ledger.report()["progress_elapsed_ms_mean"] is None


def test_the_two_means_are_computed_over_their_own_populations() -> None:
    """Neither mean can be recovered from the other, which is what "separately" has to mean.

    Two turns: one that reported progress at 120 ms and finished at 250 ms, and one that reported
    nothing and finished at 10 ms. The progress mean is 120 (the first turn alone) and the final mean
    is 130 — two different numbers over two different populations. A single blended latency would be
    185, and it appears in no field.
    """
    ledger = LatencyLedger()
    ledger.record(LatencySample("t1", str(WORKFLOW), progress_ms=120, final_ms=250, synthetic=True))
    ledger.record(LatencySample("t2", str(WORKFLOW), progress_ms=None, final_ms=10, synthetic=True))

    assert ledger.progress_mean_ms == 120.0
    assert ledger.final_mean_ms == 130.0
    assert ledger.progress_mean_ms != ledger.final_mean_ms

    report = ledger.report()
    assert report["turns"] == 2
    assert report["progress_elapsed_ms_mean"] == 120.0
    assert report["final_elapsed_ms_mean"] == 130.0
    assert report["progress_latency_measured_separately"] is True
    assert [sample["turn_id"] for sample in report["samples"]] == ["t1", "t2"]
    blended = (
        sum(
            sample.progress_ms if sample.progress_ms is not None else sample.final_ms
            for sample in ledger.samples
        )
        / 2
    )
    assert blended not in {report["progress_elapsed_ms_mean"], report["final_elapsed_ms_mean"]}, (
        "the report contains a number that is neither of the two latencies"
    )


def test_an_empty_ledger_reports_no_measurement_rather_than_zero_latency() -> None:
    """The admin view starts before anything has been measured. `None` is the honest answer."""
    report = LatencyLedger().report()
    assert report == {
        "turns": 0,
        "progress_elapsed_ms_mean": None,
        "final_elapsed_ms_mean": None,
        "progress_latency_measured_separately": True,
        "samples": [],
    }


# ---------------------------------------------------------------------------
# Over the wire
# ---------------------------------------------------------------------------


def test_a_refused_command_is_measured_too(client: TestClient, demo: Demo) -> None:
    """`close` runs in a `finally`, and that is the point: the refusals are turns as well.

    Dropping them would report the latency of the easy turns only, and the turn a patient is most
    likely to remember — the one that failed — would be missing from the numbers that describe the
    service. Measured, not completed: the command is still refused and nothing is replayed.
    """
    workflow_id = demo.seed()
    refused = demo.move(workflow_id, version=99, to_state=WorkflowState.PROVIDER_MATCHING)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["sqlstate"] == "RE002"

    latency = demo.metrics().json()["latency"]
    assert latency["turns"] == 1
    assert len(latency["samples"]) == 1
    sample = latency["samples"][0]
    assert sample["workflow_id"] == workflow_id
    assert sample["progress_elapsed_ms"] is not None
    assert sample["final_elapsed_ms"] >= sample["progress_elapsed_ms"]
    assert sample["synthetic"] is True


def test_the_terminal_frame_carries_both_numbers_and_the_metrics_keep_them_apart(
    client: TestClient, demo: Demo
) -> None:
    """The whole §12 contract in one turn, read from the two places a reader can look.

    An emergency message ends its turn with a `terminal` event, so this is the turn where a final
    reading exists on the wire at all — and the assertions are about the *relationship*: the terminal
    frame carries the progress reading inside its payload while its own event-level reading is null,
    the progress frames carry readings, and the admin view reports two means over their own
    populations rather than one latency.
    """
    workflow_id = demo.seed()
    answered = demo.message(workflow_id, text=EMERGENCY, version=1)
    assert answered.status_code == 200, answered.text

    frames = _frames(demo.events(workflow_id))
    terms = [frame for frame in frames if frame["type"] == "terminal"]
    assert len(terms) == 1, frames
    terminal = terms[0]
    assert terminal["progress_elapsed_ms"] is None
    assert isinstance(terminal["payload"]["progress_elapsed_ms"], int)
    assert isinstance(terminal["payload"]["final_elapsed_ms"], int)
    assert terminal["payload"]["final_elapsed_ms"] > terminal["payload"]["progress_elapsed_ms"] >= 0

    progress_frames = [frame for frame in frames if frame["type"] == "agent_progress"]
    notice_frames = [frame for frame in frames if frame["type"] == "notice"]
    assert progress_frames and notice_frames
    assert all(frame["progress_elapsed_ms"] is not None for frame in progress_frames)
    assert all(frame["progress_elapsed_ms"] is None for frame in notice_frames), (
        "a notice is information; counting it as progress would move the turn's first reading to "
        "whichever sentence was emitted first"
    )
    assert (
        terminal["payload"]["progress_elapsed_ms"] == progress_frames[0]["progress_elapsed_ms"]
    ), (
        "the terminal carries the turn's *first* progress reading, which is the one on the first "
        "progress event"
    )

    latency = demo.metrics().json()["latency"]
    assert latency["turns"] == len(latency["samples"]) == 1
    assert latency["progress_latency_measured_separately"] is True
    assert latency["progress_elapsed_ms_mean"] == latency["samples"][0]["progress_elapsed_ms"]
    assert latency["final_elapsed_ms_mean"] == latency["samples"][0]["final_elapsed_ms"]
    assert latency["progress_elapsed_ms_mean"] < latency["final_elapsed_ms_mean"], (
        "the 3 ms-per-read clock makes the turn's progress reading strictly smaller than its final "
        "one, so a report that returned the same number twice would fail here"
    )
