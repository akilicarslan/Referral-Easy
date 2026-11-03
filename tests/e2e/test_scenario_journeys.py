"""Scenarios 2, 3 and 4 as journeys: patient commands only, real agents, an isolated mock API.

Each runs through `scripts/eval_replay.py`'s stack, exactly as `make eval` does. The scripted patient
supplies the scenario's own fixture patient, the opening message and each answer. Nothing else is
scripted: every card, check, alternative and booking below came from the gateway and the agents.

* **Scenario 2** — no slot satisfies the hard day/time preferences. The empty result is reported
  with an evidence-backed alternative. The hard constraint is never relaxed by the system; the
  patient's choice is recorded as a `ConstraintChange`.
* **Scenario 3** — the chosen doctor's network status is not confirmed. It is shown as it is, with
  freshness and evidence, and the journey continues only after the patient chooses that doctor again.
* **Scenario 4** — the hospital needs a prior report the referral did not include. The patient
  attaches their own document, only the failed check's result is replaced, and the journey resumes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from referraleasy_eval.capture import DEFAULT_VARIANT

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_replay  # type: ignore[import-not-found]  # noqa: E402

RELEASE = "rel-20261016-8e00275"


def _cards(observation: Any, component: str) -> list[dict[str, Any]]:
    return [dict(card) for card in observation.cards if card["component"] == component]


def test_scenario_2_reports_the_empty_result_and_changes_a_preference_only_by_choice() -> None:
    observation = eval_replay.observe(RELEASE, "multi_constraint_search", DEFAULT_VARIANT)
    assert observation.error is None, observation.error
    assert observation.final_state == "BOOKED", observation.payload.get("coordination_error")

    offered = next(c for c in _cards(observation, "SlotPicker") if c["props"].get("alternative"))
    alternative = offered["props"]["alternative"]
    assert alternative["kind"] in {"different_day", "different_time_of_day"}
    assert alternative["evidence_ids"], "§2.4: an alternative without evidence is never offered"
    assert alternative["relaxes_constraint_id"] is None, "the timing constraints are hard"

    changes = observation.payload["constraint_changes"]
    assert changes and all(change["decided_by"] == "patient" for change in changes)
    assert all(change["presented_options"] == [alternative] for change in changes)
    # The hard radius was never touched: no change names it, and every candidate is inside it.
    preferences = observation.payload["source_data"]["preferences"]["constraints"]
    radius = next(
        c for c in preferences if c["field"] == "max_distance_miles" and not c["superseded_by"]
    )
    assert radius["constraint_id"] not in {change["constraint_id"] for change in changes}
    for delegation in observation.delegations:
        results = ((delegation.result or {}).get("output") or {}).get("specialist_results", {})
        for candidate in (
            (results.get("provider_matching") or {}).get("output", {}).get("candidates", [])
        ):
            assert candidate["distance_miles"] <= radius["value"]


def test_scenario_3_shows_an_unconfirmed_network_status_and_continues_only_on_a_second_choice() -> (
    None
):
    observation = eval_replay.observe(RELEASE, "network_conflict", DEFAULT_VARIANT)
    assert observation.error is None, observation.error
    assert observation.final_state == "BOOKED", observation.payload.get("coordination_error")

    review = next(c for c in _cards(observation, "DoctorPicker") if c["props"].get("coverage"))
    coverage = review["props"]["coverage"]
    assert coverage["status"] != "in network", "an unconfirmed status is never shown as confirmed"
    assert coverage["freshness"] and coverage["evidence_ids"]
    assert "not a payment decision" in review["props"]["notice"]

    acknowledged = observation.payload["coverage_acknowledged"]
    assert acknowledged["coverage"]["in_network_for_plan"] is not True
    doctor_answers = [s for s in observation.steps if s.startswith("AWAITING_DOCTOR_CHOICE")]
    assert len(doctor_answers) == 2, "the journey continued only after the doctor was chosen again"


def test_scenario_4_attaches_the_missing_report_and_reruns_only_the_failed_check() -> None:
    observation = eval_replay.observe(RELEASE, "missing_document", DEFAULT_VARIANT)
    assert observation.error is None, observation.error
    assert observation.payload.get("handoff_draft") is None, (
        "the patient could act; nobody was paged"
    )
    assert observation.final_state in {"BOOKED", "BOOKING_UNVERIFIED"}, observation.final_state
    assert observation.bookings, "the journey resumed all the way to a booking request"

    request = next(iter(_cards(observation, "PatientDetailsForm")))
    assert request["state"] == "READINESS_CHECK"
    assert request["props"]["missing_documents"] == ["prior_report"]
    offered = [d["document_id"] for d in request["props"]["documents"]]
    assert offered == ["a0000000-0000-4000-8000-000000000005"], (
        "only the patient's own prior report"
    )

    readiness = [
        r["output"]["readiness"]
        for d in observation.delegations
        for role, r in (
            ((d.result or {}).get("output") or {}).get("specialist_results") or {}
        ).items()
        if role == "referral_readiness"
    ]
    first = {c["kind"]: c for c in readiness[0]["checks"]}
    recheck = {c["kind"]: c for c in readiness[-1]["checks"]}
    kept = {c["kind"]: c for c in observation.payload["readiness_checks"]}
    assert first["prior_report"]["status"] == "missing"
    assert kept["referral_letter"]["run_id"] == first["referral_letter"]["run_id"], (
        "a passed check keeps its run"
    )
    assert kept["referral_letter"]["run_id"] != recheck["referral_letter"]["run_id"]
    assert kept["prior_report"]["status"] == "satisfied"
    assert kept["prior_report"]["run_id"] == recheck["prior_report"]["run_id"]
    assert (
        "a0000000-0000-4000-8000-000000000005"
        in observation.payload["booking_payload"]["document_ids"]
    )
