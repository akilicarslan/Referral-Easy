"""Dispatching a committed command to the coordinator (§3, §5, §12).

**This is the piece that was missing entirely.** The patient surface already told them what happens
next — `submit_message` ends with *"Message recorded. The coordinator decides what happens next."*
— and nothing ran a coordinator. The sentence was true about the design and false about the running
system, which is the worst way for a demo to be wrong: every part of it looks like it works.

Three properties decide the shape of this module, and each is a constraint rather than a preference.

**It runs off the request's thread.** A coordinator turn is a model call plus whatever it delegates
to, and §12's surface is built around the patient *not* waiting for it: the write returns a receipt
and the stream carries the progress. Dispatching inline would turn a fast receipt into a multi-second
request and make the stream decorative.

**It is serialised, by one worker and a bounded queue.** A coordinator turn reaches the model and
delegates to specialists, so an unbounded fan-out of them is an unbounded bill. A queue that is full
refuses the dispatch and says so, rather than growing until the container is killed.

**It cannot take the workflow down with it.** The dispatch happens after the patient's command has
been recorded and answered; a coordinator that is unreachable, slow or broken must leave a recorded
command recorded. Every failure path here ends in a notice on the stream and a §15 `api_failure`
event, never in an exception escaping a background thread.

**What this does not do, stated rather than implied.** It does not put the coordinator's *work* on
the patient's surface. A coordinator's substantive output has no wire contract — `AgentTaskResult`
carries an outcome and a protocol state, and nothing else — so rendering one would mean inventing a
payload shape here and hoping the agents agree. What the stream gets is the outcome the agents did
send, which is a typed contract value. The design question ("what does the coordinator return, and
how does the surface render it") is recorded in the README rather than answered by a guess.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from queue import Empty, Full, Queue
from typing import Any, Final
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from referraleasy_contracts import AgentTaskRequest
from referraleasy_contracts.generated.sse import StreamEventType
from referraleasy_gateway.events import EventLog
from referraleasy_gateway.policy import TablePolicy, allowed_actions_for
from referraleasy_gateway.timing import Clock
from referraleasy_telemetry import Synthetic, TelemetryEmitter
from referraleasy_telemetry.identity import inject_context

__all__ = [
    "COORDINATOR_ROLE",
    "DELEGATIONS_PER_TURN",
    "MODEL_CALLS_PER_TURN",
    "QUEUE_DEPTH",
    "TOOL_CALLS_PER_TURN",
    "CoordinatorDispatch",
]

_log = logging.getLogger("referraleasy.gateway.dispatch")

#: The one role the gateway dispatches to. §3's coordinator owns the plan; the specialist roles are
#: reached by *delegation from it*, not by the gateway, which is what keeps `delegate_task` the
#: coordinator's alone.
COORDINATOR_ROLE: Final = "care_coordinator"

#: How many dispatches may be waiting. Small on purpose — each one is a model call.
QUEUE_DEPTH: Final = 8

#: §8's per-turn ceilings, as the root caller declares them in the §5 envelope. They are the
#: runtime's own defaults (`referraleasy_agent_runtime.budget`), mirrored rather than imported for
#: the reason `TASKS_PATH` is spelled below: this module must not pull the agent assembly in. The
#: receiving turn applies its own environment-configured caps regardless — what the envelope's
#: numbers do is travel down the delegation chain, where a child's budget is derived from them.
#: `tests/agents/test_the_two_halves_of_a_task_meet.py` asserts the three stay equal to the runtime's.
MODEL_CALLS_PER_TURN: Final = 12
TOOL_CALLS_PER_TURN: Final = 16
DELEGATIONS_PER_TURN: Final = 8

#: `TaskEnvelope.goal` is at most 2000 characters. The patient's words travel whole in
#: `patient_message`; the *goal* is the one-line instruction the model reads first, so a longer
#: message is truncated there with an explicit marker rather than making the envelope invalid.
GOAL_MAX_CHARS: Final = 2000

#: The A2A task route. Shared with the runtime's `TASKS_PATH`, spelled here rather than imported
#: because this module must not depend on `referraleasy_agent_runtime`: that package is how a
#: service *serves* tasks, and a caller importing it would pull the whole agent assembly in.
TASKS_PATH: Final = "/tasks"

#: The **logical ref** this dispatch is reported under in §15, in the `service:path` form the
#: emitter documents. Not the URL it is sent to: a host is unbounded, and §15's API-failure chart
#: groups by this field, so a URL would split the chart by deployment. The emitter refuses a URL
#: rather than trusting the caller to know that — it did refuse one here, which is how the point was
#: made. `{role}` is deliberately not expanded: the ref is about *which call this was*, and the
#: gateway makes exactly one.
ENDPOINT_REF: Final = "agent-coordinator:/tasks"


@dataclass(frozen=True, slots=True)
class _Job:
    """One queued dispatch: the envelope and the context it has to be sent under."""

    request: AgentTaskRequest
    traceparent: dict[str, str]


class CoordinatorDispatch:
    """One coordinator call per committed command, on a worker thread.

    Built with a `client` seam so a test can assert the envelope and the headers without a socket,
    and so the shipped profile can hand it the same kind of client the rest of the gateway uses.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        release_manifest_id: str,
        log: EventLog,
        clock: Clock,
        emitter: TelemetryEmitter | None = None,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
        queue_depth: int = QUEUE_DEPTH,
    ) -> None:
        # `{role}` is `A2A_BASE_URL`'s placeholder, the same variable the agents use for their peer
        # map — one pattern rather than a URL per role that can disagree with the service name.
        self._url = base_url.replace("{role}", COORDINATOR_ROLE).rstrip("/")
        self._token = token
        self._release_manifest_id = release_manifest_id
        self._log = log
        self._clock = clock
        self._emitter = emitter
        self._timeout = timeout
        self._client = client
        self._queue: Queue[_Job] = Queue(maxsize=queue_depth)
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        """Whether there is anywhere to dispatch to, under a release that can be named.

        The release is part of "configured" because §13 makes it part of the task: a conversation
        pinned to nothing is a conversation no report can be compared against, and
        `TaskEnvelope.release_manifest_id` is required. A deployment that has a coordinator but no
        release to name is misconfigured, and saying so here is how it refuses to pretend.
        """
        return bool(self._url and self._token and self._release_manifest_id)

    # -- the caller's half ------------------------------------------------------

    def execute(
        self,
        *,
        workflow_id: UUID,
        workflow_state: str,
        state_version: int,
        command_id: str,
        specialist_inputs: Mapping[str, Any],
        execution_plan: list[dict[str, Any]],
        patient_details_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Execute a stored workflow step and return its typed source results."""
        envelope = self._envelope(
            workflow_id=workflow_id,
            workflow_state=workflow_state,
            state_version=state_version,
            command_id=command_id,
            goal="Execute the standard referral step.",
            deadline_at=self._clock.now() + timedelta(seconds=self._timeout),
            synthetic=True,
        )
        if patient_details_id:
            envelope["patient_details_id"] = patient_details_id
        request = AgentTaskRequest(
            workflow_id=str(workflow_id),
            workflow_state=workflow_state,
            session_id=f"{workflow_id}:{command_id}",
            task=envelope["goal"],
            idempotency_key=f"{workflow_id}:{command_id}",
            trace_id="",
            payload={
                "task_envelope": envelope,
                "command_id": command_id,
                "execution_mode": "workflow",
                "patient_message": "",
                "workflow_summary": f"{workflow_state} at version {state_version}.",
                "specialist_inputs": dict(specialist_inputs),
                "execution_plan": execution_plan,
            },
        )
        response = self._client_or_default().post(
            f"{self._url}{TASKS_PATH}",
            json=request.model_dump(mode="json"),
            headers={"Authorization": f"Bearer {self._token}", **dict(inject_context())},
        )
        response.raise_for_status()
        body = response.json()
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise ValueError("The coordinator returned no task result.")
        return result

    def submit(
        self,
        *,
        workflow_id: UUID,
        workflow_state: str,
        state_version: int,
        command_id: str,
        task: str,
        patient_message: str,
        workflow_summary: str,
        synthetic: bool,
        specialist_inputs: Mapping[str, Any] | None = None,
    ) -> bool:
        """Queue one dispatch. Returns whether it was queued, and never blocks on the coordinator.

        `traceparent` is captured **here**, on the request's thread, while the patient's server span
        is still active — not on the worker, where there is no context and the coordinator's trace
        would start from nothing. That capture is what makes the coordinator's span a *child* of the
        request that triggered it, which is §15's "propagate rather than correlate" and the reason a
        demo can show one trace across the gateway and the agents instead of several adjacent ones.

        The payload is the §5 task contract's two halves — the envelope and the receiving role's own
        input document — and neither is optional. They used to be absent: the dispatch sent
        `{"message", "command_id", "state"}`, which `CoordinatorInput` (required
        `patient_message`/`workflow_summary`, `extra="forbid"`) refuses at validation, so every
        coordinator turn projected to `terminal_error` before the model was ever called. The
        envelope's absence was worse in kind: `delegate_task` derives the child's envelope from the
        parent's, so a coordinator with no envelope could not delegate even if its input had
        validated. `tests/agents/test_the_two_halves_of_a_task_meet.py` now validates this request
        against the receiving schema, which is the check that would have caught both.
        """
        if not self.configured:
            return False
        deadline_at = self._clock.now() + timedelta(seconds=self._timeout)
        job = _Job(
            request=AgentTaskRequest(
                workflow_id=str(workflow_id),
                # The agents' own convention, from `assembly.CaseKey`: one workflow can be acted on
                # by several commands, and the session is per-command.
                session_id=f"{workflow_id}:{command_id}",
                workflow_state=workflow_state,
                task=task,
                idempotency_key=f"{workflow_id}:{command_id}",
                trace_id=dict(inject_context()).get("traceparent", ""),
                payload={
                    "task_envelope": self._envelope(
                        workflow_id=workflow_id,
                        workflow_state=workflow_state,
                        state_version=state_version,
                        command_id=command_id,
                        goal=task,
                        deadline_at=deadline_at,
                        synthetic=synthetic,
                    ),
                    "command_id": command_id,
                    # `CoordinatorInput`'s own fields. `completed_steps` is left to its default: the
                    # gateway records answers, not a named step history, and naming steps it did not
                    # record would be inventing workflow state — the one thing this repo does not do.
                    "patient_message": patient_message,
                    "workflow_summary": workflow_summary,
                    **({"specialist_inputs": dict(specialist_inputs)} if specialist_inputs else {}),
                },
            ),
            traceparent=dict(inject_context()),
        )
        try:
            self._queue.put_nowait(job)
        except Full:
            # Refused rather than grown. The queue being full is a fact about the deployment — the
            # coordinator is slower than the patient is — and the honest answer is to say so.
            _log.warning("the dispatch queue is full; refusing a dispatch for %s", workflow_id)
            return False
        self._ensure_worker()
        return True

    def _envelope(
        self,
        *,
        workflow_id: UUID,
        workflow_state: str,
        state_version: int,
        command_id: str,
        goal: str,
        deadline_at: datetime,
        synthetic: bool,
    ) -> dict[str, Any]:
        """The §5 envelope for a root task, from the facts the gateway owns.

        Every field is read, derived or declared — none is guessed:

        * The workflow facts (id, state, version, command) come from the row the command just
          committed.
        * `allowed_actions` is the §4 table's own answer for `(coordinator, state)`, from the same
          seed the database's table is checked against. It is empty in states where the coordinator
          may do nothing, which is a true answer rather than a missing one.
        * `deadline` and `call_budget` are this dispatcher's own declaration: the deadline is the
          dispatch timeout, and the counters are §8's defaults a delegation chain inherits.
        * `release_manifest_id` is the release this gateway was configured to dispatch under (§13),
          resolved at startup — the envelope cannot be built without one, which `configured` says.
        * `task_id` is derived from `(workflow, command)` so a redelivery of the same committed
          command is the same task rather than a second one.
        """
        deadline_iso = deadline_at.isoformat()
        return {
            "task_id": str(uuid5(NAMESPACE_URL, f"referraleasy://task/{workflow_id}/{command_id}")),
            "workflow_id": str(workflow_id),
            "command_id": command_id,
            "goal": goal if len(goal) <= GOAL_MAX_CHARS else goal[: GOAL_MAX_CHARS - 1] + "…",
            "workflow_state": workflow_state,
            "current_state_version": state_version,
            "allowed_actions": list(
                allowed_actions_for(
                    TablePolicy.from_seed().rows,
                    role=COORDINATOR_ROLE,
                    workflow_state=workflow_state,
                )
            ),
            "deadline": deadline_iso,
            "call_budget": {
                "deadline_at": deadline_iso,
                "remaining_model_calls": MODEL_CALLS_PER_TURN,
                "remaining_tool_calls": TOOL_CALLS_PER_TURN,
                "remaining_delegations": DELEGATIONS_PER_TURN,
            },
            "release_manifest_id": self._release_manifest_id,
            "synthetic": synthetic,
        }

    # -- the worker's half ------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._drain,
                name="referraleasy-coordinator-dispatch",
                # A daemon, so a dispatch in flight cannot hold the process open past shutdown. A
                # coordinator call is not worth delaying a redeploy for, and the command it is
                # acting on is already recorded.
                daemon=True,
            )
            self._worker.start()

    def _drain(self) -> None:
        while True:
            try:
                job = self._queue.get(timeout=1.0)
            except Empty:
                # Idle exit: the thread is started on demand and stops when there is nothing to do,
                # so a gateway that never sees a message does not carry a thread for it.
                with self._lock:
                    if self._queue.empty():
                        self._worker = None
                        return
                # A submit raced with the idle timeout while this thread was still alive.
                continue
            try:
                self._deliver(job)
            except Exception:
                # The module's third property, enforced at the loop's edge: a background thread that
                # dies takes every later dispatch with it, and the failure it carries would be
                # reported nowhere a reader can see.
                _log.exception("a coordinator dispatch failed outside its own handling")
            finally:
                self._queue.task_done()

    def _deliver(self, job: _Job) -> None:
        """Send one envelope, and record what came back on the workflow's stream."""
        workflow_id = UUID(job.request.workflow_id)
        headers = {"Authorization": f"Bearer {self._token}", **job.traceparent}
        try:
            response = self._client_or_default().post(
                f"{self._url}{TASKS_PATH}",
                json=job.request.model_dump(mode="json"),
                headers=headers,
            )
        except httpx.HTTPError as error:
            self._failed(workflow_id, detail=type(error).__name__)
            return
        if response.status_code != 200:
            self._failed(workflow_id, detail=str(response.status_code))
            return
        self._noticed(workflow_id, self._outcome_of(response))

    def _outcome_of(self, response: httpx.Response) -> str:
        """The §5 outcome from the agent's envelope, or a statement that it was not one.

        The envelope is `{"trace": …, "result": {"outcome"|"event": …, "a2a_state": …}}`. Only the
        contract's own values are read, and a body that is not that shape is reported as unreadable
        rather than guessed at — an agent that answers 200 with something else is a bug worth seeing,
        not a success worth recording.
        """
        try:
            body = response.json()
        except ValueError:
            return "unreadable-response"
        if not isinstance(body, dict):
            return "unreadable-response"
        result = body.get("result")
        if not isinstance(result, dict):
            return "unreadable-response"
        outcome = result.get("outcome") or result.get("event")
        return str(outcome) if outcome else "unreadable-response"

    def _noticed(self, workflow_id: UUID, outcome: str) -> None:
        """Append the coordinator's answer to the workflow's stream.

        A `notice`, not a state change: §12 keeps `notice` as "information about the surface", and
        this is exactly that — the workflow's *state* moves when something decides it moves, and the
        coordinator's outcome is a fact about a call rather than a transition.
        """
        self._log.append(
            workflow_id,
            StreamEventType.notice,
            {
                "text": f"The coordinator ran and reported: {outcome}.",
                "level": "warning" if outcome not in ("completed", "no_match") else "info",
                "synthetic": True,
            },
            created_at=self._clock.now(),
        )

    def _failed(self, workflow_id: UUID, *, detail: str) -> None:
        """Report an unreachable or refusing coordinator on the stream, and to §15.

        §15 asks for API failures "by endpoint and status", so the failure is a measurement and not
        only a sentence on a surface — an operator looking at the API-failure chart should be able to
        see the coordinator being down without reading a patient's stream.

        The endpoint is a **logical ref**, `service:path`, and not the URL it was sent to. The
        emitter refuses a URL outright, for a reason worth keeping in view: a host is unbounded, and a
        dimension whose values are hosts splits a chart into one bar per deployment.
        """
        if self._emitter is not None:
            self._emitter.api_failure(
                endpoint=ENDPOINT_REF,
                failure_kind="coordinator_unreachable",
                synthetic=Synthetic.SYNTHETIC,
            )
        self._log.append(
            workflow_id,
            StreamEventType.notice,
            {
                # The patient is told the workflow is still recorded, because it is: this runs after
                # the command committed, and a failed dispatch does not undo it. A notice that only
                # said "the coordinator failed" would leave them unsure whether their message landed.
                "text": (
                    "The coordinator could not be reached, so nothing has been decided yet. Your "
                    "message is recorded and nothing has been lost."
                ),
                "level": "warning",
                "detail": {"detail": detail},
                "synthetic": True,
            },
            created_at=self._clock.now(),
        )

    def _client_or_default(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client
