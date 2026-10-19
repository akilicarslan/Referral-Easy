"""The pipeline: claim, resume, act, checkpoint, advance — once per step, exactly (§7).

Everything in this package so far is a part; this is where they are assembled into the one thing an
operator would call a worker. The assembly is where the interesting requirements live, because each
of them is a statement about *what happens with more than one of something*:

* **Two replicas, one command.** A step that writes to the outside world runs under a `command_id`
  derived from (workflow, key, step). Replica B, arriving at a step whose command is already recorded,
  does not re-send: it replays the ledger's outcome and then goes to the lookup, which is the only
  thing that can tell it what actually happened. The booking is sent once, by whoever won.
* **A crash mid-pipeline, resumed.** The steps are named, ordered and written down in a checkpoint in
  the workflow payload. The next replica re-reads it, verifies it, and starts at the step after the
  last one proven complete — not at the beginning, and not wherever it hopes the previous worker got
  to.
* **A late write, refused.** Every checkpoint and every advance carries the tokens the worker read
  (owner, generation, state version). A worker whose lease expired writes nothing: the statement
  matches no row, and the refusal is re-read into a typed error rather than retried.
* **The end of normal flow, one-way.** Recovery is entered only through
  :class:`~referraleasy_workers.recovery.RecoveryPlanner` and only after the third transient failure
  of the same call; from there the state machine has no edge back to the booking threshold, so nothing
  downstream can quietly book.
* **A register.** The pipeline returns a report of what it did, rather than logging it: a caller that
  wants to know whether a booking was created must be able to get the answer as data, not as a string
  in a log file.

There is no model call anywhere in this module, and no place to put one: the sentences a patient may
receive come from the tables in :mod:`referraleasy_workers.recovery` and
:mod:`referraleasy_workers.booking`, and the summary a patient consents to for a handoff is built by
:func:`summarise_for_patient` from the workflow's own facts. A model in this path would be able to
change what a patient is told about their own appointment, which is not a decision to delegate to a
generator — and, on the failure path, it would add a second thing that can fail.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import NAMESPACE_URL, uuid5

from referraleasy_agent_runtime import BudgetCaps, Clock
from referraleasy_contracts.generated.booking import BookingAttemptResult
from referraleasy_contracts.generated.common import HandoffReasonClass
from referraleasy_domain import Workflow
from referraleasy_workers.booking import BookingApi, BookingAttemptView
from referraleasy_workers.booking_record import BookingRecordOutcome, ensure_confirmed_booking
from referraleasy_workers.checkpoint import (
    HANDOFF_KEY,
    Checkpoint,
    ResumePlan,
    make_checkpoint,
    plan_resume,
    write_payload,
)
from referraleasy_workers.commands import CommandOutcome, run_command
from referraleasy_workers.db import ConnectionLike
from referraleasy_workers.events import BOOKING_UNVERIFIED_STATE
from referraleasy_workers.hitl import HandoffRecord, HandoffRequest, HumanQueue, default_ticket_ref
from referraleasy_workers.ownership import OwnedWorkflow
from referraleasy_workers.reconcile import BookingReconciler, ReconciliationOutcome
from referraleasy_workers.recovery import (
    BOOKED_STATE,
    FailureClass,
    RecoveryAction,
    RecoveryPlan,
    RecoveryPlanner,
    WorkflowTransitions,
    classify_failure,
)
from referraleasy_workers.settings import WorkerSettings

__all__ = [
    "BOOKING_STEPS",
    "SEND_BOOKING_STEP",
    "BookingPipeline",
    "HandoffOutcome",
    "PipelineRun",
    "PipelineStepRecord",
    "booking_steps_for",
    "summarise_for_patient",
]

#: The step that writes to the outside world. Named once, because two of the rules below are about
#: *that* step rather than about the sequence: the command id it runs under, and the state from which
#: it must not be re-planned (§7).
SEND_BOOKING_STEP: Final = "send_booking"

#: The booking leg as named, ordered steps. The names are the contract a checkpoint is verified
#: against: a checkpoint that names a step this tuple does not have is treated as unproven, and the
#: pipeline restarts rather than resuming into a step that no longer exists.
BOOKING_STEPS: tuple[str, ...] = (SEND_BOOKING_STEP, "confirm_outcome")


def booking_steps_for(state: str, planned: Sequence[str]) -> tuple[str, ...]:
    """Drop the booking write from a plan when the workflow's position says one may already exist.

    §7's rule is that an unknown outcome is resolved by a lookup under the same key and by nothing
    else. Once a workflow is in `BOOKING_UNVERIFIED`, *no* plan may contain `send_booking`, whoever
    planned it: the state is the statement that a request may be out there, and a replica that
    re-planned the write would be re-sending a booking from behind a state whose entire purpose is to
    forbid exactly that. The policy table agrees — `scheduling:create_booking` is permitted from
    `BOOKING_IN_FLIGHT` and not from this state — so the alternative to dropping the step here is a
    `RE006` refusal that leaves the lookup un-run and the workflow stuck.

    A checkpoint is an optimisation, not the authority (see :mod:`referraleasy_workers.checkpoint`):
    this filter holds even when the payload proves nothing, which is the case that matters, because
    every "we do not know" route into `BOOKING_UNVERIFIED` — an exhausted command budget, a transport
    timeout, an abandonment with a write in flight — advances the workflow without writing one.
    """
    if state != BOOKING_UNVERIFIED_STATE or SEND_BOOKING_STEP not in planned:
        return tuple(planned)
    return tuple(step for step in planned if step != SEND_BOOKING_STEP)


#: The results that mean the facility has an appointment for this patient. Named once, because the
#: step, the record's guard and the run's report all have to agree on which answers those are.
_BOOKED_RESULTS: Final = (BookingAttemptResult.created, BookingAttemptResult.duplicate_replayed)


@dataclass(frozen=True, slots=True)
class PipelineStepRecord:
    """One step's outcome, kept as a value so a test can assert on the sequence rather than the logs."""

    step: str
    executed: bool
    skipped: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class HandoffOutcome:
    """What the pipeline did about a handoff: staged, queued, or waiting for the patient's consent."""

    request: HandoffRequest
    #: Written only when consent was present. `None` here means the summary is staged and waiting —
    #: which is a state of the *workflow*, not an error, so it is a value rather than an exception.
    record: HandoffRecord | None = None
    staged: bool = False

    @property
    def queued(self) -> bool:
        return self.record is not None

    @property
    def awaiting_consent(self) -> bool:
        return self.record is None

    @property
    def patient_message(self) -> str:
        if self.queued:
            return (
                "We could not finish this automatically. We have shared the summary you approved with "
                "a member of our team, and they will pick it up."
            )
        return (
            "We could not finish this automatically. Here is exactly what we would share with a "
            "member of our team — please review it and tell us whether we may."
        )


@dataclass(frozen=True, slots=True)
class PipelineRun:
    """Everything one pipeline run is allowed to tell the caller."""

    workflow_id: Any
    steps: tuple[PipelineStepRecord, ...] = ()
    resumed: bool = False
    resume_reason: str = ""
    checkpoint: Checkpoint | None = None
    state: str = ""
    attempt: BookingAttemptView | None = None
    command: CommandOutcome[BookingAttemptView] | None = None
    reconciliation: ReconciliationOutcome | None = None
    #: The durable record the `BOOKED` state rests on (007's guard). `None` on every path that did not
    #: reach the facility's confirmation — which is also every path that did not reach `BOOKED`.
    booking_record: BookingRecordOutcome | None = None
    recovery: RecoveryPlan | None = None
    handoff: HandoffOutcome | None = None
    booking_key: str = ""
    detail: str = ""

    @property
    def booked(self) -> bool:
        """Whether the workflow reached `BOOKED`, *with* the evidence that says why.

        Two routes reach it and a caller must not have to know which one ran: the facility's definite
        answer on the send (`attempt`), and a reconciliation that found the booking under the same key.
        Requiring the reconciliation alone made this `False` on the direct-create path — the ordinary
        one — while the workflow row said `BOOKED`, which is a report contradicting the database in the
        one place a caller looks to avoid reading it.
        """
        if self.state != BOOKED_STATE:
            return False
        if self.reconciliation is not None:
            return self.reconciliation.booked
        return self.attempt is not None and self.attempt.result in _BOOKED_RESULTS

    @property
    def sent_by_this_replica(self) -> bool:
        return self.command is not None and self.command.executed

    @property
    def deduplicated_onto_another_replica(self) -> bool:
        """True when this replica found the command already done and replayed it instead of re-sending."""
        return self.command is not None and self.command.deduplicated

    @property
    def booking_may_exist(self) -> bool:
        return self.state == BOOKING_UNVERIFIED_STATE

    @property
    def needs_human(self) -> bool:
        return self.handoff is not None or (
            self.recovery is not None and self.recovery.action is RecoveryAction.HANDOFF
        )

    @property
    def patient_message(self) -> str:
        if self.recovery is not None and self.handoff is not None:
            return self.handoff.patient_message
        if self.attempt is not None and self.attempt.is_definite:
            return (
                "Your appointment with the hospital is confirmed."
                if self.attempt.result
                in (BookingAttemptResult.created, BookingAttemptResult.duplicate_replayed)
                else "That appointment time could not be booked."
            )
        if self.reconciliation is not None:
            return self.reconciliation.patient_message
        return self.detail or "Nothing further was done."

    @property
    def executed_steps(self) -> tuple[str, ...]:
        return tuple(record.step for record in self.steps if record.executed)


def summarise_for_patient(
    *, workflow: Workflow, reason_class: HandoffReasonClass, sources: Sequence[str] = ()
) -> str:
    """Build the text a patient reviews and consents to. Facts only, from the workflow itself.

    The summary has to state what will be shared *and* what is not yet known, because the patient is
    consenting to a specific sentence reaching a specific person. It is assembled here from the
    workflow's own state and evidence rather than generated: a summary produced at handoff time by a
    model would be text the patient approved only in the sense that they clicked past it.
    """
    state = workflow.state
    lines = [
        "Here is what we would share with a member of our team:",
        f"- where your referral has reached: {state}",
        f"- why a person is needed: {_REASON_SENTENCES[reason_class]}",
    ]
    if sources:
        lines.append("- what we have already checked:")
        lines.extend(f"    * {line}" for line in sources)
    lines.append(
        "- your name, contact details and the referral documents you already gave us, so they can "
        "look at the same information we did."
    )
    return "\n".join(lines)


#: One sentence per trigger. The pattern is `_REASON_SENTENCES[reason_class]` — a table, not a call.
_REASON_SENTENCES: dict[HandoffReasonClass, str] = {
    HandoffReasonClass.technical_outage: "the hospital's systems have not been answering reliably",
    HandoffReasonClass.no_match: "we could not find a provider, hospital or slot that fits your constraints",
    HandoffReasonClass.missing_document: "a document the referral needs is still missing",
    HandoffReasonClass.conflicting_sources: "two sources tell us different things about this referral",
    HandoffReasonClass.prolonged_unknown_booking: (
        "we have not been able to confirm whether an appointment was made, and we will not guess"
    ),
    HandoffReasonClass.patient_requested: "you asked to speak to a person",
    HandoffReasonClass.no_reliable_recovery: "we have run out of safe automatic steps",
}


class BookingPipeline:
    """Resumable, idempotent, and honest about what it did — see the module docstring."""

    def __init__(
        self,
        *,
        api: BookingApi,
        caps: BudgetCaps,
        settings: WorkerSettings,
        reconciler: BookingReconciler | None = None,
        clock: Clock | None = None,
        steps: Sequence[str] = BOOKING_STEPS,
    ) -> None:
        self._api = api
        self._caps = caps
        self._settings = settings
        self._reconciler = reconciler
        self._clock = clock
        self._steps = tuple(steps)

    async def run(
        self,
        conn: ConnectionLike,
        *,
        workflow: OwnedWorkflow,
        key: str,
        request: Mapping[str, Any],
        steps: Sequence[str] | None = None,
        consent_given_at: datetime | None = None,
        sources: Sequence[str] = (),
    ) -> PipelineRun:
        """Run the booking leg from the last verified checkpoint, and stop at the first thing that
        needs a human.

        ``steps`` defaults to the pipeline's own sequence; the resume plan is computed against it, so a
        checkpoint naming a step that is not in the sequence is treated as absent (restart) rather
        than as a licence to skip something. The plan is then put through
        :func:`booking_steps_for`, so a workflow that may already hold a booking can never be planned
        into sending another one (§7).
        """
        sequence = tuple(steps or self._steps)
        plan: ResumePlan = plan_resume(workflow.row, steps=sequence)
        planned = plan.steps_remaining
        planned = booking_steps_for(workflow.state, planned)
        executed: list[PipelineStepRecord] = []
        records: list[PipelineStepRecord] = []
        detail = plan.reason
        plan_note = ""
        if planned != plan.steps_remaining:
            # Nothing was silently dropped: the report says the write was not planned, and why.
            plan_note = (
                f"{plan.reason}; the booking write is not planned from {workflow.state} — a lookup "
                "under the same key is the only way out of an unverified outcome (§7)"
            )
            detail = plan_note
        attempt: BookingAttemptView | None = None
        command: CommandOutcome[BookingAttemptView] | None = None
        reconciliation: ReconciliationOutcome | None = None
        booking_record: BookingRecordOutcome | None = None
        recovery: RecoveryPlan | None = None
        handoff: HandoffOutcome | None = None

        for step in planned:
            if step == SEND_BOOKING_STEP:
                outcome = await self._send_booking(
                    conn, workflow=workflow, key=key, request=request
                )
                attempt, command = outcome.attempt, outcome.command
                records.extend(outcome.records)
                executed.extend(record for record in outcome.records if record.executed)
                if outcome.booking_record is not None:
                    booking_record = outcome.booking_record
                if outcome.stop:
                    recovery, handoff = outcome.recovery, outcome.handoff
                    detail = outcome.detail
                    break
            elif step == "confirm_outcome":
                outcome = await self._confirm_outcome(
                    conn,
                    workflow=workflow,
                    key=key,
                    consent_given_at=consent_given_at,
                    sources=sources,
                )
                reconciliation = outcome.reconciliation or reconciliation
                if outcome.booking_record is not None:
                    booking_record = outcome.booking_record
                records.extend(outcome.records)
                executed.extend(record for record in outcome.records if record.executed)
                if outcome.recovery is not None or outcome.handoff is not None:
                    recovery, handoff = outcome.recovery, outcome.handoff
                    detail = outcome.detail
                    break
                detail = outcome.detail

        if plan_note and not detail.startswith(plan_note):
            # Each step's detail becomes the run's detail as the loop goes on, so a note about the
            # *plan* would be overwritten by the last step that ran — and "the booking write was not
            # planned" is exactly the fact a reader of a resumed run needs, because it explains an
            # absence (no send, no command) rather than an event.
            detail = f"{plan_note}; {detail}" if detail else plan_note

        return PipelineRun(
            workflow_id=workflow.id,
            steps=tuple(records),
            resumed=plan.resumed,
            resume_reason=plan.reason,
            checkpoint=plan.checkpoint,
            state=workflow.state,
            attempt=attempt,
            command=command,
            reconciliation=reconciliation,
            booking_record=booking_record,
            recovery=recovery,
            handoff=handoff,
            booking_key=key,
            detail=detail,
        )

    # ------------------------------------------------------------------ steps

    async def _send_booking(
        self, conn: ConnectionLike, *, workflow: OwnedWorkflow, key: str, request: Mapping[str, Any]
    ) -> _StepOutcome:
        """Send once, under the workflow's own key, and branch on what came back."""
        workflow.assert_policy("scheduling", "create_booking")
        command_id = command_id_for(workflow.id, key, SEND_BOOKING_STEP)
        outcome: CommandOutcome[BookingAttemptView] = await run_command(
            conn,
            command_id=command_id,
            request={"idempotency_key": key, **request},
            # The lease's timeout is the attempt's: `run_with_retries` hands the call a lease sized
            # to what is left of the turn, and a request that outlives it would be a timeout the
            # budget cannot see. The callable takes the lease (that is the runtime's calling
            # convention), and the lease is what the transport is told to wait for.
            execute=lambda lease: self._api.create_booking(
                key=key,
                payload=request,
                command_id=command_id,
                timeout_ms=lease.timeout_ms,
            ),
            caps=self._caps,
            clock=self._clock,
            site="scheduling:create_booking",
            write_attempt=True,
        )

        if outcome.deduplicated or outcome.value is None:
            # Another replica sent this booking, or the attempt ended without a readable answer. Both
            # mean the same thing here: nothing else may be sent, and the outcome belongs to the
            # lookup. This is the whole point of one command_id per logical booking.
            #
            # They are *not* the same fact, though, and the step record says which happened: a replay
            # means somebody else's request is the one on the wire, while `executed` with no value
            # means this replica sent and the answer never arrived (`outcome_unknown`, §17). A record
            # that told an operator "already recorded" in the second case would be a lie about who
            # sent what, in the one place they would go looking.
            already_recorded = outcome.deduplicated
            # The step is complete either way — the ledger holds its outcome — so the checkpoint is
            # written here as well as on the unknown-*view* path below. Without it, the next replica
            # would resume at `send_booking` with nothing proving it had run, which is the case
            # `booking_steps_for` exists to keep from becoming a second booking.
            workflow.write_checkpoint(
                make_checkpoint(
                    workflow.row,
                    step=SEND_BOOKING_STEP,
                    data={
                        "outcome": outcome.outcome.value,
                        "command_id": command_id,
                        "sent_by_this_replica": outcome.executed,
                    },
                )
            )
            workflow.advance(BOOKING_UNVERIFIED_STATE)
            return _StepOutcome(
                records=[
                    PipelineStepRecord(
                        step=SEND_BOOKING_STEP,
                        executed=outcome.executed,
                        skipped=not outcome.executed,
                        detail=(
                            f"command {command_id} was already recorded; not sending again"
                            if already_recorded
                            else f"sent; the command ended as {outcome.outcome.value}, unread"
                        ),
                    )
                ],
                command=outcome,
                detail=(
                    "the booking command was already recorded; reconciling instead of resending"
                    if already_recorded
                    else "the command ended without a readable answer; reconciling instead of resending"
                ),
                stop=False,
            )

        view = outcome.value
        if view.requires_reconciliation:
            workflow.write_checkpoint(
                make_checkpoint(
                    workflow.row, step=SEND_BOOKING_STEP, data={"outcome": view.result.value}
                )
            )
            workflow.advance(BOOKING_UNVERIFIED_STATE)
            return _StepOutcome(
                records=[
                    PipelineStepRecord(
                        step=SEND_BOOKING_STEP,
                        executed=True,
                        detail=f"sent; answered {view.result.value} — reconciled by lookup",
                    )
                ],
                attempt=view,
                command=outcome,
                detail=f"the attempt answered {view.result.value}; the outcome is unknown",
                stop=False,
            )

        if view.result in (BookingAttemptResult.created, BookingAttemptResult.duplicate_replayed):
            # The facility has an appointment. The workflow may not be called `BOOKED` until the
            # durable record says the same thing (007's guard), so the record is written here, by the
            # only writer that holds both the facility's id and this workflow's lease.
            record = ensure_confirmed_booking(
                conn,
                workflow=workflow.row,
                external_ref=view.booking_request_id,
                requested_by=self._requested_by(workflow),
            )
            if not record.confirmed:
                # The facility reports a booking and our own record does not confirm one: two sources
                # disagree about whether this patient has an appointment (§11). Neither answer may be
                # written over the other, so a person decides.
                plan = self._planner(conn).plan_escalation(
                    state=workflow.state,
                    reason_class=HandoffReasonClass.conflicting_sources,
                    detail=record.detail,
                    failure_class=FailureClass.OUTCOME_UNKNOWN,
                    attempts=1,
                )
                staged = await self._apply_plan(
                    conn, workflow=workflow, plan=plan, sources=(record.detail,)
                )
                return _StepOutcome(
                    records=[
                        PipelineStepRecord(
                            step=SEND_BOOKING_STEP,
                            executed=True,
                            detail=f"{view.result.value} → {plan.action.value}",
                        )
                    ],
                    attempt=view,
                    command=outcome,
                    booking_record=record,
                    recovery=plan,
                    handoff=staged,
                    detail=plan.detail,
                    stop=True,
                )
            workflow.write_checkpoint(
                make_checkpoint(
                    workflow.row,
                    step=SEND_BOOKING_STEP,
                    data={
                        "outcome": view.result.value,
                        "booking_request_id": view.booking_request_id,
                        "booking_record_id": str(record.booking_request_id),
                    },
                )
            )
            workflow.advance(BOOKED_STATE)
            return _StepOutcome(
                records=[
                    PipelineStepRecord(step=SEND_BOOKING_STEP, executed=True, detail="booked")
                ],
                attempt=view,
                command=outcome,
                booking_record=record,
                detail="the facility confirmed the booking",
                stop=True,
            )

        # A definite refusal. Recovery decides among the actions the machine permits — and a slot
        # conflict in BOOKING_IN_FLIGHT has no edge to AWAITING_SLOT_CHOICE, so it becomes a handoff
        # rather than a state nobody defined.
        failure = classify_failure(view)
        plan = self._planner(conn).plan(state=workflow.state, failure=view, attempts=1)
        staged = await self._apply_plan(
            conn, workflow=workflow, plan=plan, sources=(view.detail or "",)
        )
        return _StepOutcome(
            records=[
                PipelineStepRecord(
                    step=SEND_BOOKING_STEP,
                    executed=True,
                    detail=f"{view.result.value} → {plan.action.value}",
                )
            ],
            attempt=view,
            command=outcome,
            recovery=plan,
            handoff=staged,
            detail=f"{failure.value}; {plan.detail}",
            stop=True,
        )

    async def _confirm_outcome(
        self,
        conn: ConnectionLike,
        *,
        workflow: OwnedWorkflow,
        key: str,
        consent_given_at: datetime | None,
        sources: Sequence[str],
    ) -> _StepOutcome:
        """Resolve an unknown outcome with `lookup_request` under the same key (§7)."""
        workflow.assert_policy("scheduling", "lookup_request")
        if (
            self._reconciler is None
        ):  # pragma: no cover - a pipeline without a reconciler cannot confirm
            raise ValueError("no reconciler configured; an unknown outcome cannot be resolved")
        outcome = await self._reconciler.reconcile(key=key)

        if outcome.booked:
            record = ensure_confirmed_booking(
                conn,
                workflow=workflow.row,
                external_ref=outcome.booking_request_id,
                requested_by=self._requested_by(workflow),
            )
            if not record.confirmed:
                plan = self._planner(conn).plan_escalation(
                    state=workflow.state,
                    reason_class=HandoffReasonClass.conflicting_sources,
                    detail=record.detail,
                    failure_class=FailureClass.OUTCOME_UNKNOWN,
                    attempts=1,
                )
                staged = await self._apply_plan(
                    conn,
                    workflow=workflow,
                    plan=plan,
                    sources=(outcome.attempts_summary(), record.detail),
                    consent_given_at=consent_given_at,
                )
                return _StepOutcome(
                    records=[
                        PipelineStepRecord(
                            step="confirm_outcome",
                            executed=True,
                            detail=f"the booking is unconfirmable → {plan.action.value}",
                        )
                    ],
                    reconciliation=outcome,
                    booking_record=record,
                    recovery=plan,
                    handoff=staged,
                    detail=plan.detail,
                    stop=True,
                )
            workflow.write_checkpoint(
                make_checkpoint(
                    workflow.row,
                    step="confirm_outcome",
                    data={
                        "outcome": outcome.status.value,
                        "booking_request_id": outcome.booking_request_id,
                        "booking_record_id": str(record.booking_request_id),
                    },
                )
            )
            workflow.advance(BOOKED_STATE)
            return _StepOutcome(
                records=[
                    PipelineStepRecord(step="confirm_outcome", executed=True, detail="booked")
                ],
                reconciliation=outcome,
                booking_record=record,
                detail="the facility confirmed the booking under the same key",
                stop=True,
            )
        if outcome.not_booked:
            # Safe to send again, and only under the SAME key: this is an idempotent retry of one
            # logical attempt, not a new one. The workflow goes back to the state that may book.
            workflow.advance("BOOKING_IN_FLIGHT")
            return _StepOutcome(
                records=[
                    PipelineStepRecord(
                        step="confirm_outcome", executed=True, detail="definitively not booked"
                    )
                ],
                reconciliation=outcome,
                detail="the facility has no booking under this key; the attempt may be sent again",
                stop=True,
            )
        if outcome.is_pending:
            return _StepOutcome(
                records=[
                    PipelineStepRecord(
                        step="confirm_outcome", executed=True, detail="still pending"
                    )
                ],
                reconciliation=outcome,
                detail=outcome.detail,
                stop=True,
            )

        # Escalated. This is §11's `prolonged_unknown_booking`, named at the call site because the
        # trigger is the *budget running out*, not the fact that something was unknown.
        plan = self._planner(conn).plan_escalation(
            state=workflow.state,
            reason_class=HandoffReasonClass.prolonged_unknown_booking,
            detail=f"{outcome.escalation_reason.value}: {outcome.detail}"
            if outcome.escalation_reason
            else outcome.detail,
        )
        staged = await self._apply_plan(
            conn,
            workflow=workflow,
            plan=plan,
            sources=(outcome.attempts_summary(), *tuple(sources)),
            consent_given_at=consent_given_at,
        )
        return _StepOutcome(
            records=[
                PipelineStepRecord(
                    step="confirm_outcome",
                    executed=True,
                    detail=f"escalated: {outcome.attempts_summary()}",
                )
            ],
            reconciliation=outcome,
            recovery=plan,
            handoff=staged,
            detail=plan.detail,
            stop=True,
        )

    # ------------------------------------------------------------------ helpers

    def _planner(self, conn: ConnectionLike) -> RecoveryPlanner:
        """A planner over the *live* transition table — the same rows the guard trigger enforces."""
        return RecoveryPlanner(transitions=WorkflowTransitions(conn))

    @staticmethod
    def _requested_by(workflow: OwnedWorkflow) -> str:
        """Who asked the database to record the booking: this worker, named as one.

        `booking_request.requested_by` is an audit column rather than a foreign key, and it should say
        which *process* made the write. Two replicas racing to record the same booking produce the same
        row either way — the approval is consumed by the first insert and the second finds the row —
        so the value names the winner, which is exactly what an audit trail is for.
        """
        return f"workers:{workflow.worker_id}"

    async def _apply_plan(
        self,
        conn: ConnectionLike,
        *,
        workflow: OwnedWorkflow,
        plan: RecoveryPlan,
        sources: Sequence[str] = (),
        consent_given_at: datetime | None = None,
    ) -> HandoffOutcome | None:
        """Carry out a recovery plan: stage and queue a handoff, or move the workflow, or stop.

        Ordering matters and is the same in every branch: the *staging record* is written while
        automation still owns the workflow (a human owner would refuse it, and refusing is correct —
        once a person holds the ticket, the automation's version of events is stale), then the handoff
        row, then the workflow's own move to `HUMAN_HANDOFF`.
        """
        if plan.automation_must_stop:
            return None

        if plan.action is RecoveryAction.HANDOFF:
            reason_class = plan.handoff_reason or HandoffReasonClass.no_reliable_recovery
            request = HandoffRequest(
                workflow_id=workflow.id,
                reason_class=reason_class,
                patient_visible_summary=summarise_for_patient(
                    workflow=workflow.row, reason_class=reason_class, sources=sources
                ),
                reason_detail=plan.detail,
                sources=tuple(sources),
                consent_given_at=consent_given_at,
                ticket_ref=default_ticket_ref(),
            )
            write_payload(
                conn,
                workflow=workflow.row,
                worker_id=workflow.worker_id,
                key=HANDOFF_KEY,
                value=request.payload(),
            )
            if not request.has_consent:
                # §11: the patient sees the summary before it is shared, and an un-consented handoff
                # is not queued. The staging write stands; the row does not.
                return HandoffOutcome(request=request, record=None, staged=True)
            record = HumanQueue(conn).queue(request)
            workflow.advance(plan.target_state or "HUMAN_HANDOFF")
            return HandoffOutcome(request=request, record=record, staged=True)

        if plan.target_state is not None:
            workflow.advance(plan.target_state)
        return None


@dataclass(frozen=True, slots=True)
class _StepOutcome:
    """Internal: what one step left behind."""

    records: list[PipelineStepRecord] = field(default_factory=list)
    attempt: BookingAttemptView | None = None
    command: CommandOutcome[BookingAttemptView] | None = None
    reconciliation: ReconciliationOutcome | None = None
    booking_record: BookingRecordOutcome | None = None
    recovery: RecoveryPlan | None = None
    handoff: HandoffOutcome | None = None
    detail: str = ""
    stop: bool = False


def command_id_for(workflow_id: Any, key: str, step: str) -> str:
    """A deterministic command id for one step of one logical booking.

    Deterministic rather than random, and that is the whole mechanism: two replicas working the same
    workflow produce the same id, so the ledger dedupes them. A random id would make every replica a
    new command, which is the double-booking bug wearing a UUID.

    Returned as a **string**: `command_dedup.command_id` is `text`, and a `UUID` object bound to a
    text parameter is refused by PostgreSQL (`function command_begin(uuid, unknown) does not exist`)
    rather than coerced. The ledger's key is text; this is its canonical spelling.
    """
    return str(uuid5(NAMESPACE_URL, f"referraleasy:command:{workflow_id}:{key}:{step}"))
