"""Execute the standard referral plan with source data and governed tools.

This path uses fixed rules for routine work. The ADK path still handles model tasks.
Neither path grants a tool permission. Each call uses the same policy and budget.
"""

from __future__ import annotations

import asyncio
from typing import Any

from referraleasy_agent_runtime import AgentRole, UpstreamTransient
from referraleasy_agents.decisions import next_allowed_actions
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.schemas import schema_for
from referraleasy_agents.tools.toolbox import ToolSet


async def _invoke(tools: ToolSet, name: str, **arguments: Any) -> dict[str, Any]:
    attempts = 1 if name in {"create_booking", "delegate_task"} else 3
    for attempt in range(attempts):
        try:
            return await tools.invoke(name, **arguments)
        except UpstreamTransient:
            if attempt + 1 == attempts:
                raise
            tools.budget.require_time()
            await tools.budget.clock.sleep_ms(min(150 * 2**attempt, tools.budget.remaining_ms()))
    raise AssertionError("The bounded tool loop did not return.")


async def execute_workflow(tools: ToolSet, role: AgentRole, data: dict[str, Any]) -> Any:
    """Return a schema-validated result from actual tool responses."""
    result: dict[str, Any]
    reason = "ok"
    if role is AgentRole.CARE_COORDINATOR:
        result = await _coordinate(tools, data)
    elif role is AgentRole.REFERRAL_INTAKE:
        answer = await _invoke(
            tools,
            "validate_referral",
            referral_text=data["referral_source"],
            document_ids=data.get("document_ids", []),
        )
        result = {
            key: answer[key]
            for key in ("draft", "completed_intake", "missing_fields", "contradictions")
        }
        if answer["missing_fields"] or answer["contradictions"]:
            reason = "missing_mandatory_field"
        if answer["draft"]["injection_attempt_detected"]:
            reason = "guardrail_blocked"
    elif role is AgentRole.COVERAGE_NETWORK:
        if data.get("provider_id"):
            answer = await _invoke(
                tools,
                "check_provider_network",
                **{key: data.get(key) for key in ("plan_id", "provider_id", "hospital_id")},
            )
            result = {"coverage": answer["coverage"]}
        else:
            answer = await _invoke(tools, "check_plan", plan_id=data["plan_id"])
            result = {"plan": answer["plan"]}
    elif role is AgentRole.PROVIDER_MATCHING:
        constraints = data.get("constraints", [])
        radius = min(
            (item["value"] for item in constraints if item["field"] == "max_distance_miles"),
            default=None,
        )
        answer = await _invoke(
            tools,
            "find_candidates",
            specialty=data["specialty"],
            origin=data["origin"],
            constraints=constraints,
            hospital_ids=data.get("hospital_candidates", []),
            max_distance_miles=radius,
        )
        # Some constraints need slot or network evidence. Never mark them satisfied here.
        candidates = answer["candidates"]
        result = {
            "candidates": candidates,
            "directory_labels": answer.get("directory_labels", {}),
            "mandatory_conditions": {
                "specialty_identified": bool(data["specialty"]),
                "approval_present": False,
                "slot_valid": False,
            },
        }
        if not candidates:
            reason = "no_candidate_matched"
    elif role is AgentRole.REFERRAL_READINESS:
        answer = await _invoke(
            tools,
            "validate_referral_package",
            **{
                key: data.get(key)
                for key in ("hospital_id", "plan_id", "visit_type", "document_ids")
            },
        )
        result = {"readiness": answer["readiness"], "checks": answer["checks"]}
        if not answer["can_proceed"]:
            reason = "document_missing"
    elif role is AgentRole.SCHEDULING:
        if tools.workflow_state == "BOOKING_IN_FLIGHT" and not data.get("reconcile_only"):
            answer = await _invoke(
                tools,
                "create_booking",
                idempotency_key=data["idempotency_key"],
                payload=data["booking_payload"],
                approval=data["approval"],
                expected_slot_version=data.get("expected_slot_version"),
            )
            result = _booking_output(answer)
            if (answer.get("booking") or {}).get("state") != "CONFIRMED":
                reason = "booking_outcome_unknown"
        elif tools.workflow_state in {"BOOKING_IN_FLIGHT", "BOOKING_UNVERIFIED"}:
            answer = await _invoke(tools, "lookup_request", idempotency_key=data["idempotency_key"])
            result = _booking_output(answer)
            if (answer.get("booking") or {}).get("state") != "CONFIRMED":
                reason = "booking_outcome_unknown"
        else:
            answer = await _invoke(
                tools,
                "list_slots",
                **{
                    key: data.get(key)
                    for key in ("hospital_id", "provider_id", "visit_type", "preferred_days")
                },
            )
            result = {
                "slots": answer["slots"],
                "availability": answer["availability"],
                "patient_message": "Choose an available appointment time.",
            }
            if not answer["slots"]:
                reason = "no_slot_available"
    else:
        raise UnknownDataError("This role has no standard workflow operation.")

    result["decision"] = {
        "action": f"{role.value}:{tools.trace_entries()[-1]['tool']}",
        "evidence_ids": tools.citation_ids(),
        "missing_fields": [],
        "next_allowed_actions": next_allowed_actions(tools.box.policy, tools.workflow_state)
        if tools.box.policy
        else [],
        "reason_code": reason,
    }
    return schema_for(role).model_validate(result)


def _booking_output(answer: dict[str, Any]) -> dict[str, Any]:
    booking = answer.get("booking")
    state = booking.get("state") if isinstance(booking, dict) else None
    return {
        "booking_request": booking,
        "appointment": answer.get("appointment"),
        "reconciliation": answer.get("reconciliation"),
        "lookup_outcome": answer.get("lookup_outcome"),
        "patient_message": (
            "The hospital confirmed the appointment."
            if state == "CONFIRMED"
            else "The hospital has not confirmed the appointment."
        ),
    }


async def _coordinate(tools: ToolSet, data: dict[str, Any]) -> dict[str, Any]:
    plan = data.get("execution_plan", [])
    targets = [step["target"] for step in plan]
    supplied = data.get("specialist_inputs", {})
    if len(set(targets)) != len(targets) or set(targets) != set(supplied):
        raise UnknownDataError("The plan must name each supplied specialist exactly once.")
    # Only plan coverage and matching are independent in the standard flow.
    parallel = {"coverage_network", "provider_matching"}
    independent = {step["target"] for step in plan if step["independent"]}
    if independent and (independent != parallel or set(targets) != parallel):
        raise UnknownDataError("These specialist steps are not independent.")

    async def run(step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        answer = await _invoke(
            tools, "delegate_task", target_role=step["target"], goal=step["goal"]
        )
        return step["target"], answer["response"]

    if independent:
        answers = await asyncio.gather(*(run(step) for step in plan))
    else:
        answers = [await run(step) for step in plan]
    return {"plan": plan, "specialist_results": dict(answers)}
