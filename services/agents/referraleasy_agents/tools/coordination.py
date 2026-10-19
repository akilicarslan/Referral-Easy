"""The Care Coordinator's one tool: `delegate_task` (§3).

The coordinator's authority is *which specialist to call and what to ask*
(`prompts.ROLE_BRIEFINGS`), and both are expressed by calling this tool. What makes
that real rather than notional:

* **The delegation is spent from the parent's budget.** `TurnBudget.delegation`
  yields a child budget that shares the parent's absolute deadline and counters, so
  a specialist cannot extend the turn and a chain of them cannot outlive it. When
  the delegation count is exhausted the runtime raises `BudgetExhausted`, which
  projects to a typed outcome — not to a fifth call.
* **The child's envelope is derived, never dictated.** The coordinator's own task
  arrived in a §5 envelope, and the child's is built from it: same workflow, same
  command, the parent's task as `parent_task_id`, the same state version, the same
  deadline and budget declaration, and the same `release_manifest_id`. The model
  chooses the *target* and the *goal*; it does not get to name the workflow, the
  command, the state, the version or the release — those are exactly the fields a
  model could otherwise use to move a delegation somewhere other than where the
  workflow is. `tools/specs.py` declares no parameter for any of them.
* **The child's key is derived, not minted.** Same workflow, same command, same
  target and same goal produce the same `idempotency_key`, so a re-sent delegation
  is the same task rather than a second one.
* **The child's task is the shape a specialist's `/tasks` route accepts**: the
  envelope travels under the `task_envelope` meta key the receiving handler splits
  off, with its own `command_id` alongside. (Before this, the envelope's fields were
  sent flat — a request the receiving handler refuses, because a task with no
  envelope and no command id cannot be filed under §7's keys.)

The coordinator holds no other tool at all (`referraleasy_agent_runtime.roles`): it
cannot retrieve, cannot write, and cannot book. Every capability it has, it has by
asking someone else.

The target's structured input comes from ``specialist_inputs`` in the caller's validated workflow
context. Only the target's document is sent, and it is validated again at this seam. An absent
input is refused before sending; the model's goal and conversation summary never manufacture the
specialty, patient, document or location facts a specialist consumes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from referraleasy_agent_runtime import AgentRole, UnknownRoleError
from referraleasy_agents.canonical import deterministic_uuid, id_text
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.schemas import schema_for
from referraleasy_agents.tools.call import DelegationPort, ToolCall
from referraleasy_contracts.generated.task import TaskEnvelope

__all__ = [
    "delegate_task",
]


#: The keys a child's answer must carry for the coordinator to read it. The runtime's
#: `envelope` produces exactly these; asserting on them here means a peer that
#: answered with prose is a failure rather than an empty success.
_ANSWER_KEYS = ("state", "outcome", "is_success")


def _as_port(call: ToolCall) -> DelegationPort:
    port = call.delegation
    if port is None:
        raise UnknownDataError(
            "delegate_task has no delegation transport configured. A coordinator with no way to call "
            "a specialist must fail loudly rather than report that it delegated."
        )
    return port


def _evidence_ref_ids(refs: Sequence[Any]) -> list[str]:
    """The ids of an envelope's `evidence_refs`.

    ``EvidenceRef`` is a *model* (``{evidence_id: …}``), not a ``RootModel``, so
    :func:`id_text` applied to the entry itself would return its repr — the failure
    `id_text` exists to prevent, one level up. The unwrapping is explicit here, and
    `test_tool_surface.py` pins both shapes.
    """
    return [id_text(getattr(ref, "evidence_id", ref)) for ref in refs]


def _child_goal(goal: str) -> str:
    text = goal.strip()
    if not text:
        raise UnknownDataError(
            "delegate_task needs a goal: a specialist asked for nothing decides nothing."
        )
    return text


async def delegate_task(
    call: ToolCall,
    *,
    target_role: str,
    goal: str,
    evidence_ids: Sequence[str] | None = None,
    constraint_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Delegate one goal to one specialist, under this turn's remaining budget.

    ``evidence_ids`` and ``constraint_ids`` *narrow* what the child receives: a
    caller that names ids gets those, and a caller that names none carries its own
    envelope's context forward unchanged. Neither can add an id the caller's own
    envelope does not already carry, because a delegation is not a way to receive
    authority — and there is no parameter through which a model could inflate the
    child's budget, deadline or state.
    """
    try:
        target = AgentRole(target_role)
    except ValueError as error:
        raise UnknownRoleError(
            f"{target_role!r} is not one of the seven specialists "
            f"({sorted(role.value for role in AgentRole)})."
        ) from error
    if target is call.role:
        raise UnknownDataError(
            f"{call.role.value} cannot delegate to itself: a self-call is a loop, and §8 closes loops "
            "by going to recovery instead."
        )
    parent = call.envelope
    if parent is None:
        raise UnknownDataError(
            "delegate_task has no task envelope, so there is no workflow, command, state version or "
            "release to build the child's task from. A delegation that invented those would be "
            "asking a specialist to act on an unidentified workflow (§5)."
        )
    text = _child_goal(goal)
    # Bound here, and checked here, rather than at the send: a coordinator with no
    # transport must fail before it has built a child task it cannot deliver.
    port = _as_port(call)
    supplied = call.payload.get("specialist_inputs", {})
    document = supplied.get(target.value) if isinstance(supplied, Mapping) else None
    if not isinstance(document, Mapping):
        raise UnknownDataError(
            f"delegate_task has no workflow-supplied input document for {target.value}; "
            "a goal is not a specialist's structured input"
        )
    # Only the target's own validated fields cross the hop. In particular, Matching refuses identity.
    validated_input = (
        schema_for(target, direction="input").model_validate(document).model_dump(mode="json")
    )

    workflow_id = id_text(parent.workflow_id)
    command_id = id_text(parent.command_id)
    wanted_constraints = None if constraint_ids is None else {str(item) for item in constraint_ids}
    available_constraints = list(parent.constraints or [])
    if wanted_constraints is not None:
        child_constraints = [
            constraint
            for constraint in available_constraints
            if id_text(constraint.constraint_id) in wanted_constraints
        ]
        unknown = sorted(
            wanted_constraints
            - {id_text(constraint.constraint_id) for constraint in available_constraints}
        )
        if unknown:
            raise UnknownDataError(
                f"delegate_task was asked for constraints {unknown}, which the caller's own envelope "
                f"does not carry. A delegation narrows what the caller holds; it is never a way to "
                "give a specialist a constraint nobody recorded (§2.1)."
            )
    else:
        child_constraints = available_constraints

    available_evidence = _evidence_ref_ids(parent.evidence_refs or [])
    if evidence_ids is not None:
        wanted_evidence = [str(item) for item in evidence_ids]
        unknown_evidence = sorted(set(wanted_evidence) - set(available_evidence))
        if unknown_evidence:
            raise UnknownDataError(
                f"delegate_task was asked to pass evidence {unknown_evidence}, which this turn's "
                "envelope does not reference. A citation must be a fact the workflow already holds "
                "(§5)."
            )
        child_evidence = wanted_evidence
    else:
        child_evidence = available_evidence

    # The child's envelope: the caller's own, with the task's own identity (a new
    # task id, the parent's as `parent_task_id`), the goal, and the two fields the
    # caller was allowed to narrow. Everything else is carried forward byte for byte.
    child = TaskEnvelope(
        task_id=deterministic_uuid("task", workflow_id, command_id, target.value, text),
        workflow_id=workflow_id,
        command_id=command_id,
        parent_task_id=id_text(parent.task_id),
        goal=text,
        workflow_state=parent.workflow_state,
        # `.root`, because `StateVersion` is a `RootModel`: `int(StateVersion(...))` is a
        # `TypeError`, and the child's envelope would fail to build every time.
        current_state_version=int(parent.current_state_version.root),
        constraints=child_constraints or None,
        evidence_refs=([{"evidence_id": item} for item in child_evidence] or None),
        allowed_actions=list(parent.allowed_actions),
        deadline=parent.deadline,
        call_budget=parent.call_budget,
        release_manifest_id=id_text(parent.release_manifest_id),
        patient_details_id=parent.patient_details_id,
        synthetic=parent.synthetic,
    )

    request: dict[str, Any] = {
        "workflow_id": workflow_id,
        "session_id": f"{workflow_id}:{command_id}",
        "workflow_state": call.workflow_state,
        "task": text,
        # Derived: the same delegation re-sent is the same task, not a second one.
        "idempotency_key": deterministic_uuid(
            "delegation", workflow_id, command_id, target.value, text
        ),
        "trace_id": str(call.payload.get("trace_id") or command_id),
        "payload": {
            **validated_input,
            # The receiving handler splits `task_envelope` off before validating the
            # role's own input (`handler.META_KEYS`), which is how the child gets the
            # authenticated workflow state and version.
            "task_envelope": child.model_dump(mode="json"),
            "command_id": command_id,
            **({"execution_mode": "workflow"} if call.payload.get("execution_plan") else {}),
        },
    }

    # Spend the delegation and receive the child budget. The child shares this
    # turn's deadline and counters, so every ceiling holds for the subtree too.
    with call.budget.delegation(target) as child_budget:
        if call.payload.get("execution_plan"):
            snapshot = child_budget.snapshot()
            branches = (
                2
                if all(step.get("independent") for step in call.payload["execution_plan"])
                and len(call.payload["execution_plan"]) == 2
                else 1
            )
            declaration = request["payload"]["task_envelope"]["call_budget"]
            declaration.update(
                remaining_model_calls=max(
                    0, (child_budget.caps.max_model_calls - snapshot.model_calls) // branches
                ),
                remaining_tool_calls=max(
                    0, (child_budget.caps.max_tool_calls - snapshot.tool_calls) // branches
                ),
                remaining_delegations=max(
                    0, (child_budget.caps.max_delegations - snapshot.delegations) // branches
                ),
            )
        answer = await port.send(target_role=target, request=request, budget=child_budget)

    missing = [key for key in _ANSWER_KEYS if key not in answer]
    if missing:
        raise UnknownDataError(
            f"{target.value} answered without {missing}; a reply that does not carry its protocol "
            "state and outcome cannot be read as a result."
        )
    return {
        "target_role": target.value,
        "task_id": id_text(child.task_id),
        "state": answer.get("state"),
        "outcome": answer.get("outcome"),
        "is_success": answer.get("is_success"),
        "requires_reconciliation": answer.get("requires_reconciliation", False),
        "response": dict(answer),
        "evidence_ids": [],
    }
