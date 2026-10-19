"""The turn's own contract: the payload split, the decision's two refusals, and the recomputation.

§5 gives every specialist a versioned input and output schema, and §4 requires every
decision to carry `action`, `evidence_ids`, `missing_fields`, `reason_code` and
`next_allowed_actions`. This file is about the code between those two statements —
the three places where a turn's *shape* is decided before any model sees it:

* **`_split_payload`** — which keys are the turn's orchestration metadata and which
  are the role's input. Two keys are named (`META_KEYS`) and everything else is the
  schema's, because "whatever the schema does not know" would be a hole in
  `extra="forbid"` and with it in §6.
* **`build_decision`** — the two decision shapes that must not exist: an action the
  envelope did not allow, and a decision with no evidence that is not a pure
  clarification request.
* **`next_allowed_actions`** — §5's "computed, not proposed". It answers by asking
  the same authority the tool gate asks, so the list cannot be more permissive than
  the gate, and this file asserts that by probing the gate's own backend.

The envelope is validated here rather than trusted: it is what the tool gate reads its
workflow state from, and a malformed one must be refused where the field names are
visible instead of surfacing as an opaque tool refusal later.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from agents_fixtures import COMMAND_ID, body_for, envelope, payload_for
from policy_table import InMemoryPolicy
from pydantic import ValidationError

from referraleasy_agent_runtime import AgentRole, PolicyRefused, allowed_tools
from referraleasy_agents.canonical import id_text
from referraleasy_agents.decisions import (
    assert_action_allowed,
    build_decision,
    mandatory_conditions,
    next_allowed_actions,
)
from referraleasy_agents.handler import META_KEYS, _split_payload
from referraleasy_contracts.generated.common import ActionRef, ReasonCode

pytestmark = pytest.mark.contract

#: The state most of this file's probes use. Booking is where the interesting rules
#: are: an approval is required, a slot is in play, and the coordinator may not write.
STATE = "BOOKING_IN_FLIGHT"


# ── the payload split ─────────────────────────────────────────────────────────────────────────


def test_the_meta_keys_are_named_one_by_one() -> None:
    """Two keys, both orchestration, and the list is short enough to read."""
    assert META_KEYS == ("task_envelope", "command_id", "execution_mode")


def test_the_split_removes_exactly_the_meta_keys() -> None:
    payload = {
        "command_id": COMMAND_ID,
        "task_envelope": envelope().model_dump(mode="json"),
        "specialty": "cardiology",
    }
    body, parsed, command_id = _split_payload(payload)
    assert body == {"specialty": "cardiology"}
    assert command_id == COMMAND_ID
    assert parsed is not None and parsed.workflow_state == STATE


def test_the_split_reads_the_command_id_off_the_envelope_when_the_payload_omits_it() -> None:
    """One of the two is required, and the envelope is the other half's source."""
    body, parsed, command_id = _split_payload(
        {"task_envelope": envelope().model_dump(mode="json"), "specialty": "cardiology"}
    )
    assert body == {"specialty": "cardiology"}
    assert command_id == COMMAND_ID
    assert parsed is not None


def test_a_payload_with_neither_a_command_id_nor_an_envelope_is_refused() -> None:
    with pytest.raises(ValueError) as caught:
        _split_payload({"specialty": "cardiology"})
    assert "command_id" in str(caught.value)


def test_a_payload_whose_command_id_disagrees_with_its_envelope_is_refused() -> None:
    """One request, one command: the session key and the tool calls must agree.

    The turn's session is keyed by the payload's `command_id` while its tool calls
    carry the envelope's. Two different values would file half the turn under a
    command the other half is not the answer to, and a redelivery would then match
    only one of them.
    """
    other = "0f0f0f0f-0000-4000-8000-00000000000e"
    with pytest.raises(ValueError) as caught:
        _split_payload(
            {
                "command_id": other,
                "task_envelope": envelope().model_dump(mode="json"),
                "specialty": "cardiology",
            }
        )
    assert other in str(caught.value)


def test_a_malformed_envelope_is_refused_at_the_split() -> None:
    """Refused here, with the field name, rather than as a tool refusal later."""
    bad = envelope().model_dump(mode="json")
    del bad["current_state_version"]
    with pytest.raises(ValidationError) as caught:
        _split_payload({"command_id": COMMAND_ID, "task_envelope": bad})
    assert "current_state_version" in str(caught.value)


def test_an_envelope_with_an_unknown_field_is_refused() -> None:
    """`extra="forbid"` on the envelope is what keeps a sixth fact from being added."""
    bad = envelope().model_dump(mode="json")
    bad["authority_override"] = True
    with pytest.raises(ValidationError):
        _split_payload({"command_id": COMMAND_ID, "task_envelope": bad})


def test_the_split_does_not_filter_the_role_s_input() -> None:
    """Everything that is not a meta key goes to the schema — including an unknown key.

    If the split dropped what the schema did not know, `extra="forbid"` would never
    fire and §6's minimisation would be advisory. The refusal is `_validated_input`'s,
    and this test shows the key reaches it.
    """
    body, _, _ = _split_payload(
        payload_for(AgentRole.PROVIDER_MATCHING, date_of_birth="1980-01-01")
    )
    assert "date_of_birth" in body
    from referraleasy_agents.handler import _validated_input

    refused = _validated_input(AgentRole.PROVIDER_MATCHING, body)
    assert not hasattr(refused, "value"), (
        "an identity field reached Provider Matching past its schema"
    )
    assert "date_of_birth" in refused.detail


def test_an_unknown_payload_key_is_an_input_refusal_and_not_a_silent_drop() -> None:
    """The refusal names the path and nothing of the value (§6)."""
    from referraleasy_agents.handler import _validated_input

    refused = _validated_input(
        AgentRole.PROVIDER_MATCHING,
        {**body_for(AgentRole.PROVIDER_MATCHING), "contact_phone": "555-0100"},
    )
    assert "contact_phone" in refused.detail
    assert "555-0100" not in refused.detail, (
        "the refusal echoed the value it refused; a refusal that quotes the identity field it "
        "rejected has carried the identity into a trace"
    )


# ── the decision ─────────────────────────────────────────────────────────────────────────────


def test_a_decision_inside_the_envelope_is_built() -> None:
    allowed = ["referral_intake:validate_referral"]
    decision = build_decision(
        action="referral_intake:validate_referral",
        reason_code=ReasonCode.ok,
        evidence_ids=["0f0f0f0f-0000-4000-8000-000000000007"],
        next_actions=allowed,
        allowed_actions=allowed,
        role=AgentRole.REFERRAL_INTAKE,
        workflow_state=STATE,
    )
    assert decision.action == "referral_intake:validate_referral"
    assert [id_text(item) for item in decision.next_allowed_actions] == allowed


def test_a_contract_id_is_read_with_id_text_and_never_with_str() -> None:
    """The trap that caught this file, pinned so it does not catch the next reader.

    `ActionRef`, `EvidenceId` and `ProviderId` are `RootModel`s: `str()` returns the
    pydantic repr (`root='scheduling:list_slots'`), which reads like a value and
    compares like a repr. Nothing in the package may use it, and the two spellings must
    not be conflated — a comparison between a repr and an id silently fails to match,
    which on this surface means "the envelope did not allow the action".
    """
    from referraleasy_agents.canonical import id_text

    reference = ActionRef("scheduling:list_slots")
    assert id_text(reference) == "scheduling:list_slots"
    assert str(reference) != id_text(reference)
    assert "root=" in str(reference)
    # And the literal: an id that arrived as a string is unchanged by `id_text`.
    assert id_text("scheduling:list_slots") == "scheduling:list_slots"


def test_an_action_outside_the_envelope_is_refused_as_a_policy_refusal() -> None:
    """Refused as the runtime's own error, so a caller handling a refused tool handles this."""
    with pytest.raises(PolicyRefused) as caught:
        build_decision(
            action="scheduling:create_booking",
            reason_code=ReasonCode.ok,
            evidence_ids=["0f0f0f0f-0000-4000-8000-000000000007"],
            allowed_actions=["scheduling:list_slots"],
            role=AgentRole.SCHEDULING,
            workflow_state=STATE,
        )
    assert "scheduling:create_booking" in str(caught.value)


def test_allowed_actions_without_a_role_is_a_programming_error_not_a_pass() -> None:
    """The check is skipped only when there is nothing to check against, never silently."""
    with pytest.raises(ValueError) as caught:
        build_decision(
            action="scheduling:create_booking",
            reason_code=ReasonCode.ok,
            evidence_ids=["0f0f0f0f-0000-4000-8000-000000000007"],
            allowed_actions=["scheduling:list_slots"],
        )
    assert "role" in str(caught.value)


@pytest.mark.parametrize(
    "code", [ReasonCode.ok, ReasonCode.coverage_unknown, ReasonCode.no_candidate_matched]
)
def test_a_decision_with_no_evidence_is_refused_for_an_answer_code(code: ReasonCode) -> None:
    """§5: only a pure clarification request may carry no evidence."""
    with pytest.raises(ValueError) as caught:
        build_decision(action="coverage_network:check_plan", reason_code=code)
    assert "evidence" in str(caught.value)


@pytest.mark.parametrize("code", [ReasonCode.missing_mandatory_field, ReasonCode.ambiguous_input])
def test_a_pure_clarification_request_is_legal_without_evidence(code: ReasonCode) -> None:
    decision = build_decision(action="referral_intake:validate_referral", reason_code=code)
    assert decision.evidence_ids == []
    assert decision.reason_code is code


def test_reason_code_other_requires_a_detail() -> None:
    with pytest.raises(ValueError) as caught:
        build_decision(
            action="coverage_network:check_plan", reason_code=ReasonCode.other, evidence_ids=["x"]
        )
    assert "reason_detail" in str(caught.value)
    assert build_decision(
        action="coverage_network:check_plan",
        reason_code=ReasonCode.other,
        reason_detail="two sources disagreed in a way no code names",
        evidence_ids=["0f0f0f0f-0000-4000-8000-000000000007"],
    ).reason_detail


def test_allowed_actions_may_arrive_as_contract_objects_or_as_strings() -> None:
    """Both spellings, because the envelope holds `ActionRef` and a tool holds strings."""
    assert_action_allowed(
        "scheduling:list_slots",
        [ActionRef("scheduling:list_slots")],
        role=AgentRole.SCHEDULING,
        workflow_state=STATE,
    )
    with pytest.raises(PolicyRefused):
        assert_action_allowed(
            "scheduling:create_booking",
            [ActionRef("scheduling:list_slots")],
            role=AgentRole.SCHEDULING,
            workflow_state=STATE,
        )


def test_an_empty_allowed_list_allows_nothing() -> None:
    """The default-deny reading, on the output side."""
    with pytest.raises(PolicyRefused):
        assert_action_allowed(
            "scheduling:list_slots", [], role=AgentRole.SCHEDULING, workflow_state=STATE
        )


def test_mandatory_conditions_derive_their_blockers() -> None:
    """§2.1's three conditions, with "blocked but all three hold" refused."""
    conditions = mandatory_conditions(
        specialty_identified=True, approval_present=False, slot_valid=False
    )
    assert not conditions.approval_present
    assert {str(reason.value) for reason in conditions.blocking_reasons or ()} == {
        "approval_expired",
        "no_slot_available",
    }
    with pytest.raises(ValueError):
        mandatory_conditions(
            specialty_identified=True,
            approval_present=True,
            slot_valid=True,
            blocking_reasons=[ReasonCode.policy_blocked],
        )


# ── the computed next actions ────────────────────────────────────────────────────────────────


def test_next_allowed_actions_is_derived_from_the_policy_table_not_from_the_roles() -> None:
    """Every action it returns is one the gate would permit; asking proves it.

    The direction asserted is the one that matters: the list is a *subset* of what the
    authority allows. A list that named an action the table denies would be a decision
    offering a step the tool layer refuses.
    """
    policy = InMemoryPolicy()
    listed = next_allowed_actions(policy, STATE)
    assert listed, f"no action is permitted in {STATE}; the table has a dead state"
    for action in listed:
        role, _, tool = action.partition(":")
        policy.policy_assert(role, tool, STATE)


def test_next_allowed_actions_is_sorted_and_free_of_duplicates() -> None:
    listed = next_allowed_actions(InMemoryPolicy(), STATE)
    assert listed == sorted(set(listed))


def test_next_allowed_actions_cannot_name_a_tool_outside_a_role_s_allowlist() -> None:
    """The policy table is default-deny, so a tool no role holds cannot appear.

    Not the same claim as the subset one above: a table row for a platform-only tool
    would pass `policy_assert` and still not be an agent's action.
    """
    listed = next_allowed_actions(InMemoryPolicy(), STATE)
    for action in listed:
        role, _, tool = action.partition(":")
        assert tool in allowed_tools(AgentRole(role)), action


def test_next_allowed_actions_is_the_same_in_every_state_it_is_asked_about() -> None:
    """Determinism: the same authority and the same state give the same list.

    The result is used in a decision and compared against what the model returned, so
    a list that varied between two calls would make the comparison meaningless.
    """
    policy = InMemoryPolicy()
    from policy_table import all_states

    for state in all_states():
        assert next_allowed_actions(policy, state) == next_allowed_actions(policy, state)


def test_a_state_with_nothing_permitted_returns_an_empty_list_and_that_is_a_finding() -> None:
    """A dead state is reported as one, never papered over with a default action."""

    class _DenyAll:
        def policy_assert(self, role: str, tool: str, workflow_state: str) -> None:
            raise RuntimeError("no rows")

    assert next_allowed_actions(_DenyAll(), STATE) == []


def test_the_probe_treats_an_unreachable_authority_as_nothing_permitted() -> None:
    """An exception from the backend means "no", not "propagate".

    The gate is the authority on refusals; this function is answering "what may happen
    next", and a query that failed has not said yes.
    """

    class _Broken:
        def policy_assert(self, role: str, tool: str, workflow_state: str) -> None:
            raise RuntimeError("the authority is unavailable")

    assert next_allowed_actions(_Broken(), STATE) == []


def test_the_probe_does_not_swallow_a_cancellation() -> None:
    """The other half: `Exception`, not `BaseException`.

    A `KeyboardInterrupt` or a cancelled task is not an authority saying no, and a
    loop that caught one would keep probing a table after the turn had been cancelled.
    """
    import asyncio

    class _Interrupted:
        def policy_assert(self, role: str, tool: str, workflow_state: str) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        next_allowed_actions(_Interrupted(), STATE)


def _started_toolset(
    role: AgentRole, *, policy: object, payload: Mapping[str, object] | None = None
) -> Any:
    from agents_fixtures import StubApi, toolset_for

    _, toolset = toolset_for(
        role,
        api=StubApi(),
        policy=policy,
        workflow_state=STATE,
        envelope=envelope(),
        payload=payload,
    )
    return toolset


def _model_output(role: AgentRole, *, actions: Sequence[str]) -> Any:
    from agents_fixtures import valid_output

    return valid_output(
        role,
        decision={
            "action": "scheduling:list_slots",
            "reason_code": "ok",
            "evidence_ids": [],
            "missing_fields": [],
            "next_allowed_actions": list(actions),
        },
    )


@pytest.mark.asyncio
async def test_the_recomputation_replaces_the_model_s_list_rather_than_merging_it() -> None:
    """§5, as the handler applies it: computed, not proposed — and not unioned either.

    A union is the quiet failure. It would keep every action the model invented while
    adding the ones the table allows, so the decision would read as though the authority
    had endorsed the invention. The invented action here is one no role may take in this
    state, so a merge would be visible.
    """
    from referraleasy_agents.handler import _with_computed_actions

    policy = InMemoryPolicy()
    invented = ["provider_matching:find_candidates", "scheduling:cancel_booking"]
    replaced = _with_computed_actions(
        AgentRole.SCHEDULING,
        _model_output(AgentRole.SCHEDULING, actions=invented),
        _started_toolset(AgentRole.SCHEDULING, policy=policy),
    )
    listed = [id_text(item) for item in replaced.decision.next_allowed_actions]
    assert listed == next_allowed_actions(policy, STATE)
    assert not set(invented) & set(listed), (
        "the recomputed list still contains an action the model proposed; the replacement "
        "merged instead of replacing"
    )


@pytest.mark.asyncio
async def test_a_list_the_table_already_agrees_with_is_left_as_it_is() -> None:
    """Replacement is a value operation, not a rebuild: an agreeing list is not rewritten."""
    from referraleasy_agents.handler import _with_computed_actions

    policy = InMemoryPolicy()
    agreeing = next_allowed_actions(policy, STATE)
    output = _model_output(AgentRole.SCHEDULING, actions=agreeing)
    assert (
        _with_computed_actions(
            AgentRole.SCHEDULING, output, _started_toolset(AgentRole.SCHEDULING, policy=policy)
        )
        is output
    )


@pytest.mark.asyncio
async def test_without_a_policy_backend_the_model_s_list_stands_and_the_trace_says_so() -> None:
    """No authority means nothing is invented — and the gap is recorded, not silent."""
    from referraleasy_agents.handler import _with_computed_actions, _with_replacement_note

    output = _model_output(AgentRole.SCHEDULING, actions=["scheduling:list_slots"])
    toolset = _started_toolset(AgentRole.SCHEDULING, policy=None)
    assert _with_computed_actions(AgentRole.SCHEDULING, output, toolset) is output
    note = _with_replacement_note(output, toolset)
    assert "not checked" in note["next_allowed_actions"]


@pytest.mark.asyncio
async def test_a_checked_turn_carries_no_replacement_note() -> None:
    """The complement: the note means "could not check", and it is absent when it could."""
    from referraleasy_agents.handler import _with_replacement_note

    output = _model_output(AgentRole.SCHEDULING, actions=["scheduling:list_slots"])
    toolset = _started_toolset(AgentRole.SCHEDULING, policy=InMemoryPolicy())
    assert _with_replacement_note(output, toolset) == {}


@pytest.mark.asyncio
async def test_the_recomputed_list_is_what_the_decision_returns_to_the_caller() -> None:
    """End to end through a real turn: no hand-built output, no hand-built toolset.

    The model's script proposes the action its own role may not take in this state, and
    the report the turn returns carries the table's list — which is the whole claim, at
    the level a caller sees it.
    """
    from agents_fixtures import run_turn

    from referraleasy_agents.offline_model import TextStep

    invented = "provider_matching:find_candidates"
    output = _model_output(AgentRole.SCHEDULING, actions=[invented, "scheduling:lookup_request"])
    report = await run_turn(
        AgentRole.SCHEDULING,
        script=[TextStep(text=output.model_dump_json())],
        workflow_state=STATE,
        payload={
            "command_id": COMMAND_ID,
            "task_envelope": envelope().model_dump(mode="json"),
            **body_for(AgentRole.SCHEDULING),
        },
        policy=InMemoryPolicy(),
    )
    decision = report.trace["decision"]
    assert decision["next_allowed_actions"] == next_allowed_actions(InMemoryPolicy(), STATE)
    assert invented not in decision["next_allowed_actions"]
