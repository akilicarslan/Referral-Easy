"""§9's output guardrail, run by the agents on every result before it leaves the service.

`referraleasy_guardrails.output.OutputGuardrail` asks three questions of a :class:`SourceOracle` —
does this entity exist, what time does this slot start, what state is this booking in — and the
ids it asks about live in the hospital API and the directory. The agents are where those answers
already are: every tool result of the turn is on the :class:`ToolSet`. So the oracle here answers
from the turn's own tool results and the task's own inputs, and nothing else. An id that appears in
neither was not retrieved by this turn, and a result that shows it to a patient is refused.

What is checked is the **patient-facing projection** of a result (:func:`patient_facing`), not the
whole document. A result carries plenty that a patient never sees — evidence ids, freshness, the
raw availability query — and running a content check over those would refuse a slot for carrying
`status: available` (which is not a booking state) or a referral for carrying the referring
clinician's own wording. The projection is the set of values the gateway turns into cards: option
ids and their labels, slot ids with their times, the booking's id with its state and its sentence,
a coverage status with its evidence, and every sentence addressed to the patient.

A refusal is a typed refusal of the turn (`terminal_error`, stage `output_guardrail`), with every
violation named in the trace — never a silently trimmed result.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Final

from referraleasy_agent_runtime import AgentRole
from referraleasy_agents.tools.toolbox import ToolSet
from referraleasy_guardrails.output import (
    ENTITY_ID_FIELDS,
    OutputCheckResult,
    OutputGuardrail,
    OutputRejected,
    OutputViolation,
)

__all__ = ["TurnOracle", "check_result", "guard_result", "patient_facing"]

#: Keys under which a tool result or an input names an entity. The guardrail's own list, plus the
#: contract's spelling of a booking's id.
_ID_KEYS: Final = ENTITY_ID_FIELDS | {"booking_request_id", "appointment_id"}


def _walk(value: Any) -> Iterator[tuple[str, Any, Mapping[str, Any]]]:
    """Every (key, value, containing mapping) in a JSON-like document."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item, value
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)


class TurnOracle:
    """A :class:`~referraleasy_guardrails.output.SourceOracle` over one turn's tool results.

    The inputs count as a source because they are the gateway's recorded workflow facts (the plan,
    the chosen hospital and doctor) that the task was built from — not anything a model wrote.
    """

    def __init__(self, results: Iterable[Any], inputs: Mapping[str, Any] | None = None) -> None:
        self._ids: set[str] = set()
        self._slot_times: dict[str, str] = {}
        self._booking_states: dict[str, str] = {}
        documents = [*results, dict(inputs or {})]
        for document in documents:
            for key, value, holder in _walk(document):
                if key in _ID_KEYS:
                    for item in value if isinstance(value, list) else [value]:
                        if isinstance(item, str) and item:
                            self._ids.add(item)
                if (
                    key == "slot_id"
                    and isinstance(value, str)
                    and isinstance(holder.get("start_at"), str)
                ):
                    self._slot_times[value] = holder["start_at"]
                if key == "booking_request_id" and isinstance(holder.get("state"), str):
                    self._booking_states[str(value)] = holder["state"]
        self.sources: tuple[str, ...] = tuple(
            json.dumps(document, sort_keys=True, default=str) for document in documents
        )

    def entity_exists(self, entity_id: str) -> bool:
        return entity_id in self._ids

    def slot_time(self, slot_id: str) -> str | None:
        return self._slot_times.get(slot_id)

    def booking_state(self, booking_id: str) -> str | None:
        return self._booking_states.get(booking_id)


def _sentences(output: Mapping[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for key in ("patient_message", "patient_question"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            found[key] = value
    for key, value, _ in _walk(output):
        if key == "question_for_patient" and isinstance(value, str) and value.strip():
            found.setdefault("question_for_patient", value)
    return found


def patient_facing(role: AgentRole, output: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The groups of values from `output` that a patient will be shown, each checked on its own.

    Grouped because two checks pair values: a time is verified against *its* slot, and a booking
    sentence against *its* booking's state. One flat bag would pair every time with every slot.
    """
    sentences = _sentences(output)
    groups: list[dict[str, Any]] = []
    if role is AgentRole.PROVIDER_MATCHING:
        candidates = [c for c in output.get("candidates") or () if isinstance(c, Mapping)]
        labels = output.get("directory_labels") or {}
        groups.append(
            {
                "provider_ids": [str(c["provider_id"]) for c in candidates if c.get("provider_id")],
                "hospital_ids": [str(c["hospital_id"]) for c in candidates if c.get("hospital_id")],
                # A mapping, not a list: the guardrail's text checks walk mapping values and skip
                # bare strings inside a list, so a list of labels would never be read.
                "labels": {str(key): str(label) for key, label in labels.items()},
            }
        )
    elif role is AgentRole.SCHEDULING:
        for slot in output.get("slots") or ():
            if isinstance(slot, Mapping):
                groups.append(
                    {
                        key: slot[key]
                        for key in ("slot_id", "start_at", "hospital_id", "provider_id")
                        if slot.get(key) is not None
                    }
                )
        booking = output.get("booking_request")
        if isinstance(booking, Mapping) and booking.get("booking_request_id"):
            # The sentence travels with the state it narrates: "confirmed" over a REQUESTED
            # booking is the §2.5 failure this pairing exists to catch.
            groups.append(
                {
                    "booking_id": str(booking["booking_request_id"]),
                    "booking_state": booking.get("state"),
                    **sentences,
                }
            )
            sentences = {}
    elif role is AgentRole.COVERAGE_NETWORK:
        coverage = output.get("coverage")
        if isinstance(coverage, Mapping):
            groups.append(
                {
                    key: coverage[key]
                    for key in ("plan_id", "provider_id", "hospital_id")
                    if coverage.get(key)
                }
                | {
                    "coverage_status": coverage.get("status"),
                    "evidence_ids": list(coverage.get("evidence_ids") or ()),
                }
            )
        plan = output.get("plan")
        if isinstance(plan, Mapping) and plan.get("plan_id"):
            groups.append({"plan_id": plan["plan_id"]})
    elif role is AgentRole.REFERRAL_READINESS:
        checks = [c for c in output.get("checks") or () if isinstance(c, Mapping)]
        groups.append(
            {"document_id": [str(c["document_id"]) for c in checks if c.get("document_id")]}
        )
    elif role is AgentRole.RECOVERY_HANDOFF:
        handoff = output.get("handoff")
        if isinstance(handoff, Mapping):
            groups.append(
                {
                    key: value
                    for key, value in handoff.items()
                    if key in {"patient_visible_summary", "summary"} and isinstance(value, str)
                }
            )
    if sentences:
        groups.append(sentences)
    return [
        group for group in groups if any(value not in (None, [], "") for value in group.values())
    ]


def check_result(
    role: AgentRole,
    output: Mapping[str, Any],
    *,
    results: Sequence[Any],
    inputs: Mapping[str, Any] | None = None,
) -> OutputCheckResult:
    """Every patient-facing group of `output`, checked against the turn's own records."""
    oracle = TurnOracle(results, inputs)
    guardrail = OutputGuardrail(oracle)
    violations: list[OutputViolation] = []
    checked: list[str] = []
    for group in patient_facing(role, output):
        result = guardrail.check_props(group, sources=oracle.sources)
        violations.extend(result.violations)
        checked.extend(name for name in result.checked if name not in checked)
    return OutputCheckResult(
        ok=not violations, violations=tuple(violations), checked=tuple(checked)
    )


def guard_result(role: AgentRole, output: Any, tools: ToolSet) -> OutputCheckResult:
    """Check a role's output before it leaves the service; raise `OutputRejected` on a violation."""
    document = output.model_dump(mode="json") if hasattr(output, "model_dump") else dict(output)
    result = check_result(
        role,
        document,
        results=[payload for _, payload in tools.results],
        inputs=tools.payload,
    )
    if not result.ok:
        raise OutputRejected(result)
    return result
