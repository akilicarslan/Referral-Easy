"""What the gateway writes into §15, asserted on the record rather than on the index.

The gateway measured turns for §12's stream ledger from the beginning and told the telemetry path
nothing, so `latency.turn.ms` had no writer in any service. That is not a cosmetic gap: §12's
feedback participation rate is votes over the turns a vote could have come from, and the admin read
takes that denominator from this metric — which is why the Feedback page answered `available: false`
against the running stack, refusing to print a rate it could not compute.

These tests assert the *contract* of what is emitted: which metric, which dimensions, which outcome
class. Whether the record reaches OpenSearch is a different claim, and it is made by
`tests/telemetry/test_query_live.py` against a running collector instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway_fixtures import Demo

from referraleasy_contracts.generated.common import ReasonCode, ResultOutcome
from referraleasy_gateway.app import create_app
from referraleasy_gateway.commands import turn_outcome
from referraleasy_telemetry import DropCounter, OutcomeClass, Telemetry, TelemetryEmitter

#: A refusal the regex layer decides on its own, so the test does not depend on a model layer being
#: configured. Same input `tests/gateway/test_guardrails.py` uses for the layer-1 case.
ABUSE = "fuck you"

#: The emergency path: blocked, handled rather than refused, and ended with a `terminal` event —
#: which is what makes it the turn that carries an outcome class. Same text as `test_latency.py`.
EMERGENCY = "I have chest pain and cannot breathe"


class RecordingLogger:
    """The emitter's logger, kept. The telemetry package's own recorder, near enough.

    Defined here rather than imported from `tests/telemetry/conftest.py`, which is on `sys.path`
    under the bare name `conftest` — the trap `tests/test_test_layout.py` exists to catch.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> None:
        self.records.append(kwargs)

    def attributes(self, key: str, *, event: str | None = None) -> list[Any]:
        """One attribute across the records that carry it.

        Records without the attribute are skipped rather than reported as `None`: a turn emits three
        records and only some carry a given dimension, so returning `None` for the others would make
        every assertion read as a list padded with absences. `event` narrows to one event name first,
        which is what a test asking "what did the *guardrail* records say" means.
        """
        return [
            record["attributes"][key]
            for record in self.records
            if key in record.get("attributes", {})
            and (event is None or record["attributes"].get("event.name") == event)
        ]

    def metrics(self) -> list[str]:
        return self.attributes("metric.name")


@pytest.fixture
def recorder() -> RecordingLogger:
    return RecordingLogger()


@pytest.fixture
def watched_app(settings: Any, clock: Any, control: Any, recorder: RecordingLogger) -> FastAPI:
    """The demo app, wired to a recorder instead of a collector.

    Built here rather than through the shared `app` fixture, which deliberately passes no telemetry
    bundle: that is the state the rest of the suite runs in, and it is worth keeping as the default
    so that "a test app exports nothing" stays true without anyone having to assert it.
    """
    from opentelemetry import trace

    return create_app(
        settings=settings,
        clock=clock,
        control=control,
        telemetry=Telemetry(
            tracer=trace.get_tracer("gateway-turn-telemetry-test"),
            emitter=TelemetryEmitter(recorder),
            drops=DropCounter(),
        ),
    )


@pytest.fixture
def watched(watched_app: FastAPI) -> Iterator[Demo]:
    with TestClient(watched_app) as client:
        yield Demo(client)


def _turn_latencies(recorder: RecordingLogger) -> list[float]:
    return [
        float(record["attributes"]["metric.value"])
        for record in recorder.records
        if record.get("attributes", {}).get("metric.name") == "latency.turn.ms"
    ]


def _outcome_classes(recorder: RecordingLogger) -> list[str]:
    """§15's outcome class, which travels as an attribute rather than as a record field.

    The record carries several dimensions and the class is one of them — `outcome.class` — so a
    test that looked for a top-level key would find nothing and read as "no class was recorded".

    Scoped to `event.name = "outcome"` because a turn writes three records of its own (count,
    latency, outcome) and a guardrail screening writes a fourth that carries the class too — a
    `block` is §15's `blocked` in its own right. Asking the recorder for "every record that has an
    outcome class" answers a different question from "what did this turn end as".
    """
    return recorder.attributes("outcome.class", event="outcome")


def test_a_turn_is_measured_as_latency_turn_ms(watched: Demo, recorder: RecordingLogger) -> None:
    """§15's conversational distribution, which nothing emitted before this."""
    workflow_id = watched.seed()
    assert watched.message(workflow_id, text="hello there", version=1).status_code in (200, 202)

    latencies = _turn_latencies(recorder)
    assert len(latencies) == 1, recorder.metrics()
    assert latencies[0] >= 0.0


def test_a_recorded_message_is_measured_without_an_outcome_class(
    watched: Demo, recorder: RecordingLogger
) -> None:
    """Most turns state no outcome, and the record says so rather than having one inferred.

    `terminal` is emitted on the endings that produce a consequential result — a booking, an
    emergency — and a plain recorded message is not one: the gateway recorded it and the coordinator
    decides what happens next. There is a tempting inference available (a `202` with `committed:
    true` is "accepted, not finished", which §15 would class `success_partial`) and it is
    deliberately not made here: the class would then be derived from an HTTP status rather than from
    anything the turn recorded, and a chart with no lineage is the thing this repository refuses.
    The turn is still counted and still contributes its latency.
    """
    workflow_id = watched.seed()
    assert watched.message(workflow_id, text="hello there", version=1).status_code == 202

    assert _outcome_classes(recorder) == []
    assert len(_turn_latencies(recorder)) == 1


def test_an_emergency_turn_carries_its_outcome_class(
    watched: Demo, recorder: RecordingLogger
) -> None:
    """Where a turn *does* end with a `terminal` event, §15 gets the class that event named.

    The emergency path is the one a demo exercises: the guardrail blocks, the patient gets the
    approved response, and the turn ends `needs_input` + `guardrail_blocked` — which is §15's
    `blocked`, not an error. That distinction is the whole reason `blocked` is its own class: a
    guardrail doing its job is not a platform failure, and folding it into `error` would make the
    failure rate disagree with the number of things that actually failed.
    """
    workflow_id = watched.seed()
    answered = watched.message(workflow_id, text=EMERGENCY, version=1)
    assert answered.status_code == 200, answered.text

    assert _outcome_classes(recorder) == [OutcomeClass.BLOCKED]


def test_a_refused_turn_is_recorded_as_blocked(watched: Demo, recorder: RecordingLogger) -> None:
    """The refusal raises out of the command, so `close` runs from a `finally` with the reason gone.

    This is the case `Turn.refused` exists for, and the one §16's rules alert on: a turn the
    guardrail stopped is `blocked`, which is neither a success nor an error, and a stack that
    recorded it as an error would report a failure rate that disagrees with the number of things
    that failed.
    """
    workflow_id = watched.seed()
    refused = watched.message(workflow_id, text=ABUSE, version=1)
    assert refused.status_code == 422, refused.text

    assert _outcome_classes(recorder) == [OutcomeClass.BLOCKED]
    # ...and the turn is still measured. A refused turn took time, and dropping it from the latency
    # distribution would report the speed of the turns that were allowed through.
    assert len(_turn_latencies(recorder)) == 1


def test_a_screening_writes_one_decision_per_layer_that_answered(
    watched: Demo, recorder: RecordingLogger
) -> None:
    """§15's guardrail decisions, dimensioned by stage.

    Per layer rather than per screening, because "which layer decided" is the question an operator
    asks when a good message is refused. Only layer 1 runs here — the shipped demo profile leaves the
    two model layers unconfigured, and an unconfigured layer is a refusal for writes rather than a
    fallback to allow (§9) — so the count is one, which is the assertion that a layer that never ran
    writes nothing.
    """
    workflow_id = watched.seed()
    watched.message(workflow_id, text="hello there", version=1)

    stages = recorder.attributes("guardrail.stage", event="guardrail_decision")
    decisions = recorder.attributes("guardrail.decision", event="guardrail_decision")
    assert stages == ["regex"], recorder.records
    assert decisions == ["allow"]


def test_the_decision_and_the_refusal_are_the_same_event_seen_twice(
    watched: Demo, recorder: RecordingLogger
) -> None:
    """A blocked screening writes `block`, and the turn it refused writes `blocked`.

    Two records, two readers: the guardrail decision is §15's per-stage measurement and the turn
    outcome is what a feedback rate and §16's rules read. They must not be one record with two
    meanings, and they must not disagree — this is the test that would catch a refusal that stopped
    writing one of them.
    """
    workflow_id = watched.seed()
    watched.message(workflow_id, text=ABUSE, version=1)

    assert "block" in recorder.attributes("guardrail.decision")
    assert OutcomeClass.BLOCKED in _outcome_classes(recorder)


# -- the mapping itself ---------------------------------------------------------------------


@pytest.mark.parametrize("outcome", list(ResultOutcome))
def test_every_result_outcome_maps_somewhere(outcome: ResultOutcome) -> None:
    """Total over §5's vocabulary: a new result value must not silently fall through a mapping."""
    assert turn_outcome(outcome, ReasonCode.ok) is not None


def test_an_unfinished_turn_is_measured_without_a_class() -> None:
    """§15 has no "we did not finish" class, and the record says so rather than guessing.

    A turn that never reached a `terminal` event is still counted and still contributes its
    latency; only the class is absent. Inventing a fallback would put a number on a chart that no
    lineage explains.
    """
    assert turn_outcome(None, ReasonCode.ok) is None
