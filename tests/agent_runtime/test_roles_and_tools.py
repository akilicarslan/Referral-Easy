"""Tool authority: the allowlist, the database's policy table, and the gap between them.

The interesting cases are the refusals: a tool that belongs to another role, a
state in which this role may not act, and — most importantly — a skill that claims
a tool it must not have. None of these need a database: the policy table is a
double, and the point of the seam is that the *runtime* is being tested, not the
SQL.
"""

from __future__ import annotations

import pytest
from agent_runtime_doubles import FakeClock, RecordingPolicy, RecordingSkillSource

from referraleasy_agent_runtime.budget import BudgetCaps, BudgetExhausted, TurnBudget
from referraleasy_agent_runtime.errors import (
    PolicyRefused,
    PolicyUnavailable,
    ToolNotAllowed,
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
from referraleasy_agent_runtime.skills import select_skills
from referraleasy_agent_runtime.tools import (
    POLICY_ASSERT_SQL,
    SqlPolicyBackend,
    ToolGate,
    governed_tool_call,
    is_write_tool,
    translate_policy_refusal,
)
from referraleasy_domain import PolicyDeniedError

SPEC_ROLE_TOOLS: dict[str, set[str]] = {
    "care_coordinator": {"delegate_task"},
    "referral_intake": {"validate_referral", "resolve_location", "patient_preferences"},
    "coverage_network": {"check_plan", "check_provider_network", "get_coverage_rules"},
    "provider_matching": {"find_candidates", "search_directory"},
    "referral_readiness": {
        "get_referral_requirements",
        "inspect_document",
        "validate_referral_package",
    },
    "scheduling": {
        "list_slots",
        "create_booking",
        "lookup_request",
        "cancel_booking",
        "reschedule_booking",
    },
    "recovery_handoff": {"classify_error", "create_handoff", "search_directory"},
}


def test_the_seven_roles_match_the_spec() -> None:
    assert {role.value for role in AgentRole} == set(SPEC_ROLE_TOOLS)


def test_every_role_carries_exactly_the_tools_the_spec_gives_it() -> None:
    assert {role.value: set(allowed_tools(role)) for role in AgentRole} == SPEC_ROLE_TOOLS
    assert {role.value: set(ROLE_TOOLS[role]) for role in AgentRole} == SPEC_ROLE_TOOLS


def test_scheduling_is_the_only_holder_of_booking_write_authority() -> None:
    assert booking_write_authority() == frozenset({AgentRole.SCHEDULING})
    assert {"create_booking", "cancel_booking", "reschedule_booking"} == BOOKING_WRITE_TOOLS
    for role in AgentRole:
        assert owns_booking_writes(role) is (role is AgentRole.SCHEDULING)


def test_the_coordinator_can_never_write_a_booking_or_change_a_patients_choice() -> None:
    gate = ToolGate(AgentRole.CARE_COORDINATOR)

    for tool in BOOKING_WRITE_TOOLS:
        with pytest.raises(ToolNotAllowed) as raised:
            gate.check(tool, workflow_state="AWAITING_FINAL_APPROVAL")
        assert raised.value.role == "care_coordinator"
        assert raised.value.tool == tool

    with pytest.raises(ToolNotAllowed):
        gate.check("list_slots", workflow_state="AWAITING_SLOT_CHOICE")
    assert gate.allows == {"delegate_task"}


def test_the_allowlist_refuses_a_tool_that_belongs_to_another_role() -> None:
    gate = ToolGate(AgentRole.PROVIDER_MATCHING, policy=RecordingPolicy())

    with pytest.raises(ToolNotAllowed) as raised:
        gate.check("list_slots", workflow_state="AWAITING_SLOT_CHOICE")

    refusal = raised.value
    assert refusal.role == "provider_matching"
    assert refusal.workflow_state == "AWAITING_SLOT_CHOICE"
    # A refusal is a policy decision, not an agent call result: it projects to the
    # protocol event `rejected`, never to a ResultOutcome.
    result = refusal.to_result()
    assert result.outcome is None
    assert result.event is not None and result.event.value == "policy_refused"
    assert result.a2a_state.value == "rejected"


def test_a_local_refusal_never_reaches_the_policy_backend() -> None:
    policy = RecordingPolicy()
    gate = ToolGate(AgentRole.PROVIDER_MATCHING, policy=policy)

    with pytest.raises(ToolNotAllowed):
        gate.check("create_booking", workflow_state="BOOKING_IN_FLIGHT")

    assert policy.calls == [], "a tool outside the allowlist must not cost a database round-trip"


def test_a_permitted_tool_is_checked_against_the_database_by_state() -> None:
    policy = RecordingPolicy()
    gate = ToolGate(AgentRole.SCHEDULING, policy=policy)

    gate.check("list_slots", workflow_state="AWAITING_SLOT_CHOICE")
    assert policy.calls == [("scheduling", "list_slots", "AWAITING_SLOT_CHOICE")]


def test_a_policy_denial_from_the_database_is_translated() -> None:
    policy = RecordingPolicy(allowed=False)
    gate = ToolGate(AgentRole.SCHEDULING, policy=policy)

    with pytest.raises(PolicyRefused) as raised:
        gate.check("create_booking", workflow_state="INTAKE")

    assert "create_booking" in str(raised.value)
    assert raised.value.to_result().a2a_state.value == "rejected"


def test_a_database_error_that_is_not_a_denial_is_not_flattened_into_one() -> None:
    """A dropped connection must stay transient, not retire a permitted tool."""
    policy = RecordingPolicy(error=ConnectionError("the database went away"))
    gate = ToolGate(AgentRole.SCHEDULING, policy=policy)

    with pytest.raises(ConnectionError):
        gate.check("list_slots", workflow_state="AWAITING_SLOT_CHOICE")


def test_the_gate_is_default_deny() -> None:
    """Absence is a refusal: no authority, no state, no backend — no permission."""
    with pytest.raises(PolicyUnavailable, match="no policy backend"):
        ToolGate(AgentRole.SCHEDULING).check("list_slots", workflow_state="AWAITING_SLOT_CHOICE")

    with pytest.raises(PolicyUnavailable, match="workflow state is required"):
        ToolGate(AgentRole.SCHEDULING, policy=RecordingPolicy()).check(
            "list_slots", workflow_state=None
        )

    with pytest.raises(PolicyUnavailable):
        ToolGate(AgentRole.REFERRAL_INTAKE).check("validate_referral", workflow_state=None)


def test_the_sql_backend_calls_the_databases_own_function() -> None:
    executed: list[tuple[str, tuple[str, str, str]]] = []

    def execute(statement: str, parameters: tuple[str, str, str]) -> None:
        executed.append((statement, parameters))

    backend = SqlPolicyBackend(execute)
    backend.policy_assert("scheduling", "list_slots", "AWAITING_SLOT_CHOICE")

    assert executed == [(POLICY_ASSERT_SQL, ("scheduling", "list_slots", "AWAITING_SLOT_CHOICE"))]
    assert POLICY_ASSERT_SQL.startswith("SELECT policy_assert(")


def test_the_sql_backend_translates_the_databases_rev006() -> None:
    class FakeDatabaseError(Exception):
        sqlstate = "RE006"

    def execute(statement: str, parameters: tuple[str, str, str]) -> None:
        raise FakeDatabaseError(
            "policy denies role scheduling calling tool create_booking in workflow state INTAKE"
        )

    with pytest.raises(PolicyRefused) as raised:
        SqlPolicyBackend(execute).policy_assert("scheduling", "create_booking", "INTAKE")

    assert isinstance(raised.value.__cause__, FakeDatabaseError)
    assert raised.value.to_result().event is not None


def test_the_sql_backend_leaves_a_non_denial_error_alone() -> None:
    class FakeOperationalError(Exception):
        sqlstate = "08006"

    def execute(statement: str, parameters: tuple[str, str, str]) -> None:
        raise FakeOperationalError("connection failure")

    with pytest.raises(FakeOperationalError):
        SqlPolicyBackend(execute).policy_assert("scheduling", "list_slots", "AWAITING_SLOT_CHOICE")


def test_a_refusal_passing_through_both_layers_is_translated_once() -> None:
    """The SQL adapter translates, and the gate translates — but only one refusal comes out."""

    class FakeDatabaseError(Exception):
        sqlstate = "RE006"

    def execute(statement: str, parameters: tuple[str, str, str]) -> None:
        raise FakeDatabaseError("denied")

    gate = ToolGate(AgentRole.SCHEDULING, policy=SqlPolicyBackend(execute))

    with pytest.raises(PolicyRefused) as raised:
        gate.check("create_booking", workflow_state="INTAKE")

    assert isinstance(raised.value.__cause__, FakeDatabaseError)
    assert raised.value.to_result().a2a_state.value == "rejected"


def test_translate_policy_refusal_only_claims_denials() -> None:
    assert (
        translate_policy_refusal(
            PolicyDeniedError("denied"), role="r", tool="t", workflow_state="INTAKE"
        )
        is not None
    )
    assert (
        translate_policy_refusal(ValueError("nope"), role="r", tool="t", workflow_state="INTAKE")
        is None
    )


def test_a_governed_tool_call_costs_authority_then_quota(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    budget = TurnBudget(caps, clock=clock, role=AgentRole.SCHEDULING)
    gate = ToolGate(AgentRole.SCHEDULING, policy=RecordingPolicy())

    with governed_tool_call(
        gate, budget, "list_slots", workflow_state="AWAITING_SLOT_CHOICE"
    ) as lease:
        assert lease.timeout_ms == 1500
    assert budget.snapshot().tool_calls == 1


def test_a_refused_tool_call_costs_no_budget(clock: FakeClock, caps: BudgetCaps) -> None:
    budget = TurnBudget(caps, clock=clock, role=AgentRole.PROVIDER_MATCHING)
    gate = ToolGate(AgentRole.PROVIDER_MATCHING, policy=RecordingPolicy())

    with (
        pytest.raises(ToolNotAllowed),
        governed_tool_call(gate, budget, "create_booking", workflow_state="BOOKING_IN_FLIGHT"),
    ):
        pass

    assert budget.snapshot().tool_calls == 0


def test_a_booking_write_is_budgeted_as_a_write_attempt(clock: FakeClock, caps: BudgetCaps) -> None:
    budget = TurnBudget(caps, clock=clock, role=AgentRole.SCHEDULING)
    gate = ToolGate(AgentRole.SCHEDULING, policy=RecordingPolicy())
    clock.advance(30_000)

    with (
        pytest.raises(BudgetExhausted) as raised,
        governed_tool_call(gate, budget, "create_booking", workflow_state="BOOKING_IN_FLIGHT"),
    ):
        pass

    assert raised.value.after_write_attempt is True
    assert raised.value.to_result().requires_reconciliation is True


def test_only_booking_writes_are_read_as_possibly_having_committed() -> None:
    assert is_write_tool("create_booking") is True
    assert is_write_tool("cancel_booking") is True
    assert is_write_tool("reschedule_booking") is True
    assert is_write_tool("list_slots") is False
    assert is_write_tool("find_candidates") is False


def test_a_skill_grants_no_tool_permission() -> None:
    """The claim in a manifest is documentation; authority is the allowlist.

    ``RecordingSkillSource`` is imported at module level, not here, and that is
    load-bearing: an import inside a function body is resolved at *call* time
    against ``sys.modules``, so a bare ``from conftest import ...`` here picked up
    whichever ``conftest.py`` pytest happened to import first — this test passed
    when the directory ran alone and failed in the full-suite run. A green run has
    to mean the test ran, so the double comes from ``agent_runtime_doubles``.
    """
    source = RecordingSkillSource(
        {
            "booking-wizard": {
                "name": "booking-wizard",
                "roles": ["provider_matching"],
                "tags": ["booking"],
                "tools": ["create_booking", "list_slots"],
                "grants_tool_permissions": True,
            }
        }
    )

    selection = select_skills(source, role=AgentRole.PROVIDER_MATCHING, tags=["booking"])
    assert selection.names == ("booking-wizard",)
    assert selection.declared_tools == {"create_booking", "list_slots"}
    assert selection.trace_entry()["declared_tools_not_granted"] == [
        "create_booking",
        "list_slots",
    ]

    gate = ToolGate(AgentRole.PROVIDER_MATCHING, policy=RecordingPolicy())
    assert gate.allows == {"find_candidates", "search_directory"}
    with pytest.raises(ToolNotAllowed):
        gate.check("create_booking", workflow_state="BOOKING_IN_FLIGHT")
    with pytest.raises(ToolNotAllowed):
        gate.check("list_slots", workflow_state="AWAITING_SLOT_CHOICE")


def test_role_codes_are_validated_at_the_edge() -> None:
    assert role_from_code("  Scheduling ") is AgentRole.SCHEDULING
    assert role_from_env({"AGENT_ROLE": "scheduling"}) is AgentRole.SCHEDULING
    assert service_name(AgentRole.RECOVERY_HANDOFF) == "agent-recovery_handoff"

    from referraleasy_agent_runtime.errors import UnknownRoleError

    # Surrounding whitespace is a copy-paste artefact, not a different role.
    assert role_from_env({"AGENT_ROLE": " care_coordinator "}) is AgentRole.CARE_COORDINATOR

    with pytest.raises(UnknownRoleError, match="booking_agent"):
        role_from_code("booking_agent")

    from referraleasy_agent_runtime.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="is not set"):
        role_from_env({})
