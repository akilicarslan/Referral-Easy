"""Scheduling's five tools (§3) — the only booking write authority in the platform.

Three of §3's rules are enforced here, in code, and each has a test:

1. **"cannot write without a valid approval."** :func:`create_booking` checks the
   approval's payload hash against the exact payload being written, its
   `state_version` against the envelope's `current_state_version`, its `expires_at`
   against the present, and its `consumed` flag. Every failure raises
   :class:`NoApprovalRefused`. The check does not consult the model: the model
   supplies the approval *object*, the authority for "is it live" is the hash, the
   version and the clock.
2. **"on a timeout it cannot create a duplicate under a new key."** A transport
   failure on the write path raises :class:`BookingOutcomeUnknown`
   (`outcome_unknown`), never the retryable one; and both `create_booking` and
   `lookup_request` refuse a key that disagrees with the key the *turn* carries.
   The model cannot swap keys, so the recovery path is the same-key lookup.
3. **"the system never auto-selects a slot and never pre-books."**
   :func:`list_slots` returns the slots and the availability route's `result_kind`
   and has no field that could carry a choice. There is no `selected` key to leave
   unset — a caller that wants a choice records `PatientChoice` somewhere else, on
   a patient's decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from referraleasy_agents.canonical import id_text, payload_hash
from referraleasy_agents.errors import (
    BookingOutcomeUnknown,
    DuplicateBookingKeyRefused,
    NoApprovalRefused,
    UnknownDataError,
)
from referraleasy_agents.tools.call import (
    ToolCall,
    WriteGuardSubject,
    require_command_id,
    require_workflow_id,
)
from referraleasy_contracts.generated.booking import (
    AppointmentRecord,
    BookingAttempt,
    BookingPayload,
    BookingRequest,
    ReconciliationState,
)
from referraleasy_contracts.generated.directory import AvailabilityResult

__all__ = [
    "MIN_IDEMPOTENCY_KEY_LENGTH",
    "cancel_booking",
    "check_approval",
    "create_booking",
    "enforce_idempotency_key",
    "list_slots",
    "lookup_request",
    "reschedule_booking",
]

#: The shortest idempotency key the mock API accepts, mirrored rather than guessed.
MIN_IDEMPOTENCY_KEY_LENGTH = 8

#: Transport statuses on the write path that do not establish "nothing happened".
#: 504 is the mock API's own `booking_outcome_unknown`; the other 5xx and a dropped
#: connection are treated the same way, because the conservative reading of "the
#: request may have arrived" is that it may have arrived. See `tools/client.py` for
#: why the body's `detail.reconcile_with` hint is not available here.
_WRITE_FAILURE_STATUSES = frozenset({408, 500, 502, 503, 504})


def _canonical_key(key: str | None) -> str | None:
    text = (key or "").strip()
    return text or None


def enforce_idempotency_key(call: WriteGuardSubject, key: str | None, *, tool: str) -> str:
    """The key for this call, refusing a second one for the same work (§7).

    The turn's structured input may already carry an idempotency key — the one the
    workflow minted for this booking request. When it does, that is the only key
    this tool will use: a model that supplies a different one is asking to create a
    second appointment, whether it means to or not.
    """
    supplied = _canonical_key(key)
    if supplied is None:
        raise NoApprovalRefused(
            f"{tool} needs the booking request's idempotency key; without one there is no way to "
            "replay this request or to reconcile it after a timeout (§7).",
            role=call.role.value,
            tool=tool,
            workflow_state=call.workflow_state,
        )
    if len(supplied) < MIN_IDEMPOTENCY_KEY_LENGTH:
        raise NoApprovalRefused(
            f"idempotency key {supplied!r} is shorter than {MIN_IDEMPOTENCY_KEY_LENGTH} characters, "
            "which the booking API refuses; the write would be rejected and the turn spent for "
            "nothing.",
            role=call.role.value,
            tool=tool,
            workflow_state=call.workflow_state,
        )
    original = _canonical_key(str(call.payload.get("idempotency_key") or "") or None)
    if original is not None and original != supplied:
        raise DuplicateBookingKeyRefused(
            f"{tool} was called with key {supplied!r} but this workflow's booking request has key "
            f"{original!r}. A different key is a different appointment; reconcile the original with "
            "lookup_request using the key it was created under (§3, §7).",
            role=call.role.value,
            tool=tool,
            workflow_state=call.workflow_state,
        )
    return supplied


def check_approval(
    call: WriteGuardSubject, payload: Mapping[str, Any], approval: Mapping[str, Any] | None
) -> str | None:
    """Refuse the write unless a live, matching approval is present.

    Returns the approval id (which may be absent — the approval object is what
    travels) so the caller can pass it to the API.
    """
    refusals: dict[str, Any] = {
        "role": call.role.value,
        "tool": "create_booking",
        "workflow_state": call.workflow_state,
    }
    if call.envelope is None:
        raise NoApprovalRefused(
            "create_booking has no task envelope, so there is no server-side state version to check "
            "the approval against. Default-deny: without that fact the write is refused (§2.1).",
            **refusals,
        )
    if approval is None:
        raise NoApprovalRefused(
            "create_booking was called with no approval. Booking write authority is not permission to "
            "write: the patient's explicit approval, bound to this payload, is required first (§2.1, §3).",
            **refusals,
        )

    expected = payload_hash(payload)
    claimed = str(approval.get("payload_hash") or "")
    if claimed != expected:
        raise NoApprovalRefused(
            "the approval's payload_hash does not match the payload being written "
            f"(approval {claimed or '<absent>'}, payload {expected}). The approval binds one exact "
            "payload; anything else is `approval_payload_changed` (§5, §7).",
            **refusals,
        )

    approval_version = approval.get("state_version")
    expected_version = call.current_state_version or -1
    if call.workflow_state == "BOOKING_IN_FLIGHT":
        expected_version -= 1
    if approval_version is None or int(approval_version) != expected_version:
        raise NoApprovalRefused(
            f"the approval was taken at state version {approval_version} but the workflow is at "
            f"{call.current_state_version}. A stale approval no longer describes what the patient "
            "agreed to (§5: `approval_expired`).",
            **refusals,
        )

    expires_at = approval.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at.strip():
        raise NoApprovalRefused(
            "the approval carries no expires_at. An approval with no expiry is one that can be spent "
            "at any time in the future, which is not what the patient gave (§2.1).",
            **refusals,
        )
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise NoApprovalRefused(
            f"the approval's expires_at {expires_at!r} is not a parseable timestamp, so the approval "
            "cannot be shown to be live.",
            **refusals,
        ) from error
    if expires <= datetime.now(UTC):
        raise NoApprovalRefused(
            f"the approval expired at {expires_at} and is not usable (§5: `approval_expired`).",
            **refusals,
        )

    if approval.get("consumed") is True:
        raise NoApprovalRefused(
            "the approval has already been consumed by another booking request; reusing it would make "
            "one consent cover two appointments (§2.1).",
            **refusals,
        )
    if not approval.get("granted_by"):
        raise NoApprovalRefused(
            "the approval does not say who granted it. Only an explicit patient (or human operator) "
            "decision is an approval, and an unattributed one cannot be verified (§2.4).",
            **refusals,
        )
    return str(approval.get("approval_id")) if approval.get("approval_id") else None


async def list_slots(
    call: ToolCall,
    *,
    hospital_id: str,
    provider_id: str,
    visit_type: str,
    earliest: str | None = None,
    latest: str | None = None,
    preferred_days: Sequence[str] | None = None,
) -> dict[str, Any]:
    """List the slots for one provider at one hospital. Never chooses one.

    `result_kind` is returned alongside the slots because the route refuses to
    collapse two different answers: `no_slots_for_constraints` means "we asked and
    found nothing" and `upstream_error` means "we could not ask". The second is
    `outcome_unknown`-shaped, and a caller that read `slots: []` alone would report
    an outage to the patient as a fact about the hospital.
    """
    body = await call.api.get(
        "/availability",
        params={
            "hospital_id": hospital_id,
            "provider_id": provider_id,
            "visit_type": visit_type,
            "earliest": earliest,
            "latest": latest,
            "preferred_days": ",".join(preferred_days) if preferred_days else None,
        },
        budget=call.budget,
        lease=call.lease,
    )
    availability = AvailabilityResult.model_validate(body)
    return {
        "slots": [slot.model_dump(mode="json") for slot in availability.slots],
        "result_kind": availability.result_kind.value,
        "partial": availability.partial,
        "availability": availability.model_dump(mode="json"),
        # No `selected_slot`, no `recommended`, no ordering-by-preference: the
        # patient chooses, and a field here would be the system choosing (§4).
        "evidence_ids": [id_text(availability.evidence_id)],
    }


async def create_booking(
    call: ToolCall,
    *,
    idempotency_key: str,
    payload: Mapping[str, Any],
    approval: Mapping[str, Any] | None = None,
    expected_slot_version: int | None = None,
) -> dict[str, Any]:
    """Write the booking request the patient approved, exactly once.

    The payload is validated against `BookingPayload` before anything is sent, so a
    write cannot carry a field the contract does not have — and the hash the
    approval is checked against is taken over the *validated* payload, which is what
    makes the binding reproducible by the side that minted the approval.

    The workflow and the command are **not tool arguments**: they come from the task
    envelope (`require_workflow_id`, `require_command_id`). A model that could name
    them could file a booking under a different workflow's records, and the write
    would still be approval-valid — the approval binds the payload, not the address
    it is sent to. ``payload`` here is this tool's own subject matter, the
    approval-bound booking body, and not the turn's input document of the same name
    on `ToolCall`.
    """
    key = enforce_idempotency_key(call, idempotency_key, tool="create_booking")
    booking_payload = BookingPayload.model_validate(dict(payload))
    canonical = booking_payload.model_dump(mode="json", exclude_none=True)
    # The approval guard runs first: with no envelope there is no state version to
    # bind to, and `check_approval` is where that refusal belongs. The identifiers
    # below then read the same envelope.
    approval_id = check_approval(call, canonical, approval)
    workflow_id = require_workflow_id(call)
    command_id = require_command_id(call)

    body: dict[str, Any] = {
        **canonical,
        "workflow_id": workflow_id,
        "command_id": command_id,
        "idempotency_key": key,
    }
    if approval_id is not None:
        body["approval_id"] = approval_id
    if expected_slot_version is not None:
        body["expected_slot_version"] = expected_slot_version

    try:
        response = await call.api.post("/appointments", body, budget=call.budget, lease=call.lease)
    except Exception as error:
        raise _write_failure(error, key=key) from error

    return _booking_result(response)


async def lookup_request(call: ToolCall, *, idempotency_key: str) -> dict[str, Any]:
    """Reconcile a booking whose outcome is unknown — with the same key.

    This is the only sanctioned next step after :class:`BookingOutcomeUnknown`, and
    it is a lookup rather than a retry: it asks what happened instead of trying to
    make something happen.
    """
    key = enforce_idempotency_key(call, idempotency_key, tool="lookup_request")
    body = await call.api.get(
        "/appointments/lookup_request",
        params={"idempotency_key": key},
        budget=call.budget,
        lease=call.lease,
    )
    booking = body.get("booking")
    if isinstance(booking, Mapping):
        BookingRequest.model_validate(booking)
    appointment = body.get("appointment")
    if isinstance(appointment, Mapping):
        AppointmentRecord.model_validate(appointment)
    reconciliation = body.get("reconciliation")
    if isinstance(reconciliation, Mapping):
        ReconciliationState.model_validate(reconciliation)
    lookup = body.get("lookup") or {}
    return {
        "outcome": body.get("outcome"),
        "lookup_outcome": lookup.get("outcome") if isinstance(lookup, Mapping) else None,
        "lookup": lookup,
        "booking": booking,
        "appointment": appointment,
        "reconciliation": reconciliation,
        "attempts": body.get("attempts") or [],
        "idempotency_key": key,
        "evidence_ids": [],
    }


async def cancel_booking(
    call: ToolCall, *, booking_request_id: str, reason: str | None = None
) -> dict[str, Any]:
    """Cancel a booking request, and report the state it actually reached.

    The key is derived from the booking request rather than minted per call, so a
    retried cancellation replays instead of cancelling something twice. A
    cancellation is not finished when it is requested: the response's
    `booking.state` is the only authority, which is why it is returned whole.
    """
    body: dict[str, Any] = {"idempotency_key": f"re-cancel-{booking_request_id}"}
    if reason:
        body["reason"] = reason
    response = await call.api.post(
        f"/appointments/{booking_request_id}/cancel", body, budget=call.budget, lease=call.lease
    )
    booking = response.get("booking")
    if isinstance(booking, Mapping):
        BookingRequest.model_validate(booking)
    return {
        "outcome": response.get("outcome"),
        "booking": booking,
        "appointment": response.get("appointment"),
        "idempotent_replay": response.get("idempotent_replay"),
        "evidence_ids": [],
    }


async def reschedule_booking(
    call: ToolCall,
    *,
    booking_request_id: str,
    new_slot_id: str,
    idempotency_key: str,
    reason: str | None = None,
    expected_slot_version: int | None = None,
) -> dict[str, Any]:
    """Move a booking to another slot, replacement first.

    The mock API's `old_booking_cancelled` says whether the original was actually
    cancelled — true only when the replacement was verified first. That flag is
    returned rather than interpreted: a caller that reported "moved" without it
    would be describing a failed move as a success.
    """
    key = enforce_idempotency_key(call, idempotency_key, tool="reschedule_booking")
    body: dict[str, Any] = {"new_slot_id": new_slot_id, "idempotency_key": key}
    if reason:
        body["reason"] = reason
    if expected_slot_version is not None:
        body["expected_slot_version"] = expected_slot_version
    try:
        response = await call.api.post(
            f"/appointments/{booking_request_id}/reschedule",
            body,
            budget=call.budget,
            lease=call.lease,
        )
    except Exception as error:
        raise _write_failure(error, key=key) from error
    booking = response.get("booking")
    if isinstance(booking, Mapping):
        BookingRequest.model_validate(booking)
    return {
        "outcome": response.get("outcome"),
        "booking": booking,
        "appointment": response.get("appointment"),
        "superseded_booking": response.get("superseded_booking"),
        "old_booking_cancelled": response.get("old_booking_cancelled"),
        "cancellation_deferred_reason": response.get("cancellation_deferred_reason"),
        "idempotent_replay": response.get("idempotent_replay"),
        "evidence_ids": [],
    }


def _write_failure(error: BaseException, *, key: str) -> BaseException:
    """Classify a failed write: "unknown" stays unknown, and nothing else is retried.

    A transport fault on the write path may have delivered the request, so it
    becomes :class:`BookingOutcomeUnknown` — which is `outcome_unknown`, not
    `retryable_error`. A 4xx is left as the runtime classified it: the request was
    understood and refused, no booking exists, and re-running the turn may fix it.
    """
    from referraleasy_agent_runtime import UpstreamRejected, UpstreamTransient

    if isinstance(error, UpstreamTransient) and (
        error.status_code is None or error.status_code in _WRITE_FAILURE_STATUSES
    ):
        return BookingOutcomeUnknown(
            f"the booking write on key {key!r} failed without establishing whether it was "
            f"received ({error}). Do not write again: call lookup_request with the same key.",
            idempotency_key=key,
            status_code=error.status_code,
        )
    if isinstance(error, UpstreamRejected):
        return error
    return error


def _booking_result(response: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a create-booking response against the published booking contracts."""
    booking = response.get("booking")
    if not isinstance(booking, Mapping):
        raise UnknownDataError(
            "the booking API answered without a `booking` object. The write's state is the only "
            "authority on whether an appointment exists, and it is missing."
        )
    BookingRequest.model_validate(booking)
    appointment = response.get("appointment")
    if isinstance(appointment, Mapping):
        AppointmentRecord.model_validate(appointment)
    attempts = [
        BookingAttempt.model_validate(attempt).model_dump(mode="json")
        for attempt in response.get("attempts") or []
        if isinstance(attempt, Mapping)
    ]
    return {
        "outcome": response.get("outcome"),
        "booking": booking,
        "appointment": appointment,
        "attempts": attempts,
        # Transport fact, never a confirmation: "was this served from the stored key?"
        "idempotent_replay": response.get("idempotent_replay"),
        "evidence_ids": [],
    }
