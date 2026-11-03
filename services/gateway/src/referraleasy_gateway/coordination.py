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
from referraleasy_contracts.generated.patient import (
    AlternativeOption,
    Constraint,
    ConstraintChange,
    ReferralIntake,
)
from referraleasy_gateway.auth import Session, SessionKind
from referraleasy_gateway.commands import CommandService, DispatchTicket
from referraleasy_gateway.continuations import (
    ContinuationClaim,
    ContinuationExpired,
    ContinuationKind,
    ContinuationLifecycle,
    ContinuationReplaced,
)
from referraleasy_gateway.control import ControlState, WorkflowRow
from referraleasy_gateway.release import WorkflowReleases, pinned_release
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
        release_manifest_id: str | None = None,
    ) -> Mapping[str, Any]: ...


class CoordinationError(ValueError):
    """The source data or a specialist result cannot support the next step."""


def _id(*parts: Any) -> str:
    return str(uuid5(NAMESPACE_URL, "referraleasy:coordination:" + ":".join(map(str, parts))))


def _booking_request(row: WorkflowRow, ticket: DispatchTicket | None) -> dict[str, Any]:
    """The scheduler's booking input. Without a ticket it can only look up an earlier send."""
    authority = row.payload["booking_authority"]
    if ticket is not None and ticket.approval_id != str(authority.get("approval_id")):
        raise CoordinationError("The dispatch ticket names a different approval.")
    return {
        "idempotency_key": row.payload["booking_idempotency_key"],
        "booking_payload": row.payload["booking_payload"],
        "approval": authority,
        "expected_slot_version": row.payload["selected_slot_version"],
        "reconcile_only": ticket is None,
    }


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
        # §2.4: a preference moves only by an explicit decision, recorded as a ConstraintChange.
        # The recorded preferences are never edited; the change is applied on top of them here.
        for change in row.payload.get("constraint_changes", []):
            for constraint in self.constraints:
                if constraint["constraint_id"] == change["constraint_id"]:
                    constraint["value"] = change["to_value"]
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

    def document_ids(self) -> list[str]:
        """The referral's documents plus any the patient attached during the readiness check."""
        attached = [str(d) for d in self.row.payload.get("attached_document_ids", [])]
        return list(dict.fromkeys([*self.referral.get("document_ids", []), *attached]))

    def attachable(self, kinds: list[str]) -> list[dict[str, Any]]:
        """The patient's own documents of the missing kinds that the referral does not include."""
        included = set(self.document_ids())
        return [
            dict(document)
            for document in self.sources.get("patient_documents", [])
            if document.get("kind") in kinds and document.get("document_id") not in included
        ]

    def document_answer(self, state_version: int) -> list[str] | None:
        """The documents attached in answer to the card issued at `state_version`, if any yet."""
        for answer in reversed(self.row.payload.get("answers", [])):
            if (
                answer.get("state") == "READINESS_CHECK"
                and answer.get("state_version") == state_version
                and isinstance(answer.get("value", {}).get("document_ids"), list)
            ):
                return [str(item) for item in answer["value"]["document_ids"]]
        return None

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
        releases: WorkflowReleases | None = None,
    ) -> None:
        self.control = control
        self.commands = commands
        self.coordinator = coordinator
        self.clock = clock
        #: §13: a workflow is pinned to a release on its first dispatch and keeps it. `None` is a
        #: deployment with no readable catalog; its tasks carry the dispatcher's startup release.
        self.releases = releases
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
            release_manifest_id=pinned_release(row),
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
        if self.releases is not None:
            self.releases.pin(self.continuations.refresh(claim))
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
                        # The provider is in the step: a second check for another doctor at this
                        # state version is a different task, not a replay of the first.
                        step=f"provider-network:{schedule['provider_id']}",
                    )
                    coverage = CoverageResult.model_validate(
                        answers["coverage_network"]["coverage"]
                    )
                    confirmed = (
                        coverage.in_network_for_plan is True
                        and not coverage.conflict
                        and coverage.freshness.state.value == "fresh"
                    )
                    if not confirmed and not self._acknowledged(row, schedule["provider_id"]):
                        # Scenario 3: show the status as it is — unknown or out of network, with its
                        # freshness and evidence — and let the patient decide. Never resolved into a
                        # confident answer, and nothing is booked without final approval.
                        row = self.control.load(row.workflow_id) or row
                        row = self._store(
                            row,
                            coverage_review={
                                "provider_id": schedule["provider_id"],
                                "answers_before": len(row.payload.get("answers", [])),
                                "coverage": answers["coverage_network"]["coverage"],
                            },
                        )
                        self.commands.present(row, self._coverage_card(row, answers))
                        break
                    if not confirmed:
                        row = self.control.load(row.workflow_id) or row
                        row = self._store(
                            row,
                            coverage_acknowledged={
                                "provider_id": schedule["provider_id"],
                                "coverage": answers["coverage_network"]["coverage"],
                                "acknowledged_at": self.clock.now().isoformat(),
                            },
                            coverage_review=None,
                        )
                self._move(row, "READINESS_CHECK")
            elif row.state == "READINESS_CHECK":
                schedule = source.scheduling()
                request = row.payload.get("document_request")
                if request is not None:
                    attached = source.document_answer(int(request["state_version"]))
                    if attached is None:
                        break  # the patient has not attached anything yet
                    row = self._store(
                        row,
                        attached_document_ids=list(
                            dict.fromkeys(
                                [*row.payload.get("attached_document_ids", []), *attached]
                            )
                        ),
                    )
                    source = WorkflowInputs(row)
                answers = self._call(
                    claim,
                    row,
                    {
                        "referral_readiness": {
                            "hospital_id": schedule["hospital_id"],
                            "patient_id": row.patient_ref,
                            "plan_id": source.plan_id,
                            "visit_type": schedule["visit_type"],
                            "document_ids": source.document_ids(),
                        }
                    },
                    step="readiness" if request is None else "readiness-recheck",
                )
                ready = ReadinessResult.model_validate(answers["referral_readiness"]["readiness"])
                row = self.control.load(row.workflow_id) or row
                checks = self._kept_checks(
                    row.payload.get("readiness_checks"),
                    answers["referral_readiness"]["readiness"].get("checks", []),
                )
                row = self._store(row, readiness_checks=checks)
                if not ready.can_proceed:
                    missing = [str(kind) for kind in ready.missing_kinds or []]
                    attachable = source.attachable(missing) if request is None else []
                    if not attachable:
                        raise CoordinationError(
                            "The chosen hospital needs a complete referral document package."
                        )
                    row = self._store(
                        row,
                        document_request={
                            "state_version": row.state_version,
                            "missing_kinds": missing,
                        },
                    )
                    self.commands.present(row, self._document_card(missing, attachable))
                    break
                if request is not None:
                    row = self._store(row, document_request=None)
                answers = self._call(claim, row, {"scheduling": schedule}, step="slots")
                slots = self._slots(answers["scheduling"]["slots"], source)
                if not slots:
                    other_times = self._slots(
                        answers["scheduling"]["slots"], source, honour_preferences=False
                    )
                    alternative = self._time_alternative(
                        source, other_times, answers["scheduling"].get("availability") or {}
                    )
                    if alternative is None:
                        raise CoordinationError(
                            "No appointment time matches the recorded preferences."
                        )
                    # Scenario 2: report the empty result and offer the doctor's other times as an
                    # evidence-backed alternative. Nothing is relaxed until the patient picks one.
                    row = self.control.load(row.workflow_id) or row
                    self._store(row, offered_slots=other_times, slot_alternative=alternative)
                    self._move(
                        row,
                        "AWAITING_SLOT_CHOICE",
                        {
                            "options": [
                                {"id": slot["slot_id"], "label": slot["start_at"], **slot}
                                for slot in other_times
                            ],
                            "alternative": alternative,
                            "notice": alternative["description"],
                        },
                    )
                    break
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
                alternative = row.payload.get("slot_alternative")
                if alternative and selected in alternative.get("candidate_ids", []):
                    row = self._store(
                        row,
                        constraint_changes=[
                            *row.payload.get("constraint_changes", []),
                            *self._accepted(source, alternative, selected, row),
                        ],
                        slot_alternative=None,
                    )
                    source = WorkflowInputs(row)
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
                        "document_ids": source.document_ids(),
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
    def _acknowledged(row: WorkflowRow, provider_id: str) -> bool:
        """Whether the patient chose this doctor again *after* seeing the coverage finding."""
        review = row.payload.get("coverage_review")
        if not review or review.get("provider_id") != provider_id:
            return False
        later = row.payload.get("answers", [])[int(review["answers_before"]) :]
        return any(
            answer.get("state") == "AWAITING_DOCTOR_CHOICE"
            and answer.get("value", {}).get("provider_id") == provider_id
            for answer in later
        )

    @staticmethod
    def _coverage_card(row: WorkflowRow, answers: Mapping[str, Any]) -> dict[str, Any]:
        coverage = dict(answers["coverage_network"]["coverage"])
        status = (
            "out of network"
            if coverage.get("in_network_for_plan") is False
            else "unknown"
            if coverage.get("in_network_for_plan") is None or coverage.get("conflict")
            else "in network, but the record is not fresh"
        )
        freshness = coverage.get("freshness") or {}
        candidates = row.payload["specialist_results"]["provider_matching"]["candidates"]
        labels = row.payload["specialist_results"]["provider_matching"].get("directory_labels", {})
        hospital = next(
            (
                str(a["value"]["hospital_id"])
                for a in reversed(row.payload.get("answers", []))
                if a.get("state") == "AWAITING_HOSPITAL_CHOICE"
            ),
            None,
        )
        return {
            "options": [
                {"id": c["provider_id"], "label": labels.get(c["provider_id"], c["provider_id"])}
                for c in candidates
                if c["hospital_id"] == hospital
            ],
            "coverage": {
                "provider_id": coverage.get("provider_id"),
                "status": status,
                "conflict": bool(coverage.get("conflict")),
                "freshness": freshness,
                "evidence_ids": list(coverage.get("evidence_ids") or []),
                "disclaimer_key": coverage.get("disclaimer_key"),
            },
            "notice": (
                f"Your plan's network status for this doctor is {status} "
                f"(records checked: {freshness.get('state', 'unknown')}). This is not a payment "
                "decision. Choose the same doctor to continue anyway, or another doctor."
            ),
        }

    @staticmethod
    def _kept_checks(
        previous: list[dict[str, Any]] | None, current: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Scenario 4: a check that already passed keeps its original result and run id.

        The readiness agent validates the whole package each time; what changes on a re-check is
        only the result of the checks that had failed. Those are taken from the new run, and every
        other check is the one recorded first.
        """
        if not previous:
            return list(current)
        passed = {
            check["kind"]: check
            for check in previous
            if check.get("status") in {"satisfied", "not_required"}
        }
        return [passed.get(check["kind"], check) for check in current]

    @staticmethod
    def _document_card(missing: list[str], documents: list[dict[str, Any]]) -> dict[str, Any]:
        names = ", ".join(kind.replace("_", " ") for kind in missing)
        return {
            "title": "The hospital needs another document",
            "subtitle": (
                f"Before an appointment can be offered, the hospital asks for: {names}. "
                "Attach yours to continue. Nothing is booked until you approve it."
            ),
            "missing_documents": missing,
            "documents": documents,
            "fields": [
                {
                    "name": "note",
                    "label": "Note for the hospital (optional)",
                    "type": "textarea",
                    "required": False,
                }
            ],
            "submit_label": "Attach and continue",
            "synthetic": True,
        }

    @staticmethod
    def _local(slot: Mapping[str, Any]) -> datetime:
        return datetime.fromisoformat(str(slot["start_at"]).replace("Z", "+00:00")).astimezone(
            ZoneInfo(str(slot["display_timezone"]))
        )

    @staticmethod
    def _period(local: datetime) -> str:
        return "morning" if local.hour < 12 else "afternoon" if local.hour < 17 else "evening"

    def _time_alternative(
        self,
        source: WorkflowInputs,
        other_times: list[dict[str, Any]],
        availability: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """An AlternativeOption for the chosen doctor's other times, or None when there is none.

        Only offered when day or time-of-day preferences are what excluded every slot, and only with
        the availability record's evidence: §2.4 forbids an alternative nobody can trace.
        """
        timing = [
            c
            for c in source.constraints
            if c["field"] in {"preferred_day", "preferred_time_of_day"}
        ]
        evidence = [str(availability["evidence_id"])] if availability.get("evidence_id") else []
        if not other_times or not timing or not evidence:
            return None
        first = self._local(other_times[0])
        wanted = " ".join(str(c["value"]) for c in timing)
        kind = (
            "different_day"
            if any(c["field"] == "preferred_day" for c in timing)
            else "different_time_of_day"
        )
        description = (
            f"No time with this doctor matches your {wanted} preference. The doctor's next time is "
            f"{first.strftime('%A %d %B at %H:%M')} {other_times[0]['display_timezone']}. Choosing "
            "one of these times changes that preference for this referral; nothing else changes."
        )
        option = AlternativeOption.model_validate(
            {
                "option_id": _id("alternative", kind, *[s["slot_id"] for s in other_times]),
                "kind": kind,
                "description": description,
                # Null: every timing preference here is hard, and a hard constraint is never relaxed
                # by the system — only by the patient's choice, recorded as a ConstraintChange.
                "relaxes_constraint_id": None,
                "candidate_ids": [str(s["slot_id"]) for s in other_times],
                "evidence_ids": evidence,
            }
        )
        return option.model_dump(mode="json")

    def _accepted(
        self,
        source: WorkflowInputs,
        alternative: Mapping[str, Any],
        slot_id: str,
        row: WorkflowRow,
    ) -> list[dict[str, Any]]:
        """The ConstraintChanges the patient made by choosing `slot_id` from the alternative."""
        slot = next(s for s in row.payload.get("offered_slots", []) if s["slot_id"] == slot_id)
        local = self._local(slot)
        actual = {
            "preferred_day": local.strftime("%A").lower(),
            "preferred_time_of_day": self._period(local),
        }
        changes = []
        for constraint in source.constraints:
            field = constraint["field"]
            if field in actual and str(constraint["value"]).lower() != actual[field]:
                changes.append(
                    ConstraintChange.model_validate(
                        {
                            "constraint_id": constraint["constraint_id"],
                            "from_value": constraint["value"],
                            "to_value": actual[field],
                            "decided_by": "patient",
                            "decided_at": self.clock.now().isoformat(),
                            "presented_options": [dict(alternative)],
                        }
                    ).model_dump(mode="json")
                )
        return changes

    @staticmethod
    def _slots(
        slots: list[dict[str, Any]], source: WorkflowInputs, *, honour_preferences: bool = True
    ) -> list[dict[str, Any]]:
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
            if not honour_preferences or (
                (not days or local.strftime("%A").lower() in days)
                and (not periods or period in periods)
            ):
                accepted.append(raw)
        return accepted

    def _book(self, claim: ContinuationClaim, row: WorkflowRow, source: WorkflowInputs) -> None:
        schedule = source.scheduling()
        already_sent = bool(row.payload.get("booking_sent"))
        # A first send needs a ticket, so §16's halt is checked where the hospital is asked. A resend
        # after a crash only looks the earlier send up, and the record below needs no ticket at all:
        # refusing to record a booking the hospital already made is how an appointment is orphaned.
        ticket = None if already_sent else self.commands.authorise_dispatch(row)
        request = _booking_request(row, ticket)
        self._store(row, booking_sent=True)
        schedule.update(request)
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
