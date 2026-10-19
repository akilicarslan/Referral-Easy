"""The gateway hands a committed message to §3's coordinator — and survives not being able to.

The claim under test is a small one with a large consequence: `submit_message` used to end with
*"Message recorded. The coordinator decides what happens next."* while no coordinator ran. These
tests are written around the two ways that can go wrong now that it does — a dispatch that is never
sent, and one that is sent and whose failure takes the workflow's thread down with it.

The delivery tests poll the workflow's log rather than reaching into the dispatcher, because the
thing being asserted is what the *patient* ends up seeing, and the dispatch happens on another
thread by design. A test that called the delivery method directly would assert the part that was
never in doubt.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway_fixtures import Demo
from opentelemetry.sdk.trace import TracerProvider

from referraleasy_contracts.generated.sse import StreamEventType
from referraleasy_gateway.app import create_app
from referraleasy_gateway.dispatch import COORDINATOR_ROLE, CoordinatorDispatch
from referraleasy_gateway.events import EventLog
from referraleasy_gateway.timing import ManualClock
from referraleasy_telemetry import DropCounter, Telemetry, TelemetryEmitter

COORDINATOR_URL = f"http://agent-{COORDINATOR_ROLE}:8080"


class _Recorder:
    """The emitter's logger, keeping what a dispatch reported to §15."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> None:
        self.records.append(kwargs)

    def kinds(self) -> list[Any]:
        return [
            record["attributes"].get("api.failure_kind")
            for record in self.records
            if "api.failure_kind" in record.get("attributes", {})
        ]


class _Transport:
    """An httpx-shaped client that answers from a script instead of a socket."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        body: dict[str, Any] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body or {"result": {"outcome": "completed", "a2a_state": "completed"}}
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.raises is not None:
            raise self.raises
        request = httpx.Request("POST", url)
        return httpx.Response(self.status_code, json=self.body, request=request)


@pytest.fixture
def log() -> EventLog:
    return EventLog(synthetic=True)


@pytest.fixture
def recorded() -> _Recorder:
    return _Recorder()


#: The release every dispatched task is pinned to (§13). A real manifest id, not a placeholder:
#: `configured` refuses to dispatch without one, and a test that invented a value to get past that
#: would be testing a deployment this stack never runs.
RELEASE_ID = "rel-20261016-8e00275"


def _dispatcher(log: EventLog, transport: _Transport, recorded: _Recorder) -> CoordinatorDispatch:
    return CoordinatorDispatch(
        base_url="http://agent-{role}:8080",
        token="service-token",
        release_manifest_id=RELEASE_ID,
        log=log,
        clock=ManualClock(),
        emitter=TelemetryEmitter(recorded),
        client=transport,  # type: ignore[arg-type]
    )


def _submit(
    dispatcher: CoordinatorDispatch,
    workflow_id: UUID,
    *,
    workflow_state: str = "INTAKE",
    command_id: str = "cmd-1",
    task: str = "t",
    patient_message: str = "hello",
    workflow_summary: str = "Workflow is in INTAKE at version 1.",
) -> bool:
    """Submit with the facts a committed message supplies — never with a hand-built payload."""
    return dispatcher.submit(
        workflow_id=workflow_id,
        workflow_state=workflow_state,
        state_version=1,
        command_id=command_id,
        task=task,
        patient_message=patient_message,
        workflow_summary=workflow_summary,
        synthetic=True,
    )


def _await_notice(log: EventLog, workflow_id: UUID, timeout: float = 3.0) -> Any:
    """Wait for the dispatch to land on the workflow's stream. Fails rather than hanging."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        notices = [
            event for event in log.events_for(workflow_id) if event.type is StreamEventType.notice
        ]
        if notices:
            return notices[-1]
        time.sleep(0.01)
    raise AssertionError(
        f"no notice reached workflow {workflow_id} within {timeout:.0f}s; the dispatch either was "
        f"not sent or its worker thread did not survive it"
    )


def test_the_envelope_carries_the_workflow_and_the_patients_command(
    log: EventLog, recorded: _Recorder
) -> None:
    """The A2A request shape, including the two fields that are conventions rather than obvious.

    `session_id` is the agents' own `f"{workflow_id}:{command_id}"` — one workflow can be acted on by
    several commands, and the session is per-command. `idempotency_key` is what makes §15's *logical*
    trigger count survive a retry.
    """
    transport = _Transport()
    dispatcher = _dispatcher(log, transport, recorded)
    workflow_id = uuid4()

    assert _submit(dispatcher, workflow_id, task="The patient sent this message.")

    _await_notice(log, workflow_id)
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == f"{COORDINATOR_URL}/tasks"
    assert call["headers"]["Authorization"] == "Bearer service-token"
    assert call["json"]["workflow_id"] == str(workflow_id)
    assert call["json"]["session_id"] == f"{workflow_id}:cmd-1"
    assert call["json"]["idempotency_key"] == f"{workflow_id}:cmd-1"
    assert call["json"]["workflow_state"] == "INTAKE"

    # The payload is §5's two halves, and the next hop validates both: the envelope is why
    # `delegate_task` can derive a child task, and `patient_message`/`workflow_summary` are the
    # coordinator's own input fields (its schema forbids any other key name). The cross-schema
    # proof lives in `tests/agents/test_the_two_halves_of_a_task_meet.py`; this assertion is the
    # sender's half, kept here so a payload edit fails next to the code that makes it.
    payload = call["json"]["payload"]
    assert set(payload) == {"task_envelope", "command_id", "patient_message", "workflow_summary"}
    envelope = payload["task_envelope"]
    assert envelope["workflow_id"] == str(workflow_id)
    assert envelope["command_id"] == "cmd-1"
    assert envelope["workflow_state"] == "INTAKE"
    assert envelope["current_state_version"] == 1
    assert envelope["release_manifest_id"] == RELEASE_ID
    assert envelope["allowed_actions"] == ["care_coordinator:delegate_task"]
    assert envelope["synthetic"] is True
    assert payload["patient_message"] == "hello"


def test_the_dispatch_carries_the_callers_trace_context(log: EventLog, recorded: _Recorder) -> None:
    """Propagation, not correlation: the agent's span becomes a child of the request that caused it.

    Without the header the coordinator's trace would start from nothing, and a demo would show the
    gateway's trace and the agents' traces as unrelated entries that happen to be adjacent — which is
    the difference §15 names between propagating and correlating.
    """
    transport = _Transport()
    dispatcher = _dispatcher(log, transport, recorded)
    workflow_id = uuid4()

    # Inside a span, because that is the only state in which there is anything to propagate: `submit`
    # captures the *active* context, and a real caller reaches it from inside the request's server
    # span. A test that called it with no span would assert that no header is sent — true, and not
    # the claim.
    provider = TracerProvider()
    with provider.get_tracer("dispatch-test").start_as_current_span("POST /messages"):
        _submit(dispatcher, workflow_id)
    _await_notice(log, workflow_id)

    headers = transport.calls[0]["headers"]
    assert "traceparent" in headers, headers
    # `00-<32 hex>-<16 hex>-<2 hex>`, the W3C shape — not a bare id this repo invented.
    parts = headers["traceparent"].split("-")
    assert len(parts) == 4, headers["traceparent"]
    assert len(parts[1]) == 32 and len(parts[2]) == 16


def test_a_successful_dispatch_tells_the_patient_what_came_back(
    log: EventLog, recorded: _Recorder
) -> None:
    """The coordinator's §5 outcome, on the stream, as a `notice` rather than a state change."""
    transport = _Transport(body={"result": {"outcome": "completed", "a2a_state": "completed"}})
    dispatcher = _dispatcher(log, transport, recorded)
    workflow_id = uuid4()

    _submit(dispatcher, workflow_id)

    notice = _await_notice(log, workflow_id)
    assert notice.type is StreamEventType.notice
    assert "completed" in notice.payload["text"]
    assert notice.payload["level"] == "info"


def test_an_unreachable_coordinator_is_reported_and_measured(
    log: EventLog, recorded: _Recorder
) -> None:
    """§15's API failures are measurements, so the coordinator being down is not only a sentence.

    An operator reading the API-failure chart should be able to see this without opening a patient's
    stream — and the notice says the message is still recorded, because it is: the dispatch runs
    after the command committed, and a failed dispatch does not undo one.
    """
    transport = _Transport(raises=httpx.ConnectError("connection refused"))
    dispatcher = _dispatcher(log, transport, recorded)
    workflow_id = uuid4()

    _submit(dispatcher, workflow_id)

    notice = _await_notice(log, workflow_id)
    assert notice.payload["level"] == "warning"
    assert "recorded" in notice.payload["text"]
    assert recorded.kinds() == ["coordinator_unreachable"]


def test_a_non_200_answer_is_a_failure_not_a_success(log: EventLog, recorded: _Recorder) -> None:
    """The agent's task route answers 200 even when it refuses, so a non-200 is the hop itself."""
    transport = _Transport(status_code=503)
    dispatcher = _dispatcher(log, transport, recorded)
    workflow_id = uuid4()

    _submit(dispatcher, workflow_id)

    notice = _await_notice(log, workflow_id)
    assert notice.payload["level"] == "warning"
    assert recorded.kinds() == ["coordinator_unreachable"]


def test_a_body_that_is_not_an_envelope_is_not_reported_as_success(
    log: EventLog, recorded: _Recorder
) -> None:
    """An agent answering 200 with something else is a bug worth seeing, not a success to record."""
    transport = _Transport(body={"unexpected": True})
    dispatcher = _dispatcher(log, transport, recorded)
    workflow_id = uuid4()

    _submit(dispatcher, workflow_id)

    notice = _await_notice(log, workflow_id)
    assert "unreadable-response" in notice.payload["text"]


def test_a_full_queue_refuses_rather_than_growing(log: EventLog, recorded: _Recorder) -> None:
    """A bounded queue is the difference between a slow coordinator and an unbounded bill."""
    dispatcher = CoordinatorDispatch(
        base_url="http://agent-{role}:8080",
        token="t",
        release_manifest_id=RELEASE_ID,
        log=log,
        clock=ManualClock(),
        client=_Transport(raises=httpx.ConnectError("never called")),  # type: ignore[arg-type]
        queue_depth=1,
    )
    # The worker is never started, so nothing drains the one slot.
    assert dispatcher._queue.put_nowait  # the queue exists and is empty
    dispatcher._queue.put_nowait(object())  # type: ignore[arg-type]

    assert not _submit(dispatcher, uuid4(), command_id="c")


# -- from the patient's side ---------------------------------------------------------------


def _app_with_dispatch(
    settings: Any,
    clock: Any,
    control: Any,
    guardrails: Any,
    recorded: _Recorder,
) -> FastAPI:
    from opentelemetry import trace

    app = create_app(
        settings=settings,
        clock=clock,
        control=control,
        # The *configured* pipeline, and it is load-bearing: with the shipped profile's two model
        # layers unconfigured, §9's closed-dependency rule turns every screening into `clarify`, and
        # a clarify returns before the commit. Nothing reaches the coordinator — which is correct,
        # and is why reaching this path in a test means supplying real layers behind the seam.
        guardrails=guardrails,
        telemetry=Telemetry(
            tracer=trace.get_tracer("gateway-dispatch-test"),
            emitter=TelemetryEmitter(recorded),
            drops=DropCounter(),
        ),
    )
    # Nothing is injected here: `create_app` builds its dispatcher from the environment, and the
    # unit profile has no `A2A_BASE_URL`, so it builds none. That is the state the test below is
    # about — and engineering a fake one in would make the assertion meaningless.
    return app


@pytest.fixture
def watched(
    settings: Any, clock: Any, control: Any, configured_pipeline: Any, recorded: _Recorder
) -> Iterator[Demo]:
    app = _app_with_dispatch(settings, clock, control, configured_pipeline, recorded)
    with TestClient(app) as client:
        yield Demo(client)


def test_the_receipt_says_whether_anything_will_act_on_the_message(watched: Demo) -> None:
    """No coordinator configured is a supported state, and the patient is told rather than misled.

    The shipped unit profile has no `A2A_BASE_URL`, so this is what the suite's own app does — and
    the assertion is on the *shape* of the answer: `dispatched` is present and false, and the notice
    does not promise a coordinator. A profile that quietly claimed one would be the same lie the
    original sentence was.
    """
    demo = watched
    workflow_id = demo.seed()
    answered = demo.message(workflow_id, text="hello there", version=1)
    assert answered.status_code == 202, answered.text

    body = json.loads(answered.text) if isinstance(answered.text, str) else answered.json()
    assert body["dispatched"] is False

    # One frame per `data: ` line — the same parse `test_latency.py` uses, and the split has to be on
    # lines rather than on blank-line-separated blocks, because that is what the wire carries.
    frames = [
        json.loads(line[len("data: ") :])
        for line in demo.events(workflow_id).text.splitlines()
        if line.startswith("data: ")
    ]
    notices = [frame for frame in frames if frame["type"] == "notice"]
    assert any("No coordinator is configured" in frame["payload"]["text"] for frame in notices), (
        notices
    )


def test_a_dispatch_arriving_during_idle_exit_is_not_stranded(
    log: EventLog, recorded: _Recorder
) -> None:
    from queue import Empty, Queue

    transport = _Transport()
    dispatcher = _dispatcher(log, transport, recorded)
    first, second = uuid4(), uuid4()

    class IdleRaceQueue(Queue):
        took_first = False
        raced = False

        def get(self, block=True, timeout=None):
            if self.took_first and not self.raced:
                self.raced = True
                # Enqueue after the last idle get has timed out, before the worker returns.
                assert _submit(dispatcher, second)
                raise Empty
            result = super().get(block=block, timeout=timeout)
            self.took_first = True
            return result

    dispatcher._queue = IdleRaceQueue()
    assert _submit(dispatcher, first)
    _await_notice(log, first)
    _await_notice(log, second)
    assert len(transport.calls) == 2
