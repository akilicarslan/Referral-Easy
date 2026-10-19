"""ReferralEasy agent runtime — the plumbing the seven ADK specialists share.

An agent service is supposed to be thin. Everything an agent would otherwise
re-implement lives here:

======================  ==================================================================
Budgets and deadlines   :mod:`~referraleasy_agent_runtime.budget`,
                        :mod:`~referraleasy_agent_runtime.retries` — 3 s per attempt, at most
                        3 attempts including the first, a 30 s turn, 8/12/16 caps, shared
                        downward and never multiplied by a retry hidden in a transport.
Tool authority          :mod:`~referraleasy_agent_runtime.tools` — an allowlist checked here,
                        a workflow-state check owned by the database, both mandatory.
Result shaping          :mod:`~referraleasy_agent_runtime.results` — the outcome beside the
                        protocol state, always, delegated to ``referraleasy_contracts``.
A2A transport auth      :mod:`~referraleasy_agent_runtime.auth`,
                        :mod:`~referraleasy_agent_runtime.http` — bearer token verified at
                        the transport; a payload ``user_id`` is never believed.
Skills                  :mod:`~referraleasy_agent_runtime.skills` — select what the task
                        needs, record the effective list, grant no authority.
Suspend and resume      :mod:`~referraleasy_agent_runtime.suspend` — end the turn on
                        ``needs_input`` without holding a model call open.
Serving                 :mod:`~referraleasy_agent_runtime.app` — one image, the role chosen
                        by ``AGENT_ROLE``.
======================  ==================================================================

Typical agent code is three lines: resolve the settings, build the gate and the
budget once, and let :func:`referraleasy_agent_runtime.app.main` serve it.
"""

from referraleasy_agent_runtime.app import (
    AgentTaskRequest,
    TaskHandler,
    TurnReport,
    asgi_app,
    create_app,
    main,
    preflight,
)
from referraleasy_agent_runtime.auth import (
    UNTRUSTED_IDENTITY_KEYS,
    BearerTokenVerifier,
    ServicePrincipal,
    drop_untrusted_identity,
    service_auth_dependency,
    service_auth_headers,
)
from referraleasy_agent_runtime.budget import (
    BudgetCaps,
    BudgetExhausted,
    BudgetSnapshot,
    CallLease,
    CapKind,
    Clock,
    SystemClock,
    TurnBudget,
)
from referraleasy_agent_runtime.errors import (
    AgentRuntimeError,
    ConfigurationError,
    PolicyRefused,
    PolicyUnavailable,
    ServiceAuthError,
    SkillSourceError,
    SuspensionError,
    ToolNotAllowed,
    ToolRefused,
    UnknownRoleError,
    UpstreamRejected,
    UpstreamTransient,
)
from referraleasy_agent_runtime.http import (
    HttpProfile,
    JsonServiceClient,
    ServiceResponse,
    a2a_client_config,
    build_http_client,
)
from referraleasy_agent_runtime.model import (
    SUPPORTED_PROVIDERS,
    AgentModelOverride,
    ModelRef,
    ModelSource,
    build_model,
    parse_overrides,
    resolve_model,
)
from referraleasy_agent_runtime.results import (
    AgentTaskResult,
    ProtocolEvent,
    envelope,
    needs_input,
    no_match,
    project_event,
    project_failure,
    project_outcome,
    retryable,
    succeeded,
    terminal_error,
    unknown_outcome,
)
from referraleasy_agent_runtime.retries import (
    NO_RETRIES,
    CallOutcome,
    backoff_delay_ms,
    is_transient,
    parse_retry_after,
    run_with_retries,
)
from referraleasy_agent_runtime.roles import (
    BOOKING_WRITE_TOOLS,
    ROLE_TOOLS,
    AgentRole,
    allowed_tools,
    booking_write_authority,
    owns_booking_writes,
    role_from_code,
    role_from_env,
    service_name,
)
from referraleasy_agent_runtime.settings import AgentSettings, resolve_port
from referraleasy_agent_runtime.skills import (
    EmptySkillSource,
    SkillManifest,
    SkillSelection,
    SkillSource,
    SkillsPackageSource,
    gate_manifests,
    load_instructions,
    select_skills,
)
from referraleasy_agent_runtime.suspend import (
    InMemorySuspensionStore,
    ResumePlan,
    Suspension,
    SuspensionReason,
    SuspensionStore,
    build_resume_plan,
    resume,
    suspend_turn,
)
from referraleasy_agent_runtime.tools import (
    PolicyBackend,
    SqlPolicyBackend,
    ToolGate,
    governed_tool_call,
    is_write_tool,
    translate_policy_refusal,
)

__all__ = [
    "BOOKING_WRITE_TOOLS",
    "NO_RETRIES",
    "ROLE_TOOLS",
    "SUPPORTED_PROVIDERS",
    "UNTRUSTED_IDENTITY_KEYS",
    "AgentModelOverride",
    "AgentRole",
    "AgentRuntimeError",
    "AgentSettings",
    "AgentTaskRequest",
    "AgentTaskResult",
    "BearerTokenVerifier",
    "BudgetCaps",
    "BudgetExhausted",
    "BudgetSnapshot",
    "CallLease",
    "CallOutcome",
    "CapKind",
    "Clock",
    "ConfigurationError",
    "EmptySkillSource",
    "HttpProfile",
    "InMemorySuspensionStore",
    "JsonServiceClient",
    "ModelRef",
    "ModelSource",
    "PolicyBackend",
    "PolicyRefused",
    "PolicyUnavailable",
    "ProtocolEvent",
    "ResumePlan",
    "ServiceAuthError",
    "ServicePrincipal",
    "ServiceResponse",
    "SkillManifest",
    "SkillSelection",
    "SkillSource",
    "SkillSourceError",
    "SkillsPackageSource",
    "SqlPolicyBackend",
    "Suspension",
    "SuspensionError",
    "SuspensionReason",
    "SuspensionStore",
    "SystemClock",
    "TaskHandler",
    "ToolGate",
    "ToolNotAllowed",
    "ToolRefused",
    "TurnBudget",
    "TurnReport",
    "UnknownRoleError",
    "UpstreamRejected",
    "UpstreamTransient",
    "a2a_client_config",
    "allowed_tools",
    "asgi_app",
    "backoff_delay_ms",
    "booking_write_authority",
    "build_http_client",
    "build_model",
    "build_resume_plan",
    "create_app",
    "drop_untrusted_identity",
    "envelope",
    "gate_manifests",
    "governed_tool_call",
    "is_transient",
    "is_write_tool",
    "load_instructions",
    "main",
    "needs_input",
    "no_match",
    "owns_booking_writes",
    "parse_overrides",
    "parse_retry_after",
    "preflight",
    "project_event",
    "project_failure",
    "project_outcome",
    "resolve_model",
    "resolve_port",
    "resume",
    "retryable",
    "role_from_code",
    "role_from_env",
    "run_with_retries",
    "select_skills",
    "service_auth_dependency",
    "service_auth_headers",
    "service_name",
    "succeeded",
    "suspend_turn",
    "terminal_error",
    "translate_policy_refusal",
    "unknown_outcome",
]
