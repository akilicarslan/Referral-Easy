"""What a decision may say, what next it may name, and what it may claim.

Three jobs, all of them shared by seven agents, all of them places a model could
otherwise be the authority:

1. **`next_allowed_actions` is computed, not proposed.** §4: the coordinator's LLM
   cannot exceed the policy table, and §5 puts `next_allowed_actions` in every
   decision output. The list here is derived by *asking the policy backend* — the
   same `policy_assert(role, tool, state)` the `ToolGate` calls — over the seven
   roles' tools, so an action appears in a decision only if the authority that owns
   the table said yes. There is no local copy of the table to drift.
2. **A decision may not name an action the envelope did not allow.** §4 again:
   "An action outside this list is refused by the tool layer even if the model
   requests it." :func:`assert_action_allowed` is that refusal, on the *output*
   side, before the decision leaves the service.
3. **A patient-facing sentence may not overclaim.** §2.5: `REQUESTED` and
   `PENDING_REVIEW` are never presented as confirmed, and §3 forbids Coverage from
   promising that a visit is covered. Both are checks over the sentence the agent
   is about to show a patient, because both are the kind of sentence a fluent model
   produces by accident.

The probing in (1) costs one `policy_assert` per (role, tool) pair and is
deliberately not cached here: the table is default-deny and the backend may be a
database whose rows change with a release. A deployment that finds the cost
material caches per `(workflow_state, release_manifest_id)` — which is a
performance change, not a semantics change, and is not made here because caching
an authority's answer in the process that must obey it is how a stale permission
outlives a revoked one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from referraleasy_agent_runtime import ROLE_TOOLS, AgentRole, PolicyBackend, PolicyRefused
from referraleasy_agents.canonical import id_text
from referraleasy_agents.errors import CoverageGuaranteeRefused
from referraleasy_contracts.generated.common import (
    ActionRef,
    BookingState,
    DegradationLevel,
    EvidenceId,
    MissingField,
    ReasonCode,
)
from referraleasy_contracts.generated.patient import MandatoryConditions
from referraleasy_contracts.generated.task import DecisionOutput

__all__ = [
    "BOOKING_STATE_WORDING",
    "GUARANTEE_PHRASES",
    "assert_action_allowed",
    "assert_state_wording",
    "build_decision",
    "mandatory_conditions",
    "next_allowed_actions",
]

#: How each booking state may be described to a patient (§2.5). The two that must
#: never read as "confirmed" are the point of the table.
BOOKING_STATE_WORDING: Final[Mapping[BookingState, str]] = {
    BookingState.REQUESTED: "we have sent the request to the hospital and are waiting for their reply",
    BookingState.PENDING_REVIEW: "the hospital is reviewing the request; nothing is booked yet",
    BookingState.CONFIRMED: "the hospital confirmed the appointment",
    BookingState.REJECTED: "the hospital could not accept this request",
    BookingState.CANCEL_PENDING: "the cancellation has been requested and is not finished",
    BookingState.CANCELLED: "the appointment is cancelled",
}

#: Phrases that promise payment or coverage. §3: Coverage & Network "cannot
#: guarantee insurance/payment". Matched case-insensitively on the words a model
#: actually reaches for; the list is short on purpose — a long list of synonyms
#: would read as exhaustive and be trusted further than it deserves.
GUARANTEE_PHRASES: Final[tuple[str, ...]] = (
    "guaranteed",
    "guarantee",
    "fully covered",
    "will be covered",
    "is covered",
    "no cost to you",
    "we will pay",
    "insurance will pay",
    "free of charge",
)

#: Words that assert a confirmed appointment. Refused for states that are not confirmed.
_CONFIRMED_WORDS: Final[tuple[str, ...]] = (
    "confirmed",
    "booked",
    "scheduled you",
    "you are booked",
)


def next_allowed_actions(
    policy: PolicyBackend,
    workflow_state: str,
    *,
    roles: Iterable[AgentRole] = tuple(AgentRole),
) -> list[str]:
    """Every ``role:tool`` the policy table permits in ``workflow_state``.

    Derived by asking the same authority the tool gate asks, so the answer cannot
    be more permissive than the gate. Sorted for a stable trace, and never empty
    by accident: a state with no permitted action returns ``[]``, which is a
    finding (a dead state) rather than something to paper over.
    """
    permitted: list[str] = []
    for role in roles:
        for tool in sorted(ROLE_TOOLS[role]):
            try:
                policy.policy_assert(role.value, tool, workflow_state)
            except Exception:
                continue
            permitted.append(f"{role.value}:{tool}")
    return sorted(permitted)


def assert_action_allowed(
    action: str,
    allowed_actions: Sequence[ActionRef | str],
    *,
    role: AgentRole,
    workflow_state: str | None,
) -> None:
    """Refuse a decision whose action the envelope did not allow (§4).

    Raised as the runtime's own :class:`PolicyRefused`, so a caller that already
    handles a refused tool call handles this too — the refusal is the same kind of
    event: a server-side policy decision, projected as `rejected`, not an agent
    result.
    """
    allowed = {id_text(item) for item in allowed_actions}
    if action not in allowed:
        raise PolicyRefused(
            f"{role.value} returned action {action!r}, which is not in the envelope's "
            f"allowed_actions {sorted(allowed)}",
            role=role.value,
            tool=action,
            workflow_state=workflow_state,
        )


def assert_state_wording(sentence: str, state: BookingState) -> None:
    """Refuse a sentence that calls a not-yet-confirmed booking confirmed (§2.5).

    Only the two "in flight" states are policed. For `CONFIRMED` the word is the
    accurate one, and refusing it would be the rule that fights its own purpose.
    """
    if state in (BookingState.CONFIRMED, BookingState.CANCELLED):
        return
    lowered = sentence.lower()
    for word in _CONFIRMED_WORDS:
        if word in lowered:
            raise CoverageGuaranteeRefused(
                f"a booking in state {state.value} must not be described as {word!r}: "
                f"describe it as {BOOKING_STATE_WORDING[state]!r} instead (§2.5)."
            )
    if state is BookingState.REJECTED and "confirmed" in lowered:
        raise CoverageGuaranteeRefused(
            f"a booking in state {state.value} must not be described as confirmed (§2.5)."
        )


def assert_no_payment_guarantee(sentence: str) -> None:
    """Refuse a patient-facing sentence that promises coverage or payment (§3)."""
    lowered = sentence.lower()
    for phrase in GUARANTEE_PHRASES:
        if phrase in lowered:
            raise CoverageGuaranteeRefused(
                f"the sentence {sentence!r} contains {phrase!r}, which reads as a coverage or "
                "payment guarantee. Coverage & Network reports a network status with a freshness "
                "date and never a guarantee; cite `coverage_not_a_guarantee` instead (§3)."
            )


def build_decision(
    *,
    action: str,
    reason_code: ReasonCode,
    evidence_ids: Sequence[EvidenceId | str] = (),
    missing_fields: Sequence[MissingField] = (),
    next_actions: Sequence[str] = (),
    reason_detail: str | None = None,
    degraded: DegradationLevel | None = None,
    allowed_actions: Sequence[ActionRef | str] | None = None,
    role: AgentRole | None = None,
    workflow_state: str | None = None,
) -> DecisionOutput:
    """Assemble a §4 decision output, refusing the two shapes that must not exist.

    * an action outside the envelope's `allowed_actions` (when one was supplied),
    * a decision with no evidence that is not a pure clarification request.

    §5: "A decision with no evidence is only legal for a pure clarification
    request." The exception is named precisely: the reason code must be one of the
    codes that mean "we must ask", so `ok` with no evidence is refused rather than
    waved through because the action looked harmless.
    """
    if allowed_actions is not None:
        if role is None:
            raise ValueError(
                "build_decision needs a role to refuse an action outside allowed_actions"
            )
        assert_action_allowed(action, allowed_actions, role=role, workflow_state=workflow_state)
    clarification_codes = {ReasonCode.missing_mandatory_field, ReasonCode.ambiguous_input}
    if not evidence_ids and reason_code not in clarification_codes:
        raise ValueError(
            f"a decision with reason_code {reason_code.value!r} and no evidence_ids is not legal: "
            "only a pure clarification request (missing_mandatory_field / ambiguous_input) may "
            "carry no evidence (§5)."
        )
    if reason_code is ReasonCode.other and not reason_detail:
        raise ValueError("reason_code 'other' requires reason_detail (§5).")
    return DecisionOutput(
        action=action,
        reason_code=reason_code,
        reason_detail=reason_detail,
        evidence_ids=[EvidenceId(id_text(item)) for item in evidence_ids],
        missing_fields=list(missing_fields),
        next_allowed_actions=[ActionRef(id_text(item)) for item in next_actions],
        degraded=degraded,
    )


def mandatory_conditions(
    *,
    specialty_identified: bool,
    approval_present: bool,
    slot_valid: bool,
    blocking_reasons: Sequence[ReasonCode] = (),
) -> MandatoryConditions:
    """§2.1's three mandatory conditions, with the reasons they block.

    Returned as the published `MandatoryConditions` contract shape. `blocking_reasons`
    is derived here when the caller does not supply it, so a decision cannot claim
    "all three hold" while listing a blocker.
    """
    reasons = list(blocking_reasons)
    if not specialty_identified and ReasonCode.missing_mandatory_field not in reasons:
        reasons.append(ReasonCode.missing_mandatory_field)
    if not approval_present and ReasonCode.approval_expired not in reasons:
        reasons.append(ReasonCode.approval_expired)
    if not slot_valid and ReasonCode.no_slot_available not in reasons:
        reasons.append(ReasonCode.no_slot_available)
    if specialty_identified and approval_present and slot_valid and reasons:
        raise ValueError(
            "mandatory_conditions: all three conditions hold but blocking_reasons is non-empty "
            f"({[reason.value for reason in reasons]}); a decision may not both hold and be blocked."
        )
    return MandatoryConditions(
        specialty_identified=specialty_identified,
        approval_present=approval_present,
        slot_valid=slot_valid,
        blocking_reasons=reasons or None,
    )
