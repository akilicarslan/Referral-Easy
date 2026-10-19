"""The seven specialist roles, and the tools each one may call.

This is the *in-process* half of the tool authority question ("is this tool on
this agent's allowlist?"). The other half — "may this agent touch this workflow
in its current state?" — is answered by the database, and is asked in
`referraleasy_agent_runtime.tools`. Neither half is negotiable by a prompt, a
skill or an LLM.

The table below is transcribed from §3 of the architecture and is the same data
`migrations/014_align_domain_vocabulary.sql` writes into `agent_role` and
`policy_rule`. The runtime copy exists so that a misconfigured or malicious tool
call is refused *before* it reaches a database round-trip, and so a unit test can
assert the allowlist without a PostgreSQL instance.

Booking write authority (§4)
---------------------------
`scheduling` is the sole holder of booking write authority. `care_coordinator`
can never write a booking and can never change a patient's choice: it delegates,
and the delegate holds the pen. That invariant is asserted at import time below,
so a future edit that widens the authority fails at import rather than at 03:00
in production.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from referraleasy_agent_runtime.errors import ConfigurationError, UnknownRoleError

__all__ = [
    "AGENT_ROLE_ENV",
    "BOOKING_WRITE_TOOLS",
    "DELEGATION_TOOL",
    "ROLE_LABELS",
    "ROLE_TOOLS",
    "AgentRole",
    "allowed_tools",
    "booking_write_authority",
    "owns_booking_writes",
    "role_from_code",
    "role_from_env",
    "service_name",
]


class AgentRole(StrEnum):
    """The role an agent process serves, chosen by ``AGENT_ROLE``.

    One image, seven services: the role decides the system prompt, the skill set
    and — through the allowlist below — the tools. It never decides authority at
    runtime; it only selects which pre-approved authority applies.
    """

    CARE_COORDINATOR = "care_coordinator"
    REFERRAL_INTAKE = "referral_intake"
    COVERAGE_NETWORK = "coverage_network"
    PROVIDER_MATCHING = "provider_matching"
    REFERRAL_READINESS = "referral_readiness"
    SCHEDULING = "scheduling"
    RECOVERY_HANDOFF = "recovery_handoff"


#: Environment variable that selects the role a deployed image serves.
AGENT_ROLE_ENV: Final = "AGENT_ROLE"

ROLE_LABELS: Final[Mapping[AgentRole, str]] = MappingProxyType(
    {
        AgentRole.CARE_COORDINATOR: "Care Coordinator",
        AgentRole.REFERRAL_INTAKE: "Referral Intake",
        AgentRole.COVERAGE_NETWORK: "Coverage & Network",
        AgentRole.PROVIDER_MATCHING: "Provider Matching",
        AgentRole.REFERRAL_READINESS: "Referral Readiness",
        AgentRole.SCHEDULING: "Scheduling",
        AgentRole.RECOVERY_HANDOFF: "Recovery & Handoff",
    }
)

#: The only tool the coordinator has: it does not act, it delegates (§3).
DELEGATION_TOOL: Final = "delegate_task"

ROLE_TOOLS: Final[Mapping[AgentRole, frozenset[str]]] = MappingProxyType(
    {
        AgentRole.CARE_COORDINATOR: frozenset({DELEGATION_TOOL}),
        AgentRole.REFERRAL_INTAKE: frozenset(
            {"validate_referral", "resolve_location", "patient_preferences"}
        ),
        AgentRole.COVERAGE_NETWORK: frozenset(
            {"check_plan", "check_provider_network", "get_coverage_rules"}
        ),
        AgentRole.PROVIDER_MATCHING: frozenset({"find_candidates", "search_directory"}),
        AgentRole.REFERRAL_READINESS: frozenset(
            {"get_referral_requirements", "inspect_document", "validate_referral_package"}
        ),
        AgentRole.SCHEDULING: frozenset(
            {
                "list_slots",
                "create_booking",
                "lookup_request",
                "cancel_booking",
                "reschedule_booking",
            }
        ),
        AgentRole.RECOVERY_HANDOFF: frozenset(
            {"classify_error", "create_handoff", "search_directory"}
        ),
    }
)

#: Tools that move a booking from "chosen" to "committed". These are the calls whose *failure*
#: cannot be read as "nothing happened" — see `referraleasy_agent_runtime.errors` and the
#: `outcome_unknown` projection trap.
BOOKING_WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {"create_booking", "cancel_booking", "reschedule_booking"}
)

#: The one role permitted to call them.
BOOKING_WRITE_ROLE: Final[AgentRole] = AgentRole.SCHEDULING


def allowed_tools(role: AgentRole) -> frozenset[str]:
    """Return the tools ``role`` may call, ever, in any state.

    Deliberately takes *only* a role. A skill, a manifest, a prompt or a model
    output has no way to add to this set: adding a skill grants no tool
    permission, and this signature is what makes that structural rather than a
    convention someone has to remember.
    """
    return ROLE_TOOLS[role]


def owns_booking_writes(role: AgentRole) -> bool:
    """Whether ``role`` holds booking write authority (§4)."""
    return bool(BOOKING_WRITE_TOOLS & allowed_tools(role))


def booking_write_authority() -> frozenset[AgentRole]:
    """The roles that hold booking write authority. Exactly one, by construction."""
    return frozenset(role for role in AgentRole if owns_booking_writes(role))


def role_from_code(code: str) -> AgentRole:
    """Parse a role code, refusing anything that is not one of the seven."""
    normalised = code.strip().lower()
    try:
        return AgentRole(normalised)
    except ValueError as exc:
        known = ", ".join(sorted(role.value for role in AgentRole))
        raise UnknownRoleError(f"unknown agent role {code!r}: the seven roles are {known}") from exc


def role_from_env(env: Mapping[str, str]) -> AgentRole:
    """Read ``AGENT_ROLE`` and resolve it to a role.

    A missing or empty value is a startup failure, not a default: the wrong
    default would silently serve the wrong agent's authority.
    """
    raw = env.get(AGENT_ROLE_ENV, "").strip()
    if not raw:
        known = ", ".join(sorted(role.value for role in AgentRole))
        raise ConfigurationError(
            f"{AGENT_ROLE_ENV} is not set. It selects which of the seven agents this image "
            f"serves; one of: {known}."
        )
    return role_from_code(raw)


def service_name(role: AgentRole) -> str:
    """The stable service name for ``role`` (used for logs, traces and release manifests)."""
    return f"agent-{role.value}"


def _assert_booking_write_authority_is_singular() -> None:
    """Import-time invariant: only `scheduling` may hold booking write authority (§4)."""
    holders = booking_write_authority()
    if holders != frozenset({BOOKING_WRITE_ROLE}):
        raise ConfigurationError(
            "booking write authority must rest with exactly "
            f"{BOOKING_WRITE_ROLE.value!r}; found {sorted(role.value for role in holders)}"
        )
    coordinator_tools = allowed_tools(AgentRole.CARE_COORDINATOR)
    if coordinator_tools != frozenset({DELEGATION_TOOL}):
        raise ConfigurationError(
            "the care coordinator must hold only the delegation tool and can never write a "
            f"booking or change a patient's choice; found {sorted(coordinator_tools)}"
        )


_assert_booking_write_authority_is_singular()
