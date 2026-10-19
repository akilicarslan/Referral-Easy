"""Resume accepted patient commands through the standard referral journey.

Source data, specialist results, patient answers and pending work have separate keys.
Every transition and booking uses CommandService. Only patient answers select options.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from referraleasy_contracts.generated.booking import BookingPayload, BookingRequest
from referraleasy_contracts.generated.coverage import CoverageResult, InsurancePlan
from referraleasy_contracts.generated.directory import AccessibilityFeature, ProviderCandidate, Slot
from referraleasy_contracts.generated.documents import ReadinessResult
from referraleasy_contracts.generated.patient import Constraint, ReferralIntake
from referraleasy_gateway.auth import Session, SessionKind
from referraleasy_gateway.commands import CommandService
from referraleasy_gateway.continuations import (
    ContinuationClaim,
    ContinuationExpired,
    ContinuationKind,
    ContinuationLifecycle,
    ContinuationReplaced,
)
from referraleasy_gateway.control import ControlState, WorkflowRow
from referraleasy_gateway.timing import Clock

_log = logging.getLogger(__name__)


class CoordinatorPort(Protocol):
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
    ) -> Mapping[str, Any]: ...


class CoordinationError(ValueError):
    """The source data or a specialist result cannot support the next step."""


def _id(*parts: Any) -> str:
    return str(uuid5(NAMESPACE_URL, "referraleasy:coordination:" + ":".join(map(str, parts))))


class WorkflowInputs:
    """Build each specialist's minimum input from recorded source data."""

    def __init__(self, row: WorkflowRow) -> None:
        self.row = row
        self.sources = dict(row.payload.get("source_data", {}))
        self.referral = dict(self.sources.get("referral", {}))
        self.preferences = dict(self.sources.get("preferences", {}))
        if self.referral.get("patient_id") != row.patient_ref:
            raise CoordinationError("A referral for this patient is required.")
        if self.preferences.get("patient_id") != row.patient_ref:
            raise CoordinationError("Structured preferences for this patient are required.")
        self.constraints = [
            Constraint.model_validate(c).model_dump(mode="json")
            for c in self.preferences.get("constraints", [])
            if not c.get("superseded_by")
        ]
        for constraint in self.constraints:
            field, value = constraint["field"], constraint["value"]
            if field == "max_distance_miles":
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
            elif field == "accepts_new_patients":
                valid = type(value) is bool
            elif field == "preferred_day":
                valid = isinstance(value, str) and value.lower() in {
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                }
            elif field == "preferred_time_of_day":
                valid = value in {"morning", "afternoon", "evening"}
            elif field == "accessibility":
                valid = value in {feature.value for feature in AccessibilityFeature}
            elif field in {"specialty", "visit_type"}:
                valid = value == self.referral.get(field)
            else:
                valid = isinstance(value, str) and bool(value.strip())
            if not valid:
                raise CoordinationError(f"The recorded {field} constraint needs review.")
        for field in ("insurance_plan", "accepts_new_patients"):
            if len({c["value"] for c in self.constraints if c["field"] == field}) > 1:
                raise CoordinationError(f"The recorded {field} constraints conflict.")
        self.plan_id = next(
            (c["value"] for c in self.constraints if c["field"] == "insurance_plan"), None
        )

    def choice(self, state: str) -> str | None:
        answer = next(
            (a for a in reversed(self.row.payload.get("answers", [])) if a.get("state") == state),
            None,
        )
        if not answer:
            return None
        value = answer["value"]
        selection_key = {
            "AWAITING_HOSPITAL_CHOICE": "hospital_id",
            "AWAITING_DOCTOR_CHOICE": "provider_id",
            "AWAITING_SLOT_CHOICE": "slot_id",
        }.get(state, "selected_id")
        return next(
            (
                str(value[key])
                for key in (selection_key, "selected_id", "option_id", "id")
                if value.get(key) is not None
            ),
            None,
        )

    def scheduling(self) -> dict[str, Any]:
        hospital = self.choice("AWAITING_HOSPITAL_CHOICE")
        provider = self.choice("AWAITING_DOCTOR_CHOICE")
        if not hospital or not provider or not self.referral.get("visit_type"):
            raise CoordinationError("Choose a hospital and doctor before requesting times.")
        return {
            "hospital_id": hospital,
            "provider_id": provider,
            "visit_type": self.referral["visit_type"],
            "patient_details_id": self.sources.get("patient_details_id"),
            "preferred_days": [
                str(c["value"]) for c in self.constraints if c["field"] == "preferred_day"
            ],
        }


class CoordinationService:
    """Claim stored work, run bounded steps, and stop when a patient must answer."""

    def __init__(
        self,
        *,
        control: ControlState,
        commands: CommandService,
        coordinator: CoordinatorPort,
        clock: Clock,
    ) -> None:
        self.control = control
        self.commands = commands
        self.coordinator = coordinator
        self.clock = clock
        self.continuations = ContinuationLifecycle(control, clock=clock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        commands.enable_coordination()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="referral-coordination"
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_pending()
            except Exception:
                _log.exception("Could not read pending coordination work")
            self._stop.wait(0.25)

    def run_pending(self) -> int:
        count = 0
        for claim in self.continuations.claim_pending():
            count += 1
            try:
                self._resume(claim)
            except ContinuationReplaced:
                continue
            except Exception as error:
                _log.exception("Coordination failed for workflow %s", claim.workflow_id)
                current = self.continuations.recoverable(claim)
                if current is None:
                    continue
                self._recover(current, str(error))
            self.continuations.finish(claim)
        return count

    def _session(self) -> Session:
        now = self.clock.now()
        return Session(
            subject="autonomous-coordinator",
            kind=SessionKind.admin,
            audience="internal",
            patient_ref=None,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            token_fingerprint="internal",
        )

    def _store(self, row: WorkflowRow, **patch: Any) -> WorkflowRow:
        return self.control.update_payload(
            row.workflow_id, expected_state_version=row.state_version, patch=patch
        )

    def _move(
        self, row: WorkflowRow, state: str, props: Mapping[str, Any] | None = None
    ) -> WorkflowRow:
        self.commands.advance(
            self._session(),
            row.workflow_id,
            command_id=_id(row.workflow_id, row.state_version, state),
            expected_state_version=row.state_version,
            to_state=state,
            props=props,
        )
        moved = self.control.load(row.workflow_id)
        assert moved is not None
        return moved

    def _call(
        self, claim: ContinuationClaim, row: WorkflowRow, inputs: dict[str, Any], *, step: str
    ) -> dict[str, Any]:
        plan = [
            {
                "step": role,
                "target": role,
                "goal": f"Run {step} from the supplied sources.",
                "independent": len(inputs) == 2
                and set(inputs) == {"coverage_network", "provider_matching"},
            }
            for role in inputs
        ]
        result = self.coordinator.execute(
            workflow_id=row.workflow_id,
            workflow_state=row.state,
            state_version=row.state_version,
            command_id=_id(row.workflow_id, row.state_version, step),
            specialist_inputs=inputs,
            execution_plan=plan,
            patient_details_id=row.payload.get("source_data", {}).get("patient_details_id"),
        )
        row = self.continuations.refresh(claim)
        output = result.get("output")
        results = output.get("specialist_results") if isinstance(output, Mapping) else None
        if not isinstance(results, Mapping) or set(results) != set(inputs):
            raise CoordinationError("A specialist did not return a complete result.")
        answers: dict[str, Any] = {}
        for role, response in results.items():
            if not isinstance(response, Mapping) or not isinstance(response.get("output"), Mapping):
                raise CoordinationError(f"The {role} result needs review.")
            answers[role] = dict(response["output"])
        # Store results apart from the source records and the conversation.
        self._store(
            row, specialist_results={**row.payload.get("specialist_results", {}), **answers}
        )
        return answers

    def _resume(self, claim: ContinuationClaim) -> None:
        for _ in range(12):
            row = self.continuations.refresh(claim)
            self.commands.repair_surface(row)
            if row.state in {"BOOKED", "HUMAN_HANDOFF"}:
                break
            source = WorkflowInputs(row)
            if row.state == "INTAKE":
                answers = self._call(
                    claim,
                    row,
                    {
                        "referral_intake": {
                            "referral_source": json.dumps(source.referral),
                            "patient_id": row.patient_ref,
                            "document_ids": source.referral.get("document_ids", []),
                        }
                    },
                    step="intake",
                )
                intake = answers["referral_intake"]
                if (
                    not intake.get("completed_intake")
                    or intake.get("contradictions")
                    or intake["draft"].get("injection_attempt_detected")
                ):
                    raise CoordinationError("The referral has missing or conflicting fields.")
                ReferralIntake.model_validate(intake["completed_intake"])
                self._move(row, "PROVIDER_MATCHING")
            elif row.state == "PROVIDER_MATCHING":
                inputs: dict[str, Any] = {
                    "provider_matching": {
                        "specialty": source.referral["specialty"],
                        "origin": source.preferences["search_origin"],
                        "constraints": source.constraints,
                        "plan_id": source.plan_id,
                    }
                }
                if source.plan_id:
                    inputs["coverage_network"] = {"plan_id": source.plan_id}
                answers = self._call(claim, row, inputs, step="search")
                if source.plan_id:
                    InsurancePlan.model_validate(answers["coverage_network"]["plan"])
                candidates = answers["provider_matching"]["candidates"]
                for candidate in candidates:
                    ProviderCandidate.model_validate(candidate)
                if not candidates:
                    raise CoordinationError("No provider matches the recorded preferences.")
                row = self.control.load(row.workflow_id) or row
                hospitals = list(dict.fromkeys(c["hospital_id"] for c in candidates))
                labels = answers["provider_matching"].get("directory_labels", {})
                self._move(
                    row,
                    "AWAITING_HOSPITAL_CHOICE",
                    {"options": [{"id": key, "label": labels.get(key, key)} for key in hospitals]},
                )
                break
            elif row.state == "AWAITING_HOSPITAL_CHOICE":
                hospital = source.choice(row.state)
                if hospital is None:
                    break
                candidates = row.payload["specialist_results"]["provider_matching"]["candidates"]
                labels = row.payload["specialist_results"]["provider_matching"].get(
                    "directory_labels", {}
                )
                providers = [c["provider_id"] for c in candidates if c["hospital_id"] == hospital]
                if not providers:
                    raise CoordinationError("The chosen hospital has no offered doctor.")
                self._move(
                    row,
                    "AWAITING_DOCTOR_CHOICE",
                    {"options": [{"id": key, "label": labels.get(key, key)} for key in providers]},
                )
                break
            elif row.state == "AWAITING_DOCTOR_CHOICE":
                if source.choice(row.state) is None:
                    break
                schedule = source.scheduling()
                if source.plan_id:
                    answers = self._call(
                        claim,
                        row,
                        {
                            "coverage_network": {
                                "plan_id": source.plan_id,
                                "hospital_id": schedule["hospital_id"],
                                "provider_id": schedule["provider_id"],
                            }
                        },
                        step="provider-network",
                    )
                    coverage = CoverageResult.model_validate(
                        answers["coverage_network"]["coverage"]
                    )
                    if (
                        coverage.in_network_for_plan is not True
                        or coverage.conflict
                        or coverage.freshness.state.value != "fresh"
                    ):
                        raise CoordinationError(
                            "The provider network status needs review. Coverage is not a payment guarantee."
                        )
                self._move(row, "READINESS_CHECK")
            elif row.state == "READINESS_CHECK":
                schedule = source.scheduling()
                answers = self._call(
                    claim,
                    row,
                    {
                        "referral_readiness": {
                            "hospital_id": schedule["hospital_id"],
                            "patient_id": row.patient_ref,
                            "plan_id": source.plan_id,
                            "visit_type": schedule["visit_type"],
                            "document_ids": source.referral.get("document_ids", []),
                        }
                    },
                    step="readiness",
                )
                ready = ReadinessResult.model_validate(answers["referral_readiness"]["readiness"])
                if not ready.can_proceed:
                    raise CoordinationError(
                        "The chosen hospital needs a complete referral document package."
                    )
                answers = self._call(claim, row, {"scheduling": schedule}, step="slots")
                slots = self._slots(answers["scheduling"]["slots"], source)
                if not slots:
                    raise CoordinationError("No appointment time matches the recorded preferences.")
                row = self.control.load(row.workflow_id) or row
                self._store(row, offered_slots=slots)
                self._move(
                    row,
                    "AWAITING_SLOT_CHOICE",
                    {
                        "options": [
                            {"id": slot["slot_id"], "label": slot["start_at"], **slot}
                            for slot in slots
                        ]
                    },
                )
                break
            elif row.state == "AWAITING_SLOT_CHOICE":
                selected = source.choice(row.state)
                if selected is None:
                    break
                answers = self._call(
                    claim, row, {"scheduling": source.scheduling()}, step="validate-slot"
                )
                slots = self._slots(answers["scheduling"]["slots"], source)
                slot = next((s for s in slots if s["slot_id"] == selected), None)
                previous = next(
                    (s for s in row.payload.get("offered_slots", []) if s["slot_id"] == selected),
                    None,
                )
                if slot is None or previous is None or slot["version"] != previous["version"]:
                    raise CoordinationError(
                        "The chosen appointment time changed. Choose a current time before approval."
                    )
                payload = BookingPayload.model_validate(
                    {
                        "patient_id": row.patient_ref,
                        "hospital_id": slot["hospital_id"],
                        "provider_id": slot["provider_id"],
                        "slot_id": selected,
                        "visit_type": slot["visit_type"],
                        "document_ids": source.referral.get("document_ids", []),
                    }
                ).model_dump(mode="json", exclude_none=True)
                self._store(row, selected_slot_version=slot["version"])
                self._move(row, "AWAITING_FINAL_APPROVAL", {"booking_payload": payload})
                break
            elif row.state == "AWAITING_FINAL_APPROVAL":
                if claim.kind is not ContinuationKind.BOOKING_CONSENT:
                    break
                approval = self.commands.prepare_booking(row)
                self._store(
                    row,
                    booking_authority=approval,
                    booking_idempotency_key=_id(row.workflow_id, approval["approval_id"]),
                    booking_screening_text=row.payload["messages"][-1]["text"],
                )
                self._move(row, "BOOKING_IN_FLIGHT")
            elif row.state == "BOOKING_IN_FLIGHT":
                self._book(claim, row, source)
                break
            elif row.state == "BOOKING_UNVERIFIED":
                self._reconcile(claim, row, source)
                break
            else:
                break
        else:
            raise CoordinationError("The coordination step limit was reached.")
        row = self.control.load(row.workflow_id) or row

    @staticmethod
    def _slots(slots: list[dict[str, Any]], source: WorkflowInputs) -> list[dict[str, Any]]:
        accepted = []
        schedule = source.scheduling()
        days = {
            str(c["value"]).lower() for c in source.constraints if c["field"] == "preferred_day"
        }
        periods = {c["value"] for c in source.constraints if c["field"] == "preferred_time_of_day"}
        for raw in slots:
            slot = Slot.model_validate(raw)
            if (
                raw["hospital_id"] != schedule["hospital_id"]
                or raw["provider_id"] != schedule["provider_id"]
                or raw["visit_type"] != schedule["visit_type"]
            ):
                raise CoordinationError("A returned slot belongs to a different selection.")
            if slot.status.value != "available":
                continue
            local = datetime.fromisoformat(raw["start_at"].replace("Z", "+00:00")).astimezone(
                ZoneInfo(raw["display_timezone"])
            )
            period = "morning" if local.hour < 12 else "afternoon" if local.hour < 17 else "evening"
            if (not days or local.strftime("%A").lower() in days) and (
                not periods or period in periods
            ):
                accepted.append(raw)
        return accepted

    def _book(self, claim: ContinuationClaim, row: WorkflowRow, source: WorkflowInputs) -> None:
        schedule = source.scheduling()
        already_sent = bool(row.payload.get("booking_sent"))
        if not already_sent:
            self.commands.assert_booking_writes()
        self._store(row, booking_sent=True)
        schedule.update(
            idempotency_key=row.payload["booking_idempotency_key"],
            booking_payload=row.payload["booking_payload"],
            approval=row.payload["booking_authority"],
            expected_slot_version=row.payload["selected_slot_version"],
            reconcile_only=already_sent,
        )
        booking: dict[str, Any] | None = None
        output: Mapping[str, Any] = {}
        try:
            answers = self._call(
                claim,
                row,
                {"scheduling": schedule},
                step="booking-lookup" if already_sent else "booking",
            )
            output = answers["scheduling"]
            booking = output.get("booking_request")
            if booking:
                parsed = BookingRequest.model_validate(booking)
                if parsed.idempotency_key != schedule["idempotency_key"]:
                    raise CoordinationError("The booking result has a different request key.")
                if (
                    parsed.payload.model_dump(mode="json", exclude_none=True)
                    != row.payload["booking_payload"]
                ):
                    raise CoordinationError("The booking result has a different approved payload.")
        except (ContinuationReplaced, ContinuationExpired):
            raise
        except Exception:
            _log.exception("The hospital booking outcome needs reconciliation")
            booking = None
        confirmed = bool(
            booking
            and booking["state"] == "CONFIRMED"
            and self._verified(output, row.payload["booking_payload"])
        )
        self.continuations.refresh(claim)
        self.commands.book(
            self._session(),
            row.workflow_id,
            command_id=_id(
                row.workflow_id, row.payload["booking_authority"]["approval_id"], "record-booking"
            ),
            approval_id=UUID(row.payload["booking_authority"]["approval_id"]),
            expected_state_version=row.state_version,
            booking_payload=row.payload["booking_payload"],
            screening_text=row.payload["booking_screening_text"],
            outcome="confirmed" if confirmed else "unverified",
            external_ref=booking.get("external_reference") if confirmed and booking else None,
        )

    def _reconcile(
        self, claim: ContinuationClaim, row: WorkflowRow, source: WorkflowInputs
    ) -> None:
        schedule = source.scheduling()
        schedule["idempotency_key"] = row.payload["booking_idempotency_key"]
        answers = self._call(claim, row, {"scheduling": schedule}, step="reconcile")
        output = answers["scheduling"]
        attempts = int(row.payload.get("reconciliation_attempts", 0)) + 1
        self._store(row, reconciliation_attempts=attempts)
        if output.get("lookup_outcome") == "not_booked":
            raise CoordinationError(
                "The hospital reports no appointment. Review the original request before another booking attempt."
            )
        booking = output.get("booking_request")
        if not booking:
            raise CoordinationError(
                "The original booking remains unknown. A coordinator must review it before any new request."
            )
        parsed = BookingRequest.model_validate(booking)
        if (
            parsed.idempotency_key != schedule["idempotency_key"]
            or parsed.payload.model_dump(mode="json", exclude_none=True)
            != row.payload["booking_payload"]
        ):
            raise CoordinationError("The lookup returned a different booking request.")
        if booking["state"] == "CONFIRMED" and self._verified(
            output, row.payload["booking_payload"]
        ):
            self.commands.record_confirmation(row, external_ref=booking["external_reference"])
        else:
            if attempts >= 3:
                raise CoordinationError(
                    "The original booking remains unverified after three checks."
                )
            self.commands.refresh_booking_lookup(row)

    @staticmethod
    def _verified(output: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
        appointment = output.get("appointment")
        return bool(
            isinstance(appointment, Mapping)
            and appointment.get("verified") is True
            and appointment.get("state") == "CONFIRMED"
            and all(
                appointment.get(key) == payload.get(key)
                for key in ("patient_id", "hospital_id", "provider_id", "slot_id")
            )
        )

    def _recover(self, row: WorkflowRow, detail: str) -> None:
        # Do not expose raw exception text, source documents or credentials to the patient.
        reason = "technical_outage"
        summary = (
            "Automatic coordination stopped before the next safe step. Please review this referral."
        )
        if "document" in detail.lower():
            reason, summary = (
                "missing_document",
                "The hospital needs a complete referral document package.",
            )
        elif "network" in detail.lower() or "conflict" in detail.lower():
            reason, summary = (
                "conflicting_sources",
                "The provider or referral source data needs review.",
            )
        elif "No provider" in detail or "No appointment" in detail:
            reason, summary = (
                "no_match",
                "No appointment matches the recorded preferences. Review the available alternatives.",
            )
        draft = {
            "reason_class": reason,
            "patient_visible_summary": summary,
            "reason_detail": "Automatic coordination could not complete this step.",
            "sources": [],
        }
        row = self._store(
            row,
            coordination_error={"class": "coordination_failed", "detail": detail[:500]},
            handoff_draft=draft,
        )
        if row.state not in {
            "BOOKED",
            "HUMAN_HANDOFF",
            "CANCELLED",
            "FAILED",
            "ABANDONED",
        }:
            self._move(row, "HUMAN_HANDOFF", {"handoff": draft, "consent_required": True})
