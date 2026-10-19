"""Worker processes for ReferralEasy: deadlines, retries, leases, reconciliation and HITL (§7, §11).

What lives here, and the one thing that does not.

The *budgets* are not here. `packages/agent-runtime` owns the retry curve, the attempt caps and the
`Retry-After` handling (§8, ADR-0030); every module in this package imports them and none re-declares
one. The same goes for the control-state schema: refusal codes, the lease, the transition table and
the outbox's own functions are SQL, and this package calls them. A worker that re-implemented any of
those would be the second place a rule lives, and the first place it drifts.

What is here is the *process* half: which step comes next after a crash, who owns a workflow while a
step runs, what a lookup is allowed to ask, when recovery starts, when a human is called, and what a
patient is told. Those are the decisions that cannot be a `WHERE` clause, because they are about what
to do rather than about what is true.

Reading order, for somebody new to the package:

1. :mod:`referraleasy_workers.ownership` — a lease and a generation, and how a late write is refused.
2. :mod:`referraleasy_workers.checkpoint` — where a second replica resumes from.
3. :mod:`referraleasy_workers.commands` — one command id, one outcome, one retry owner.
4. :mod:`referraleasy_workers.reconcile` — the only way out of "we do not know".
5. :mod:`referraleasy_workers.recovery` — classification, and the actions the machine permits.
6. :mod:`referraleasy_workers.hitl` — the human side, and what consent gates.
7. :mod:`referraleasy_workers.service` — all of the above, in order, as one pipeline.
8. :mod:`referraleasy_workers.outbox` — the queue, with a terminal state for the undeliverable.
"""

from __future__ import annotations

from referraleasy_workers.booking import (
    BookingApi,
    BookingAttemptView,
    CancelView,
    HttpBookingApi,
    LookupOutcome,
    LookupView,
    RescheduleView,
)
from referraleasy_workers.booking_record import (
    BOOKING_REQUEST_CONFIRM_STATEMENT,
    BookingRecordOutcome,
    ensure_confirmed_booking,
    existing_booking_request,
    live_approval_id,
)
from referraleasy_workers.checkpoint import (
    CHECKPOINT_KEY,
    HANDOFF_KEY,
    Checkpoint,
    CheckpointRead,
    ResumePlan,
    make_checkpoint,
    plan_resume,
    read_checkpoint,
    write_checkpoint,
    write_payload,
)
from referraleasy_workers.commands import CommandOutcome, run_command
from referraleasy_workers.db import connect, connection
from referraleasy_workers.errors import (
    ConsentRequiredError,
    RecoveryRefusedError,
    SecondKeyRefusedError,
    WorkerError,
    ensure_same_key,
)
from referraleasy_workers.events import (
    ABANDONED_STATE,
    BOOKING_UNVERIFIED_STATE,
    TerminalEvent,
    TerminalEventRecord,
)
from referraleasy_workers.hitl import (
    HITL_TRIGGERS,
    OPERATOR_RETURN_STATES,
    DirectoryContact,
    HandoffRecord,
    HandoffRequest,
    HumanQueue,
    OperatorDirectory,
    OperatorResponse,
    ResolutionOutcome,
    TicketDraft,
    TicketView,
    default_ticket_ref,
    queued_handoffs,
    workflow_is_human_owned,
)
from referraleasy_workers.outbox import (
    DeliveryFailure,
    DeliveryOutcome,
    DeliveryResult,
    HttpNotificationSender,
    OutboxRunReport,
    OutboxSender,
    OutboxWorker,
)
from referraleasy_workers.ownership import OwnedWorkflow
from referraleasy_workers.reconcile import (
    BookingReconciler,
    EscalationReason,
    ReconciliationAction,
    ReconciliationOutcome,
    WallClock,
)
from referraleasy_workers.recovery import (
    BOOKED_STATE,
    DEFAULT_TRANSIENT_ATTEMPTS_BEFORE_RECOVERY,
    HUMAN_HANDOFF_STATE,
    RECOVERY_STATE,
    FailureClass,
    RecoveryAction,
    RecoveryPlan,
    RecoveryPlanner,
    RescheduleAction,
    ReschedulePlan,
    TransitionRules,
    WorkflowTransitions,
    classify_failure,
    plan_reschedule,
    reaches_booking_threshold,
)
from referraleasy_workers.service import (
    BOOKING_STEPS,
    SEND_BOOKING_STEP,
    BookingPipeline,
    HandoffOutcome,
    PipelineRun,
    PipelineStepRecord,
    booking_steps_for,
    command_id_for,
    summarise_for_patient,
)
from referraleasy_workers.settings import WorkerSettings, default_worker_id

__all__ = [
    "ABANDONED_STATE",
    "BOOKED_STATE",
    "BOOKING_REQUEST_CONFIRM_STATEMENT",
    "BOOKING_STEPS",
    "BOOKING_UNVERIFIED_STATE",
    "CHECKPOINT_KEY",
    "DEFAULT_TRANSIENT_ATTEMPTS_BEFORE_RECOVERY",
    "HANDOFF_KEY",
    # HITL
    "HITL_TRIGGERS",
    "HUMAN_HANDOFF_STATE",
    "OPERATOR_RETURN_STATES",
    "RECOVERY_STATE",
    #: The step that writes to the outside world, named once so "the booking write" is a symbol
    #: rather than a string that two modules can disagree about (§7).
    "SEND_BOOKING_STEP",
    # booking
    "BookingApi",
    "BookingAttemptView",
    # pipeline
    "BookingPipeline",
    "BookingReconciler",
    # durable booking record
    "BookingRecordOutcome",
    "CancelView",
    "Checkpoint",
    "CheckpointRead",
    "CommandOutcome",
    "ConsentRequiredError",
    "DeliveryFailure",
    "DeliveryOutcome",
    "DeliveryResult",
    "DirectoryContact",
    "EscalationReason",
    # recovery
    "FailureClass",
    "HandoffOutcome",
    "HandoffRecord",
    "HandoffRequest",
    "HttpBookingApi",
    "HttpNotificationSender",
    "HumanQueue",
    "LookupOutcome",
    "LookupView",
    "OperatorDirectory",
    "OperatorResponse",
    "OutboxRunReport",
    "OutboxSender",
    # outbox
    "OutboxWorker",
    # ownership, durability, commands
    "OwnedWorkflow",
    "PipelineRun",
    "PipelineStepRecord",
    "ReconciliationAction",
    "ReconciliationOutcome",
    "RecoveryAction",
    "RecoveryPlan",
    "RecoveryPlanner",
    "RecoveryRefusedError",
    "RescheduleAction",
    "ReschedulePlan",
    "RescheduleView",
    "ResolutionOutcome",
    "ResumePlan",
    "SecondKeyRefusedError",
    # events and reconciliation
    "TerminalEvent",
    "TerminalEventRecord",
    "TicketDraft",
    "TicketView",
    "TransitionRules",
    # clocks, errors
    "WallClock",
    "WorkerError",
    # settings and connections
    "WorkerSettings",
    "WorkflowTransitions",
    "booking_steps_for",
    "classify_failure",
    "command_id_for",
    "connect",
    "connection",
    "default_ticket_ref",
    "default_worker_id",
    "ensure_confirmed_booking",
    "ensure_same_key",
    "existing_booking_request",
    "live_approval_id",
    "make_checkpoint",
    "plan_reschedule",
    "plan_resume",
    "queued_handoffs",
    "reaches_booking_threshold",
    "read_checkpoint",
    "run_command",
    "summarise_for_patient",
    "workflow_is_human_owned",
    "write_checkpoint",
    "write_payload",
]
