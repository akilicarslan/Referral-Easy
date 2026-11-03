"""From one replayed journey to one run record per case (§14).

The replay harness (`scripts/eval_replay.py`) drives a scenario through the running stack and hands
this module a :class:`JourneyObservation`: what the gateway's admin view said at the end, every
delegation the gateway made with the result that came back, every tool the agents invoked, and the
wall clock. Nothing here touches a service — it is a pure function from that observation to the
:class:`~referraleasy_eval.models.RunRecord` the rules read, so the mapping is testable without a
stack and a change to it is a reviewable diff.

Three rules decide the mapping, and each is a place a capture could flatter the system:

* **A field nothing produced stays empty.** The matching agent does not populate
  `constraint_fit`, so a run's constraint outcomes are empty and the constraint rules report
  `eval_error`/`not_applicable` with that reason. Inferring satisfaction from "a candidate was
  offered" would score the expectation against itself.
* **Unknown is not satisfied.** A `constraint_fit` entry whose `satisfied` is null is recorded as
  unsatisfied with the detail `unknown`, which is the contract's own rule (`directory.json`).
* **A journey that did not complete is not a record.** An observation carrying `error` produces no
  records, so its cases report as unmeasured — a capture failure, counted apart from a low score.

The journey's `outcome` is §5's `ResultOutcome` for the whole conversation, derived from where it
settled (:func:`journey_outcome`). `delegated_roles` is the *distinct* specialists consulted, in the
order first consulted: §14's "unnecessary delegations" asks whether a specialist was consulted that
could not contribute, and a journey legitimately asks the scheduler three times.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from referraleasy_eval.models import (
    BookingRecord,
    Case,
    ConstraintOutcome,
    Escalation,
    RunRecord,
    ToolCall,
)

__all__ = [
    "DEFAULT_VARIANT",
    "DelegationObservation",
    "JourneyObservation",
    "ToolObservation",
    "journey_outcome",
    "record_for",
    "records_for",
    "variant_of",
]

#: The run every case uses unless its expectation names a `replay_variant`.
DEFAULT_VARIANT: Final = "primary"

#: Keys under which an agent output carries the evidence it rests on (`common.json#EvidenceId`).
_EVIDENCE_KEYS: Final = frozenset(
    {"evidence_ids", "evidence_id", "winning_evidence_id", "conflicting_evidence_ids", "provenance"}
)

_AWAITING_PATIENT: Final = frozenset(
    {
        "AWAITING_HOSPITAL_CHOICE",
        "AWAITING_DOCTOR_CHOICE",
        "AWAITING_SLOT_CHOICE",
        "AWAITING_FINAL_APPROVAL",
    }
)
_SELECTION_STATES: Final = {
    "AWAITING_HOSPITAL_CHOICE": "hospital_id",
    "AWAITING_DOCTOR_CHOICE": "provider_id",
    "AWAITING_SLOT_CHOICE": "slot_id",
}


@dataclass(frozen=True, slots=True)
class DelegationObservation:
    """One coordinator call the gateway made, as the recording port saw it."""

    workflow_state: str
    inputs: Mapping[str, Any]
    result: Mapping[str, Any] | None
    error: str | None = None
    elapsed_ms: int = 0


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """One governed tool call an agent made. `outcome` is `ok` or the exception class name."""

    role: str
    name: str
    arguments: Mapping[str, Any]
    outcome: str = "ok"


@dataclass(frozen=True, slots=True)
class JourneyObservation:
    """Everything one replayed journey produced. No judgement, and no field filled by default."""

    scenario_id: str
    variant: str
    workflow_id: str = ""
    final_state: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    bookings: Sequence[Mapping[str, Any]] = ()
    delegations: Sequence[DelegationObservation] = ()
    tool_calls: Sequence[ToolObservation] = ()
    latency_ms: int = 0
    model_calls: int = 0
    cost_usd: float = 0.0
    steps: Sequence[str] = ()
    #: Every card the patient answered, as `{"state", "component", "props"}`, in order.
    cards: Sequence[Mapping[str, Any]] = ()
    synthetic: bool = True
    #: Set when the journey could not be driven to a settled state. No record is produced from it.
    error: str | None = None
    #: A second run from the same pinned inputs, for scenarios that assert reproducibility.
    repeat: JourneyObservation | None = None


def variant_of(case: Case) -> str:
    return str(case.expected("replay_variant", DEFAULT_VARIANT))


def journey_outcome(final_state: str, handoff_reason: str | None) -> str:
    """§5's `ResultOutcome` for a whole journey, from where it settled.

    A handoff is split by its reason because the dataset asks two different questions of it: "no
    specialist exists" is §5's `no_match`, while a handoff for any other reason means automation
    stopped short of a result, which is `terminal_error` for the run even though a person now owns
    the referral.
    """
    if final_state == "BOOKED":
        return "completed"
    if final_state in {"BOOKING_UNVERIFIED", "BOOKING_IN_FLIGHT"}:
        return "outcome_unknown"
    if final_state in _AWAITING_PATIENT:
        return "needs_input"
    if final_state == "HUMAN_HANDOFF":
        return "no_match" if handoff_reason == "no_match" else "terminal_error"
    if final_state == "RECOVERY":
        return "retryable_error"
    if final_state in {"FAILED", "CANCELLED", "ABANDONED"}:
        return "terminal_error"
    return ""


def _outputs(observation: JourneyObservation) -> Iterable[tuple[str, Mapping[str, Any]]]:
    """Each specialist's output, in call order, as `(role, output)`."""
    for delegation in observation.delegations:
        output = (delegation.result or {}).get("output")
        if not isinstance(output, Mapping):
            continue
        results = output.get("specialist_results")
        if not isinstance(results, Mapping):
            continue
        for role, response in results.items():
            inner = response.get("output") if isinstance(response, Mapping) else None
            if isinstance(inner, Mapping):
                yield str(role), inner


def _evidence(value: Any, found: dict[str, None]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _EVIDENCE_KEYS:
                for evidence_id in item if isinstance(item, list) else [item]:
                    if isinstance(evidence_id, str) and evidence_id:
                        found.setdefault(evidence_id)
            else:
                _evidence(item, found)
    elif isinstance(value, list):
        for item in value:
            _evidence(item, found)


def _selection(payload: Mapping[str, Any]) -> dict[str, str | None]:
    chosen: dict[str, str | None] = {key: None for key in _SELECTION_STATES.values()}
    for answer in payload.get("answers") or ():
        key = _SELECTION_STATES.get(str(answer.get("state")))
        value = answer.get("value") or {}
        if key and isinstance(value, Mapping) and value.get(key) is not None:
            chosen[key] = str(value[key])
    return chosen


def _constraints(
    observation: JourneyObservation, selected: Mapping[str, str | None]
) -> tuple[ConstraintOutcome, ...]:
    preferences = (observation.payload.get("source_data") or {}).get("preferences") or {}
    kinds = {
        str(c["constraint_id"]): str(c.get("kind", ""))
        for c in preferences.get("constraints") or ()
        if not c.get("superseded_by") and c.get("constraint_id")
    }
    fits: dict[str, Mapping[str, Any]] = {}
    for role, output in _outputs(observation):
        if role != "provider_matching":
            continue
        for candidate in output.get("candidates") or ():
            if selected.get("provider_id") and candidate.get("provider_id") != selected.get(
                "provider_id"
            ):
                continue
            for fit in candidate.get("constraint_fit") or ():
                fits[str(fit.get("constraint_id"))] = fit
    outcomes = []
    for code, fit in fits.items():
        kind = kinds.get(code)
        if kind not in ("hard", "soft"):
            continue
        satisfied = fit.get("satisfied")
        outcomes.append(
            ConstraintOutcome(
                code=code,
                kind=kind,
                satisfied=satisfied is True,
                detail="unknown" if satisfied is None else str(fit.get("reason_code", "")),
            )
        )
    return tuple(outcomes)


def _documents(observation: JourneyObservation) -> tuple[tuple[str, ...], tuple[str, ...]]:
    readiness: Mapping[str, Any] | None = None
    for role, output in _outputs(observation):
        if role == "referral_readiness" and isinstance(output.get("readiness"), Mapping):
            readiness = output["readiness"]
    if readiness is None:
        return (), ()
    checks = [c for c in readiness.get("checks") or () if isinstance(c, Mapping)]
    required = tuple(str(c["kind"]) for c in checks if c.get("status") != "not_required")
    attached = tuple(str(c["kind"]) for c in checks if c.get("status") == "satisfied")
    return required, attached


def _escalation(observation: JourneyObservation) -> Escalation | None:
    payload = observation.payload
    handoff = payload.get("handoff") if isinstance(payload.get("handoff"), Mapping) else None
    draft = (
        payload.get("handoff_draft") if isinstance(payload.get("handoff_draft"), Mapping) else None
    )
    if handoff is None and draft is None and observation.final_state != "HUMAN_HANDOFF":
        return None
    reason = str((handoff or {}).get("reason_class") or (draft or {}).get("reason_class") or "")
    handoff_id = str((handoff or {}).get("handoff_id") or f"draft:{observation.workflow_id}")
    return Escalation(handoff_id=handoff_id, reason_code=reason, ticket_id=None)


def _bookings(observation: JourneyObservation) -> tuple[BookingRecord, ...]:
    payload = observation.payload.get("booking_payload") or {}
    return tuple(
        BookingRecord(
            booking_id=str(row.get("booking_id", "")),
            state=str(row.get("state", "")),
            provider_id=str(payload.get("provider_id", "")),
            hospital_id=str(payload.get("hospital_id", "")),
            slot_id=str(payload.get("slot_id", "")),
            approval_id=str(row["approval_id"]) if row.get("approval_id") else None,
        )
        for row in observation.bookings
    )


def _fingerprint(observation: JourneyObservation) -> tuple[Any, ...]:
    selected = _selection(observation.payload)
    return (
        observation.final_state,
        selected["hospital_id"],
        selected["provider_id"],
        selected["slot_id"],
        len(observation.bookings),
    )


def record_for(case: Case, observation: JourneyObservation) -> RunRecord:
    """The record this case is scored against. The observation must be the case's own run."""
    payload = observation.payload
    selected = _selection(payload)
    escalation = _escalation(observation)
    required, attached = _documents(observation)
    evidence: dict[str, None] = {}
    for _, output in _outputs(observation):
        _evidence(output, evidence)
    authority = payload.get("booking_authority") or {}
    tool_calls = tuple(
        ToolCall(name=call.name, arguments=dict(call.arguments), outcome=call.outcome)
        for call in observation.tool_calls
    )
    roles: dict[str, None] = {}
    for delegation in observation.delegations:
        for role in delegation.inputs:
            roles.setdefault(str(role))
    outcome = journey_outcome(
        observation.final_state, escalation.reason_code if escalation else None
    )
    if observation.repeat is not None and _fingerprint(observation) != _fingerprint(
        observation.repeat
    ):
        # Two runs from one pinned input set that settle differently are not one observation.
        outcome = "diverged"
    return RunRecord(
        case_id=case.case_id,
        workflow_id=observation.workflow_id,
        outcome=outcome,
        final_state=observation.final_state,
        selected_provider_id=selected["provider_id"],
        selected_hospital_id=selected["hospital_id"],
        selected_slot_id=selected["slot_id"],
        bookings=_bookings(observation),
        tool_calls=tool_calls,
        delegated_roles=tuple(roles),
        evidence_ids=tuple(evidence),
        constraints=_constraints(observation, selected),
        escalation=escalation,
        required_document_kinds=required,
        attached_document_kinds=attached,
        live_approval_ids=(str(authority["approval_id"]),) if authority.get("approval_id") else (),
        booking_attempts=sum(1 for call in tool_calls if call.name == "create_booking"),
        latency_ms=observation.latency_ms,
        model_calls=observation.model_calls,
        tool_calls_made=len(tool_calls),
        cost_usd=observation.cost_usd,
        synthetic=observation.synthetic,
    )


def records_for(
    cases: Iterable[Case], observations: Mapping[tuple[str, str], JourneyObservation]
) -> tuple[RunRecord, ...]:
    """One record per case whose run completed. A case with no completed run gets no record."""
    records = []
    for case in cases:
        observation = observations.get((case.scenario_id, variant_of(case)))
        if observation is None or observation.error is not None:
            continue
        records.append(record_for(case, observation))
    return tuple(records)
