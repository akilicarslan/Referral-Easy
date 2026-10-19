"""Shared names for the agent tests: payloads, outputs, a stub API, and one turn runner.

A plain module with a unique name, not `conftest.py`, for the reason
`tests/skills/skills_fixtures.py` gives: two `conftest.py` files on `sys.path` under
the bare name `conftest` shadow each other, and which one a test got would depend on
collection order. Import from here.

What is here, and why each thing is a constant rather than a literal in a test:

* **`ROLES`, `CARD_NAME_BY_ROLE`, `SKILL_BY_ROLE`** — §3's seven roles and the skill
  each one owns (§6), so a test can iterate the vocabulary instead of restating it.
* **`StubApi`** — an `Api` that answers from a table and records every call. The
  handler tests must exercise the real `ToolSet.invoke` path (gate, lease, ledger)
  without a socket, so only the transport is replaced.
* **`minimal_output(role, ...)`** — the smallest output each role's own schema
  accepts. A handler test asserts "the turn produced a valid answer, and here is how
  it was projected", and it needs a valid answer to start from.
* **`run_turn(...)`** — one turn, offline, through the real ADK `Runner`.

The IDs are fixed literals rather than generated: a test that asserts a downstream id
should be able to say "this is the provider id" without running a fixture generator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from google.adk.sessions import InMemorySessionService

from referraleasy_agent_runtime import (
    AgentRole,
    AgentTaskRequest,
    BudgetCaps,
    TurnBudget,
    TurnReport,
)
from referraleasy_agents.cards import CARD_NAME_BY_ROLE, SUB_AGENT_NAMES
from referraleasy_agents.handler import AgentService, RoleHandler
from referraleasy_agents.offline_model import ScriptedLlm, ScriptStep, scripted_model
from referraleasy_agents.schemas import schema_for
from referraleasy_agents.tools.toolbox import ToolBox, ToolSet
from referraleasy_contracts.generated.task import TaskEnvelope

__all__ = [
    "API_URL",
    "API_URL_ENV",
    "CARD_NAME_BY_ROLE",
    "COMMAND_ID",
    "CONSTRAINT_ID",
    "HOSPITAL_ID",
    "PATIENT_ID",
    "PLAN_ID",
    "PROVIDER_ID",
    "RELEASE_MANIFEST_ID",
    "ROLES",
    "SKILL_BY_ROLE",
    "SLOT_ID",
    "SUB_AGENT_NAMES",
    "TASK_ID",
    "WORKFLOW_ID",
    "AllowAllPolicy",
    "StubApi",
    "body_for",
    "checkpoint",
    "constraint",
    "decision",
    "envelope",
    "handler_for",
    "minimal_output",
    "payload_for",
    "run_turn",
    "toolset_for",
    "valid_output",
]

#: The mock provider API, as `Makefile`/`scripts/health.py` name it.
API_URL_ENV = "MOCK_API_URL"
API_URL = "http://localhost:4010"

#: §3's seven roles, in the order the architecture lists them.
ROLES: tuple[AgentRole, ...] = (
    AgentRole.CARE_COORDINATOR,
    AgentRole.REFERRAL_INTAKE,
    AgentRole.COVERAGE_NETWORK,
    AgentRole.PROVIDER_MATCHING,
    AgentRole.REFERRAL_READINESS,
    AgentRole.SCHEDULING,
    AgentRole.RECOVERY_HANDOFF,
)

#: §6's skill package to owning role, exactly. The complement is the §3 assertion that
#: Care Coordinator owns none.
SKILL_BY_ROLE: Mapping[AgentRole, tuple[str, ...]] = {
    AgentRole.REFERRAL_INTAKE: ("referral-intake",),
    AgentRole.COVERAGE_NETWORK: ("coverage-interpretation",),
    AgentRole.PROVIDER_MATCHING: ("constraint-negotiation", "directory-grounding"),
    AgentRole.REFERRAL_READINESS: ("referral-readiness",),
    AgentRole.SCHEDULING: ("booking-confirmation", "uncertain-booking-reconciliation"),
    AgentRole.RECOVERY_HANDOFF: ("handoff-triage",),
}

#: Fixed ids. Coherent across the fixtures so a payload and an output can be read
#: together, and obviously synthetic.
PATIENT_ID = "0f0f0f0f-0000-4000-8000-000000000001"
PROVIDER_ID = "0f0f0f0f-0000-4000-8000-000000000002"
HOSPITAL_ID = "0f0f0f0f-0000-4000-8000-000000000003"
PLAN_ID = "0f0f0f0f-0000-4000-8000-000000000004"
SLOT_ID = "0f0f0f0f-0000-4000-8000-000000000005"
REFERRAL_ID = "0f0f0f0f-0000-4000-8000-000000000006"
EVIDENCE_ID = "0f0f0f0f-0000-4000-8000-000000000007"
WORKFLOW_ID = "0f0f0f0f-0000-4000-8000-000000000008"
COMMAND_ID = "0f0f0f0f-0000-4000-8000-000000000009"
TASK_ID = "0f0f0f0f-0000-4000-8000-00000000000c"

#: A release manifest id in the format the migration and cards use.
RELEASE_MANIFEST_ID = "rel-20261014-abc1234"

#: A timestamp, fixed so a fixture's output is byte-comparable across runs.
NOW = "2026-01-05T09:00:00+00:00"


class AllowAllPolicy:
    """A policy backend that permits everything, for tests about something else.

    The default-deny table lives in `policy_table.py`; this exists so a test about,
    say, the trace shape is not silently also a test about the policy table. A test
    that is about authority uses `InMemoryPolicy`.
    """

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, str]] = []

    def policy_assert(self, role: str, tool: str, workflow_state: str) -> None:
        self.asked.append((role, tool, workflow_state))


class StubApi:
    """An `Api` that answers from a table of ``path -> body`` and records every call.

    Two things it deliberately does *not* do: it does not validate the query string
    against the path (a test that cares passes an exact key), and it does not raise on
    an unknown path — it raises `KeyError`, because a tool reaching a route the test
    did not expect is a bug in the test's understanding of the tool.
    """

    def __init__(self, responses: Mapping[str, Any] | None = None) -> None:
        self.responses: dict[str, Any] = dict(responses or {})
        self.calls: list[tuple[str, str, Mapping[str, Any]]] = []

    def _answer(self, path: str) -> Any:
        if path in self.responses:
            return self.responses[path]
        raise KeyError(
            f"StubApi has no response for {path!r}; routes it knows: {sorted(self.responses)}"
        )

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        budget: Any = None,
        lease: Any = None,
    ) -> dict[str, Any]:
        del budget, lease
        self.calls.append(("GET", path, dict(params or {})))
        answer = self._answer(path)
        return answer if isinstance(answer, dict) else {"data": answer}

    async def post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        budget: Any = None,
        lease: Any = None,
    ) -> dict[str, Any]:
        del budget, lease
        self.calls.append(("POST", path, dict(payload)))
        answer = self._answer(path)
        return answer if isinstance(answer, dict) else {"data": answer}


def decision(
    *,
    action: str,
    reason_code: str = "ok",
    evidence_ids: Sequence[str] = (),
    missing_fields: Sequence[Mapping[str, Any]] = (),
    next_allowed_actions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """A §4 decision output. `next_allowed_actions` defaults to the action itself."""
    return {
        "action": action,
        "reason_code": reason_code,
        "evidence_ids": [str(item) for item in evidence_ids],
        "missing_fields": [dict(item) for item in missing_fields],
        "next_allowed_actions": list(next_allowed_actions or [action]),
    }


def _coverage(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "coverage_id": "0f0f0f0f-0000-4000-8000-00000000000a",
        "plan_id": PLAN_ID,
        "accepts_insurance": True,
        "in_network_for_plan": True,
        "status": "in_network",
        "referral_required": True,
        "prior_auth_required": False,
        "freshness": {"state": "fresh", "age_seconds": 60},
        "evidence_ids": [EVIDENCE_ID],
        "synthetic": True,
    }
    body.update(overrides)
    return body


def minimal_output(role: AgentRole, **overrides: Any) -> dict[str, Any]:
    """The smallest valid output for ``role``. Validated by a test, not assumed."""
    if role is AgentRole.CARE_COORDINATOR:
        body: dict[str, Any] = {
            "plan": [],
            "patient_question": None,
            "decision": decision(action="care_coordinator:delegate_task"),
        }
    elif role is AgentRole.REFERRAL_INTAKE:
        body = {
            "draft": {
                "referral_id": REFERRAL_ID,
                "patient_id": PATIENT_ID,
                "specialty": "cardiology",
            },
            "completed_intake": None,
            "missing_fields": [],
            "contradictions": [],
            "decision": decision(action="referral_intake:validate_referral"),
        }
    elif role is AgentRole.COVERAGE_NETWORK:
        body = {
            "coverage": _coverage(),
            "conflict": None,
            "decision": decision(action="coverage_network:check_plan"),
        }
    elif role is AgentRole.PROVIDER_MATCHING:
        body = {
            "candidates": [],
            "alternatives": [],
            "constraint_change": None,
            "mandatory_conditions": {
                "specialty_identified": True,
                "approval_present": False,
                "slot_valid": False,
            },
            "decision": decision(action="provider_matching:find_candidates"),
        }
    elif role is AgentRole.REFERRAL_READINESS:
        body = {
            "readiness": {
                "readiness_id": "0f0f0f0f-0000-4000-8000-00000000000b",
                "workflow_id": WORKFLOW_ID,
                "hospital_id": HOSPITAL_ID,
                "visit_type": "first_visit",
                "checks": [],
                "satisfied": True,
                "evidence_ids": [EVIDENCE_ID],
                "checked_at": NOW,
            },
            "checks": [],
            "requirements": [],
            "decision": decision(action="referral_readiness:validate_referral_package"),
        }
    elif role is AgentRole.SCHEDULING:
        body = {
            "slots": [],
            "availability": None,
            "booking_request": None,
            "appointment": None,
            "attempt": None,
            "reconciliation": None,
            "patient_message": "Here are the times that are open; nothing is booked yet.",
            "decision": decision(action="scheduling:list_slots"),
        }
    elif role is AgentRole.RECOVERY_HANDOFF:
        body = {
            "handoff": None,
            "action_plan": {
                "failure_class": "technical_outage",
                "retry_same_agent": True,
                "return_state": "RECOVERY",
                "permitted_actions": ["recovery_handoff:classify_error"],
            },
            "patient_message": "Something on our side did not answer; we are retrying.",
            "decision": decision(action="recovery_handoff:classify_error"),
        }
    else:  # pragma: no cover - AgentRole is closed
        raise AssertionError(f"no fixture for {role}")
    body.update(overrides)
    return body


def body_for(role: AgentRole, **overrides: Any) -> dict[str, Any]:
    """A valid role input for ``role`` — the part of a payload the schema owns.

    `payload_for` adds the orchestration keys on top; this is the half that must
    satisfy the role's own `extra="forbid"` input schema, and no test needs both.
    """
    bodies: Mapping[AgentRole, dict[str, Any]] = {
        AgentRole.CARE_COORDINATOR: {
            "patient_message": "I need to see a heart doctor near Chicago.",
            "workflow_summary": "The patient asked for a cardiology referral.",
            "completed_steps": [],
        },
        AgentRole.REFERRAL_INTAKE: {
            "referral_source": "Referral ID: REF-1\nSpecialty: cardiology\nIssued: 2026-01-05",
            "patient_id": PATIENT_ID,
            "document_ids": [],
        },
        AgentRole.COVERAGE_NETWORK: {"plan_id": PLAN_ID, "hospital_id": HOSPITAL_ID},
        AgentRole.PROVIDER_MATCHING: {
            "specialty": "cardiology",
            "origin": {"latitude": 41.8956, "longitude": -87.6227},
            "constraints": [],
            "hospital_candidates": [],
        },
        AgentRole.REFERRAL_READINESS: {
            "hospital_id": HOSPITAL_ID,
            "patient_id": PATIENT_ID,
            "visit_type": "first_visit",
            "document_ids": [],
        },
        AgentRole.SCHEDULING: {
            "hospital_id": HOSPITAL_ID,
            "provider_id": PROVIDER_ID,
            "visit_type": "first_visit",
            "patient_details_id": PATIENT_ID,
        },
        AgentRole.RECOVERY_HANDOFF: {
            "failed_role": "coverage_network",
            "error_class": "UpstreamTransient",
            "transient_attempts": 1,
        },
    }
    return {**bodies[role], **overrides}


def payload_for(role: AgentRole, **overrides: Any) -> dict[str, Any]:
    """A whole task payload: the role's input plus the orchestration metadata.

    `command_id` is here and **not** in the role's input schema, on purpose: the
    handler splits `META_KEYS` off before validating, and a schema that accepted the
    command id would be a schema every future key could be added to.
    """
    return {"command_id": COMMAND_ID, **body_for(role, **overrides)}


def checkpoint() -> str:
    """A timestamp string that moves, for a test that needs one to differ."""
    return datetime.now(UTC).isoformat()


#: The envelope's deadline, fixed so a fixture is byte-comparable across runs.
DEADLINE = "2030-01-05T09:30:00+00:00"

#: A constraint id, for a fixture that narrows an envelope's constraint set by id.
CONSTRAINT_ID = "0f0f0f0f-0000-4000-8000-00000000000d"


def constraint(
    *,
    constraint_id: str = CONSTRAINT_ID,
    field: str = "max_distance_miles",
    kind: str = "hard",
    value: Any = 25.0,
    source: str = "patient_stated",
    recorded_at: str = NOW,
) -> dict[str, Any]:
    """One `Constraint`, as the envelope and a tool argument both carry it."""
    return {
        "constraint_id": constraint_id,
        "field": field,
        "kind": kind,
        "value": value,
        "source": source,
        "recorded_at": recorded_at,
    }


def envelope(
    *,
    workflow_state: str = "BOOKING_IN_FLIGHT",
    current_state_version: int = 7,
    allowed_actions: Sequence[str] = (),
    command_id: str = COMMAND_ID,
    task_id: str = TASK_ID,
    parent_task_id: str | None = None,
    constraints: Sequence[Mapping[str, Any]] | None = None,
    evidence_refs: Sequence[str] | None = None,
    patient_details_id: str | None = None,
    remaining_delegations: int = 4,
) -> TaskEnvelope:
    """A §5 `TaskEnvelope`, built rather than mocked.

    Every field a tool reads is a fact this function produces: the state version the
    approval guard compares against, the allowed actions a decision is checked
    against, the evidence a delegate may narrow to. A `Mock` would prove the reads
    against values nobody wrote.
    """
    return TaskEnvelope(
        task_id=task_id,
        parent_task_id=parent_task_id,
        workflow_id=WORKFLOW_ID,
        command_id=command_id,
        goal="run one turn of this specialist under the caller's budget",
        workflow_state=workflow_state,
        current_state_version=current_state_version,
        constraints=[dict(item) for item in constraints] if constraints is not None else None,
        evidence_refs=[{"evidence_id": item} for item in evidence_refs]
        if evidence_refs is not None
        else None,
        allowed_actions=list(allowed_actions),
        deadline=DEADLINE,
        call_budget={
            "deadline_at": DEADLINE,
            "remaining_model_calls": 12,
            "remaining_tool_calls": 16,
            "remaining_delegations": remaining_delegations,
        },
        release_manifest_id=RELEASE_MANIFEST_ID,
        patient_details_id=patient_details_id,
        synthetic=True,
    )


def toolset_for(
    role: AgentRole,
    *,
    api: Any,
    policy: Any,
    budget: TurnBudget | None = None,
    workflow_state: str = "INTAKE",
    envelope: Any = None,
    payload: Mapping[str, Any] | None = None,
) -> tuple[ToolBox, ToolSet]:
    """A `ToolBox` and one started `ToolSet`, exactly as the handler builds them."""
    box = ToolBox(role=role, api=api, policy=policy)
    return box, box.start(
        budget=budget or TurnBudget(BudgetCaps()),
        envelope=envelope,
        payload=payload,
        workflow_state=workflow_state,
    )


def handler_for(
    role: AgentRole,
    *,
    model: ScriptedLlm,
    api: Any = None,
    policy: Any = None,
    sessions: Any = None,
    skill_source: Any = None,
    sub_agent_models: Mapping[str, Any] | None = None,
) -> RoleHandler:
    """A `RoleHandler` over an offline model — no socket, no credential."""
    return RoleHandler(
        AgentService(
            role=role,
            api=api if api is not None else StubApi(),
            policy=policy if policy is not None else AllowAllPolicy(),
            sessions=sessions if sessions is not None else InMemorySessionService(),
            skill_source=skill_source,
            model=model,
            sub_agent_models=sub_agent_models,
        )
    )


async def run_turn(
    role: AgentRole,
    *,
    script: Sequence[ScriptStep],
    workflow_state: str,
    payload: Mapping[str, Any] | None = None,
    handler: RoleHandler | None = None,
    budget: TurnBudget | None = None,
    task: str = "Run one turn.",
    **handler_kwargs: Any,
) -> TurnReport:
    """Run one turn offline and return its report.

    The caller supplies the script; everything else is the real path: the real
    `Runner`, the real `ToolSet`, the real output-schema validation.
    """
    resolved = handler or handler_for(role, model=scripted_model(script), **handler_kwargs)
    request = AgentTaskRequest(
        workflow_id=WORKFLOW_ID,
        session_id="session-1",
        workflow_state=workflow_state,
        task=task,
        idempotency_key="idem-1",
        trace_id="trace-1",
        payload=dict(payload if payload is not None else payload_for(role)),
    )
    return await resolved(request, budget or TurnBudget(BudgetCaps(), role=role))


def valid_output(role: AgentRole, **overrides: Any) -> Any:
    """`minimal_output(role)`, validated through the role's own schema."""
    return schema_for(role, direction="output").model_validate(minimal_output(role, **overrides))
