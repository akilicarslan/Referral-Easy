"""§3's forbidden authority column: one refusal per row, each by a named layer.

The architecture's §3 table has a third column — *Forbidden authority* — and the requirement attached to
it is specific: the boundary must be enforced **by the runtime** (the allowlist, the policy table, the
turn budget) or by the shape of the code (a contract schema, a function signature, a check that
raises) — never by asking the model nicely. `referraleasy_agents.forbidden` is that requirement as
data, and this file is the test over it:

* every sentence of every role's brief has exactly one entry (`check_coverage_of_briefings`);
* every entry's `check` **raises** when it is called — a check that returned `False` would be a
  description, and the parametrised run below calls all of them;
* the exception class is the one its mechanism claims, so "the budget refuses it" and "the schema
  refuses it" are different assertions rather than the same one;
* the entries whose capability is also a policy row name a triple the migration really denies.

The last one is what stops a `policy_triple` from pointing at a row nobody wrote: §3's forbidden
column and `migrations/014_align_domain_vocabulary.sql` are two statements of the same boundaries, and
this file makes them agree by reading both.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from policy_table import POLICY_ROWS, HardDenial, InMemoryPolicy, hard_denials
from pydantic import ValidationError

from referraleasy_agent_runtime import (
    AgentRole,
    AgentRuntimeError,
    BudgetExhausted,
    PolicyRefused,
    ToolNotAllowed,
    allowed_tools,
)
from referraleasy_agents.forbidden import (
    FORBIDDEN,
    ForbiddenAuthority,
    Mechanism,
    check_coverage_of_briefings,
)
from referraleasy_agents.prompts import ROLE_BRIEFINGS
from referraleasy_contracts.generated.common import WorkflowState

pytestmark = pytest.mark.contract

#: Which layer refuses, expressed as the exception it must raise. This is the whole claim of the §3
#: table written once: `ALLOWLIST`, `POLICY` and `BUDGET` are the runtime refusing before any work
#: happens, `SCHEMA` is pydantic refusing a shape, `SIGNATURE` is Python refusing a call, and `CODE`
#: is one of this package's own guards. A mechanism whose check raised the wrong class would be
#: claiming a layer it does not have.
MECHANISM_REFUSAL: Mapping[Mechanism, tuple[type[BaseException], ...]] = {
    Mechanism.ALLOWLIST: (ToolNotAllowed,),
    Mechanism.POLICY: (PolicyRefused,),
    Mechanism.BUDGET: (BudgetExhausted,),
    Mechanism.SCHEMA: (ValidationError,),
    Mechanism.SIGNATURE: (TypeError,),
    # `AgentRuntimeError` is the package's base class: every code guard here is one of its
    # subclasses (`UnknownDataError`, `NoApprovalRefused`, `OneWayDoorRefused`, ...), and the
    # `ToolRefused` family is included by inheritance.
    Mechanism.CODE: (AgentRuntimeError,),
}

#: Every entry with its role, so one parametrised run calls all of them.
ENTRIES: Sequence[tuple[str, int, ForbiddenAuthority]] = tuple(
    (role.value, entry.capability_index, entry)
    for role in sorted(AgentRole, key=lambda item: item.value)
    for entry in FORBIDDEN[role]
)

#: The seven agents, as the migration's role strings.
AGENT_ROLES: frozenset[str] = frozenset(role.value for role in AgentRole)

#: The platform's own two actors. A human operator's ticket queue and the platform's consent recorder
#: are not agents and hold no agent tool (§11).
PLATFORM_ACTORS: frozenset[str] = frozenset({"operator", "system"})


def test_every_forbidden_sentence_has_exactly_one_refusal() -> None:
    """The coverage assertion itself, called first so a briefing edit fails loudly."""
    check_coverage_of_briefings()


def test_the_table_has_no_prompt_layer() -> None:
    """A seventh mechanism called `prompt` is the failure this whole module exists to prevent."""
    assert {member.value for member in Mechanism} == {
        "allowlist",
        "policy",
        "budget",
        "schema",
        "signature",
        "code",
    }


def test_every_role_has_one_entry_per_forbidden_sentence() -> None:
    assert set(FORBIDDEN) == set(AgentRole)
    for role, briefing in ROLE_BRIEFINGS.items():
        entries = FORBIDDEN[role]
        assert len(entries) == len(briefing.forbidden) >= 1
        assert [entry.capability_index for entry in entries] == list(range(len(briefing.forbidden)))
        for entry in entries:
            assert entry.capability == briefing.forbidden[entry.capability_index]
            assert entry.detail.strip()
            assert entry.mechanism in MECHANISM_REFUSAL


def test_the_section_three_forbidden_column_is_present_in_every_brief() -> None:
    """The transcriptions, checked as keywords per role.

    `check_coverage_of_briefings` proves the briefs and the refusal table agree; it cannot notice a
    sentence that was dropped from *both*. These are §3's own phrases, so a row that quietly left the
    table fails here.
    """
    markers: Mapping[AgentRole, tuple[str, ...]] = {
        AgentRole.CARE_COORDINATOR: (
            "writing a booking directly",
            "patient's choice",
            "policy table",
        ),
        AgentRole.REFERRAL_INTAKE: (
            "diagnosing",
            "specialty that the referral does not state",
            "inventing",
        ),
        AgentRole.COVERAGE_NETWORK: (
            "guaranteeing insurance",
            "inventing a coverage rule",
            "unknown",
        ),
        AgentRole.PROVIDER_MATCHING: (
            "choosing a candidate",
            "relaxing a hard constraint",
            "ranking",
        ),
        AgentRole.REFERRAL_READINESS: ("assuming a document", "approval", "hospital is chosen"),
        AgentRole.SCHEDULING: ("without a live approval", "duplicate", "auto-selecting"),
        AgentRole.RECOVERY_HANDOFF: (
            "overriding a policy block",
            "third transient failure",
            "reword",
        ),
    }
    assert set(markers) == set(AgentRole)
    for role, phrases in markers.items():
        forbidden = " ".join(ROLE_BRIEFINGS[role].forbidden).lower()
        for phrase in phrases:
            assert phrase.lower() in forbidden, (
                f"{role.value}: §3's forbidden column lost {phrase!r}"
            )


@pytest.mark.parametrize(
    ("role", "index", "entry"),
    ENTRIES,
    ids=[f"{role}-{index}" for role, index, _ in ENTRIES],
)
def test_every_forbidden_capability_is_refused_by_its_mechanism(
    role: str, index: int, entry: ForbiddenAuthority
) -> None:
    """Call the forbidden act. Raising is the contract; the class is the layer."""
    expected = MECHANISM_REFUSAL[entry.mechanism]
    with pytest.raises(expected) as caught:
        entry.check()
    assert str(caught.value).strip(), (
        f"{role}[{index}] was refused with an empty message; a refusal a caller cannot read is a "
        "refusal a caller cannot act on"
    )


@pytest.mark.parametrize(
    "denial",
    hard_denials(),
    ids=[f"{denial.role}-{denial.tool}-{denial.state}" for denial in hard_denials()],
)
def test_every_hard_denial_names_a_real_actor_a_real_tool_and_a_real_state(
    denial: HardDenial,
) -> None:
    """The migration's `allowed = false` rows, checked against the vocabulary §3 fixes.

    Two kinds of actor appear in the table, and both are legitimate: the seven agents, and the
    platform's own two (`system`, `operator`) — §11 gives a human operator a ticket queue and a
    consent recorder is the platform's, not an agent's. Neither is a ninth agent, which is what this
    asserts by name.
    """
    assert denial.role in AGENT_ROLES | PLATFORM_ACTORS
    assert denial.state in {state.value for state in WorkflowState}
    assert denial.tool.strip()
    assert denial.note, "a denial without a note is a row nobody can explain"


@pytest.mark.parametrize(
    "denial",
    [denial for denial in hard_denials() if denial.role in AGENT_ROLES],
    ids=[
        f"{denial.role}-{denial.tool}-{denial.state}"
        for denial in hard_denials()
        if denial.role in AGENT_ROLES
    ],
)
def test_an_agent_denial_is_either_a_tool_it_never_holds_or_a_state_specific_rule(
    denial: HardDenial,
) -> None:
    """A denial means one of two things, and both are checkable against the runtime's own data.

    * The role does not hold the tool at all — §3's boundary stated by name
      (`care_coordinator`/`create_booking`), where the allowlist is the enforcement and the row is the
      policy table saying the same thing;
    * the role holds the tool but not in this state — §4's ordering rules
      (`check_provider_network` in `COVERAGE_CHECK`, `list_slots` in `BOOKING_IN_FLIGHT`), where the
      tool is permitted somewhere else and the row is genuinely about the state.
    """
    role = AgentRole(denial.role)
    holds = denial.tool in allowed_tools(role)
    if holds:
        assert InMemoryPolicy.states_for(denial.role, denial.tool), (
            f"{denial.role} may not call {denial.tool} in {denial.state}, and is not permitted to "
            "call it in any state either — a tool no state allows belongs in the allowlist, not in "
            "a denial row"
        )
    else:
        assert not InMemoryPolicy.states_for(denial.role, denial.tool)


def test_the_platform_actors_rows_are_the_platforms_own_steps() -> None:
    """`system` and `operator` are not agents, and what they may do is not a tool any agent holds."""
    permitted = {row.tool for row in POLICY_ROWS if row.role in PLATFORM_ACTORS and row.allowed}
    assert permitted == {"await_patient_choice", "capture_consent", "resolve_ticket"}
    for tool in permitted:
        assert all(tool not in allowed_tools(role) for role in AgentRole), (
            f"{tool} is a platform step and is in {[role.value for role in AgentRole if tool in allowed_tools(role)]}"
        )


def test_every_policy_triple_in_the_table_is_a_named_denial() -> None:
    """§3's entries and the migration agree, by reading the migration rather than restating it."""
    denied = {denial.key for denial in hard_denials()}
    triples = [
        (role, index, entry.policy_triple)
        for role, index, entry in ENTRIES
        if entry.policy_triple is not None
    ]
    assert triples, "no entry names a policy triple; the cross-check would be vacuous"
    for role, index, triple in triples:
        assert triple in denied, (
            f"{role}[{index}] says the policy table denies {triple}, and the migration has no "
            "`allowed = false` row for it"
        )


def test_every_layer_the_table_claims_is_a_layer_that_refuses_something() -> None:
    """No mechanism is decorative: each of the six refuses at least one real row."""
    used = {entry.mechanism for _, _, entry in ENTRIES}
    assert used == set(Mechanism), (
        f"unused layers: {sorted(member.value for member in set(Mechanism) - used)}"
    )


def test_no_role_leans_on_a_single_layer() -> None:
    """Each role's forbidden column is held up by more than one kind of refusal.

    Not a count: a property. A role whose whole boundary rested on the prompt would have no entries
    at all, and one whose boundary rested on a single guard would be one refactor away from none — so
    every role has at least two distinct mechanisms behind it.
    """
    for role, entries in FORBIDDEN.items():
        mechanisms = {entry.mechanism for entry in entries}
        assert len(mechanisms) >= 2, f"{role.value} is held up by {mechanisms} alone"
