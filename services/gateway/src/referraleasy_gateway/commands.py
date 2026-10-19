# ruff: noqa: RUF002  # Turkish quoted from §9 of ReferralEasy_Architecture_v3.md is kept verbatim
"""The command plane: what a patient action, a message, a transition and a booking actually do.

This is the only module that *changes* control state, and everything it changes is one of four
operations, each with a different authority behind it:

==============================  ===============================  ======================================
operation                        authority                       what it may change
==============================  ===============================  ======================================
patient action (`system:*`)      a `policy_rule` row             the answer in `workflow.payload`
patient message                  the guardrail pipeline          the message in `workflow.payload`
`POST …/transitions`             an admin session                `workflow.state`
`POST …/bookings`                policy + guardrails + approval  the booking row and `workflow.state`
==============================  ===============================  ======================================

Six rules decide the shape of all of them, and they are the ones the phase is measured on.

**An action ref names the tool the platform runs, not the caller's role.** A patient tapping a card
is not a `scheduling` agent; they are a person whose tap causes the platform to run a scheduling
lookup. So the policy check answers "may this action run in this state at all", which is what §4's
table is about, and the caller's identity is checked separately by the *scope* rule below. A patient
cannot reach the booking write by submitting it: they can only submit an action the server put on an
instance they hold (`instance.action != action.action` is a refusal), and no card in the catalog
issues a booking-write action.

**Every action ref goes through the policy table.** `_authorise` is the only door, and it asks
`ActionPolicy` — never "is this the scheduler?" A role check here would be a second copy of §4's
table, and the copy is what drifts. `tests/gateway/test_actions.py` edits the table mid-test to show
the answer comes from the row rather than from the code.

**Ownership is checked on every read and every write.** A patient asking for a workflow that is not
theirs gets `404`, not `403`: "forbidden" confirms the id exists, which is a fact about someone else
(§9). An admin session is the coordinator's and is not patient-scoped.

**A stale `workflow_state_version` is refused, never applied**, and the refusal comes from the
control-state port (`IllegalTransitionError`, `RE002`) so the memory and PostgreSQL profiles refuse it
the same way.

**Only a completed command is recorded.** `command_id` is the idempotency key, so a repeat of a
completed command returns its stored response byte for byte and does no work — that is what makes
"replaying a stream does not create a second booking" a property of the server rather than of a
client's retry policy. A command that *raised* is left unrecorded: nothing ran, so the next attempt
re-evaluates the world instead of replaying a refusal that may no longer be true. `RE005` still bites
in that state, because `begin_command` compares the request hash on every attempt: one id, one body,
whoever asks.

**Partial output is shown and not committed.** `POST …/transitions` with `complete: false` emits the
text as `partial_text` events (non-authoritative, §12), calls `AgentDecision.commit` to *prove* it
refuses, and answers `202` having changed nothing: no state move, no surface, no ledger row.

**Who decides the next state.** Not this service. A patient's choice is recorded as an answer; the
coordinator (§4's flow — the agent runtime in Phase 3, or `POST …/transitions` in the demo) decides
where the workflow goes next and supplies the props for the card that state shows. The gateway holds
no copy of `workflow_transition`: a second state machine in the transport is exactly what migration
014's guard exists to prevent, so an illegal edge is refused by the database, and `_route_to_human`
treats that refusal as a fact rather than a bug.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel

from referraleasy_contracts.generated.common import (
    BookingState,
    GuardrailCategory,
    GuardrailDecision,
    ReasonCode,
    ResultOutcome,
    WorkflowState,
)
from referraleasy_contracts.generated.sse import (
    ClientAction,
    ComponentInstance,
    RenderableComponent,
    StreamEvent,
    StreamEventType,
    SurfaceUpdate,
    WorkflowStateChange,
)
from referraleasy_domain import (
    CommandConflictError,
    CommandDecision,
    DomainError,
    IllegalTransitionError,
    canonical_request_hash,
)
from referraleasy_domain.approval import PAYLOAD_HASH_KEY
from referraleasy_gateway.approval import ApprovalService, payload_hash_of
from referraleasy_gateway.auth import Session
from referraleasy_gateway.continuations import ContinuationKind, ContinuationLifecycle
from referraleasy_gateway.control import ControlState, WorkflowRow
from referraleasy_gateway.dispatch import CoordinatorDispatch
from referraleasy_gateway.errors import (
    BookingHaltedError,
    ContractViolationError,
    InputBlockedError,
    NotFoundError,
)
from referraleasy_gateway.events import EventLog
from referraleasy_gateway.guardrails import GuardrailOutcome, GuardrailPipeline
from referraleasy_gateway.killswitch import KillSwitch
from referraleasy_gateway.policy import ActionPolicy
from referraleasy_gateway.projection import (
    SurfaceChange,
    WorkflowSurfaceProjection,
)
from referraleasy_gateway.surfaces import (
    HELP_CARD_COMPONENT,
    AgentDecision,
    PartialOutputError,
    instance_id_for,
    surface_id_for,
    unwrap,
)
from referraleasy_gateway.timing import Clock, LatencyLedger, LatencySample, TurnClock
from referraleasy_telemetry import LatencyKind, OutcomeClass, Synthetic, TelemetryEmitter

__all__ = [
    "ACTION_AWAIT_CHOICE",
    "ACTION_CAPTURE_CONSENT",
    "ACTION_CREATE_BOOKING",
    "ACTION_LOOKUP_REQUEST",
    "ANSWER_KEY",
    "BOOKING_OUTCOME_CONFIRMED",
    "BOOKING_OUTCOME_UNVERIFIED",
    "BOOKING_PAYLOAD_KEY",
    "MESSAGE_KEY",
    "PAYLOAD_HASH_KEY",
    "CommandReceipt",
    "CommandService",
    "SubmittedAction",
    "Turn",
    "question_is_open",
    "unwrap",
]

#: The action refs this service executes, listed against the policy table's eight roles: two belong
#: to `system` (the surface's own questions), two to `scheduling` (the booking and its
#: reconciliation). Nothing outside this set is executable, and `ACTION_CREATE_BOOKING` is reachable
#: only from `book`, never from a card.
ACTION_AWAIT_CHOICE: Final = "system:await_patient_choice"
ACTION_CAPTURE_CONSENT: Final = "system:capture_consent"
ACTION_CREATE_BOOKING: Final = "scheduling:create_booking"
ACTION_LOOKUP_REQUEST: Final = "scheduling:lookup_request"

#: What the scheduler reports back. `confirmed` moves the workflow to `BOOKED`; anything short of a
#: confirmed result moves it to `BOOKING_UNVERIFIED`, which §7 resolves by reconciliation and never
#: by re-booking.
BOOKING_OUTCOME_CONFIRMED: Final = "confirmed"
BOOKING_OUTCOME_UNVERIFIED: Final = "unverified"

#: The wire's two outcomes as the states the port records. This is the *wire* vocabulary, which is
#: `unverified`/`confirmed` and deliberately not `BookingState`: `unverified` is not a booking state,
#: it is the absence of an answer, and the row for it says `REQUESTED` — the gateway asked and no
#: hospital has replied. One table, so the two vocabularies meet in one place and a third outcome is
#: a row to add rather than a branch to find. `CONFIRMED` requires the hospital's own `external_ref`;
#: see `control._assert_recordable_booking`.
BOOKING_STATES: Final[dict[str, BookingState]] = {
    BOOKING_OUTCOME_CONFIRMED: BookingState.CONFIRMED,
    BOOKING_OUTCOME_UNVERIFIED: BookingState.REQUESTED,
}


def _reference(external_ref: str | None) -> str | None:
    """The hospital's own reference, or nothing at all: a blank string is not a reference.

    `control._assert_recordable_booking` reads the field this way (`.strip()`), so this is the same
    judgement applied to the same value rather than a second rule — and it is applied before the
    request is hashed, so a blank reference and an absent one are one request: nothing downstream
    could tell them apart, and a ledger that kept them apart would answer `RE005` about a difference
    that is not a difference.

    Writing `''` through would also be the one thing the column cannot hold twice:
    `booking_request_external_ref_key` is unique over every non-null value, so a row whose
    `external_ref` is set and names nothing would collide with the next such row.
    """
    return external_ref if (external_ref or "").strip() else None


def _outcome_for(outcome: str | None, external_ref: str | None) -> str:
    """The outcome a booking request means, for the request that did not name one.

    The reference decides it, because the reference is the evidence and the outcome is the claim. A
    caller that hands over the hospital's reference is asserting a confirmation and has nothing left
    to say that the reference does not already say; a caller that hands over nothing is claiming
    nothing about the hospital, which is `unverified` — the outcome §7 resolves by reconciliation.
    Reading *that* caller as `confirmed` would be inventing the hospital's word, and it is also the
    reading 008's `booking_request_confirmed_has_ref` refuses.

    A request that names an outcome is left exactly as it named it, because silence about the result
    is the only thing a default may fill. `confirmed` with no reference is therefore not downgraded
    to `unverified` here: the port refuses it (`ContractViolationError`, 422), since a caller that
    claims a confirmation and supplies no evidence is telling the gateway something provably
    incomplete — a different act from saying nothing.
    """
    if outcome is not None:
        return outcome
    return BOOKING_OUTCOME_CONFIRMED if external_ref is not None else BOOKING_OUTCOME_UNVERIFIED


#: Payload keys this module reads or writes. `booking_payload` is the coordinator's, and is what the
#: `BookingReview` card renders and what consent is bound to.
ANSWER_KEY: Final = "answers"
MESSAGE_KEY: Final = "messages"
BOOKING_PAYLOAD_KEY: Final = "booking_payload"


@dataclass(frozen=True, slots=True)
class SubmittedAction:
    """A `ClientAction` from the wire, with the contract's wrappers unwrapped."""

    command_id: str
    instance_id: UUID
    action: str
    workflow_state_version: int
    value: Mapping[str, Any] | None

    @classmethod
    def from_wire(cls, action: ClientAction) -> SubmittedAction:
        return cls(
            command_id=str(unwrap(action.command_id)),
            instance_id=unwrap(action.instance_id),
            action=str(unwrap(action.action)),
            workflow_state_version=int(unwrap(action.workflow_state_version)),
            value=action.value,
        )


#: §5's task result → §15's outcome class.
#:
#: **No service had crossed these two vocabularies before**, so this is the first mapping of them and
#: the reason it is a table rather than a `getattr` is that every row is a judgement. §15's classes
#: are not a rename of §5's results: `blocked` and `fallback` are their own classes precisely because
#: folding a refusal into `error` makes a failure rate disagree with the number of things that
#: failed, and folding it into `success` hides that the platform declined to do what was asked.
_TURN_OUTCOME: Final[Mapping[ResultOutcome, OutcomeClass]] = {
    # The platform answered the question.
    ResultOutcome.completed: OutcomeClass.SUCCESS_FULL,
    # §15: *"Empty geçerli sonuç error değildir"* — a payer network with no in-network specialist is
    # a correct answer to the question that was asked, not a failure to answer it.
    ResultOutcome.no_match: OutcomeClass.SUCCESS_EMPTY,
    # The turn ended by asking the patient something, which is what it set out to do, so it is not
    # an error; and the work it started is still open, so it is not a full success either.
    ResultOutcome.needs_input: OutcomeClass.SUCCESS_PARTIAL,
    ResultOutcome.retryable_error: OutcomeClass.ERROR,
    ResultOutcome.terminal_error: OutcomeClass.ERROR,
    # §5 has no "we do not know" success, and neither does §15 — but this is a turn that ended
    # without an answer the platform can stand behind, and `error` is closer to that than either
    # success class. It is one row, not a category, and the count is small by construction.
    ResultOutcome.outcome_unknown: OutcomeClass.ERROR,
}

#: The reasons that make a turn `blocked` rather than whatever its result says. §9's guardrail and
#: §7's policy both refuse rather than fail, and the refusal is the interesting event (§16's rules
#: alert on it), so it is carried as its own class instead of being flattened into the result.
_BLOCKING_REASONS: Final[frozenset[ReasonCode]] = frozenset(
    {ReasonCode.guardrail_blocked, ReasonCode.policy_blocked}
)


def turn_outcome(outcome: ResultOutcome | None, reason_code: ReasonCode) -> OutcomeClass | None:
    """The §15 class for one turn, or ``None`` when the turn never reached an outcome.

    A turn can end without a `terminal` event — a refusal raises out of the command, and a surface
    update answers without one. **No class is invented for those.** §15 has no "we did not finish"
    class, and a fallback here would put a number on a chart with no lineage behind it; the record
    is still emitted, so the turn is counted and its latency is in the distribution, and only the
    class is absent. The one case that *is* classed is the refusal, because there the turn knows
    exactly what happened and §16's rules are written on it.
    """
    if reason_code in _BLOCKING_REASONS:
        return OutcomeClass.BLOCKED
    if outcome is None:
        return None
    return _TURN_OUTCOME[outcome]


class Turn:
    """One turn's events, and its two latencies kept apart (§12).

    A turn is one command: the work between accepting it and answering it. `progress` emits a
    non-authoritative event and fixes the turn's **first** progress reading; `terminal` ends the turn
    and carries the *final* latency together with that reading, so both numbers travel and neither
    stands in for the other. `close` records the sample in the ledger even for turns that never
    reached a terminal event — a surface update is an outcome too, and dropping those from the
    measurement would report the latency of the easy turns only.
    """

    def __init__(
        self,
        *,
        workflow_id: UUID,
        turn_id: str,
        label: str,
        log: EventLog,
        clock: Clock,
        latencies: LatencyLedger,
        synthetic: bool,
        emitter: TelemetryEmitter | None = None,
    ) -> None:
        self.workflow_id = workflow_id
        self.turn_id = turn_id
        self.label = label
        self.events: list[StreamEvent] = []
        self._log = log
        self._clock = clock
        self._latencies = latencies
        self._synthetic = synthetic
        #: §15's turn measurement. `None` is a supported state and not a misconfiguration: the unit
        #: suite builds an app with no telemetry bundle and must not reach a provider for it, which
        #: is the same reason `create_app` takes `telemetry` as an injected argument.
        self._emitter = emitter
        self._turn = TurnClock(
            turn_id=turn_id, workflow_id=str(workflow_id), clock=clock, synthetic=synthetic
        )
        self._first_progress_ms: int | None = None
        self._closed = False
        #: Set by `terminal` (which knows the result) or by `refused` (which knows the refusal).
        self._outcome: ResultOutcome | None = None
        self._reason_code: ReasonCode = ReasonCode.ok

    # -- emitting -------------------------------------------------------------------------------

    def _emit(
        self,
        event_type: StreamEventType,
        # The union is `EventLog.append`'s, and deliberately no narrower: `workflow_state` and
        # `surface_update` below are built from the contract's own models and are stored as those
        # models, so a plain-mapping signature here would have to re-encode them first.
        payload: BaseModel | Mapping[str, Any] | None,
        *,
        counts_as_progress: bool,
    ) -> StreamEvent:
        reading: int | None = None
        if counts_as_progress:
            reading = self._turn.progress_ms()
            if self._first_progress_ms is None:
                self._first_progress_ms = reading
        event = self._log.append(
            self.workflow_id,
            event_type,
            payload,
            created_at=self._clock.now(),
            progress_elapsed_ms=reading,
        )
        self.events.append(event)
        return event

    def progress(self, *, step: str, detail: Mapping[str, Any] | None = None) -> StreamEvent:
        """`agent_progress`: telemetry about work in progress.

        Emitted near the top of a turn, because its whole purpose is to be the first thing the patient
        sees. A turn that emits it after the work has already happened reports a progress latency that
        measures nothing.
        """
        body: dict[str, Any] = {"step": step, "label": self.label, "synthetic": self._synthetic}
        if detail:
            body["detail"] = dict(detail)
        return self._emit(StreamEventType.agent_progress, body, counts_as_progress=True)

    def partial(self, text: str) -> StreamEvent:
        """`partial_text`: model output that is *arriving*. Never authoritative (§12).

        Engineered the same way as the refusal it precedes: the event exists so progress is visible,
        and the only reader that may turn it into state is `AgentDecision.commit`, which refuses an
        unfinished decision.
        """
        return self._emit(
            StreamEventType.partial_text,
            {"text": text, "complete": False, "synthetic": self._synthetic},
            counts_as_progress=True,
        )

    def notice(
        self, text: str, *, level: str = "info", detail: Mapping[str, Any] | None = None
    ) -> StreamEvent:
        """`notice`: an inline sentence about the surface. Information, not a state change."""
        body: dict[str, Any] = {"text": text, "level": level, "synthetic": self._synthetic}
        if detail:
            body["detail"] = dict(detail)
        return self._emit(StreamEventType.notice, body, counts_as_progress=False)

    def workflow_state(
        self,
        *,
        from_state: str,
        to_state: str,
        state_version: int,
        reason_code: ReasonCode,
        degraded: str | None = None,
    ) -> StreamEvent:
        """`workflow_state`: the authoritative move, built from the contract's own model."""
        change = WorkflowStateChange(
            from_state=WorkflowState(from_state),
            to_state=WorkflowState(to_state),
            state_version=state_version,
            reason_code=reason_code,
            degraded=degraded,
        )
        return self._emit(StreamEventType.workflow_state, change, counts_as_progress=False)

    def surface(
        self,
        components: Sequence[ComponentInstance],
        *,
        remove_instance_ids: Sequence[UUID] = (),
    ) -> StreamEvent:
        """`surface_update`: what the patient is looking at now, replacing by `instance_id`.

        The projection stores instances before they are emitted, so an event can only
        carry an instance the server recorded — a card that exists on the wire but not on our side is
        a card whose action could never be validated.
        """
        update = SurfaceUpdate(
            surface_id=surface_id_for(self.workflow_id),
            components=list(components),
            remove_instance_ids=list(remove_instance_ids) or None,
        )
        return self._emit(StreamEventType.surface_update, update, counts_as_progress=False)

    def terminal(
        self,
        *,
        outcome: ResultOutcome,
        reason_code: ReasonCode = ReasonCode.ok,
        detail: Mapping[str, Any] | None = None,
    ) -> StreamEvent:
        """`terminal`: the turn's outcome, and the only event that carries a final latency.

        §12: `progress_elapsed_ms` is null here and the final timing travels in the payload, together
        with the turn's first progress reading, so a reader can compare the two numbers rather than
        being handed one of them.
        """
        final_ms = self._turn.final_ms()
        body: dict[str, Any] = {
            "outcome": outcome.value,
            "reason_code": reason_code.value,
            "final_elapsed_ms": final_ms,
            "progress_elapsed_ms": self._first_progress_ms,
            "synthetic": self._synthetic,
        }
        if detail:
            body["detail"] = dict(detail)
        # Remembered for the §15 record `close` writes. The stream and the telemetry event are two
        # different readers of one outcome, and this is where the turn learns it.
        self._outcome = outcome
        self._reason_code = reason_code
        event = self._log.append(
            self.workflow_id,
            StreamEventType.terminal,
            body,
            created_at=self._clock.now(),
            progress_elapsed_ms=None,
        )
        self.events.append(event)
        return event

    def refused(self, *, reason_code: ReasonCode = ReasonCode.guardrail_blocked) -> None:
        """Record that this turn was **refused**, for §15's `blocked` class.

        Emits nothing to the stream, deliberately. The patient's answer to a refusal is the HTTP
        status the route raises (`422`, §9), and adding a `terminal` event here would change what the
        surface shows in order to change what a chart counts. This is the second reader of the same
        fact, not a second telling of it.

        Called where the refusal happens, because that is the only place that knows: the command
        raises out of its `try` and `close` runs from the `finally`, so by then the reason is gone.
        """
        self._reason_code = reason_code

    def close(self) -> None:
        """Record the turn's two latencies. Idempotent: refused turns close from a `finally`."""
        if self._closed:
            return
        self._closed = True
        if not self._turn.has_final:
            self._turn.final_ms()
        sample = self._turn.sample()
        self._latencies.record(sample)
        self._measure(sample)

    def _measure(self, sample: LatencySample) -> None:
        """Emit §15's turn: its count, its latency, and its outcome class.

        **This is the record the Feedback page divides by.** §12's participation rate is votes over
        the turns a vote could have come from, and `admin.py` takes that denominator from
        `latency.turn.ms` — so a stack that never emits this has a feedback rate it cannot compute
        and refuses to print. Before this, nothing did: the gateway measured turns for §12's stream
        ledger and never told the telemetry path, which is why the denominator was always zero.

        `attempt(latency_kind=TURN)` rather than a method of its own, because that is what the
        emitter's shape says: §15 names three latency distributions, `LatencyKind` is the parameter
        that selects one, and `_latency_metric` turns the kind into the metric name. There is no
        `EventName` for a turn to hang a bespoke emitter on, which is the same statement made the
        other way round. `number=1` because a conversational turn does not retry — what retries is an
        attempt, and those are recorded where they happen.
        """
        if self._emitter is None:
            return
        self._emitter.attempt(
            duration_ms=float(sample.final_ms),
            number=1,
            outcome=turn_outcome(self._outcome, self._reason_code),
            latency_kind=LatencyKind.TURN,
            # The gateway carries this as a bool and §15's dimension is a two-value vocabulary, so
            # the translation happens here rather than at the emitter. Passing the bool through
            # reaches the store as the string `"True"`, which the emitter refuses — it did, and that
            # refusal is why this line is a mapping and not a pass-through.
            synthetic=Synthetic.SYNTHETIC if self._synthetic else Synthetic.LIVE_DEMO,
        )


class CommandReceipt:
    """The answer to a command: what to send back, and whether the work was done or replayed."""

    __slots__ = ("body", "command_id", "replayed", "status_code")

    def __init__(
        self,
        *,
        command_id: str,
        status_code: int,
        body: Mapping[str, Any],
        replayed: bool = False,
    ) -> None:
        self.command_id = command_id
        self.status_code = status_code
        self.body = dict(body)
        self.replayed = replayed

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"CommandReceipt(command_id={self.command_id!r}, status_code={self.status_code}, "
            f"replayed={self.replayed})"
        )


class CommandService:
    """The four operations, over ports. One instance per app; it holds no state of its own."""

    def __init__(
        self,
        *,
        control: ControlState,
        surfaces: WorkflowSurfaceProjection,
        log: EventLog,
        approvals: ApprovalService,
        policy: ActionPolicy,
        guardrails: GuardrailPipeline,
        clock: Clock,
        latencies: LatencyLedger,
        synthetic: bool = True,
        emitter: TelemetryEmitter | None = None,
        dispatch: CoordinatorDispatch | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self._control = control
        self._surfaces = surfaces
        self._log = log
        self._approvals = approvals
        self._policy = policy
        self._guardrails = guardrails
        self._clock = clock
        self._latencies = latencies
        self._synthetic = synthetic
        self._emitter = emitter
        #: §3's coordinator, reached once per committed command. `None` is the unit suite's app and
        #: any deployment with no agent to reach: a command is recorded and nothing decides it.
        self._dispatch = dispatch
        self._autonomous = False
        self._continuations = ContinuationLifecycle(control, clock=clock)
        self._kill_switch = kill_switch

    def assert_booking_writes(self) -> None:
        if self._kill_switch is not None and self._kill_switch.state.engaged:
            raise BookingHaltedError("Booking writes are stopped by the admin kill switch.")

    def enable_coordination(self) -> None:
        """Store work with each accepted patient command."""
        self._autonomous = True

    def _pending_work(
        self, command_id: str, kind: ContinuationKind = ContinuationKind.ANSWER
    ) -> Mapping[str, Any]:
        return self._continuations.pending_patch(command_id, kind) if self._autonomous else {}

    def prepare_booking(self, row: WorkflowRow) -> Mapping[str, Any]:
        """Check consent and guardrails before the hospital write can start."""
        self.assert_booking_writes()
        payload = _booking_payload(row)
        if payload is None:
            raise ContractViolationError("The workflow has no reviewed booking payload.")
        text = str(_message_log(row)[-1]["text"]) if _message_log(row) else ""
        screening = self._guardrails.screen(
            text, workflow_state=row.state, short_reply_expected=False
        )
        self._guardrails.require_complete(screening)
        if screening.decision is not GuardrailDecision.allow:
            raise InputBlockedError(
                "The booking input needs review.", category=screening.category.value
            )
        binding = self._approvals.authorise(
            workflow_id=row.workflow_id,
            patient_ref=row.patient_ref,
            state_version=row.state_version,
            payload=payload,
        )
        return {
            "approval_id": str(binding.approval_id),
            "payload_hash": binding.payload_hash,
            "state_version": binding.state_version,
            "expires_at": binding.expires_at.isoformat(),
            "granted_by": row.patient_ref,
            "consumed": False,
        }

    def record_confirmation(self, row: WorkflowRow, *, external_ref: str) -> None:
        """Record a verified lookup result against the existing booking."""
        self._authorise(ACTION_LOOKUP_REQUEST, row.state)
        attempt = self._control.confirm_booking(
            row.workflow_id, expected_state_version=row.state_version, external_ref=external_ref
        )
        assert attempt.workflow is not None
        turn = self._turn(row, f"reconciliation:{attempt.booking.booking_id}")
        try:
            moved = self._after_advance(row, attempt.workflow, turn, ReasonCode.ok)
            self._show_surface(
                moved,
                {
                    "appointment": {
                        "booking_id": str(attempt.booking.booking_id),
                        "external_ref": external_ref,
                        "state": "CONFIRMED",
                    }
                },
                turn,
            )
            turn.terminal(outcome=ResultOutcome.completed, reason_code=ReasonCode.ok)
        finally:
            turn.close()

    def refresh_booking_lookup(self, row: WorkflowRow) -> None:
        turn = self._turn(row, f"lookup:{row.workflow_id}:{self._clock.now().isoformat()}")
        try:
            self._publish_surface(self._surfaces.reopen_lookup(row), turn)
        finally:
            turn.close()

    def repair_surface(self, row: WorkflowRow) -> None:
        """Publish a repaired card after a process stopped during projection."""
        change = self._surfaces.repair(row)
        if change is None:
            return
        turn = self._turn(row, f"projection:{row.workflow_id}:{row.state_version}")
        try:
            self._publish_surface(change, turn)
        finally:
            turn.close()

    # -- reads ----------------------------------------------------------------------------------

    def snapshot(self, session: Session, workflow_id: UUID) -> dict[str, Any]:
        """What a client needs to render: state, version, outstanding cards, stream cursor."""
        row = self._load_scoped(session, workflow_id, admin=session.patient_ref is None)
        self.repair_surface(row)
        visible = self._surfaces.visible(row)
        events = self._log.events_for(workflow_id)
        latest = events[-1] if events else None
        terminal = next(
            (event for event in reversed(events) if event.type is StreamEventType.terminal), None
        )
        return {
            "workflow_id": str(row.workflow_id),
            "ticket_ref": row.ticket_ref,
            "state": row.state,
            "state_version": row.state_version,
            "surface_id": str(surface_id_for(workflow_id)),
            "components": [instance.model_dump(mode="json") for instance in visible],
            "terminal": terminal.model_dump(mode="json") if terminal else None,
            "last_event_id": latest.event_id if latest else None,
            "last_seq": latest.seq if latest else None,
            "synthetic": self._synthetic,
        }

    def help_card(self, session: Session, workflow_id: UUID) -> dict[str, Any]:
        """The static help card (§9). Deliberately the least privileged read in the service.

        It consults no policy row, no guardrail layer and no ledger, because it is the one thing that
        has to keep working when those are unavailable — *"statik rehber/yardım kartı
        kullanılabilir"*. It *is* scoped to the patient like every other read: an unscoped fallback
        would be a way to confirm that another patient's workflow exists.
        """
        row = self._load_scoped(session, workflow_id)
        instance = ComponentInstance(
            instance_id=instance_id_for(row.workflow_id, HELP_CARD_COMPONENT, "help"),
            component=HELP_CARD_COMPONENT,
            props={
                "handoff": {
                    "reason_class": "patient_requested",
                    "queue": "demo-coordinator",
                    "contact": "A coordinator will pick this up from the demo queue.",
                },
                "static": True,
            },
            workflow_state_version=row.state_version,
            action=None,
            submitted=False,
            disabled=True,
            notice=(
                "Static help card. It needs no guardrail and no model, so it is served even while "
                "those are unavailable (§9)."
            ),
        )
        return {
            "workflow_id": str(row.workflow_id),
            "component": instance.model_dump(mode="json"),
            "source": "static_help_card",
            "guardrails_configured": self._guardrails.configured,
            "synthetic": self._synthetic,
        }

    # -- patient action -------------------------------------------------------------------------

    def submit_action(
        self, session: Session, workflow_id: UUID, action: SubmittedAction
    ) -> CommandReceipt:
        """Record a patient's answer to a card the server issued (§12, §9).

        The order is the contract's: version, instance, policy, then the work. The instance check is
        what makes "the client never invents an action" enforceable — the submitted action must be
        the one *we* put on that instance, so a client cannot ask for something the surface never
        offered, and `submitted` keeps a replay from soliciting the same choice twice.
        """
        row = self._load_scoped(session, workflow_id)
        decision = self._begin(action.command_id, _action_request_hash(workflow_id, action))
        if decision.is_replay:
            return self._replay(decision)
        if action.workflow_state_version != row.state_version:
            raise IllegalTransitionError(
                f"the action names workflow_state_version {action.workflow_state_version}, but "
                f"workflow {workflow_id} is at {row.state_version}; a stale action is refused rather "
                "than applied (RE002, §9)"
            )

        turn = self._turn(row, f"action:{action.command_id}")
        try:
            instance = self._surfaces.require_action(row, action.instance_id, action.action)

            # Policy is checked against the state the *call* happens in: the table's rows are written
            # per (role, tool, state), so the question is always "is this tool permitted here", never
            # "may this caller use this tool anywhere". `AWAITING_FINAL_APPROVAL` permits
            # `system:capture_consent` while the same table denies it in `BOOKING_IN_FLIGHT`, which
            # is how §9's re-consent rule is expressed as data rather than as a branch.
            self._authorise(action.action, row.state)
            turn.progress(step="action", detail={"action": action.action})

            if action.action == ACTION_CAPTURE_CONSENT:
                body = self._capture_consent(row, action)
            elif action.action == ACTION_AWAIT_CHOICE:
                self._validate_choice(instance, action.value)
                body = self._record_answer(row, action)
            elif action.action == ACTION_LOOKUP_REQUEST:
                body = self._record_lookup(row, action)
            else:  # pragma: no cover - the catalog and the policy table decide what can arrive
                raise ContractViolationError(
                    f"{action.action!r} is a permitted action in {row.state} but this service has no "
                    "implementation for it; a policy row alone is not an implementation"
                )

            change = self._surfaces.answer(
                row,
                action.instance_id,
                handoff=body["handoff"] if body.get("recorded") == "handoff_consent" else None,
            )
            turn.notice(f"Recorded: {action.action}.")
            self._publish_surface(change, turn)
        finally:
            turn.close()
        return self._complete(decision, 200, body, workflow_id=workflow_id)

    # -- patient message ------------------------------------------------------------------------

    def submit_message(
        self,
        session: Session,
        workflow_id: UUID,
        *,
        command_id: str,
        expected_state_version: int,
        text: str,
    ) -> CommandReceipt:
        """Free-text input from the patient, screened before anything else happens (§9).

        The three outcomes are deliberately three different transport answers:

        * **blocked** (injection, abuse, exfiltration, oversized) — `422`. Nothing is recorded; the
          input is not something to answer.
        * **emergency** — `200`, with the approved fixed response and the workflow routed to
          `HUMAN_HANDOFF`. §9 is explicit that this is *not* a silent off-topic block: the difference
          between "we will not process this" and "a human must see this" is the whole point of the
          category.
        * **allowed or ambiguous** — `202`. The message is recorded against the workflow and the
          coordinator decides what it means. A message never commits state on its own.
        """
        row = self._load_scoped(session, workflow_id)
        decision = self._begin(
            command_id,
            canonical_request_hash(
                {
                    "workflow_id": str(workflow_id),
                    "kind": "message",
                    "text": text,
                    "state_version": expected_state_version,
                }
            ),
        )
        if decision.is_replay:
            return self._replay(decision)
        if expected_state_version != row.state_version:
            raise IllegalTransitionError(
                f"the message names workflow_state_version {expected_state_version}, but workflow "
                f"{workflow_id} is at {row.state_version} (RE002, §9)"
            )

        turn = self._turn(row, f"message:{command_id}")
        try:
            turn.progress(step="reading")
            outcome = self._guardrails.screen(
                text, workflow_state=row.state, short_reply_expected=question_is_open(row)
            )
            self._publish_verdicts(turn, outcome)

            if outcome.decision is GuardrailDecision.block:
                if outcome.category is not GuardrailCategory.emergency:
                    # The turn is about to raise, so `close` — which runs from the `finally` — will
                    # never see a `terminal` event. Tell it here, where the refusal is known, or
                    # §16's alert rules would have no `blocked` class to fire on.
                    turn.refused(reason_code=ReasonCode.guardrail_blocked)
                    raise InputBlockedError(
                        _blocked_message(outcome),
                        category=outcome.category.value,
                        detail={"layers": [verdict.layer.value for verdict in outcome.verdicts]},
                    )
                # An emergency is handled, not refused: the approved response goes out, a human is
                # asked to look, and the turn ends with `needs_input` rather than an error.
                routed_to = self._route_to_human(
                    row, turn, reason_code=ReasonCode.guardrail_blocked
                )
                if outcome.response:
                    turn.notice(outcome.response, level="warning")
                turn.terminal(
                    outcome=ResultOutcome.needs_input,
                    reason_code=ReasonCode.guardrail_blocked,
                    detail={"category": outcome.category.value, "routed_to": routed_to},
                )
                return self._complete(
                    decision,
                    200,
                    {
                        "command_id": command_id,
                        "recorded": "message",
                        "routed_to": routed_to,
                        "response": outcome.response,
                        "state": row.state,
                        "synthetic": self._synthetic,
                    },
                    workflow_id=workflow_id,
                )

            updated = self._control.update_payload(
                row.workflow_id,
                expected_state_version=row.state_version,
                patch={
                    **(
                        self._pending_work(command_id, ContinuationKind.MESSAGE)
                        if outcome.decision is GuardrailDecision.allow
                        else {}
                    ),
                    MESSAGE_KEY: [
                        *_message_log(row),
                        {
                            "text": text,
                            "at": self._clock.now().isoformat(),
                            "message_id": command_id,
                        },
                    ],
                },
            )
            body: dict[str, Any] = {
                "command_id": command_id,
                "recorded": "message",
                "state": updated.state,
                "state_version": updated.state_version,
                "guardrail_category": outcome.category.value,
                "synthetic": self._synthetic,
            }
            if outcome.decision is GuardrailDecision.clarify:
                # Accepted and *not* acted on: the honest answer to "tamam" with no question open is
                # to ask one, and asking is cheaper than guessing (§9 layer 1).
                body["committed"] = False
                body["reason"] = ReasonCode.ambiguous_input.value
                turn.notice(outcome.reason)
                return self._complete(decision, 202, body, workflow_id=workflow_id)

            body["committed"] = True
            # The sentence below is the reason this line exists. It has been true of the design since
            # the surface was written and false of the running system until now: the coordinator was
            # told about but never called. `dispatched` is reported rather than assumed, so a
            # deployment with nothing to dispatch to does not tell the patient someone is on it.
            dispatched = self._hand_to_the_coordinator(
                updated, command_id=command_id, text=text, updated_state=updated.state
            )
            body["dispatched"] = dispatched
            turn.notice(
                "Message recorded. The coordinator decides what happens next."
                if dispatched
                else "Message recorded. No coordinator is configured in this profile, so nothing "
                "will act on it yet."
            )
            return self._complete(decision, 202, body, workflow_id=workflow_id)
        finally:
            turn.close()

    # -- transition -----------------------------------------------------------------------------

    def advance(
        self,
        session: Session,
        workflow_id: UUID,
        *,
        command_id: str,
        expected_state_version: int,
        to_state: str,
        reason_code: ReasonCode = ReasonCode.ok,
        complete: bool = True,
        props: Mapping[str, Any] | None = None,
        partial_text: str = "",
    ) -> CommandReceipt:
        """Move the workflow, issue the card the new state shows, and close the turn.

        Two things this route refuses to do:

        * **commit a partial decision.** `complete=False` streams the text as `partial_text` and
          answers `202` with everything untouched. `AgentDecision.commit` is consulted on purpose, so
          the refusal is a property of the value rather than a branch someone can delete here.
        * **invent a card.** The props for the new state come from the caller and are validated
          against the catalog; a `HospitalPicker` with no `options` is refused rather than forwarded,
          because a card the patient cannot answer is a screen that looks like a bug.
        """
        row = self._load_scoped(session, workflow_id, admin=session.patient_ref is None)
        decision = self._begin(
            command_id,
            canonical_request_hash(
                {
                    "workflow_id": str(workflow_id),
                    "kind": "transition",
                    "to_state": to_state,
                    "state_version": expected_state_version,
                    "reason_code": reason_code.value,
                    "complete": complete,
                    "props": dict(props) if props else None,
                }
            ),
        )
        if decision.is_replay:
            return self._replay(decision)

        turn = self._turn(row, f"transition:{command_id}")
        try:
            turn.progress(step="decision" if complete else "deciding")
            if not complete:
                return self._refuse_partial(decision, row, command_id, turn, partial_text)

            committed = AgentDecision(
                complete=True, to_state=to_state, props=dict(props or {}), text=partial_text
            ).commit()
            assert committed.to_state is not None  # narrowed by the value we just built
            # The state machine's refusals first, then the card's, and neither after anything is
            # written. A target the flow can never reach must not be reported as a props problem —
            # fixing the props would not help — while a move that *is* possible must not leave the
            # workflow advanced into a state whose card cannot be rendered, showing the patient
            # nothing with no way to answer. The pre-flight is the same checks the write makes.
            self._control.assert_can_advance(
                row.workflow_id,
                expected_state_version=expected_state_version,
                to_state=committed.to_state,
            )
            # The wire's `to_state` as the catalog's key. The state machine's refusals have already
            # run, so a name the contract does not have was refused above as a move that is not an
            # edge (RE002 in both profiles) — the same reading `Turn.workflow_state` applies to this
            # string when the event is emitted, rather than a bare `str` handed to the catalog.
            self._surfaces.prepare(row, WorkflowState(committed.to_state), committed.props)
            moved = self._advance(
                row, expected_state_version, committed.to_state, turn, reason_code
            )
            instances = self._show_surface(moved, committed.props, turn)
            return self._complete(
                decision,
                200,
                {
                    "command_id": command_id,
                    "committed": True,
                    "state": moved.state,
                    "state_version": moved.state_version,
                    "components": [instance.model_dump(mode="json") for instance in instances],
                    "synthetic": self._synthetic,
                },
                workflow_id=workflow_id,
            )
        finally:
            turn.close()

    # -- booking --------------------------------------------------------------------------------

    def book(
        self,
        session: Session,
        workflow_id: UUID,
        *,
        command_id: str,
        approval_id: UUID,
        expected_state_version: int,
        booking_payload: Mapping[str, Any],
        screening_text: str,
        outcome: str | None = None,
        external_ref: str | None = None,
    ) -> CommandReceipt:
        """The booking write. Four gates, in an order that refuses as early as it can.

        1. **version** — `RE002` before anything else is read.
        2. **policy table** — `scheduling:create_booking` in the *current* state. The seed permits it
           in `BOOKING_IN_FLIGHT` and denies it in every other state and for every other role, so this
           gate is the whole of §9's "only Scheduling may use the booking write action".
        3. **guardrails** — the input the booking came from is screened, and an *unconfigured*
           pipeline is a refusal (`503`), not a pass. This is the rule the phase is measured on.
        4. **approval** — patient + workflow + state_version + payload hash + expiry, all five, and
           the hash must be the workflow's own. The encoder is the domain's canonical one; a second
           encoder here is how "the patient approved something else" happens. The version compared
           against is the one the workflow *entered* this state from, not the current one: the
           database's guard (migration 014's `workflow_booking_consent_guard`) makes the same
           comparison against `OLD.state_version` at the transition, and consent is captured in the
           state before it by construction. Comparing against the current version refuses every
           booking, since reaching `BOOKING_IN_FLIGHT` is itself the version step that separates the
           two.

        Only then is the row written. `UNIQUE(approval_id)` is the last gate and it is the database's:
        an approval authorises exactly one booking even if everything above were wrong.

        `outcome` and `external_ref` are the scheduler's report, and they travel *with* the write
        rather than after it: a confirmation is what makes `BOOKED` reachable at all (014's consent
        guard asks for a `confirmed` booking, and the port records one only when the caller says so),
        and §7's "state transitions are atomic" is what makes the row and the transition arrive
        together. The gateway does not have an opinion about either: it asks, it is told, and it
        records what it was told.

        `outcome` may be left out, and then the reference decides what was meant — see
        `_outcome_for`. Both are resolved *before* the request is read or hashed, so what the ledger
        records is the meaning of the request rather than its wording: a retry that states the
        outcome the first attempt left implicit is the same command, not a `RE005` conflict about two
        bodies that mean one thing.
        """
        # Resolved first: everything below — the hash, the outcome check, the row, the transition —
        # is about one definite request, and `None` is a spelling of "not said" rather than a value.
        external_ref = _reference(external_ref)
        outcome = _outcome_for(outcome, external_ref)
        row = self._load_scoped(session, workflow_id, admin=session.patient_ref is None)
        decision = self._begin(
            command_id,
            canonical_request_hash(
                {
                    "workflow_id": str(workflow_id),
                    "kind": "booking",
                    "approval_id": str(approval_id),
                    "state_version": expected_state_version,
                    "booking_payload": dict(booking_payload),
                    "outcome": outcome,
                    # Part of the request, so a retry that reports a different reference is a
                    # different command rather than a replay of the first (RE005 keeps them apart).
                    "external_ref": external_ref,
                }
            ),
        )
        if decision.is_replay:
            return self._replay(decision)
        if expected_state_version != row.state_version:
            raise IllegalTransitionError(
                f"the booking names workflow_state_version {expected_state_version}, but workflow "
                f"{workflow_id} is at {row.state_version} (RE002, §7)"
            )
        if outcome not in BOOKING_STATES:
            raise ContractViolationError(
                f"outcome must be {BOOKING_OUTCOME_CONFIRMED!r} or {BOOKING_OUTCOME_UNVERIFIED!r}, "
                f"not {outcome!r}; §7 resolves an unknown result by reconciliation, never by guessing"
            )

        self._authorise(ACTION_CREATE_BOOKING, row.state)

        turn = self._turn(row, f"booking:{command_id}")
        try:
            turn.progress(step="booking")
            screening = self._guardrails.screen(
                screening_text, workflow_state=row.state, short_reply_expected=False
            )
            self._publish_verdicts(turn, screening)
            # §9: the guardrail dependency being off closes the write path. `503`, and the static
            # help card is still served — see `help_card`, which never reaches this method.
            self._guardrails.require_complete(screening)
            if screening.decision is not GuardrailDecision.allow:
                routed_to = (
                    self._route_to_human(row, turn, reason_code=ReasonCode.guardrail_blocked)
                    if screening.category is GuardrailCategory.emergency
                    else None
                )
                if screening.category is GuardrailCategory.emergency:
                    if screening.response:
                        turn.notice(screening.response, level="warning")
                    turn.terminal(
                        outcome=ResultOutcome.needs_input,
                        reason_code=ReasonCode.guardrail_blocked,
                        detail={"category": screening.category.value, "routed_to": routed_to},
                    )
                # A write is not attempted on an input the guardrail would not let through — not even
                # when the verdict is `clarify`: asking the patient is a different command, and
                # booking while asking would be answering before hearing.
                raise InputBlockedError(
                    _blocked_message(screening, context="the booking was not attempted"),
                    category=screening.category.value,
                    detail={
                        "routed_to": routed_to,
                        "response": screening.response,
                        "layers": [verdict.layer.value for verdict in screening.verdicts],
                    },
                )

            stored_hash = row.booking_payload_hash
            presented_hash = payload_hash_of(booking_payload)
            if stored_hash is None:
                raise ContractViolationError(
                    f"workflow.payload has no {PAYLOAD_HASH_KEY!r}: the workflow never recorded the "
                    "payload the patient consented to, so there is nothing for this booking to match "
                    "(RE004, §9)"
                )
            if stored_hash != presented_hash:
                raise ContractViolationError(
                    "the booking payload is not the one this workflow holds: "
                    f"workflow.payload[{PAYLOAD_HASH_KEY!r}] is {stored_hash[:12]}…, the presented "
                    f"payload hashes to {presented_hash[:12]}… (RE004, §9)"
                )

            # The version the consent was given at, which is the version the workflow is one step
            # past. In migration 014 the check lives in `workflow_booking_consent_guard`, and it
            # compares against `OLD.state_version` as the workflow crosses into `BOOKING_IN_FLIGHT`;
            # `BOOKING_IN_FLIGHT` has no self-loop, so the row cannot have aged here and the version
            # it entered from is exactly `state_version - 1`. Passing the current version instead
            # would refuse consent that the database accepts, every single time.
            consent_version = row.state_version - 1
            binding = self._approvals.authorise(
                workflow_id=row.workflow_id,
                patient_ref=row.patient_ref,
                state_version=consent_version,
                payload=booking_payload,
            )
            if binding.approval_id != approval_id:
                raise ContractViolationError(
                    f"approval {approval_id} is not the approval that authorises this booking; the "
                    f"live binding for this payload is {binding.approval_id} (§9)"
                )

            confirmed = outcome == BOOKING_OUTCOME_CONFIRMED
            to_state = (
                WorkflowState.BOOKED.value if confirmed else WorkflowState.BOOKING_UNVERIFIED.value
            )
            reason = ReasonCode.ok if confirmed else ReasonCode.booking_outcome_unknown
            # One operation, not two: the booking row carries the scheduler's outcome, so the row
            # has to exist — confirmed, with the hospital's reference — before the workflow can move
            # to `BOOKED`, and a transition that is refused must not leave the row behind (§7, and
            # 014's guard, which is the authority on both halves).
            attempt = self._control.record_booking(
                workflow_id=row.workflow_id,
                approval_id=binding.approval_id,
                patient_ref=row.patient_ref,
                payload_hash=presented_hash,
                payload=booking_payload,
                requested_by=session.subject,
                outcome=BOOKING_STATES[outcome],
                external_ref=external_ref,
                advance_to=to_state,
                expected_state_version=row.state_version,
            )
            self._approvals.consume(binding.approval_id)

            booking = attempt.booking
            if attempt.workflow is None:  # pragma: no cover - the port was asked to move it
                raise ContractViolationError(
                    f"the control state recorded booking {booking.booking_id} without moving "
                    f"workflow {row.workflow_id} to {to_state}; the write and the move are one "
                    "operation, and a port that performs half of it has not performed it (§7)"
                )
            moved = self._after_advance(row, attempt.workflow, turn, reason)
            self._show_surface(
                moved,
                (
                    {
                        "appointment": {
                            "booking_id": str(booking.booking_id),
                            "external_ref": booking.external_ref,
                            "state": booking.state,
                        }
                    }
                    if confirmed
                    else {
                        "notice": "The hospital has not confirmed this booking yet.",
                        "can_retry": True,
                        "retry_label": "Check booking status",
                    }
                ),
                turn,
            )
            turn.terminal(
                outcome=ResultOutcome.completed if confirmed else ResultOutcome.outcome_unknown,
                reason_code=reason,
                detail={"booking_id": str(booking.booking_id), "state": booking.state},
            )
            return self._complete(
                decision,
                200,
                {
                    "command_id": command_id,
                    "booking": booking.as_dict(),
                    "approval_id": str(binding.approval_id),
                    "state": moved.state,
                    "state_version": moved.state_version,
                    "synthetic": self._synthetic,
                },
                workflow_id=workflow_id,
            )
        finally:
            turn.close()

    def bookings(self, session: Session, workflow_id: UUID) -> dict[str, Any]:
        """Every booking attempt for a workflow. The evidence for "exactly one booking"."""
        row = self._load_scoped(session, workflow_id, admin=session.patient_ref is None)
        return {
            "workflow_id": str(row.workflow_id),
            "bookings": [
                booking.as_dict() for booking in self._control.bookings_for(row.workflow_id)
            ],
            "synthetic": self._synthetic,
        }

    def admin_view(self, session: Session, workflow_id: UUID, *, tail: int = 20) -> dict[str, Any]:
        """The coordinator's view: the row, its payload, its bookings, and the end of the stream.

        `tail` rather than everything, because a debug view that grows without bound is a view nobody
        opens twice. The payload is included because a coordinator's job is to see what the patient
        was actually shown — which is also why this route is admin-only: a patient's payload is not
        theirs to browse (they see it through the cards they were issued).
        """
        row = self._load_scoped(session, workflow_id, admin=session.patient_ref is None)
        events = self._log.events_for(row.workflow_id)
        return {
            "workflow": {
                "workflow_id": str(row.workflow_id),
                "ticket_ref": row.ticket_ref,
                "patient_ref": row.patient_ref,
                "state": row.state,
                "state_version": row.state_version,
                "generation": row.generation,
                "payload": dict(row.payload),
            },
            "bookings": [
                booking.as_dict() for booking in self._control.bookings_for(row.workflow_id)
            ],
            "outstanding_components": [
                instance.model_dump(mode="json")
                for instance in self._surfaces.visible(row, actionable_only=True)
            ],
            "events": [event.model_dump(mode="json") for event in events[-tail:]],
            "event_count": len(events),
            "synthetic": self._synthetic,
        }

    # -- internals: state -----------------------------------------------------------------------

    def _load_scoped(
        self, session: Session, workflow_id: UUID, *, admin: bool = False
    ) -> WorkflowRow:
        """The workflow, if this session may see it.

        A patient asking for someone else's workflow gets `404`, not `403`: answering "forbidden"
        would confirm that the id exists, which is a fact about another patient (§9). An admin
        session is the coordinator's surface and is not patient-scoped; the *route* decides which
        session kind reaches here, so this flag is a scope decision, not a role check.
        """
        row = self._control.load(workflow_id)
        if row is None:
            raise NotFoundError(f"no workflow {workflow_id}")
        if not admin and row.patient_ref != session.patient_ref:
            raise NotFoundError(f"no workflow {workflow_id}")
        return row

    def _authorise(self, action: str, workflow_state: str) -> None:
        """The policy table, consulted once per action ref. A denial is `RE006`."""
        self._policy.assert_allowed(action, workflow_state)

    def _advance(
        self,
        row: WorkflowRow,
        expected_state_version: int,
        to_state: str,
        turn: Turn,
        reason_code: ReasonCode,
    ) -> WorkflowRow:
        moved = self._control.advance(
            row.workflow_id, expected_state_version=expected_state_version, to_state=to_state
        )
        return self._after_advance(row, moved, turn, reason_code)

    def _after_advance(
        self, row: WorkflowRow, moved: WorkflowRow, turn: Turn, reason_code: ReasonCode
    ) -> WorkflowRow:
        """The stream's half of a move, for the callers that performed the move themselves.

        `book` writes the row and the transition in one operation, because the outcome travels with
        the booking and the guard reads it at the transition; what it cannot borrow is the event,
        which is the transport's. Splitting it here keeps one place that decides what a move looks
        like on the stream, whichever call made it, and keeps `_advance` a one-liner over the port.
        """
        turn.workflow_state(
            from_state=row.state,
            to_state=moved.state,
            state_version=moved.state_version,
            reason_code=reason_code,
        )
        if reason_code is ReasonCode.guardrail_blocked:
            turn.notice("A coordinator has been asked to look at this workflow.", level="warning")
        return moved

    def _show_surface(
        self, row: WorkflowRow, props: Mapping[str, Any], turn: Turn
    ) -> list[ComponentInstance]:
        """Store the projection and publish removals before additions."""
        change = self._surfaces.show(row, props)
        self._publish_surface(change, turn)
        return list(change.components)

    @staticmethod
    def _publish_surface(change: SurfaceChange, turn: Turn) -> None:
        if change.remove_instance_ids:
            turn.surface([], remove_instance_ids=change.remove_instance_ids)
        turn.surface(change.components)

    def _refuse_partial(
        self,
        decision: CommandDecision,
        row: WorkflowRow,
        command_id: str,
        turn: Turn,
        partial_text: str,
    ) -> CommandReceipt:
        """§12's rule, enforced by the value rather than by a branch.

        `AgentDecision.commit` is called on an incomplete decision *on purpose*: if it ever stopped
        refusing, this method would answer `202` for something it had committed, and that is a bug
        this line turns into an exception instead.
        """
        partial = AgentDecision(complete=False, text=partial_text)
        try:
            partial.commit()
        except PartialOutputError as refusal:
            turn.notice(str(refusal), level="warning")
        else:  # pragma: no cover - the guard, asserted by tests/gateway/test_partial_output.py
            raise AssertionError(
                "AgentDecision.commit accepted an incomplete decision, so §12's rule is no longer "
                "enforced by the value"
            )
        for chunk in _chunks(partial_text):
            turn.partial(chunk)
        return self._complete(
            decision,
            202,
            {
                "command_id": command_id,
                "committed": False,
                "reason": "partial_output",
                "state": row.state,
                "state_version": row.state_version,
                "synthetic": self._synthetic,
            },
            workflow_id=row.workflow_id,
        )

    # -- internals: the three patient actions ---------------------------------------------------

    def _record_answer(self, row: WorkflowRow, action: SubmittedAction) -> dict[str, Any]:
        """Record what the patient chose. The gateway decides nothing about what it means.

        The answer is appended with the instance and the version that produced it, so the coordinator
        can tell an answer to *this* question from an answer to the previous one, and a reader can see
        the whole conversation rather than only its last turn.
        """
        if not action.value:
            raise ContractViolationError(
                "await_patient_choice with no value: there is no answer to record "
                "(missing_mandatory_field, §6)"
            )
        updated = self._control.update_payload(
            row.workflow_id,
            expected_state_version=row.state_version,
            patch={
                **self._pending_work(action.command_id),
                ANSWER_KEY: [
                    *_answer_log(row),
                    {
                        "instance_id": str(action.instance_id),
                        "value": dict(action.value),
                        "state": row.state,
                        "state_version": row.state_version,
                        "answered_at": self._clock.now().isoformat(),
                    },
                ],
            },
        )
        return {
            "command_id": action.command_id,
            "committed": True,
            "recorded": "answer",
            "state": updated.state,
            "state_version": updated.state_version,
            "synthetic": self._synthetic,
        }

    @staticmethod
    def _validate_choice(instance: ComponentInstance, value: Mapping[str, Any] | None) -> None:
        if not value:
            raise ContractViolationError(
                "await_patient_choice with no value: there is no answer to record"
            )
        fields = {
            RenderableComponent.HospitalPicker: ("hospital_id", "hospitals"),
            RenderableComponent.DoctorPicker: ("provider_id", "doctors"),
            RenderableComponent.SlotPicker: ("slot_id", "slots"),
        }
        field, collection = fields[instance.component]
        selected = [
            value[key]
            for key in (field, "selected_id", "option_id", "id")
            if value and key in value
        ]
        options = instance.props.get("options", instance.props.get(collection, []))
        if not selected or any(item != selected[0] for item in selected):
            raise ContractViolationError("a choice must name one server-offered option")
        option = next(
            (
                item
                for item in options
                if isinstance(item, Mapping) and item.get("id") == selected[0]
            ),
            None,
        )
        if (
            option is None
            or option.get("disabled") is True
            or option.get("available") is False
            or option.get("accepting_referrals") is False
        ):
            raise ContractViolationError("the selected option was not offered or is unavailable")

    def _capture_consent(self, row: WorkflowRow, action: SubmittedAction) -> dict[str, Any]:
        """Bind the patient's consent to the payload **the server holds**, never the client's copy.

        `ClientAction.value` is documented as never trusted because it came from the client, and
        consent is the sharpest case of that: the patient agrees to what the `BookingReview` card
        showed them, that payload is on the workflow, and the binding records its hash. This method
        accepts only an explicit boolean agreement. The booking endpoint later refuses any payload
        that hashes differently.
        """
        if (
            not action.value
            or action.value.get("consented", action.value.get("approved")) is not True
        ):
            raise ContractViolationError(
                "capture_consent: agreement has to be an explicit true boolean (RE004, §9)"
            )
        if row.state == WorkflowState.HUMAN_HANDOFF.value:
            staged = self._control.queue_handoff(
                row.workflow_id,
                expected_state_version=row.state_version,
                consent_at=self._clock.now().isoformat(),
            )
            return {
                "command_id": action.command_id,
                "committed": True,
                "recorded": "handoff_consent",
                "handoff": dict(staged),
                "synthetic": True,
            }
        booking_payload = _booking_payload(row)
        if booking_payload is None:
            raise ContractViolationError(
                f"workflow.payload has no {BOOKING_PAYLOAD_KEY!r}, so there is nothing to consent to: "
                "the review card is issued from the payload the coordinator supplies (§9)"
            )
        binding = self._approvals.bind(
            workflow_id=row.workflow_id,
            patient_ref=row.patient_ref,
            state_version=row.state_version,
            payload=booking_payload,
        )
        if self._autonomous:
            self._control.update_payload(
                row.workflow_id,
                expected_state_version=row.state_version,
                patch=self._pending_work(action.command_id, ContinuationKind.BOOKING_CONSENT),
            )
        return {
            "command_id": action.command_id,
            "committed": True,
            "recorded": "consent",
            "approval_id": str(binding.approval_id),
            "payload_hash": binding.payload_hash,
            "expires_at": binding.expires_at.isoformat(),
            "state_version": row.state_version,
            "synthetic": self._synthetic,
        }

    def _record_lookup(self, row: WorkflowRow, action: SubmittedAction) -> dict[str, Any]:
        """`scheduling:lookup_request`: ask the scheduler to reconcile an unknown outcome (§7).

        Not a write — `lookup_request` is not in `tool_registry.writes_control_state` — so it records
        the request and leaves the workflow exactly where it was. §7 resolves `BOOKING_UNVERIFIED` by
        reconciliation and never by re-booking, which is why this action exists as its own thing.
        """
        if self._autonomous:
            self._control.update_payload(
                row.workflow_id,
                expected_state_version=row.state_version,
                patch=self._pending_work(action.command_id, ContinuationKind.LOOKUP),
            )
        return {
            "command_id": action.command_id,
            "committed": True,
            "recorded": "lookup_request",
            "state": row.state,
            "state_version": row.state_version,
            "synthetic": self._synthetic,
        }

    # -- internals: guardrails ------------------------------------------------------------------

    def _publish_verdicts(self, turn: Turn, outcome: GuardrailOutcome) -> None:
        """Put the verdicts where the admin view can see them.

        §9 wants per-layer false positives and latency *measured*, so a verdict is published as a
        `notice` event carrying its detail rather than kept in a log line: `/v1/admin/metrics` reads
        the same information the stream does, and a reader can see which layer decided.
        """
        turn.notice(
            f"guardrails: {outcome.decision.value} ({outcome.category.value})",
            detail={"guardrails": outcome.as_dict()},
        )

    def _route_to_human(
        self, row: WorkflowRow, turn: Turn, *, reason_code: ReasonCode
    ) -> str | None:
        """Try to show human help. Keep the response if the store refuses the move."""
        props = {"handoff": {"reason_class": "patient_requested"}}
        try:
            self._control.assert_can_advance(
                row.workflow_id,
                expected_state_version=row.state_version,
                to_state=WorkflowState.HUMAN_HANDOFF.value,
            )
            self._surfaces.prepare(row, WorkflowState.HUMAN_HANDOFF, props)
            moved = self._advance(
                row, row.state_version, WorkflowState.HUMAN_HANDOFF.value, turn, reason_code
            )
        except DomainError as error:
            turn.notice(
                f"Could not move the workflow to a human queue ({type(error).__name__}); the "
                "message is still recorded.",
                level="warning",
            )
            return None
        self._show_surface(moved, props, turn)
        return moved.state

    # -- internals: command ledger --------------------------------------------------------------

    def _begin(self, command_id: str, request_hash: str) -> CommandDecision:
        return self._control.begin_command(command_id, request_hash)

    def _complete(
        self,
        decision: CommandDecision,
        status_code: int,
        body: Mapping[str, Any],
        *,
        workflow_id: UUID,
    ) -> CommandReceipt:
        """Store the outcome so a retry replays it instead of doing the work again (§7).

        The stored body is what a replay returns: same status, same bytes. That is the mechanism
        behind "replaying a stream does not create a second booking" — the client's retry of the
        booking command lands here and receives the first booking's response.
        """
        record = self._control.complete_command(
            decision.command_id,
            decision.record.request_hash,
            response_status=status_code,
            response=body,
        )
        if record.response_status != status_code:  # pragma: no cover - defensive
            raise CommandConflictError(
                f"command {decision.command_id} stored {record.response_status}, not {status_code}"
            )
        return CommandReceipt(command_id=decision.command_id, status_code=status_code, body=body)

    def _replay(self, decision: CommandDecision) -> CommandReceipt:
        stored = decision.stored_response()
        if stored is None:  # pragma: no cover - `is_replay` guarantees a stored response
            raise CommandConflictError(f"command {decision.command_id} has no stored response")
        status_code, body = stored
        return CommandReceipt(
            command_id=decision.command_id,
            status_code=status_code,
            body=dict(body or {}),
            replayed=True,
        )

    # -- internals: turns ----------------------------------------------------------------------

    def _hand_to_the_coordinator(
        self, row: WorkflowRow, *, command_id: str, text: str, updated_state: str
    ) -> bool:
        """Give a committed message to §3's coordinator, and return whether it was handed over.

        The task text is the patient's own words, quoted, with the state they were said in — the
        coordinator gets evidence plus the state, and decides. What it *should* be told is a design
        question (§5's task contract) and this is the honest minimum: the message as it was recorded,
        not a summary invented here that the patient never wrote.

        Called only on the path where the message was committed. A clarify, a refusal and a replay all
        return before this: there is nothing to act on, and dispatch would be an action nobody asked
        for.
        """
        if self._dispatch is None:
            return False
        if self._autonomous:
            return True
        return self._dispatch.submit(
            workflow_id=row.workflow_id,
            workflow_state=updated_state,
            state_version=row.state_version,
            command_id=command_id,
            task=(
                f"The patient sent this message while the workflow was in {updated_state}: "
                f"{text!r}. Decide what happens next and act within your tool allowlist."
            ),
            patient_message=text,
            workflow_summary=_workflow_summary(row),
            synthetic=self._synthetic,
            specialist_inputs=(
                row.payload.get("specialist_inputs")
                if isinstance(row.payload.get("specialist_inputs"), Mapping)
                else None
            ),
        )

    def _turn(self, row: WorkflowRow, turn_id: str) -> Turn:
        return Turn(
            workflow_id=row.workflow_id,
            turn_id=turn_id,
            label=f"{row.state}@{row.state_version}",
            log=self._log,
            clock=self._clock,
            latencies=self._latencies,
            synthetic=self._synthetic,
            emitter=self._emitter,
        )


# ---------------------------------------------------------------------------
# Helpers that are about the contract rather than about one command
# ---------------------------------------------------------------------------


def question_is_open(row: WorkflowRow) -> bool:
    """Whether the card the patient is looking at is a question.

    State-awareness for layer 1 (§9): `"evet"` is a valid answer to a picker and noise in
    `BOOKING_IN_FLIGHT`. The gateway knows which of those it is, so it says, rather than letting a
    regex layer guess from the text alone.
    """
    return row.state in {
        WorkflowState.AWAITING_HOSPITAL_CHOICE.value,
        WorkflowState.AWAITING_DOCTOR_CHOICE.value,
        WorkflowState.AWAITING_SLOT_CHOICE.value,
        WorkflowState.AWAITING_FINAL_APPROVAL.value,
    }


def _action_request_hash(workflow_id: UUID, action: SubmittedAction) -> str:
    """The request hash for a patient action: the body, minus the id that names the body.

    `command_id` is excluded on purpose. It is the ledger's key, and hashing it in would make two
    attempts with different ids trivially "different requests" — true, and useless: the conflict
    `RE005` exists to catch is one id reused for a *different* intent.
    """
    return canonical_request_hash(
        {
            "workflow_id": str(workflow_id),
            "kind": "action",
            "instance_id": str(action.instance_id),
            "action": action.action,
            "state_version": action.workflow_state_version,
            "value": action.value,
        }
    )


def _booking_payload(row: WorkflowRow) -> Mapping[str, Any] | None:
    value = row.payload.get(BOOKING_PAYLOAD_KEY)
    return value if isinstance(value, Mapping) else None


def _answer_log(row: WorkflowRow) -> list[Any]:
    value = row.payload.get(ANSWER_KEY)
    return list(value) if isinstance(value, list) else []


def _message_log(row: WorkflowRow) -> list[Any]:
    value = row.payload.get(MESSAGE_KEY)
    return list(value) if isinstance(value, list) else []


def _workflow_summary(row: WorkflowRow) -> str:
    """What §3 gives the coordinator as its "workflow summary", from facts the gateway wrote.

    The ticket, the state and version the command committed, and how much of the conversation is
    already recorded. Every clause is derived from the row — nothing describes a fact the store does
    not hold, which is the line between a summary and a fabrication. Named *steps* are deliberately
    absent: the gateway records answers and messages, not a step history, so `completed_steps` stays
    at its schema default rather than being filled with names invented to make the sentence read
    better.
    """
    messages = _message_log(row)
    answers = _answer_log(row)
    return (
        f"Referral ticket {row.ticket_ref} is in state {row.state} at version {row.state_version}. "
        f"The patient has sent {len(messages)} message(s) in this workflow and recorded "
        f"{len(answers)} answer(s)."
    )


def _blocked_message(
    outcome: GuardrailOutcome, *, context: str = "the message was not recorded"
) -> str:
    named = ", ".join(verdict.layer.value for verdict in outcome.verdicts) or "no layer"
    return (
        f"guardrails blocked this input as {outcome.category.value} ({named}): {outcome.reason}; "
        f"{context} (§9)"
    )


def _chunks(text: str, size: int = 24) -> list[str]:
    """Split streamed text into the chunks a client would actually see arriving."""
    return [text[index : index + size] for index in range(0, len(text), size)]
