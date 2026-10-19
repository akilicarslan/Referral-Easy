"""What a case expects, what a run did, and what a rule concluded (§14).

Three shapes, and the boundary between them is the point of the module.

* A :class:`Case` is an **expectation**: which scenario it belongs to, which fixture ids it is about,
  and which named rules apply with which parameters. It carries no observation.
* A :class:`RunRecord` is an **observation**: the artifacts one replay produced — the outcome, the
  bookings, the tool calls, the delegations, the latency. It carries no judgement.
* A :class:`Verdict` is a **judgement**: a score, or a refusal to score with the reason, and whether
  what it found is a release blocker.

Keeping them apart is what makes "N/A is not zero" implementable rather than aspirational. If a case
carried its own score, a rule that could not decide would have nothing to return *except* a number;
here it returns ``NOT_APPLICABLE`` with the reason it could not, and the report counts it without
averaging it.

**Rules are named, not embedded.** A case lists rule names and the dataset supplies the parameters,
so the same rule serves every scenario — ``single_booking_per_slot`` is one function whether it runs
over the timeout-after-commit case or the standard booking — and adding a scenario is data rather
than code. The registry in :mod:`referraleasy_eval.rules` is the one place a name is bound to a
function, and an unknown name is a load error rather than a silently skipped check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "CRITICAL",
    "EVAL_ERROR",
    "NOT_APPLICABLE",
    "SCORED",
    "BookingRecord",
    "Case",
    "ConstraintOutcome",
    "Escalation",
    "RunRecord",
    "ToolCall",
    "Verdict",
    "critical_verdict",
    "not_applicable",
    "scored",
]

#: §14's three statuses. `scored` carries a number; the other two carry the reason there is none, and
#: the query layer excludes them from distributions while still counting them in the sample.
SCORED: Final = "scored"
NOT_APPLICABLE: Final = "not_applicable"
EVAL_ERROR: Final = "eval_error"


@dataclass(frozen=True, slots=True)
class ConstraintOutcome:
    """One preference and whether the run honoured it.

    ``kind`` is the contract's ``hard`` or ``soft``. §4's rule is that a hard constraint is never
    relaxed without an explicit decision, so the two are scored the same way and *interpreted*
    differently — a rule that treated them alike would either forbid all relaxation or permit it.
    """

    code: str
    kind: str
    satisfied: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BookingRecord:
    """One booking the run wrote, as it stands at the end of the run.

    ``approval_id`` is carried because §9 makes an unapproved booking a *critical* violation rather
    than a quality miss: a booking written without a live approval bound to its payload is the one
    outcome the whole guardrail design exists to prevent, and a rule that could not see which
    approval authorised it could not tell the two apart.
    """

    booking_id: str
    state: str
    provider_id: str
    hospital_id: str
    slot_id: str
    approval_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation. ``outcome`` is the tool's own result kind, not a success flag.

    A run that recovered from a transient error has tool calls that failed and then a call that
    succeeded; scoring those as failures would punish the recovery §5 asks for.
    """

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    outcome: str = "ok"


@dataclass(frozen=True, slots=True)
class Escalation:
    """A handoff to a human. ``reason_code`` is the contract's, so the reason is checkable."""

    handoff_id: str
    reason_code: str
    ticket_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Everything one replayed case produced, and nothing about whether it was right.

    This is the interface the capture side fills. It is deliberately a value object rather than a
    live handle: §14 requires offline replay that never recreates a live booking, so scoring reads a
    record and cannot write anything back.
    """

    case_id: str
    workflow_id: str = ""
    outcome: str = ""
    final_state: str = ""
    selected_provider_id: str | None = None
    selected_hospital_id: str | None = None
    selected_slot_id: str | None = None
    bookings: tuple[BookingRecord, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    delegated_roles: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    constraints: tuple[ConstraintOutcome, ...] = ()
    escalation: Escalation | None = None
    required_document_kinds: tuple[str, ...] = ()
    attached_document_kinds: tuple[str, ...] = ()
    #: Approval ids that were live for this workflow. A booking naming one of these is approved.
    live_approval_ids: tuple[str, ...] = ()
    booking_attempts: int = 0
    latency_ms: int = 0
    model_calls: int = 0
    tool_calls_made: int = 0
    cost_usd: float = 0.0
    synthetic: bool = True

    @property
    def confirmed_bookings(self) -> tuple[BookingRecord, ...]:
        return tuple(booking for booking in self.bookings if booking.state == "CONFIRMED")

    def scoreable(self) -> bool:
        """Whether there is anything here to judge.

        A record with no outcome is not a bad run, it is a run that was never captured — and §14's
        rule cuts both ways: a zero for an uncaptured run is as wrong as a zero for an N/A case.
        """
        return bool(self.outcome)


@dataclass(frozen=True, slots=True)
class Case:
    """One scorable expectation, grounded in a scenario's fixtures.

    ``metric`` is §14's measurement name and travels to the emitted event as ``eval.metric``, which
    is what keeps two different metrics out of one series. ``rubric`` is the versioned text the rules
    interpret; it is carried per case rather than globally because a dataset may hold a case scored
    against an older rubric while the rest moved.
    """

    case_id: str
    scenario_id: str
    metric: str
    checks: tuple[str, ...]
    expectation: Mapping[str, Any]
    rubric: str = "offline-rules-v1"
    references: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    partition: str = "held_out"

    def expected(self, key: str, default: Any = None) -> Any:
        return self.expectation.get(key, default)


@dataclass(frozen=True, slots=True)
class Verdict:
    """One rule's conclusion about one case.

    ``score`` is ``None`` unless ``status`` is ``scored``, and the constructor helpers enforce that
    rather than leaving it to each rule — the same rule §14 states, at the point where a rule author
    would otherwise be free to break it.
    """

    status: str
    score: float | None
    reason: str
    rule: str = ""
    critical: bool = False

    @property
    def is_scored(self) -> bool:
        return self.status == SCORED


def scored(rule: str, score: float, reason: str) -> Verdict:
    """A measured verdict. The score is clamped to ``[0, 1]`` rather than trusted."""
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"{rule}: a score must be in [0, 1], got {score!r}")
    return Verdict(status=SCORED, score=score, reason=reason, rule=rule)


def not_applicable(rule: str, reason: str) -> Verdict:
    """The rule could not decide **and no number would be honest**.

    This is not a failure and not a zero. §14: "N/A and eval_error are not scored as zero" — an
    average that counted them as zero would report a system as worse the more of it could not be
    measured, which is the opposite of what a reader would conclude.
    """
    return Verdict(status=NOT_APPLICABLE, score=None, reason=reason, rule=rule)


def eval_error(rule: str, reason: str) -> Verdict:
    """The rule *should* have been able to decide and could not — a defect, not a gap.

    Kept distinct from :func:`not_applicable` because the two call for different responses: a gap is
    a dataset that does not cover something, an error is a rule or a capture that is broken. Merging
    them would hide the second inside the first.
    """
    return Verdict(status=EVAL_ERROR, score=None, reason=reason, rule=rule)


def critical_verdict(rule: str, reason: str) -> Verdict:
    """A violation §14 makes a release blocker, scored 0 and flagged.

    Modelled as a scored zero *and* a flag rather than a third status: it is a measurement — the run
    did the forbidden thing — and the flag is what a release gate reads. A status of its own would
    make "was this a violation" a comparison of statuses everywhere instead of a boolean in one
    place.
    """
    return Verdict(status=SCORED, score=0.0, reason=reason, rule=rule, critical=True)


#: §14's critical class, named once so the CLI, the report and the tests agree about what blocks a
#: release. ``unapproved_booking`` and ``double_booking`` are the two the plan names outright.
CRITICAL: Final[tuple[str, ...]] = ("unapproved_booking", "double_booking")
