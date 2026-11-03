"""`TurnScope`: everything a plugin may consult, handed to it at construction (WS3 S1).

Frozen, and built once per turn by the handler. Fields that belong to later workstreams are typed
`object | None` and stay `None` until the workstream that owns them lands; a plugin that needs one
and finds `None` must fail closed, not skip its check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from referraleasy_agent_runtime.budget import TurnBudget
    from referraleasy_agent_runtime.roles import AgentRole

__all__ = ["TurnScope"]


@dataclass(frozen=True, slots=True)
class TurnScope:
    """One turn's authority and limits, as the plugin stack sees them."""

    role: AgentRole
    budget: TurnBudget
    #: The deployment profile the turn runs under (`production`, `demo`, `test`).
    profile: str = "production"
    #: WS1: the verified `ActorContext`. `None` until WS1 S4.
    actor: object | None = None
    #: WS3 S4: the spend ledger port.
    spend_ledger: object | None = None
    #: WS3 S6: breaker and failure ledger stores.
    breakers: object | None = None
    failure_ledger: object | None = None
    #: WS9 S3: the control-switch snapshot taken at turn start.
    controls: object | None = None
    #: Telemetry emitter, when the service has one.
    emitter: object | None = None
