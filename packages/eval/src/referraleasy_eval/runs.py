"""Reading run records — the observations a replay produced (§14).

This module is a *boundary*, and it is written as one deliberately. Everything below it treats a
:class:`~referraleasy_eval.models.RunRecord` as a fact about what happened; this is the only place
that fact arrives from outside the process, so it is the only place it can be malformed. Every field
is parsed defensively and a record that cannot be understood raises rather than defaulting, because
a silently half-defaulted record is a run that scores well for having recorded nothing.

**Where records come from.** §14 requires offline replay that never recreates a live booking, so the
capture side is a replay harness rather than the live service: it drives the same cases through the
stack that is already running and writes the artifacts out as JSON. That harness is not part of this
package and is not written yet — see the CLI's error message for what a run file must contain, and
the report's coverage block for what happens when it contains less. The interface here is the
contract the two sides meet on.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from referraleasy_eval.models import (
    BookingRecord,
    ConstraintOutcome,
    Escalation,
    RunRecord,
    ToolCall,
)

__all__ = ["RunRecordError", "load_records", "records_from_payload"]


class RunRecordError(ValueError):
    """A record that cannot be trusted to describe a run."""


def _required(payload: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in payload:
        raise RunRecordError(
            f"{where}: no {key!r}. A record missing a field would score as though the field were "
            "empty, which reports a capture gap as a system property"
        )
    return payload[key]


def _booking(payload: Mapping[str, Any], where: str) -> BookingRecord:
    return BookingRecord(
        booking_id=str(_required(payload, "booking_id", where)),
        state=str(_required(payload, "state", where)),
        provider_id=str(payload.get("provider_id", "")),
        hospital_id=str(payload.get("hospital_id", "")),
        slot_id=str(payload.get("slot_id", "")),
        approval_id=payload.get("approval_id"),
    )


def _tool_call(payload: Mapping[str, Any], where: str) -> ToolCall:
    return ToolCall(
        name=str(_required(payload, "name", where)),
        arguments=dict(payload.get("arguments") or {}),
        outcome=str(payload.get("outcome", "ok")),
    )


def _constraint(payload: Mapping[str, Any], where: str) -> ConstraintOutcome:
    kind = str(_required(payload, "kind", where))
    if kind not in ("hard", "soft"):
        raise RunRecordError(
            f"{where}: constraint kind {kind!r} is neither 'hard' nor 'soft'. §4's rule is that a "
            "hard constraint is never relaxed without a decision, so a kind that reads as neither "
            "cannot be scored"
        )
    return ConstraintOutcome(
        code=str(_required(payload, "code", where)),
        kind=kind,
        satisfied=bool(_required(payload, "satisfied", where)),
        detail=str(payload.get("detail", "")),
    )


def _one(payload: Mapping[str, Any], index: int) -> RunRecord:
    where = f"record {index}"
    case_id = str(payload.get("case_id") or "")
    if not case_id:
        raise RunRecordError(f"{where}: no `case_id`, so it cannot be matched to a case")
    where = f"record {case_id!r}"

    escalation_payload = payload.get("escalation")
    escalation: Escalation | None = None
    if escalation_payload is not None:
        escalation = Escalation(
            handoff_id=str(_required(escalation_payload, "handoff_id", where)),
            reason_code=str(_required(escalation_payload, "reason_code", where)),
            ticket_id=escalation_payload.get("ticket_id"),
        )

    return RunRecord(
        case_id=case_id,
        workflow_id=str(payload.get("workflow_id", "")),
        outcome=str(payload.get("outcome", "")),
        final_state=str(payload.get("final_state", "")),
        selected_provider_id=payload.get("selected_provider_id"),
        selected_hospital_id=payload.get("selected_hospital_id"),
        selected_slot_id=payload.get("selected_slot_id"),
        bookings=tuple(_booking(entry, where) for entry in payload.get("bookings") or ()),
        tool_calls=tuple(_tool_call(entry, where) for entry in payload.get("tool_calls") or ()),
        delegated_roles=tuple(str(role) for role in payload.get("delegated_roles") or ()),
        evidence_ids=tuple(str(value) for value in payload.get("evidence_ids") or ()),
        constraints=tuple(_constraint(entry, where) for entry in payload.get("constraints") or ()),
        escalation=escalation,
        required_document_kinds=tuple(
            str(value) for value in payload.get("required_document_kinds") or ()
        ),
        attached_document_kinds=tuple(
            str(value) for value in payload.get("attached_document_kinds") or ()
        ),
        live_approval_ids=tuple(str(value) for value in payload.get("live_approval_ids") or ()),
        booking_attempts=int(payload.get("booking_attempts") or 0),
        latency_ms=int(payload.get("latency_ms") or 0),
        model_calls=int(payload.get("model_calls") or 0),
        tool_calls_made=int(payload.get("tool_calls_made") or 0),
        cost_usd=float(payload.get("cost_usd") or 0.0),
        synthetic=bool(payload.get("synthetic", True)),
    )


def records_from_payload(payload: Any) -> tuple[RunRecord, ...]:
    """Parse a decoded run file. Accepts either a bare list or a ``{"records": [...]}`` object."""
    # A bare list is what a capture harness writes most naturally; the object form is what this
    # module writes when it serialises, and it leaves room for the run's own metadata later.
    values = payload.get("records") if isinstance(payload, Mapping) else payload
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
        raise RunRecordError(
            "the run file is neither a list of records nor an object with a `records` list"
        )
    records = tuple(_one(entry, index) for index, entry in enumerate(values))
    seen: set[str] = set()
    for record in records:
        if record.case_id in seen:
            raise RunRecordError(
                f"{record.case_id!r} appears more than once. Scoring the second would silently "
                "replace the first, and which one survives would depend on file order"
            )
        seen.add(record.case_id)
    return records


def load_records(path: Path) -> tuple[RunRecord, ...]:
    """Read a run file. A missing file is an error with the reason, not an empty result."""
    if not path.is_file():
        raise RunRecordError(
            f"no run records at {path}. §14 evaluates a *replay*, so a run needs the artifacts a "
            "replay produced; without them every case would report as unmeasured and the report "
            "would look like a coverage problem in the dataset rather than the missing input it is"
        )
    return records_from_payload(json.loads(path.read_text(encoding="utf-8")))


def records_as_payload(records: Sequence[RunRecord]) -> Mapping[str, Any]:
    """The inverse, for tests and for the capture side to write what this module reads."""
    return {
        "records": [
            {
                "case_id": record.case_id,
                "workflow_id": record.workflow_id,
                "outcome": record.outcome,
                "final_state": record.final_state,
                "selected_provider_id": record.selected_provider_id,
                "selected_hospital_id": record.selected_hospital_id,
                "selected_slot_id": record.selected_slot_id,
                "bookings": [
                    {
                        "booking_id": booking.booking_id,
                        "state": booking.state,
                        "provider_id": booking.provider_id,
                        "hospital_id": booking.hospital_id,
                        "slot_id": booking.slot_id,
                        "approval_id": booking.approval_id,
                    }
                    for booking in record.bookings
                ],
                "tool_calls": [
                    {
                        "name": call.name,
                        "arguments": dict(call.arguments),
                        "outcome": call.outcome,
                    }
                    for call in record.tool_calls
                ],
                "delegated_roles": list(record.delegated_roles),
                "evidence_ids": list(record.evidence_ids),
                "constraints": [
                    {
                        "code": outcome.code,
                        "kind": outcome.kind,
                        "satisfied": outcome.satisfied,
                        "detail": outcome.detail,
                    }
                    for outcome in record.constraints
                ],
                "escalation": (
                    {
                        "handoff_id": record.escalation.handoff_id,
                        "reason_code": record.escalation.reason_code,
                        "ticket_id": record.escalation.ticket_id,
                    }
                    if record.escalation
                    else None
                ),
                "required_document_kinds": list(record.required_document_kinds),
                "attached_document_kinds": list(record.attached_document_kinds),
                "live_approval_ids": list(record.live_approval_ids),
                "booking_attempts": record.booking_attempts,
                "latency_ms": record.latency_ms,
                "model_calls": record.model_calls,
                "tool_calls_made": record.tool_calls_made,
                "cost_usd": record.cost_usd,
                "synthetic": record.synthetic,
            }
            for record in records
        ]
    }
