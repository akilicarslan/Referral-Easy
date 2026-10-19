"""Suspend and resume: state crosses the wait, a call never does.

The failure this module guards against is a *held call*: an LLM stream kept open
while a patient picks a hospital. The turn deadline is 30 seconds and a patient may
take a day, so a held call is either a promise the runtime cannot keep or a bill for
time in which nothing happens. Two assertions cover it — one behavioural (the
persisted record contains nothing turn-scoped) and one structural (this module has
no coroutine that could be left suspended).

The rest is the resume contract: a new turn gets a **full** deadline, and an answer
the agent never offered is refused rather than handed to a model.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from agent_runtime_doubles import FakeClock
from pydantic import ValidationError

from referraleasy_agent_runtime.budget import BudgetCaps, TurnBudget
from referraleasy_agent_runtime.errors import SuspensionError
from referraleasy_agent_runtime.roles import AgentRole
from referraleasy_agent_runtime.suspend import (
    NEVER_PERSISTED_KEYS,
    InMemorySuspensionStore,
    ResumePlan,
    Suspension,
    SuspensionReason,
    build_resume_plan,
    resume,
    suspend_turn,
)

SUSPEND_MODULE = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "agent-runtime"
    / "src"
    / "referraleasy_agent_runtime"
    / "suspend.py"
)

CHOICES = ("hospital-a", "hospital-b")


def _suspend_turn(store: InMemorySuspensionStore, **overrides: object) -> tuple[Suspension, Any]:
    fields: dict[str, Any] = {
        "store": store,
        "role": AgentRole.SCHEDULING,
        "reason": SuspensionReason.PATIENT_CHOICE,
        "question": "Which of these hospitals would you prefer?",
        "workflow_id": "wf-1",
        "session_id": "sess-1",
        "workflow_state": "AWAITING_SLOT_CHOICE",
        "idempotency_key": "wf-1:choose-hospital",
        "trace_id": "trace-1",
        "choices": CHOICES,
        "skills": ("booking-confirmation",),
    }
    fields.update(overrides)
    suspension, result = suspend_turn(**fields)
    return suspension, result


def _suspend(store: InMemorySuspensionStore, **overrides: object) -> Suspension:
    return _suspend_turn(store, **overrides)[0]


def test_a_suspended_turn_ends_with_input_required_not_a_failure() -> None:
    suspension, result = _suspend_turn(InMemorySuspensionStore())

    assert result.a2a_state.value == "input-required"
    assert result.protocol_is_terminal is False, "a suspended task must remain resumable"
    assert result.outcome is not None and result.outcome.value == "needs_input"
    assert suspension.result() == result


def test_the_persisted_record_carries_state_and_no_turn_scoped_value() -> None:
    suspension = _suspend(InMemorySuspensionStore())
    payload = suspension.to_persisted()

    assert NEVER_PERSISTED_KEYS & payload.keys() == set(), "a budget crossed the wait"
    for key in ("deadline", "remaining_ms", "budget", "caps", "lease", "attempt", "stream"):
        assert key not in payload

    # What *is* carried: enough for a new turn to continue, and no more.
    assert payload["workflow_id"] == "wf-1"
    assert payload["workflow_state"] == "AWAITING_SLOT_CHOICE"
    assert payload["idempotency_key"] == "wf-1:choose-hospital"
    assert payload["choices"] == list(CHOICES)
    assert payload["skills"] == ["booking-confirmation"]


def test_a_mutated_budget_does_not_change_the_persisted_record() -> None:
    """`budget` is accepted and discarded: the old turn's clock is not the new turn's."""
    store = InMemorySuspensionStore()
    caps = BudgetCaps.from_env({})

    first, _ = suspend_turn(
        store=store,
        role=AgentRole.SCHEDULING,
        reason=SuspensionReason.APPROVAL,
        question="Approve?",
        workflow_id="wf-1",
        session_id="sess-1",
        workflow_state="AWAITING_FINAL_APPROVAL",
        idempotency_key="key-1",
        trace_id="trace-1",
        budget=TurnBudget(caps, role=AgentRole.SCHEDULING),
    )

    assert "budget" not in first.to_persisted()
    assert first.role is AgentRole.SCHEDULING


def test_the_suspension_expires_so_an_abandoned_wait_does_not_live_forever() -> None:
    suspension = _suspend(InMemorySuspensionStore())

    assert suspension.expires_at is not None
    assert suspension.expires_at > suspension.created_at
    assert suspension.expires_at - suspension.created_at == timedelta(days=7)


def test_the_module_has_no_coroutine_to_leave_suspended() -> None:
    """Structural: an `async def` here is a call someone could keep open across a wait."""
    tree = ast.parse(SUSPEND_MODULE.read_text(encoding="utf-8"))

    offenders = [node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)]
    assert offenders == [], (
        "suspension must persist state and end the turn; a coroutine would have to be "
        f"awaited across a human wait. Found: {offenders}"
    )

    import referraleasy_agent_runtime.suspend as suspend_module

    for name in ("suspend_turn", "resume", "build_resume_plan", "InMemorySuspensionStore.save"):
        owner, _, attribute = name.rpartition(".")
        target = getattr(suspend_module if not owner else getattr(suspend_module, owner), attribute)
        assert not inspect.iscoroutinefunction(target), name


def test_resuming_gets_a_full_new_deadline_not_a_remainder(clock: FakeClock) -> None:
    """The suspended turn ended; charging the resume for it punishes a fast patient."""
    store = InMemorySuspensionStore()
    suspension = _suspend(store)

    plan = resume(store, suspension.id, {"choice": "hospital-a"})
    caps = BudgetCaps.from_env({})
    # A pinned clock rather than the wall clock. `remaining_ms` is `deadline - now`, so reading a
    # real clock makes the equality below hold only if it lands in the same millisecond the budget
    # was constructed — true in isolation, and it failed under the full suite. `fresh_budget` takes
    # a clock for exactly this reason.
    budget = plan.fresh_budget(caps, clock=clock)

    assert budget.remaining_ms() == caps.turn_deadline_ms
    assert budget.remaining_ms() == 30_000
    assert budget.snapshot().model_calls == 0
    assert budget.snapshot().tool_calls == 0
    assert budget.attempts_used("model") == 0
    assert budget.next_attempt("model") == 1, "the resumed turn's first attempt is attempt one"


def test_the_resume_plan_carries_the_workflow_state_and_the_answer() -> None:
    store = InMemorySuspensionStore()
    suspension = _suspend(store)

    plan = resume(store, suspension.id, {"choice": "hospital-b", "note": "closer to home"})

    assert isinstance(plan, ResumePlan)
    assert plan.workflow_state == "AWAITING_SLOT_CHOICE"
    assert plan.workflow_id == "wf-1"
    assert plan.session_id == "sess-1"
    assert plan.idempotency_key == suspension.idempotency_key
    assert plan.answer["choice"] == "hospital-b"
    assert plan.skills == ("booking-confirmation",)


def test_the_resume_trace_entry_says_what_was_resumed() -> None:
    store = InMemorySuspensionStore()
    suspension = _suspend(store)

    entry = resume(store, suspension.id, {"choice": "hospital-a"}).trace_entry()

    assert entry["resumed_suspension"] == suspension.id
    assert entry["role"] == "scheduling"
    assert entry["skills"] == ["booking-confirmation"]
    assert entry["answer_keys"] == ["choice"]


def test_an_answer_the_agent_never_offered_is_refused() -> None:
    """A choice outside the offered list is a bug or an attempt to change the booking."""
    store = InMemorySuspensionStore()
    suspension = _suspend(store)

    with pytest.raises(SuspensionError, match="not one of the choices offered"):
        build_resume_plan(suspension, {"choice": "hospital-z", "approved": True})


def test_a_suspension_without_offered_choices_accepts_a_free_form_answer() -> None:
    store = InMemorySuspensionStore()
    suspension = _suspend(store, choices=(), idempotency_key="wf-1:free")

    plan = build_resume_plan(suspension, {"document": "insurance_card.pdf"})

    assert plan.answer == {"document": "insurance_card.pdf"}


def test_resuming_an_unknown_suspension_fails_loudly() -> None:
    with pytest.raises(SuspensionError, match="never created"):
        resume(InMemorySuspensionStore(), "no-such-suspension", {})


def test_the_store_is_idempotent_on_the_key_that_names_the_turn() -> None:
    """A redelivered suspend must not create a second turn against the first."""
    store = InMemorySuspensionStore()
    first = _suspend(store)

    again = Suspension(
        workflow_id=first.workflow_id,
        session_id=first.session_id,
        role=first.role,
        reason=first.reason,
        question=first.question,
        workflow_state=first.workflow_state,
        idempotency_key=first.idempotency_key,
        trace_id=first.trace_id,
    )
    with pytest.raises(SuspensionError, match="idempotency key"):
        store.save(again)


def test_resuming_the_same_suspension_twice_is_not_refused() -> None:
    """Reading is not consuming: the resumed turn is idempotent on the same key."""
    store = InMemorySuspensionStore()
    suspension = _suspend(store)

    first = resume(store, suspension.id, {"choice": "hospital-a"})
    second = resume(store, suspension.id, {"choice": "hospital-a"})

    assert first.idempotency_key == second.idempotency_key
    assert first.workflow_id == second.workflow_id


def test_the_record_round_trips_through_json() -> None:
    """What is persisted is the wire form, so a reload produces the same suspension."""
    suspension = _suspend(InMemorySuspensionStore(), choices=())
    payload = suspension.to_persisted()

    assert isinstance(payload["created_at"], str), "the record must be JSON-ready"
    reloaded = Suspension.model_validate(payload)

    assert reloaded.id == suspension.id
    assert reloaded.idempotency_key == suspension.idempotency_key
    assert reloaded.role is AgentRole.SCHEDULING
    assert reloaded.reason is SuspensionReason.PATIENT_CHOICE


def test_a_suspension_record_refuses_an_unexpected_field() -> None:
    """`extra="forbid"`: a typo in a persisted key is a loud failure, not a dropped field."""
    payload = _suspend(InMemorySuspensionStore()).to_persisted()
    payload["remaining_ms"] = 4200

    with pytest.raises(ValidationError, match="remaining_ms"):
        Suspension.model_validate(payload)


def test_the_reasons_cover_every_human_wait_the_workflow_has() -> None:
    assert {reason.value for reason in SuspensionReason} == {
        "patient_choice",
        "patient_input",
        "approval",
        "document",
        "human_handoff",
    }


def test_a_human_handoff_suspension_records_why_a_person_is_needed() -> None:
    store = InMemorySuspensionStore()

    suspension, result = suspend_turn(
        store=store,
        role=AgentRole.RECOVERY_HANDOFF,
        reason=SuspensionReason.HUMAN_HANDOFF,
        question="Two slots were double-booked; which should stand?",
        workflow_id="wf-2",
        session_id="sess-2",
        workflow_state="RECONCILIATION_REQUIRED",
        idempotency_key="wf-2:reconcile",
        trace_id="trace-2",
    )

    assert suspension.reason is SuspensionReason.HUMAN_HANDOFF
    assert result.a2a_state.value == "input-required"
    assert result.requires_reconciliation is False, "the human is the reconciliation"


def test_the_clock_can_be_pinned_so_expiry_is_deterministic() -> None:
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

    suspension = _suspend(InMemorySuspensionStore(), now=now)

    assert suspension.created_at == now
    assert suspension.expires_at == now + timedelta(days=7)
