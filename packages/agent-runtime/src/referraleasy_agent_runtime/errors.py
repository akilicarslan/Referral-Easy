"""Typed failures of the agent runtime.

Two rules shape this module.

**Exhausting a budget is a value, not a crash.** A turn that runs out of time or
quota has a defined meaning ("we did not get an answer, and a fresh turn may"),
so the failure carries a projection instead of being a bare exception that a
`except Exception` upstream would flatten into a 500.

**Nothing here re-states the outcome table.** Each error exposes the application
outcome or the protocol event it represents; the projection itself is performed
by `referraleasy_contracts`, the single owner of that table. A refusal by the
allowlist is a *policy* refusal, and a bad token is an *auth* failure: both are
protocol events, not agent call results, and both are projected as such.
"""

from __future__ import annotations

from referraleasy_contracts import (
    AgentTaskResult,
    ProtocolEvent,
    project_event,
    project_outcome,
)
from referraleasy_contracts.generated.common import ResultOutcome

__all__ = [
    "AgentRuntimeError",
    "ConfigurationError",
    "PolicyRefused",
    "PolicyUnavailable",
    "ServiceAuthError",
    "SkillSourceError",
    "SuspensionError",
    "ToolNotAllowed",
    "ToolRefused",
    "UnknownRoleError",
    "UpstreamRejected",
    "UpstreamTransient",
]


class AgentRuntimeError(Exception):
    """Base class for every failure raised by this package.

    Subclasses narrow `outcome` / `event`; the base projects to
    ``terminal_error`` so an unclassified runtime failure never masquerades as a
    retryable one.
    """

    @property
    def outcome(self) -> ResultOutcome | None:
        """The application outcome this failure represents, when it is a call result."""
        return None

    @property
    def event(self) -> ProtocolEvent | None:
        """The protocol event this failure represents, when no call result exists."""
        return None

    def to_result(self) -> AgentTaskResult:
        """Project this failure onto the A2A protocol through the contracts adapter.

        A runtime error is never an answer, so callers that need something to send
        get a shaped result: an outcome-bearing error projects its outcome, an
        event-bearing error projects its event, and anything else is
        ``terminal_error``.
        """
        outcome, event = self.outcome, self.event
        if outcome is not None:
            return project_outcome(outcome)
        if event is not None:
            return project_event(event)
        return project_outcome(ResultOutcome.terminal_error)


class ConfigurationError(AgentRuntimeError):
    """The environment does not describe a runnable agent.

    Raised at startup, never mid-turn: a role, a model or a token that cannot be
    resolved is a deployment mistake that must fail loudly rather than default to
    something plausible.
    """


class UnknownRoleError(ConfigurationError):
    """`AGENT_ROLE` named a role that is not one of the seven."""


class ToolRefused(AgentRuntimeError):
    """A tool call was refused before it ran. Refusal is not a call result."""

    def __init__(self, message: str, *, role: str, tool: str, workflow_state: str | None) -> None:
        super().__init__(message)
        self.role = role
        self.tool = tool
        self.workflow_state = workflow_state

    @property
    def event(self) -> ProtocolEvent | None:
        """A refusal is a server-side policy decision, projected as ``rejected``.

        It is deliberately *not* an outcome: no agent call happened, so there is
        no `ResultOutcome` to report.
        """
        return ProtocolEvent.POLICY_REFUSED


class ToolNotAllowed(ToolRefused):
    """The tool is not on this role's allowlist, whatever the workflow state is.

    This is the check that makes a skill powerless: a skill cannot widen the
    allowlist, only the seven-role table can, and only by editing `roles.py`.
    """


class PolicyRefused(ToolRefused):
    """The database's ``policy_assert`` refused role x tool x state (§4).

    The policy table lives in PostgreSQL and is default-deny; this error is the
    translation of that refusal, not a second copy of the decision.
    """


class PolicyUnavailable(ToolRefused):
    """A state-dependent check was asked for and no policy backend was supplied.

    Default-deny applied locally: without the authority that owns the table we
    cannot claim the call is permitted, so we refuse instead of guessing.
    """


class ServiceAuthError(AgentRuntimeError):
    """Service-to-service authentication failed at the transport (§5).

    The caller is a service, identified by a bearer token. Payload contents are
    never consulted; see `referraleasy_agent_runtime.auth`.
    """

    def __init__(self, message: str, *, www_authenticate: str) -> None:
        super().__init__(message)
        self.www_authenticate = www_authenticate

    @property
    def event(self) -> ProtocolEvent | None:
        """Projected as ``auth_required``: the A2A caller must present credentials."""
        return ProtocolEvent.AUTH_FAILED


class UpstreamTransient(AgentRuntimeError):
    """An upstream call failed in a way that may succeed on a later attempt.

    Carries ``retry_after_s`` when the upstream sent a ``Retry-After``, so the
    single retry owner can honour the upstream's pacing instead of inventing one.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_s = retry_after_s

    @property
    def outcome(self) -> ResultOutcome | None:
        """A transport fault produced no answer; the call may be retried."""
        return ResultOutcome.retryable_error


class UpstreamRejected(AgentRuntimeError):
    """A peer service well-formed-ly refused the call (a 4xx that is not 429).

    Terminal on purpose: repeating a request that was understood and refused only
    spends the turn's budget to receive the same answer.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def outcome(self) -> ResultOutcome | None:
        return ResultOutcome.terminal_error


class SuspensionError(AgentRuntimeError):
    """A suspend/resume flow was driven incorrectly (unknown answer, wrong shape)."""


class SkillSourceError(AgentRuntimeError):
    """The skill source is missing a skill, or a manifest cannot be read.

    A skill that cannot be *selected* never reaches the prompt: an unreadable
    manifest is a loud failure, not an empty skill list.
    """
