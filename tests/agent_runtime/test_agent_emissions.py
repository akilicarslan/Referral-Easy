"""§15 reaches the agent services as *events*, not only as spans.

The agent half of the adoption question `test_agent_spans.py` answers for traces. Before this, an
A2A task produced a span and nothing else: `latency.invocation.ms` is one of §15's three latency
distributions and had no writer anywhere in the stack, and the same was true of `agent_trigger`.

Three claims, and the third is the one that is easy to get wrong: a **logical** trigger is not an
attempt. A caller that retries a delegation makes three inbound requests and one logical trigger, and
the emitter's deterministic `event.id` is what makes that true without this layer having to know
which request is a retry.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_runtime_doubles import SPEC_ENV
from fastapi.testclient import TestClient

from referraleasy_agent_runtime.app import TASKS_PATH, AgentTaskRequest, create_app
from referraleasy_agent_runtime.auth import AUTH_TOKEN_ENV
from referraleasy_agent_runtime.budget import TurnBudget
from referraleasy_agent_runtime.results import succeeded
from referraleasy_agent_runtime.roles import AgentRole
from referraleasy_agent_runtime.settings import AgentSettings
from referraleasy_telemetry import OutcomeClass
from referraleasy_telemetry.events import TelemetryEmitter
from referraleasy_telemetry.isolation import DropCounter
from referraleasy_telemetry.setup import Telemetry

AUTH = {"Authorization": f"Bearer {SPEC_ENV[AUTH_TOKEN_ENV]}"}

TASK_BODY = {
    "workflow_id": "wf-1",
    "session_id": "sess-1",
    "workflow_state": "AWAITING_SLOT_CHOICE",
    "task": "Confirm the booking.",
    # The key that carries §15's "logical" rule: stable across a caller's retries, distinct between
    # two separate delegations.
    "idempotency_key": "wf-1:confirm",
    "trace_id": "trace-1",
}


class _RecordingLogger:
    """The `emit(**kwargs)` shape `TelemetryEmitter` wraps."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> None:
        self.records.append(kwargs)

    def attributes(self, key: str, *, event: str | None = None) -> list[Any]:
        return [
            record["attributes"][key]
            for record in self.records
            if key in record.get("attributes", {})
            and (event is None or record["attributes"].get("event.name") == event)
        ]


class _EchoHandler:
    """A handler that answers, or raises on demand."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self._error = error

    async def __call__(self, request: AgentTaskRequest, budget: TurnBudget) -> Any:
        from referraleasy_agent_runtime.app import TurnReport

        if self._error is not None:
            raise self._error
        return TurnReport(result=succeeded(), skills=(), trace={})


@pytest.fixture
def recorder() -> _RecordingLogger:
    return _RecordingLogger()


def _client(recorder: _RecordingLogger, handler: Any = None) -> TestClient:
    from opentelemetry import trace

    settings = AgentSettings.from_env(SPEC_ENV, role=AgentRole.SCHEDULING)
    return TestClient(
        create_app(
            settings,
            handler=handler or _EchoHandler(),
            telemetry=Telemetry(
                tracer=trace.get_tracer("agent-emission-test"),
                emitter=TelemetryEmitter(recorder),
                drops=DropCounter(),
                config=None,
            ),
        )
    )


def test_a_task_emits_a_logical_trigger_for_its_role(recorder: _RecordingLogger) -> None:
    """§15's "agent logical trigger count", dimensioned by the role that was triggered."""
    with _client(recorder) as client:
        assert client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH).status_code == 200

    assert recorder.attributes("agent.role", event="agent_trigger") == ["scheduling"]


def test_a_retried_task_is_one_trigger_not_three(recorder: _RecordingLogger) -> None:
    """The property the word *logical* exists for, and the reason the id is derived rather than random.

    Three inbound requests carrying one idempotency key are three documents and one trigger: they
    hash to the same `event.id`, and every §15 count is a `cardinality(event.id)`. A stack that
    counted documents would report a retry storm as a traffic spike.
    """
    with _client(recorder) as client:
        for _ in range(3):
            assert client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH).status_code == 200

    ids = recorder.attributes("event.id", event="agent_trigger")
    assert len(ids) == 3, "three requests are three records"
    assert len(set(ids)) == 1, "…and one logical trigger"


def test_two_different_tasks_are_two_triggers(recorder: _RecordingLogger) -> None:
    """The other half: the id must not collapse two genuinely different delegations.

    Without this, the test above would pass for the wrong reason — an id that ignored the key
    entirely would collapse every trigger of a role into one.
    """
    other = {**TASK_BODY, "idempotency_key": "wf-1:cancel"}
    with _client(recorder) as client:
        client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH)
        client.post(TASKS_PATH, json=other, headers=AUTH)

    ids = recorder.attributes("event.id", event="agent_trigger")
    assert len(set(ids)) == 2


def test_a_task_is_measured_as_an_invocation(recorder: _RecordingLogger) -> None:
    """§15's middle distribution: one agent's work on one task, attempts and tool calls included."""
    with _client(recorder) as client:
        client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH)

    latency = recorder.attributes("metric.value", event="latency")
    assert recorder.attributes("metric.name", event="latency") == ["latency.invocation.ms"]
    assert len(latency) == 1 and float(latency[0]) >= 0.0
    assert recorder.attributes("latency.kind", event="latency") == ["invocation"]


def test_a_successful_task_carries_the_success_class(recorder: _RecordingLogger) -> None:
    """§5's `completed` → §15's `success_full`, through the mapping `results.outcome_class` owns."""
    with _client(recorder) as client:
        client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH)

    assert recorder.attributes("outcome.class", event="outcome") == [OutcomeClass.SUCCESS_FULL]


def test_a_failing_task_still_contributes_its_latency(recorder: _RecordingLogger) -> None:
    """§15: a timeout is still a latency sample, and the way it stops being one is a `finally`.

    A task whose handler raises gets an `error` class *and* its duration, which is the whole reason
    the measurement is emitted from `attempt_span`'s `finally` rather than after the call returns.
    """
    handler = _EchoHandler(error=RuntimeError("the model refused to answer"))
    with _client(recorder, handler) as client:
        assert client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH).status_code == 200

    assert recorder.attributes("outcome.class", event="outcome") == [OutcomeClass.ERROR]
    assert len(recorder.attributes("metric.value", event="latency")) == 1


def test_a_task_with_no_handler_wired_is_not_measured_as_an_invocation() -> None:
    """A container that is up but not wired refuses the task — and measures nothing.

    The refusal happens before the handler boundary, so there is no invocation to time. Recording
    one would put a zero-length sample in the distribution for a service that never ran any work.
    """
    recorder = _RecordingLogger()
    from opentelemetry import trace

    settings = AgentSettings.from_env(SPEC_ENV, role=AgentRole.SCHEDULING)
    app = create_app(
        settings,
        telemetry=Telemetry(
            tracer=trace.get_tracer("agent-emission-test"),
            emitter=TelemetryEmitter(recorder),
            drops=DropCounter(),
            config=None,
        ),
    )
    with TestClient(app) as client:
        assert client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH).status_code == 200

    assert recorder.attributes("metric.name", event="latency") == []
    assert recorder.attributes("agent.role", event="agent_trigger") == []
