"""Application result -> A2A protocol task state.

§1.1 requires that application results map to standard A2A task states through an adapter, and §5
requires that the project's own result enum is **never** exposed as a protocol state. This module is
that adapter, and it implements the mapping table written verbatim in the ``$comment`` on
``A2ATaskState`` in ``schemas/common.json``. If you change the table, change it there first.

The sharp edge
--------------
``outcome_unknown`` projects to the protocol state ``failed`` — which is terminal — while the
booking is still unresolved and reconciliation is in flight (§7). A caller that reads only the
protocol state will conclude "no booking exists" and may attempt a second booking with a new key.
That is precisely the double-booking failure the project forbids.

So this module never hands out a bare ``A2ATaskState``. The only way to obtain one is
:class:`AgentTaskResult`, which always carries the application outcome beside it and exposes
:attr:`~AgentTaskResult.requires_reconciliation` so the distinction is impossible to skip.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from referraleasy_contracts.generated.common import A2ATaskState, ResultOutcome

__all__ = [
    "A2A_TASK_STATE_BY_EVENT",
    "A2A_TASK_STATE_BY_OUTCOME",
    "AgentTaskRequest",
    "AgentTaskResult",
    "ProtocolEvent",
    "project_event",
    "project_outcome",
]


class AgentTaskRequest(BaseModel):
    """One inbound A2A task — the request half of the envelope.

    It lives here rather than in `referraleasy_agent_runtime.app`, where it used to: the *servers*
    of this shape are the seven agent services, and as long as it lived in the runtime the only way
    to build one was to import a module that also knows how to serve it. The gateway now sends these
    (it dispatches a committed message to the coordinator), and a wire contract with a caller and a
    callee belongs in the contracts.

    Identity is **not** here — it comes from the transport, as a service token on the request. A
    payload that carried its own identity would be a claim, and `drop_untrusted_identity` exists
    because such claims must be dropped rather than believed.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    session_id: str
    workflow_state: str
    task: str
    #: Stable across a caller's retries of one logical task and distinct between two delegations.
    #: It is what makes §15's *logical* trigger count correct: retries hash to one `event.id`.
    idempotency_key: str
    trace_id: str
    payload: Mapping[str, Any] = Field(default_factory=dict)


class ProtocolEvent(StrEnum):
    """Triggers that produce a protocol state without producing an application result.

    `ResultOutcome` describes what an agent call returned. These three protocol states instead
    describe why no agent call result exists at all: the patient cancelled, a server-side policy
    refused the request before an agent ran, or service authentication failed (§5, §9).
    """

    PATIENT_CANCELLED = "patient_cancelled"
    POLICY_REFUSED = "policy_refused"
    AUTH_FAILED = "auth_failed"


#: The normative outcome -> protocol-state table. Read-only on purpose.
A2A_TASK_STATE_BY_OUTCOME: Mapping[ResultOutcome, A2ATaskState] = MappingProxyType(
    {
        ResultOutcome.completed: A2ATaskState.completed,
        ResultOutcome.needs_input: A2ATaskState.input_required,
        # A valid empty result is a *successful* call whose answer is "nothing matched" (§8) —
        # not an error, and explicitly not retried as one.
        ResultOutcome.no_match: A2ATaskState.completed,
        ResultOutcome.retryable_error: A2ATaskState.failed,
        ResultOutcome.terminal_error: A2ATaskState.failed,
        # Terminal at the protocol level, unresolved at the application level. See module docstring.
        ResultOutcome.outcome_unknown: A2ATaskState.failed,
    }
)

#: Protocol states reachable from a non-result event.
A2A_TASK_STATE_BY_EVENT: Mapping[ProtocolEvent, A2ATaskState] = MappingProxyType(
    {
        ProtocolEvent.PATIENT_CANCELLED: A2ATaskState.canceled,
        ProtocolEvent.POLICY_REFUSED: A2ATaskState.rejected,
        ProtocolEvent.AUTH_FAILED: A2ATaskState.auth_required,
    }
)

#: Protocol states from which no further state transition is expected.
TERMINAL_A2A_STATES: frozenset[A2ATaskState] = frozenset(
    {
        A2ATaskState.completed,
        A2ATaskState.canceled,
        A2ATaskState.failed,
        A2ATaskState.rejected,
    }
)


class AgentTaskResult(BaseModel):
    """An application outcome together with its protocol projection.

    Exactly one of ``outcome`` / ``event`` is set. Constructing an inconsistent pair raises, so the
    projection can never drift from the table above.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: ResultOutcome | None = None
    event: ProtocolEvent | None = None
    a2a_state: A2ATaskState

    @model_validator(mode="after")
    def _must_be_a_faithful_projection(self) -> AgentTaskResult:
        if (self.outcome is None) == (self.event is None):
            raise ValueError(
                "AgentTaskResult requires exactly one of `outcome` (an agent call result) or "
                "`event` (a cancellation, policy refusal or auth failure)."
            )
        expected = (
            A2A_TASK_STATE_BY_OUTCOME[self.outcome]
            if self.outcome is not None
            else A2A_TASK_STATE_BY_EVENT[self.event]  # type: ignore[index]
        )
        if self.a2a_state != expected:
            raise ValueError(
                f"protocol state {self.a2a_state.value!r} is not the projection of "
                f"{self.outcome or self.event}: expected {expected.value!r}"
            )
        return self

    @property
    def protocol_is_terminal(self) -> bool:
        """Whether the A2A protocol considers this task finished."""
        return self.a2a_state in TERMINAL_A2A_STATES

    @property
    def requires_reconciliation(self) -> bool:
        """Whether the workflow must still resolve an unknown external outcome (§7).

        True *only* for ``outcome_unknown``. The caller must call ``lookup_request`` with the
        **same** idempotency key — never mint a second booking key.
        """
        return self.outcome is ResultOutcome.outcome_unknown

    @property
    def may_have_committed_side_effects(self) -> bool:
        """Whether an external system may have committed a booking we cannot see.

        This is the flag that makes ``protocol_is_terminal`` unsafe to act on by itself.
        """
        return self.requires_reconciliation

    @property
    def is_success(self) -> bool:
        """A call that ran correctly — including a valid empty ``no_match`` result."""
        return self.outcome in (ResultOutcome.completed, ResultOutcome.no_match)


def project_outcome(outcome: ResultOutcome) -> AgentTaskResult:
    """Project an agent call result onto the A2A protocol."""
    return AgentTaskResult(outcome=outcome, a2a_state=A2A_TASK_STATE_BY_OUTCOME[outcome])


def project_event(event: ProtocolEvent) -> AgentTaskResult:
    """Project a cancellation / policy refusal / auth failure onto the A2A protocol."""
    return AgentTaskResult(event=event, a2a_state=A2A_TASK_STATE_BY_EVENT[event])
