"""§9's output guardrail in the agents (`referraleasy_agents.output_guard`).

The oracle answers only from the turn's own tool results and inputs, so each test builds the turn's
results as a tool would have returned them and asks whether a result built on them may leave the
service. Every refusal here is one the guardrail exists for; the passes pin the two false positives
the projection was written to avoid.
"""

from __future__ import annotations

from typing import Any

from referraleasy_agent_runtime import AgentRole
from referraleasy_agents.output_guard import TurnOracle, check_result, patient_facing

SLOT = "40000000-0000-4000-8000-000000000001"
HOSPITAL = "20000000-0000-4000-8000-000000000001"
PROVIDER = "30000000-0000-4000-8000-000000000001"
START = "2026-10-20T14:00:00Z"


def _slots_result(**slot: Any) -> dict[str, Any]:
    return {
        "slots": [
            {
                "slot_id": SLOT,
                "hospital_id": HOSPITAL,
                "provider_id": PROVIDER,
                "start_at": START,
                "status": "available",
                **slot,
            }
        ]
    }


def _codes(role: AgentRole, output: dict[str, Any], results: list[Any]) -> list[str]:
    return [v.code for v in check_result(role, output, results=results).violations]


def test_a_slot_list_built_from_the_tool_result_passes() -> None:
    """`status: available` is a slot status, not a booking state; the projection leaves it out."""
    result = _slots_result()
    output = {**result, "patient_message": "Choose an available appointment time."}
    assert _codes(AgentRole.SCHEDULING, output, [result]) == []


def test_a_slot_time_that_disagrees_with_the_record_is_refused() -> None:
    output = _slots_result(start_at="2026-10-20T15:00:00Z")
    assert _codes(AgentRole.SCHEDULING, output, [_slots_result()]) == ["unverified_time"]


def test_an_id_no_tool_returned_this_turn_is_refused() -> None:
    retrieved = {"candidates": [{"provider_id": PROVIDER, "hospital_id": HOSPITAL}]}
    invented = {
        "candidates": [
            {"provider_id": PROVIDER, "hospital_id": HOSPITAL},
            {"provider_id": "30000000-0000-4000-8000-000000000999", "hospital_id": HOSPITAL},
        ],
        "directory_labels": {},
    }
    assert _codes(AgentRole.PROVIDER_MATCHING, invented, [retrieved]) == ["unknown_entity"]


def test_an_id_the_task_was_given_counts_as_a_source() -> None:
    """The chosen hospital and doctor arrive as inputs from the workflow's records."""
    oracle = TurnOracle([], {"hospital_id": HOSPITAL, "provider_id": PROVIDER})
    assert oracle.entity_exists(HOSPITAL) and oracle.entity_exists(PROVIDER)
    assert not oracle.entity_exists(SLOT)


def test_a_booking_narrated_as_confirmed_while_requested_is_refused() -> None:
    booking = {"booking_request_id": "b-1", "state": "REQUESTED"}
    output = {"booking_request": booking, "patient_message": "Your appointment is confirmed."}
    assert _codes(AgentRole.SCHEDULING, output, [{"booking": booking}]) == [
        "unconfirmed_booking_state"
    ]


def test_the_workflows_own_unconfirmed_sentence_passes() -> None:
    """Regression: "has not confirmed" tripped the rule, so the sentence no longer uses the word."""
    from referraleasy_agents.workflow import _booking_output

    booking = {"booking_request_id": "b-1", "state": "REQUESTED"}
    output = _booking_output({"booking": booking})
    assert _codes(AgentRole.SCHEDULING, output, [{"booking": booking}]) == []


def test_a_booking_state_that_is_not_the_records_is_refused() -> None:
    claimed = {"booking_request_id": "b-1", "state": "CONFIRMED"}
    recorded = {"booking_request_id": "b-1", "state": "REQUESTED"}
    output = {"booking_request": claimed, "patient_message": "The hospital confirmed it."}
    assert "unconfirmed_booking_state" in _codes(AgentRole.SCHEDULING, output, [{"b": recorded}])


def test_a_payment_guarantee_and_clinical_advice_are_refused() -> None:
    output = {
        "handoff": {"patient_visible_summary": "Your visit is fully covered."},
        "patient_message": "You should take 50 mg before the visit.",
    }
    codes = _codes(AgentRole.RECOVERY_HANDOFF, output, [])
    assert "coverage_guarantee" in codes and "clinical_language" in codes


def test_coverage_status_without_its_evidence_is_refused() -> None:
    coverage = {
        "plan_id": "p",
        "provider_id": PROVIDER,
        "hospital_id": HOSPITAL,
        "status": "in_network",
    }
    retrieved = {"coverage": {**coverage, "evidence_ids": ["e-1"]}, "plan": {"plan_id": "p"}}
    assert _codes(AgentRole.COVERAGE_NETWORK, {"coverage": coverage}, [retrieved]) == [
        "unsourced_coverage"
    ]
    cited = {"coverage": {**coverage, "evidence_ids": ["e-1"]}}
    assert _codes(AgentRole.COVERAGE_NETWORK, cited, [retrieved]) == []


def test_an_unsourced_phone_number_in_a_label_is_refused() -> None:
    retrieved = {"candidates": [{"provider_id": PROVIDER, "hospital_id": HOSPITAL}]}
    output = {
        **retrieved,
        "directory_labels": {HOSPITAL: "Lakeside Hospital, call 312 555 0199"},
    }
    assert _codes(AgentRole.PROVIDER_MATCHING, output, [retrieved]) == ["unsourced_contact"]
    sourced = {**retrieved, "directory_labels": output["directory_labels"]}
    assert _codes(AgentRole.PROVIDER_MATCHING, output, [sourced]) == []


def test_the_coordinator_is_checked_on_its_own_sentence_not_its_specialists_results() -> None:
    """Each specialist checks its own result; the coordinator's own patient-facing text is its own."""
    output = {
        "specialist_results": {"scheduling": {"output": {"patient_message": "guaranteed"}}},
        "patient_question": None,
    }
    assert patient_facing(AgentRole.CARE_COORDINATOR, output) == []
