"""Recovery & Handoff's three tools (§3): `classify_error`, `create_handoff`, and recovery's
own use of `search_directory`.

The two forbidden capabilities, and how each is made structural:

* **"cannot override a policy block."** `classify_error` reports the *policy's*
  answer, not its own: `permitted_actions` is derived by asking the same policy
  backend the tool gate asks (:func:`decisions.next_allowed_actions`), and a
  `policy_blocked` failure classifies to a plan in which nothing is retried and a
  human is required. There is no branch in which a blocked action becomes
  permitted — the code has no input that could express one.
* **"cannot call the same failed agent forever."** `may_retry` comes from
  :mod:`referraleasy_agents.recovery`, whose `TRANSIENT_ATTEMPT_LIMIT` is §8's
  three. On the third failure the plan says so, and `OneWayDoorRefused` exists for
  a caller that tries anyway.

`create_handoff` is honest about a gap: **no mock-API route exists for handoffs**
(`services/mock-api/src/app.ts` registers none, and `migrations/011_handoff.sql`
only gives the table). This tool therefore builds and validates the record, and
posts it to `/handoffs`, which today answers 404 and surfaces as a terminal
`UpstreamRejected`. That is stated here rather than papered over with a local
store: a handoff that is "created" in a process that then forgets it is worse than
a loud failure.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from referraleasy_agents.decisions import next_allowed_actions
from referraleasy_agents.errors import ConsentNotRecorded, UnknownDataError
from referraleasy_agents.recovery import (
    FAILURE_CLASSES,
    TRANSIENT_ATTEMPT_LIMIT,
    classify_failure,
    one_way_door_reached,
    patient_message,
)
from referraleasy_agents.schemas import Handoff
from referraleasy_agents.tools.call import ToolCall, require_workflow_id
from referraleasy_contracts.generated.common import HandoffReasonClass, WorkflowState

__all__ = [
    "classify_error",
    "create_handoff",
]

#: The route the handoff record is posted to. Named here so the gap is one search
#: away: this path does not exist in the mock API yet.
HANDOFF_ROUTE: Final = "/handoffs"

#: Which failure classes need a human rather than another automatic step.
_HUMAN_REQUIRED: Final = frozenset(
    {"policy_blocked", "guardrail_blocked", "booking_unknown", "unknown"}
)

#: The data a class is waiting for, when it is waiting for data.
_MISSING_DATA: Final[Mapping[str, tuple[str, ...]]] = {
    "data_missing": ("referral_field",),
    "document_missing": ("document",),
    "document_unreadable": ("document",),
}


async def classify_error(
    call: ToolCall,
    *,
    failed_role: str,
    error_class: str,
    transient_attempts: int,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """Classify a failure and return the bounded actions the state machine permits.

    The plan is a *report*, not a tool call: it says what may happen next, and the
    caller decides among the permitted options. `permitted_actions` is empty when no
    policy backend was supplied, which is the default-deny reading of "the authority
    is unavailable" rather than a licence to act.
    """
    classification = classify_failure(
        error_class=error_class, reason_code=reason_code, transient_attempts=transient_attempts
    )
    if classification not in FAILURE_CLASSES:
        raise UnknownDataError(
            f"failure class {classification!r} is not one of {list(FAILURE_CLASSES)}; a class outside "
            "the table has no plan attached to it."
        )
    door_reached = one_way_door_reached(transient_attempts)
    human_required = classification in _HUMAN_REQUIRED or (
        classification == "transient" and door_reached
    )
    missing = list(_MISSING_DATA.get(classification, ()))

    permitted: list[str] = []
    if call.policy is not None:
        permitted = next_allowed_actions(call.policy, WorkflowState.RECOVERY.value)

    return_state = (
        WorkflowState.HUMAN_HANDOFF.value if human_required else WorkflowState.RECOVERY.value
    )
    retry_same_agent = classification == "transient" and not door_reached
    plan = {
        "failure_class": classification,
        "retry_same_agent": retry_same_agent,
        "use_directory_fallback": classification in {"no_match", "data_missing", "transient"},
        "missing_data_needed": missing,
        "alternative_search_required": classification in {"no_match", "coverage_unknown"},
        "human_handoff_required": human_required,
        "return_state": return_state,
        "permitted_actions": permitted,
    }
    return {
        "action_plan": plan,
        # §7: composed from the table, not written by a model. The same input
        # produces the same sentence, and the sentence does not promise a step the
        # plan does not contain.
        "patient_message": patient_message(classification),
        "failed_role": failed_role,
        "transient_attempts": transient_attempts,
        "transient_attempt_limit": TRANSIENT_ATTEMPT_LIMIT,
        "one_way_door_reached": door_reached,
        "evidence_ids": [],
    }


async def create_handoff(
    call: ToolCall,
    *,
    reason_class: str,
    reason_detail: str,
    patient_visible_summary: str,
    return_state: str,
    consent_given_at: str | None = None,
) -> dict[str, Any]:
    """Queue a human handoff, with the summary the patient consented to share.

    The record is validated against :class:`referraleasy_agents.schemas.Handoff`
    *before* it is sent, so a handoff that is missing its reason class, its return
    state or its consent fails here rather than in a human's queue.

    The workflow is **not a tool argument**: it comes from the task envelope
    (`require_workflow_id`). A handoff is a human being given a case to work, and it
    would be filed against a workflow a model named rather than the one whose failure
    raised it.
    """
    if not consent_given_at or not str(consent_given_at).strip():
        raise ConsentNotRecorded(
            "create_handoff was called with no consent_given_at. The patient-visible summary is the "
            "patient's to authorise; creating a handoff is not consent (§3, §11)."
        )
    if reason_class not in {member.value for member in HandoffReasonClass}:
        raise UnknownDataError(
            f"reason_class {reason_class!r} is not one of "
            f"{sorted(member.value for member in HandoffReasonClass)}; handoff reasons are classed, "
            "not free text (§11)."
        )
    if return_state not in {member.value for member in WorkflowState}:
        raise UnknownDataError(
            f"return_state {return_state!r} is not one of the sixteen workflow states; a resolution "
            "cannot return the workflow to a state that does not exist."
        )

    record = Handoff(
        workflow_id=require_workflow_id(call),
        reason_class=HandoffReasonClass(reason_class),
        reason_detail=reason_detail,
        patient_visible_summary=patient_visible_summary,
        consent_given_at=consent_given_at,
        return_state=WorkflowState(return_state),
    )
    body = record.model_dump(mode="json")
    response = await call.api.post(HANDOFF_ROUTE, body, budget=call.budget, lease=call.lease)
    # The route may answer with the record under `handoff` or flat; a route that answers
    # with neither leaves what this tool sent standing, which is the record it validated.
    candidate = (
        response.get("handoff") if isinstance(response.get("handoff"), Mapping) else response
    )
    stored: Mapping[str, Any] = candidate if isinstance(candidate, Mapping) else {}
    queued = Handoff.model_validate({**body, **stored})
    return {
        "handoff": queued.model_dump(mode="json"),
        "handoff_id": response.get("handoff_id"),
        # Stated on every handoff because it is the rule people forget: a resolved
        # handoff does not book anything. The booking still needs the patient's
        # final approval (§2.1).
        "final_approval_still_required": True,
        "evidence_ids": [],
    }
