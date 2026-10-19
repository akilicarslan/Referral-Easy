"""ReferralEasy gateway (§9, §12).

The patient-facing HTTP transport: two session kinds verified for real, REST commands that refuse a
stale `workflow_state_version`, a replayable SSE stream, a policy check that consults the policy
*table*, and the guardrail seam that keeps the booking write closed when the model layers are not
configured.

Modules
-------
``config``       The environment contract, and the startup refusals that go with it.
``auth``         Patient/admin JWT sessions, two secrets, two verifiers.
``errors``       The one failure envelope, and SQLSTATE -> status mapping.
``timing``       The clock seam, and progress latency kept apart from final latency.
``events``       The event log: content-addressed ids, monotonic `seq`, replay.
``surfaces``     The card catalog, severability ordering, and the partial-output buffer.
``policy``       The action -> state table (§4) as data, and `is_allowed` over it.
``policy_seed``  The shipped rows, transcribed from migration 014.
``control``      The control-state port, and both implementations of it: in-memory and PostgreSQL.
``psql``         The one place a connection is opened, and the one place its refusals are translated.
``approval``     Approval binding: patient, workflow, state version, payload hash, expiry.
``guardrails``   The three-layer input screen (§9), with the two model layers behind a protocol.
``commands``     The four operations, assembled from the ports above.
``api``          Routes. Transport only — no rule lives here.
``app``          Wiring: settings in, an ASGI app out.

Read this package top-down: ``config`` -> ``app`` is the startup path, ``api`` -> ``commands`` ->
the ports is the request path. The SQL that actually enforces the rules is in ``migrations/`` and is
reached through ``referraleasy_domain``; nothing here re-implements it.
"""

from referraleasy_gateway.app import APP_DESCRIPTION, create_app
from referraleasy_gateway.approval import ApprovalService, InMemoryApprovalLedger
from referraleasy_gateway.auth import Session, SessionKind, build_verifiers
from referraleasy_gateway.commands import CommandReceipt, CommandService
from referraleasy_gateway.config import ConfigurationError, GatewaySettings
from referraleasy_gateway.control import (
    ControlState,
    InMemoryControlState,
    PostgresControlState,
    SeedableControlState,
)
from referraleasy_gateway.errors import GatewayError, GuardrailUnavailableError, to_envelope
from referraleasy_gateway.events import EventLog
from referraleasy_gateway.guardrails import GuardrailPipeline
from referraleasy_gateway.policy import TablePolicy
from referraleasy_gateway.timing import LatencyLedger, ManualClock, SystemClock

__all__ = [
    "APP_DESCRIPTION",
    "ApprovalService",
    "CommandReceipt",
    "CommandService",
    "ConfigurationError",
    "ControlState",
    "EventLog",
    "GatewayError",
    "GatewaySettings",
    "GuardrailPipeline",
    "GuardrailUnavailableError",
    "InMemoryApprovalLedger",
    "InMemoryControlState",
    "LatencyLedger",
    "ManualClock",
    "PostgresControlState",
    "SeedableControlState",
    "Session",
    "SessionKind",
    "SystemClock",
    "TablePolicy",
    "build_verifiers",
    "create_app",
    "to_envelope",
]
