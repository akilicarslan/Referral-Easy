# Turkish quoted from §3 of ReferralEasy_Architecture_v3.md is kept verbatim
"""The runtime's `TaskHandler`, implemented once for all seven specialists.

One turn, in a fixed order, with each step saying what it refuses:

1. **Split the payload.** Two keys are the turn's own orchestration metadata rather
   than the role's input — the task envelope and the command id — and they are named
   in `META_KEYS` so that "everything else goes to the schema" is true. Nothing else
   is filtered: a payload key that is not in the role's input schema is a validation
   failure, which is §6's context minimisation enforced rather than requested.
2. **Validate the input** against the role's versioned input schema *before* any
   model call. A payload a specialist must not see (a date of birth on the matching
   route) never reaches the model, and the refusal names the field paths and nothing
   of their values.
3. **Select skills** for this role and state, and load their bodies. Skills inform a
   prompt; they grant no tool.
4. **Open the ADK session** under §7's keys, so a redelivered command resumes the
   session it started and a new command starts a new one (ADR-0033).
5. **Run one ADK turn** — a real `Runner` over a real `Agent` whose tools are this
   turn's `ToolSet`, so every tool call passes the allowlist gate and the policy
   table, and both the tool calls and the model calls are charged to the turn's one
   `TurnBudget`.
6. **Validate the output** against the role's output schema, then apply the two
   checks a schema cannot make: the patient-facing sentences are refused if they
   promise coverage or describe an unconfirmed booking as confirmed, and
   `next_allowed_actions` is **recomputed from the policy table** (§5: it is computed,
   not proposed by the model).
7. **Project the outcome** through the runtime's `results` helpers, suspending the
   turn when a patient or a human has to answer (§8), and never re-running a turn to
   reword a failure.

Two things the handler deliberately does not do: it does not retry (the retry owner is
the runtime's `run_with_retries`, and a second owner would double-count attempts), and
it does not repair a malformed answer with another model call — a malformed answer is a
terminal failure for the turn, reported as one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai import types
from pydantic import ValidationError

from referraleasy_agent_runtime import (
    AgentRole,
    AgentTaskRequest,
    AgentTaskResult,
    EmptySkillSource,
    InMemorySuspensionStore,
    PolicyBackend,
    SkillSelection,
    SkillSource,
    SkillSourceError,
    SkillsPackageSource,
    SuspensionReason,
    SuspensionStore,
    TurnBudget,
    TurnReport,
    load_instructions,
    needs_input,
    no_match,
    retryable,
    select_skills,
    succeeded,
    suspend_turn,
    terminal_error,
    unknown_outcome,
)
from referraleasy_agents.assembly import (
    adk_node_name,
    budgeted_model,
    build_agent,
    build_runner,
    resolve_llm,
    session_key,
)
from referraleasy_agents.canonical import id_text
from referraleasy_agents.decisions import (
    assert_no_payment_guarantee,
    assert_state_wording,
    next_allowed_actions,
)
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.offline_model import ScriptStep
from referraleasy_agents.prompts import build_instruction
from referraleasy_agents.schemas import (
    output_schema_id,
    schema_for,
)
from referraleasy_agents.tools.call import Api, DelegationPort
from referraleasy_agents.tools.toolbox import ToolBox, ToolSet
from referraleasy_contracts.generated.booking import BookingRequest
from referraleasy_contracts.generated.common import ReasonCode
from referraleasy_contracts.generated.task import DecisionOutput, TaskEnvelope

__all__ = [
    "IDENTITY_FIELDS",
    "META_KEYS",
    "AgentService",
    "RoleHandler",
    "handler_for_role",
    "project_outcome",
]


#: Payload keys that are the turn's orchestration metadata rather than the role's own
#: input. Named one by one, because the alternative — "whatever the schema does not
#: know" — would be a hole in `extra="forbid"` and with it in §6.
META_KEYS: Final[tuple[str, ...]] = ("task_envelope", "command_id", "execution_mode")

#: Fields no specialist in this pipeline may ask for or carry (§6). Matching never
#: receives a date of birth or a phone number; scheduling fetches them through
#: `patient_details_id` at the tool layer. A question about one of these fields from
#: one of these agents means identity is travelling the wrong road, and the turn is
#: refused rather than asked around.
IDENTITY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "address",
        "birth_date",
        "contact_phone",
        "date_of_birth",
        "dob",
        "email",
        "email_address",
        "first_name",
        "full_name",
        "home_address",
        "last_name",
        "patient_name",
        "phone",
        "phone_number",
        "social_security_number",
        "ssn",
        "street_address",
    }
)

#: Reason codes that mean "an answer has to come from outside this turn" (§8), and who
#: has to give it. Kept as data so the projection is one table a reviewer can read.
_SUSPENSION_REASONS: Final[Mapping[ReasonCode, SuspensionReason]] = {
    ReasonCode.missing_mandatory_field: SuspensionReason.PATIENT_INPUT,
    ReasonCode.ambiguous_input: SuspensionReason.PATIENT_INPUT,
    ReasonCode.document_missing: SuspensionReason.DOCUMENT,
    ReasonCode.document_unreadable: SuspensionReason.DOCUMENT,
    ReasonCode.approval_expired: SuspensionReason.APPROVAL,
    ReasonCode.approval_payload_changed: SuspensionReason.APPROVAL,
    ReasonCode.human_owned: SuspensionReason.HUMAN_HANDOFF,
}

#: Reason codes that mean "a successful call whose answer is nothing matched" (§8).
_NO_MATCH_REASONS: Final[frozenset[ReasonCode]] = frozenset(
    {
        ReasonCode.no_candidate_matched,
        ReasonCode.no_slot_available,
        ReasonCode.hard_constraint_unsatisfiable,
    }
)

#: Reason codes that name a failure a fresh attempt may clear.
_RETRYABLE_REASONS: Final[frozenset[ReasonCode]] = frozenset(
    {
        ReasonCode.upstream_unavailable,
        ReasonCode.upstream_rate_limited,
        ReasonCode.upstream_timeout,
        ReasonCode.upstream_malformed_response,
    }
)

#: Reason codes that name a failure no retry fixes in this turn.
_TERMINAL_REASONS: Final[frozenset[ReasonCode]] = frozenset(
    {
        ReasonCode.policy_blocked,
        ReasonCode.guardrail_blocked,
        ReasonCode.budget_exhausted,
        ReasonCode.deadline_exceeded,
    }
)


def project_outcome(reason_code: ReasonCode) -> AgentTaskResult:
    """``reason_code`` projected onto one of the six outcomes (§5).

    Total by construction: every member of `ReasonCode` is in exactly one of the
    buckets below or in the `succeeded` default, and `test_outcome_projection.py`
    asserts that over the enum rather than over a list someone remembered to update.

    The default is `succeeded` and not "unknown": a decision that carries a reason
    code and evidence *is* an answer — `coverage_unknown`, `conflicting_evidence` and
    `no_candidate_matched` are answers this system exists to give. What keeps the
    default honest is that the terminal, retryable and no-match buckets are named
    explicitly, so a code that means "this did not work" cannot land here by omission.
    """
    if reason_code in _NO_MATCH_REASONS:
        return no_match()
    if reason_code in _RETRYABLE_REASONS:
        return retryable()
    if reason_code in _TERMINAL_REASONS:
        return terminal_error()
    return succeeded()


@dataclass(frozen=True, slots=True)
class AgentService:
    """Everything a role's handler needs that outlives one turn.

    Each field is a seam a test can replace: the transport, the policy authority, the
    delegation port, the session store, the suspension store, the skill source, the
    environment, and the model.
    """

    role: AgentRole
    api: Api
    policy: PolicyBackend | None = None
    delegation: DelegationPort | None = None
    sessions: BaseSessionService = field(default_factory=InMemorySessionService)
    suspensions: SuspensionStore = field(default_factory=InMemorySuspensionStore)
    skill_source: SkillSource | None = None
    env: Mapping[str, str] | None = None
    #: A model to use for the role's own agent, instead of resolving one. Tests and
    #: the offline profile pass a `ScriptedLlm` here.
    model: Any = None
    #: Per ADK node name, for Provider Matching's two sub-agents.
    sub_agent_models: Mapping[str, Any] | None = None
    #: Per ADK node name offline scripts, when no model was supplied for that node.
    scripts: Mapping[str, Sequence[ScriptStep]] | None = None

    def toolbox(self) -> ToolBox:
        """The per-service tool authority and transport. Built fresh, held nowhere."""
        return ToolBox(
            role=self.role,
            api=self.api,
            policy=self.policy,
            delegation=self.delegation,
        )

    def skill_source_or_empty(self) -> SkillSource:
        """The configured source, or the skills package, or nothing at all.

        A deployment that ships no skills must still serve turns: an absent skills
        package degrades to no skills and records why, rather than refusing to run.
        """
        if self.skill_source is not None:
            return self.skill_source
        try:
            return SkillsPackageSource()
        except Exception:  # pragma: no cover - depends on the installation
            return EmptySkillSource()


class RoleHandler:
    """One role's `TaskHandler`: an ADK turn, wrapped in the runtime's guarantees."""

    def __init__(self, service: AgentService) -> None:
        self.service = service

    @property
    def role(self) -> AgentRole:
        return self.service.role

    async def __call__(self, request: AgentTaskRequest, budget: TurnBudget) -> TurnReport:
        """Run one turn of this role, within ``budget``."""
        payload, envelope, command_id = _split_payload(request.payload)
        inputs = _validated_input(self.role, payload)
        if isinstance(inputs, _Refused):
            return input_refusal(self.role, request, inputs)
        try:
            selection, skills_text = self._select_skills(request.workflow_state)
        except SkillSourceError as error:
            return _refusal("skills", request, error)

        tools = self.service.toolbox().start(
            budget=budget,
            envelope=envelope,
            payload=inputs.value.model_dump(mode="json"),
            workflow_state=request.workflow_state,
        )
        if request.payload.get("execution_mode") == "workflow":
            from referraleasy_agents.workflow import execute_workflow

            output = await execute_workflow(tools, self.role, inputs.value.model_dump(mode="json"))
            return TurnReport(
                result=(
                    unknown_outcome()
                    if output.decision.reason_code is ReasonCode.booking_outcome_unknown
                    else needs_input()
                    if output.decision.reason_code in _SUSPENSION_REASONS
                    else project_outcome(output.decision.reason_code)
                ),
                output=output.model_dump(mode="json"),
                trace={"execution_mode": "workflow", "tools": tools.trace_entries()},
            )
        key = session_key(self.role, workflow_id=request.workflow_id, command_id=command_id)
        await self._open_session(key, request, command_id)
        agent = build_agent(
            self.role,
            tools=tools,
            instruction=build_instruction(self.role, skills=skills_text),
            model=self._model_for(adk_node_name(self.role), budget),
            sub_agent_models={
                name: self._model_for(name, budget)
                for name in (self.service.sub_agent_models or {})
            }
            or None,
        )
        runner = build_runner(self.role, agent=agent, sessions=self.service.sessions)
        text = await self._run(runner, key, request, inputs.value)

        parsed = _validated_output(self.role, text)
        if isinstance(parsed, _Refused):
            return _refusal("output", request, parsed.error, tools=tools, selection=selection)
        output = self._guarded_output(parsed.value, tools)
        return await self._report(
            request=request,
            output=output,
            tools=tools,
            selection=selection,
            key=key,
            budget=budget,
            session_state=await self._state(key),
        )

    # ── the pieces ───────────────────────────────────────────────────────────────────────────

    def _select_skills(self, workflow_state: str) -> tuple[SkillSelection, str]:
        source = self.service.skill_source_or_empty()
        selection = select_skills(source, role=self.role, workflow_state=workflow_state)
        return selection, load_instructions(source, selection)

    def _model_for(self, node_name: str, budget: TurnBudget) -> Any:
        """The budgeted model for one ADK node, charged to this turn.

        Every model handed out is wrapped, including a pre-supplied one: the turn's
        ``max_model_calls`` cap and the lease's timeout are the runtime's, and a
        caller that could hand over an unwrapped model could hand over an unmetered
        loop.
        """
        service = self.service
        prebuilt: Any = None
        if node_name == adk_node_name(self.role):
            prebuilt = service.model
        elif service.sub_agent_models is not None:
            prebuilt = service.sub_agent_models.get(node_name)
        if prebuilt is not None:
            return budgeted_model(prebuilt, budget=budget)
        script = (service.scripts or {}).get(node_name, ())
        return budgeted_model(
            resolve_llm(service.role, env=service.env, script=script), budget=budget
        )

    async def _open_session(self, key: Any, request: AgentTaskRequest, command_id: str) -> None:
        """Open the command's session, or reuse the one a redelivery already opened."""
        existing = await self.service.sessions.get_session(
            app_name=key.app_name, user_id=key.user_id, session_id=key.session_id
        )
        if existing is not None:
            return
        await self.service.sessions.create_session(
            app_name=key.app_name,
            user_id=key.user_id,
            session_id=key.session_id,
            state={
                "workflow_id": request.workflow_id,
                "command_id": command_id,
                "workflow_state": request.workflow_state,
            },
        )

    async def _state(self, key: Any) -> Mapping[str, Any]:
        session = await self.service.sessions.get_session(
            app_name=key.app_name, user_id=key.user_id, session_id=key.session_id
        )
        return dict(session.state) if session is not None else {}

    async def _run(
        self,
        runner: Any,
        key: Any,
        request: AgentTaskRequest,
        payload: Mapping[str, Any],
    ) -> str:
        """Run the ADK turn and return its final text.

        A typed failure raised inside a tool (a refused tool call, an exhausted
        budget) propagates out of here untouched: ADK re-raises it, and the app
        projects it. Swallowing it would turn a refusal into an answer.
        """
        message = types.Content(
            role="user",
            parts=[types.Part(text=_turn_prompt(self.role, request, payload))],
        )
        final = ""
        async for event in runner.run_async(
            user_id=key.user_id, session_id=key.session_id, new_message=message
        ):
            if not event.is_final_response():
                continue
            content = event.content
            if content is None or not content.parts:
                continue
            final = "".join(part.text or "" for part in content.parts) or final
        return final

    def _guarded_output(self, output: Any, tools: ToolSet) -> Any:
        """Apply the checks a schema cannot: patient-facing wording, and `allowed`.

        The decision's citations are checked against what the tools actually
        retrieved — a decision that cites evidence this turn never fetched is a
        fabricated citation, and it is refused (§5).
        """
        _guard_patient_text(output)
        retrieved = set(tools.citation_ids())
        cited = [id_text(item) for item in getattr(output.decision, "evidence_ids", ()) or ()]
        unknown = [item for item in cited if item not in retrieved]
        if unknown:
            raise UnknownDataError(
                f"the decision cites evidence ids this turn did not retrieve: {sorted(unknown)}. "
                f"Retrieved: {sorted(retrieved)}. A citation must be a fact this turn read (§5)."
            )
        return _with_computed_actions(self.role, output, tools)

    async def _report(
        self,
        *,
        request: AgentTaskRequest,
        output: Any,
        tools: ToolSet,
        selection: SkillSelection,
        key: Any,
        budget: TurnBudget,
        session_state: Mapping[str, Any],
    ) -> TurnReport:
        decision = output.decision
        reason_code = decision.reason_code
        trace = _trace(request, tools, selection, key, output, session_state)
        reason = _SUSPENSION_REASONS.get(reason_code)
        question = _question_for(output)

        if reason is not None or question is not None:
            suspension_reason = reason or SuspensionReason.PATIENT_CHOICE
            suspension, result = suspend_turn(
                store=self.service.suspensions,
                role=self.role,
                reason=suspension_reason,
                question=question or "A person has to answer before this workflow can continue.",
                workflow_id=request.workflow_id,
                session_id=key.session_id,
                workflow_state=request.workflow_state,
                idempotency_key=request.idempotency_key,
                trace_id=request.trace_id,
                choices=_choice_ids(output),
                expected_answer_schema=_answer_schema(decision, suspension_reason),
                skills=selection.names,
                budget=budget,
            )
            return TurnReport(
                result=result,
                skills=selection.names,
                trace={**trace, "suspension": suspension.id},
                suspension=suspension,
                output=output.model_dump(mode="json"),
            )

        if reason_code is ReasonCode.booking_outcome_unknown:
            return TurnReport(
                result=unknown_outcome(),
                skills=selection.names,
                trace=trace,
                output=output.model_dump(mode="json"),
            )
        return TurnReport(
            result=project_outcome(reason_code),
            skills=selection.names,
            trace=trace,
            output=output.model_dump(mode="json"),
        )


def handler_for_role(service: AgentService) -> RoleHandler:
    """The `TaskHandler` for ``service``'s role. One call, one object."""
    return RoleHandler(service)


# ── input and payload ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Refused:
    """A refusal, carrying the reason it will be reported with."""

    error: Exception
    detail: str


def _split_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], TaskEnvelope | None, str]:
    """Split the payload into (role input, envelope, command id).

    The envelope is validated rather than trusted: it is what the tool gate reads its
    workflow state from, and a malformed one must be refused here, where the field
    names are still visible, instead of surfacing as an opaque tool refusal.
    """
    body = {key: value for key, value in payload.items() if key not in META_KEYS}
    envelope: TaskEnvelope | None = None
    raw_envelope = payload.get("task_envelope")
    if raw_envelope is not None:
        envelope = TaskEnvelope.model_validate(raw_envelope)
    command_id = payload.get("command_id")
    if command_id is None and envelope is not None:
        command_id = id_text(envelope.command_id)
    if command_id is None:
        raise ValueError(
            "a task request must carry `command_id` in its payload (or an envelope that has "
            "one): the command is half of the key this turn's session is filed under, and a "
            "turn keyed by nothing cannot be resumed or recognised as a redelivery."
        )
    if envelope is not None and str(command_id) != id_text(envelope.command_id):
        # Two command ids in one request is a request whose session key and whose envelope
        # describe different commands. Whichever one a reader chose, half the turn would be
        # filed under the other, so it is refused rather than resolved.
        raise ValueError(
            f"the payload's command_id {str(command_id)!r} is not the envelope's "
            f"{id_text(envelope.command_id)!r}: the turn's session is keyed by the payload's "
            "command, and its tool calls carry the envelope's. One request, one command."
        )
    return body, envelope, str(command_id)


def _validated_input(role: AgentRole, payload: Mapping[str, Any]) -> Any:
    """The role's input schema, validated. ``_Refused`` names the offending fields."""
    schema = schema_for(role, direction="input")
    try:
        return _Ok(schema.model_validate(payload))
    except ValidationError as error:
        # Field paths and error types only: an invalid *value* may be exactly the
        # identity §6 keeps out of this agent's context, and echoing it back would
        # defeat the refusal it triggered.
        return _Refused(error, _error_paths(error))


def _validated_output(role: AgentRole, text: str) -> Any:
    """The role's output schema, validated against the turn's final text."""
    schema = schema_for(role, direction="output")
    if not text.strip():
        return _Refused(
            ValueError(f"{role.value} produced no final answer"),
            f"the turn ended without a final response; the output schema {output_schema_id(role)} "
            "was never satisfied",
        )
    candidate = _json_object(text)
    if candidate is None:
        return _Refused(
            ValueError("the final answer was not a JSON object"),
            f"the final answer is not a JSON object, so it cannot be validated against "
            f"{output_schema_id(role)}. Length: {len(text)} characters.",
        )
    try:
        return _Ok(schema.model_validate(candidate))
    except ValidationError as error:
        return _Refused(error, f"{output_schema_id(role)}: {_error_paths(error)}")


def _json_object(text: str) -> Any:
    """The JSON object in ``text``, when there is one.

    Two shapes are accepted and no more: the object itself, or one fenced block
    around it. There is no third attempt and no repair pass — a model that answered
    with prose did not answer, and asking it again is the retry owner's decision, not
    this function's.
    """
    import json

    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except ValueError:
        fenced = _fenced_block(stripped)
        if fenced is None:
            return None
        try:
            parsed = json.loads(fenced)
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _fenced_block(text: str) -> str | None:
    start = text.find("```")
    if start < 0:
        return None
    body = text[start + 3 :]
    newline = body.find("\n")
    if newline < 0:
        return None
    body = body[newline + 1 :]
    end = body.find("```")
    if end < 0:
        return None
    return body[:end].strip()


def _error_paths(error: ValidationError) -> str:
    """The failing field paths and error types, with no values."""
    parts = []
    for item in error.errors():
        location = ".".join(str(step) for step in item.get("loc", ()))
        parts.append(f"{location}: {item.get('type')}")
    return "; ".join(sorted(parts))


@dataclass(frozen=True, slots=True)
class _Ok:
    value: Any


def input_refusal(role: AgentRole, request: AgentTaskRequest, refused: _Refused) -> TurnReport:
    """Refuse a task whose payload the role's own input schema does not accept."""
    return TurnReport(
        result=terminal_error(),
        trace={
            "role": role.value,
            "trace_id": request.trace_id,
            "workflow_state": request.workflow_state,
            "refused": "input_schema",
            "errors": refused.detail,
        },
    )


def _refusal(
    stage: str,
    request: AgentTaskRequest,
    error: Exception,
    *,
    tools: ToolSet | None = None,
    selection: SkillSelection | None = None,
) -> TurnReport:
    """Refuse a turn whose ``stage`` failed, with no model call made for the wording."""
    detail = getattr(error, "detail", None) or getattr(error, "message", None) or str(error)
    return TurnReport(
        result=terminal_error(),
        skills=selection.names if selection is not None else (),
        trace={
            "trace_id": request.trace_id,
            "workflow_state": request.workflow_state,
            "refused": stage,
            "error": f"{type(error).__name__}: {detail}",
            "tools": tools.trace_entries() if tools is not None else [],
        },
    )


# ── the output guards ────────────────────────────────────────────────────────────────────────


def _guard_patient_text(output: Any) -> None:
    """Refuse a patient-facing sentence that promises coverage or overstates a booking.

    Only the fields that are *about* the patient's next step are checked: a
    `patient_message`, a `patient_question`, or a sub-agent's
    `question_for_patient`. Everything else is a structured field and is governed by
    its own type.
    """
    for sentence in _patient_sentences(output):
        assert_no_payment_guarantee(sentence)
        booking = getattr(output, "booking_request", None)
        if isinstance(booking, BookingRequest):
            assert_state_wording(sentence, booking.state)


def _patient_sentences(output: Any) -> list[str]:
    sentences: list[str] = []
    for field_name in ("patient_message", "patient_question"):
        value = getattr(output, field_name, None)
        if isinstance(value, str) and value.strip():
            sentences.append(value)
    for candidate in _analysis_parts(output):
        question = getattr(candidate, "question_for_patient", None)
        if isinstance(question, str) and question.strip():
            sentences.append(question)
    return sentences


def _analysis_parts(output: Any) -> list[Any]:
    """A matching output's per-candidate analyses, wherever they are nested.

    §3 puts the analyst's `question_for_patient` on the candidate analysis, so the
    guard has to reach it: a sentence the patient reads is the same sentence whether
    the parent agent wrote it or its analyst did.
    """
    parts: list[Any] = []
    for field_name in ("constraint_analysis", "analysis"):
        value = getattr(output, field_name, None)
        if value is None:
            continue
        parts.extend(getattr(value, "candidates", []) or [])
    return parts


def _with_computed_actions(role: AgentRole, output: Any, tools: ToolSet) -> Any:
    """Replace `next_allowed_actions` with the policy table's answer (§5).

    The model's list is not merged and not trusted: §5 says the next allowed actions
    are *computed*, so a policy backend that is configured replaces whatever came
    back. Without one, nothing is invented — the model's own list stands and the trace
    says it could not be checked.
    """
    policy = tools.box.policy
    state = tools.workflow_state
    if policy is None or not state:
        return output
    allowed = next_allowed_actions(policy, state)
    decision = output.decision
    if [id_text(item) for item in decision.next_allowed_actions] == allowed:
        return output
    replacement = DecisionOutput.model_validate(
        {**decision.model_dump(mode="json"), "next_allowed_actions": allowed}
    )
    return output.model_copy(update={"decision": replacement})


def _with_replacement_note(output: Any, tools: ToolSet) -> dict[str, Any]:
    """What the recomputation changed, for the trace. Absent when nothing changed."""
    policy = tools.box.policy
    if policy is None or not tools.workflow_state:
        return {"next_allowed_actions": "not checked: no policy backend is configured"}
    return {}


# ── the question, and the suspension's answer schema ─────────────────────────────────────────


def _question_for(output: Any) -> str | None:
    """The question to suspend on, from the output's own text or its missing fields."""
    for field_name in ("patient_question", "patient_message"):
        value = getattr(output, field_name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for candidate in _analysis_parts(output):
        question = getattr(candidate, "question_for_patient", None)
        if isinstance(question, str) and question.strip():
            return question.strip()
    fields = _missing_field_names(output)
    if fields:
        listed = ", ".join(fields)
        return f"The workflow is waiting on these fields before it can continue: {listed}."
    return None


def _missing_field_names(output: Any) -> list[str]:
    """The missing fields' names, refusing an identity field (§6)."""
    names = [
        str(getattr(item, "field", item))
        for item in getattr(output.decision, "missing_fields", ()) or ()
    ]
    identity = sorted(name for name in names if name.casefold() in IDENTITY_FIELDS)
    if identity:
        raise UnknownDataError(
            f"the decision asks for {identity}, which is identity data this specialist must not "
            "receive or request (§6): identity reaches the workflow through `patient_details_id` "
            "and the patient's own interface, never through this agent's question."
        )
    return names


def _choice_ids(output: Any) -> list[str]:
    """The alternatives' ids, for a suspension the patient answers by choosing one."""
    parts = _analysis_parts(output)
    choices: list[str] = []
    for part in parts:
        for option in getattr(part, "alternatives", ()) or ():
            choices.append(id_text(option.option_id))
    for option in getattr(output, "alternatives", ()) or ():
        choices.append(id_text(option.option_id))
    return choices


def _answer_schema(decision: DecisionOutput, reason: SuspensionReason) -> Mapping[str, Any] | None:
    """What a resume must supply, when that is knowable.

    For a patient-input wait the missing fields are exactly the answer's shape, and
    saying so is what lets a caller's form and the resume agree. For the other reasons
    the answer is a human's decision or a document, which has no field list here — so
    nothing is claimed rather than a schema being invented.
    """
    if reason is not SuspensionReason.PATIENT_INPUT:
        return None
    names = [str(getattr(item, "field", item)) for item in decision.missing_fields or ()]
    if not names:
        return None
    return {
        "type": "object",
        "required": sorted(names),
        "properties": {name: {"type": "string"} for name in sorted(names)},
        "additionalProperties": False,
    }


# ── the trace ────────────────────────────────────────────────────────────────────────────────


def _turn_prompt(role: AgentRole, request: AgentTaskRequest, payload: Mapping[str, Any]) -> str:
    """The user message for one turn: the task, the state, the input, the contract."""
    import json

    visible = dict(payload)
    if role is AgentRole.CARE_COORDINATOR:
        documents = visible.pop("specialist_inputs", {})
        visible["available_specialist_inputs"] = (
            sorted(documents) if isinstance(documents, Mapping) else []
        )
    return (
        f"Task: {request.task}\n"
        f"Workflow state: {request.workflow_state}\n"
        f"Workflow: {request.workflow_id}\n"
        f"Input:\n{json.dumps(visible, sort_keys=True, default=str)}\n\n"
        f"Answer with one JSON object that satisfies {output_schema_id(role)} and nothing else. "
        "Cite only evidence ids that a tool returned in this turn."
    )


def _trace(
    request: AgentTaskRequest,
    tools: ToolSet,
    selection: SkillSelection,
    key: Any,
    output: Any,
    session_state: Mapping[str, Any],
) -> dict[str, Any]:
    decision = output.decision
    return {
        "trace_id": request.trace_id,
        "workflow_id": request.workflow_id,
        "workflow_state": request.workflow_state,
        "session": key.as_trace_entry(),
        "skills": selection.trace_entry(),
        "tools": tools.trace_entries(),
        "evidence_ids": tools.citation_ids(),
        "decision": {
            "action": decision.action,
            "reason_code": str(decision.reason_code),
            "next_allowed_actions": [id_text(item) for item in decision.next_allowed_actions],
        },
        "sub_agent_state": {
            name: "present" for name, value in sorted(session_state.items()) if value
        },
        **_with_replacement_note(output, tools),
    }
