"""Suspending on ``needs_input`` — and resuming without a live model call.

A patient choosing a hospital, a human approving a booking, a missing document:
these waits are measured in minutes or days, while the turn budget is 30 seconds.
So the agent does not wait. It **ends the turn** with ``needs_input`` and persists
enough state for a *new* turn to continue later.

Why no model call is kept alive
-------------------------------
Holding an open call across a human wait is wrong on four counts, and each one is a
way the demo would fail:

* **The budget forbids it.** The turn deadline is 30 s; a held call would have to
  exceed it, which means the deadline is a lie.
* **It costs money while nothing happens.** A streamed completion pinned open for
  ten minutes is billed for ten minutes of provider time, and the patient's choice
  is usually the *smallest* part of the interaction.
* **It does not survive a restart.** A deploy, a crash or a scale-to-zero would
  drop the call and the answer with it; a persisted suspension survives all three.
* **It cannot be reconciled.** An in-memory waiter has no idempotency key, so a
  redelivered answer would create a second turn against a first one still holding
  the thread.

What is persisted is therefore *state*, never a call: the reason, the question, the
state the workflow was in, the effective skills, and the idempotency key. The
remaining budget is deliberately **not** persisted — see :class:`Suspension` — and
:meth:`ResumePlan.fresh_budget` gives the resumed turn a full new deadline, because
the turn the suspension belonged to is over.

This module contains no ``async def`` at all: there is no coroutine here that could
be left suspended on a wait. (Pinned by a test.)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from referraleasy_agent_runtime.budget import BudgetCaps, Clock, TurnBudget
from referraleasy_agent_runtime.errors import SuspensionError
from referraleasy_agent_runtime.results import needs_input
from referraleasy_agent_runtime.roles import AgentRole
from referraleasy_contracts import AgentTaskResult

__all__ = [
    "NEVER_PERSISTED_KEYS",
    "InMemorySuspensionStore",
    "ResumePlan",
    "Suspension",
    "SuspensionReason",
    "SuspensionStore",
    "build_resume_plan",
    "resume",
    "suspend_turn",
]

#: Keys that must never appear in a persisted suspension. Asserted by a test: their
#: presence would mean a turn's remaining time had been carried across a human wait,
#: which is exactly the "held call" shape this module exists to avoid.
NEVER_PERSISTED_KEYS: frozenset[str] = frozenset(
    {
        "deadline",
        "deadline_ms",
        "remaining_ms",
        "budget",
        "caps",
        "lease",
        "attempt",
        "attempts",
        "model_call",
        "stream",
    }
)

#: How long an unanswered suspension stays resumable before it is stale.
DEFAULT_SUSPENSION_TTL = timedelta(days=7)


class SuspensionReason(StrEnum):
    """Why the turn could not finish without a human or a patient."""

    PATIENT_CHOICE = "patient_choice"
    PATIENT_INPUT = "patient_input"
    APPROVAL = "approval"
    DOCUMENT = "document"
    HUMAN_HANDOFF = "human_handoff"


class Suspension(BaseModel):
    """A turn that ended because it needs an answer.

    This is the whole persisted record. Note what is absent: no budget, no
    deadline, no open call, no coroutine handle. Resuming starts a **new** turn
    from the workflow state, not a continuation of the old one.

    Where it lives durably: the suspending ``task`` row is closed as ``succeeded``
    with this object in its ``output`` (the ``task.state`` domain has no
    ``suspended`` value — *ended* is the accurate word), keyed by
    ``idempotency_key``; a ``HUMAN_HANDOFF`` suspension additionally raises a row in
    ``handoff`` so an operator queue can see it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    workflow_id: str
    session_id: str
    role: AgentRole
    reason: SuspensionReason
    question: str
    workflow_state: str
    idempotency_key: str
    trace_id: str
    choices: tuple[str, ...] = ()
    expected_answer_schema: Mapping[str, Any] | None = None
    skills: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    def to_persisted(self) -> dict[str, Any]:
        """The JSON-ready record, checked against :data:`NEVER_PERSISTED_KEYS`.

        The check runs on the way out, so a future field that would smuggle a
        budget across a wait fails here rather than in production.
        """
        payload = self.model_dump(mode="json")
        leaked = NEVER_PERSISTED_KEYS & payload.keys()
        if leaked:
            raise SuspensionError(
                f"a suspension must not persist turn-scoped state: found {sorted(leaked)}"
            )
        return payload

    def result(self) -> AgentTaskResult:
        """Project to the protocol: ``needs_input`` → ``input-required`` (not terminal)."""
        return needs_input()


@runtime_checkable
class SuspensionStore(Protocol):
    """Where a suspension waits. A database in production, a dict in a test."""

    def save(self, suspension: Suspension) -> None:
        """Persist the suspension, idempotently on ``idempotency_key``."""
        ...

    def load(self, suspension_id: str) -> Suspension | None:
        """Fetch a suspension, or ``None`` if it never existed or has been resolved."""
        ...


class InMemorySuspensionStore:
    """A dict-backed store: enough for a single process, a test or a local run."""

    def __init__(self) -> None:
        self._by_id: dict[str, Suspension] = {}
        self._by_key: dict[str, str] = {}

    def save(self, suspension: Suspension) -> None:
        existing = self._by_key.get(suspension.idempotency_key)
        if existing is not None and existing != suspension.id:
            raise SuspensionError(
                "a different suspension already exists for idempotency key "
                f"{suspension.idempotency_key!r}; answers must resume the original turn"
            )
        self._by_id[suspension.id] = suspension
        self._by_key[suspension.idempotency_key] = suspension.id

    def load(self, suspension_id: str) -> Suspension | None:
        return self._by_id.get(suspension_id)


def suspend_turn(
    *,
    store: SuspensionStore,
    role: AgentRole,
    reason: SuspensionReason,
    question: str,
    workflow_id: str,
    session_id: str,
    workflow_state: str,
    idempotency_key: str,
    trace_id: str,
    choices: Sequence[str] = (),
    expected_answer_schema: Mapping[str, Any] | None = None,
    skills: Sequence[str] = (),
    budget: TurnBudget | None = None,
    now: datetime | None = None,
) -> tuple[Suspension, AgentTaskResult]:
    """End the turn with ``needs_input``, persisting the state a resume will need.

    ``budget`` is accepted so a caller can pass the turn budget it is holding — and
    is then **discarded**. Nothing about the exhausted turn travels through the
    wait; only workflow state does.
    """
    created_at = now if now is not None else datetime.now(UTC)
    suspension = Suspension(
        workflow_id=workflow_id,
        session_id=session_id,
        role=role,
        reason=reason,
        question=question,
        workflow_state=workflow_state,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        choices=tuple(choices),
        expected_answer_schema=expected_answer_schema,
        skills=tuple(skills),
        created_at=created_at,
        expires_at=created_at + DEFAULT_SUSPENSION_TTL,
    )
    store.save(suspension)
    return suspension, suspension.result()


class ResumePlan(BaseModel):
    """Everything a *new* turn needs in order to continue a suspended one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suspension_id: str
    role: AgentRole
    workflow_state: str
    workflow_id: str
    session_id: str
    idempotency_key: str
    trace_id: str
    answer: Mapping[str, Any] = Field(default_factory=dict)
    skills: tuple[str, ...] = ()
    resumed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def fresh_budget(self, caps: BudgetCaps, *, clock: Clock | None = None) -> TurnBudget:
        """A **new** turn budget — a full ``TURN_DEADLINE_MS``, not a remainder.

        The suspended turn had, by definition, no time left: it ended. Charging the
        resumed turn for it would make an impatient answer slower than a patient
        one.
        """
        return TurnBudget(caps, clock=clock, role=self.role)

    def trace_entry(self) -> dict[str, Any]:
        return {
            "resumed_suspension": self.suspension_id,
            "role": self.role.value,
            "workflow_state": self.workflow_state,
            "skills": list(self.skills),
            "resumed_at": self.resumed_at.isoformat(),
            "answer_keys": sorted(self.answer),
        }


def build_resume_plan(suspension: Suspension, answer: Mapping[str, Any]) -> ResumePlan:
    """Validate an incoming answer against the suspension and plan the new turn.

    An answer that does not match a fixed choice list is refused here
    (:class:`SuspensionError`) rather than handed to a model: a patient choice that
    the agent did not offer is either a bug or an attempt to change a booking the
    patient never approved.
    """
    if suspension.choices and answer.get("choice") not in suspension.choices:
        raise SuspensionError(
            f"{answer.get('choice')!r} is not one of the choices offered by suspension "
            f"{suspension.id}: {list(suspension.choices)}"
        )
    return ResumePlan(
        suspension_id=suspension.id,
        role=suspension.role,
        workflow_state=suspension.workflow_state,
        workflow_id=suspension.workflow_id,
        session_id=suspension.session_id,
        idempotency_key=suspension.idempotency_key,
        trace_id=suspension.trace_id,
        answer=MappingProxyType(dict(answer)),
        skills=suspension.skills,
    )


def resume(
    store: SuspensionStore,
    suspension_id: str,
    answer: Mapping[str, Any],
) -> ResumePlan:
    """Load a suspension and plan the new turn. No model call is created here."""
    suspension = store.load(suspension_id)
    if suspension is None:
        raise SuspensionError(
            f"no suspension {suspension_id!r}: it was never created, or its turn already resumed"
        )
    return build_resume_plan(suspension, answer)
