"""§8's one-way door, and the failure sentence no model writes (§7).

Recovery & Handoff owns §3's last column, and the two rules it must not break are
counted here rather than described:

* **"cannot call the same failed agent forever."** §8's threshold is the *third*
  transient failure, and the move to `RECOVERY` is one-way. `FailureLedger` is
  exercised at 0, 1, 2 and 3 recorded failures, then `enter_recovery` is called and
  the door is shown to stay closed — including against a later `record` that reports
  a *lower* count, which is the state a restarted process could hand it.
  `refuse_if_closed` raises rather than answering `False`, because the caller that
  ignores a boolean is exactly the caller that re-calls the failed specialist.
* **"cannot override a policy block."** `classify_error`'s `permitted_actions` is
  derived from the policy backend, so a blocked action cannot appear in it: with the
  migration's own table the list equals `next_allowed_actions(policy, "RECOVERY")`,
  with an empty table it is empty, and the tool's declared surface has no parameter
  that could express an override. §3's forbidden column is also checked across *every*
  state: no `(role, tool, state)` row the migration marks `allowed = false` can appear
  as a permitted next action anywhere.

**§7, the wording.** *"No LLM call is made just to reword a failure message."* The
sentences are constants keyed by failure class, so the tests here assert identity
(`patient_message(c) is PATIENT_MESSAGES[c]`), coverage (every class in
`FAILURE_CLASSES` has a sentence), that none of them claims coverage or payment, and
that classifying a failure spends **no upstream call at all**.

One finding is pinned rather than smoothed over: at the door, the plan says a human is
required while the patient still reads the `transient` sentence, which says "We will
continue automatically." Both are true of their own clause and the two clauses pull in
different directions; a distinct sentence for the closed door is a copy decision for
whoever owns the product text, so the behaviour is asserted as it stands and reported.

`create_handoff` is exercised through the real tool set too, and its **stated gap** is
asserted: `HANDOFF_ROUTE` is `/handoffs` and the mock API registers no such route, so
the record is validated and posted into a 404 today rather than stored somewhere
private.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from agents_fixtures import (
    WORKFLOW_ID,
    AllowAllPolicy,
    StubApi,
    envelope,
    toolset_for,
)
from policy_table import InMemoryPolicy, all_states, hard_denials, rows_for

from referraleasy_agent_runtime import (
    AgentRole,
    BudgetCaps,
    PolicyUnavailable,
    TurnBudget,
)
from referraleasy_agents.decisions import assert_no_payment_guarantee, next_allowed_actions
from referraleasy_agents.errors import (
    ConsentNotRecorded,
    OneWayDoorRefused,
    UnknownDataError,
)
from referraleasy_agents.recovery import (
    FAILURE_CLASSES,
    PATIENT_MESSAGES,
    TRANSIENT_ATTEMPT_LIMIT,
    FailureLedger,
    classify_failure,
    one_way_door_reached,
    patient_message,
)
from referraleasy_agents.tools.call import ToolCall
from referraleasy_agents.tools.handoff import HANDOFF_ROUTE, classify_error
from referraleasy_agents.tools.specs import TOOL_SPECS
from referraleasy_contracts.generated.common import HandoffReasonClass, WorkflowState

pytestmark = pytest.mark.contract

RECOVERY = AgentRole.RECOVERY_HANDOFF

#: The distinct role a ledger must not confuse with the failed one.
OTHER_ROLE = AgentRole.COVERAGE_NETWORK

CONSENT_AT = "2026-01-05T08:55:00+00:00"


#: A sentinel, so `policy=None` means "no backend" instead of "the default one".
_UNSET: Any = object()


def recovery_tools(*, api: Any = None, policy: Any = _UNSET, state: str = "RECOVERY") -> Any:
    """A started `ToolSet` for Recovery & Handoff, in the state that permits its tools."""
    _, tools = toolset_for(
        RECOVERY,
        api=api if api is not None else StubApi(),
        policy=AllowAllPolicy() if policy is _UNSET else policy,
        workflow_state=state,
        envelope=envelope(workflow_state=state),
        payload={"failed_role": "coverage_network"},
    )
    return tools


def _hand_built(*, policy: Any) -> Any:
    """A `ToolCall` built by hand, the way `forbidden.py` calls the same handlers.

    Needed for the two cases a turn cannot produce: a tool called with no policy
    authority at all, and one called with a backend that denies everything. The gate
    refuses both before a handler runs, so a turn-path test of these would be a test of
    the gate wearing the tool's name.
    """
    return ToolCall(
        role=RECOVERY,
        tool="classify_error",
        workflow_state="RECOVERY",
        api=StubApi(),
        budget=TurnBudget(BudgetCaps(), role=RECOVERY),
        lease=Any,  # type: ignore[arg-type]  # the classifier reads no lease
        envelope=envelope(workflow_state="RECOVERY"),
        policy=policy,
        payload={"failed_role": "coverage_network"},
    )


# ── §8's door ────────────────────────────────────────────────────────────────────────────────


def test_the_threshold_is_the_third_transient_failure() -> None:
    assert TRANSIENT_ATTEMPT_LIMIT == 3


@pytest.mark.parametrize(
    ("attempts", "may_retry"),
    [(0, True), (1, True), (2, True), (3, False), (4, False)],
)
def test_a_specialist_is_called_while_the_door_is_open_and_not_after(
    attempts: int, may_retry: bool
) -> None:
    ledger = FailureLedger(WORKFLOW_ID)
    ledger.record(OTHER_ROLE, transient_attempts=attempts)
    assert ledger.may_retry(OTHER_ROLE) is may_retry
    assert one_way_door_reached(attempts) is (attempts >= TRANSIENT_ATTEMPT_LIMIT)


def test_the_ledger_counts_per_specialist_not_per_workflow() -> None:
    ledger = FailureLedger(WORKFLOW_ID)
    ledger.record(OTHER_ROLE, transient_attempts=TRANSIENT_ATTEMPT_LIMIT)
    assert ledger.attempts(OTHER_ROLE) == TRANSIENT_ATTEMPT_LIMIT
    assert ledger.attempts(RECOVERY) == 0
    assert ledger.may_retry(RECOVERY) is True, (
        "another specialist's failures closed this one's door"
    )


def test_entering_recovery_is_one_way() -> None:
    """`record` takes the workflow's *total*, so a lower number must not re-open the door."""
    ledger = FailureLedger(WORKFLOW_ID)
    ledger.record(OTHER_ROLE, transient_attempts=1)
    ledger.enter_recovery(OTHER_ROLE)
    assert ledger.is_closed(OTHER_ROLE) is True
    assert ledger.may_retry(OTHER_ROLE) is False

    ledger.record(OTHER_ROLE, transient_attempts=0)
    assert ledger.may_retry(OTHER_ROLE) is False, "a restarted process re-opened a one-way door"


def test_the_closed_door_refuses_rather_than_answering_false() -> None:
    """§3: an error, because the caller that ignores a boolean is the one that re-calls."""
    ledger = FailureLedger(WORKFLOW_ID)
    ledger.record(OTHER_ROLE, transient_attempts=TRANSIENT_ATTEMPT_LIMIT)
    with pytest.raises(OneWayDoorRefused) as raised:
        ledger.refuse_if_closed(OTHER_ROLE)
    message = str(raised.value)
    assert str(TRANSIENT_ATTEMPT_LIMIT) in message
    assert WORKFLOW_ID in message
    assert "recovery" in message.casefold()


def test_an_open_door_does_not_refuse() -> None:
    ledger = FailureLedger(WORKFLOW_ID)
    ledger.record(OTHER_ROLE, transient_attempts=TRANSIENT_ATTEMPT_LIMIT - 1)
    ledger.refuse_if_closed(OTHER_ROLE)  # the last normal call is still normal


# ── classifying a failure ────────────────────────────────────────────────────────────────────


def test_the_reason_code_wins_over_the_error_class() -> None:
    """The reason code is the controlled vocabulary; a class name is the fallback."""
    assert classify_failure(
        error_class="UpstreamTransient", reason_code="booking_outcome_unknown"
    ) == ("booking_unknown")
    assert classify_failure(error_class="UpstreamTransient") == "transient"
    assert classify_failure(
        error_class="PolicyRefused", reason_code="approval_payload_changed"
    ) == ("policy_blocked")


def test_an_unrecognised_failure_goes_to_a_human_rather_than_into_an_automatic_path() -> None:
    assert classify_failure(error_class="KablolamaHatasi") == "unknown"
    assert classify_failure(error_class="KablolamaHatasi", transient_attempts=1) == "transient"


@pytest.mark.parametrize(
    ("error_class", "reason_code", "attempts"),
    [
        ("UpstreamTransient", None, 0),
        ("UpstreamTransient", "upstream_timeout", 2),
        ("UpstreamRejected", "upstream_rejected", 0),
        ("PolicyRefused", "policy_blocked", 0),
        ("GuardrailRefused", None, 0),
        ("MissingData", "missing_mandatory_field", 0),
        ("DocumentError", "document_missing", 0),
        ("DocumentError", "document_unreadable", 0),
        ("CoverageError", "coverage_unknown", 0),
        ("NoCandidates", "no_candidate_matched", 0),
        ("BookingError", "booking_outcome_unknown", 0),
        ("KablolamaHatasi", None, 0),
        ("KablolamaHatasi", None, 3),
    ],
)
def test_the_classifier_only_returns_classes_that_have_a_plan(
    error_class: str, reason_code: str | None, attempts: int
) -> None:
    classification = classify_failure(
        error_class=error_class, reason_code=reason_code, transient_attempts=attempts
    )
    assert classification in FAILURE_CLASSES
    assert patient_message(classification) in PATIENT_MESSAGES.values()


def test_every_failure_class_has_a_sentence_and_no_sentence_claims_coverage() -> None:
    """§7: a class with no sentence would be a class whose wording someone has to generate."""
    assert set(PATIENT_MESSAGES) == set(FAILURE_CLASSES)
    for classification, sentence in PATIENT_MESSAGES.items():
        assert sentence.strip(), f"{classification} has an empty sentence"
        # The same guard the output reviewer applies: a failure sentence must not
        # read as a coverage or payment guarantee (§3, Coverage & Network).
        assert_no_payment_guarantee(sentence)


def test_the_sentence_is_a_table_lookup_and_not_a_generation() -> None:
    """Identity, not equality: a model-written sentence could be equal by accident, never identical."""
    for classification in FAILURE_CLASSES:
        assert patient_message(classification) is PATIENT_MESSAGES[classification]


# ── the plan, and the policy that owns it ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_policy_block_cannot_be_turned_into_a_retry() -> None:
    api = StubApi()
    result = await recovery_tools(api=api, policy=InMemoryPolicy()).invoke(
        "classify_error",
        failed_role="referral_readiness",
        error_class="PolicyRefused",
        transient_attempts=0,
        reason_code="policy_blocked",
    )
    plan = result["action_plan"]
    assert plan["failure_class"] == "policy_blocked"
    assert plan["retry_same_agent"] is False
    assert plan["human_handoff_required"] is True
    assert plan["return_state"] == WorkflowState.HUMAN_HANDOFF.value
    assert api.calls == [], "classifying a failure called something upstream; it decides in code"


@pytest.mark.asyncio
async def test_the_plan_s_permitted_actions_are_the_policy_table_s_answer() -> None:
    policy = InMemoryPolicy()
    result = await recovery_tools(policy=policy).invoke(
        "classify_error",
        failed_role="provider_matching",
        error_class="UpstreamTransient",
        transient_attempts=1,
    )
    plan = result["action_plan"]
    assert plan["permitted_actions"] == next_allowed_actions(policy, WorkflowState.RECOVERY.value)
    assert plan["permitted_actions"], "RECOVERY is not a dead state: the table permits action there"
    assert all(
        InMemoryPolicy.permitted(
            action.split(":", 1)[0], action.split(":", 1)[1], WorkflowState.RECOVERY.value
        )
        for action in plan["permitted_actions"]
    )


@pytest.mark.asyncio
async def test_a_turn_with_no_policy_backend_is_refused_by_the_gate_before_the_tool_runs() -> None:
    """Default-deny lives in the gate: the tool's own no-policy branch is not the door.

    `classify_error` also handles a missing authority (it then permits nothing), but a
    turn never reaches it that way: `ToolGate.check` refuses with `PolicyUnavailable`
    first. Both facts are asserted, because only the second would be true if the tool
    were ever called with a hand-built `ToolCall`.
    """
    with pytest.raises(PolicyUnavailable) as raised:
        await recovery_tools(policy=None).invoke(
            "classify_error",
            failed_role="provider_matching",
            error_class="UpstreamTransient",
            transient_attempts=1,
        )
    assert "no policy backend" in str(raised.value)


@pytest.mark.asyncio
async def test_a_tool_call_with_no_policy_authority_permits_nothing() -> None:
    """The tool's own default-deny, exercised on a hand-built call (`forbidden.py`'s shape)."""
    result = await classify_error(
        _hand_built(policy=None),
        failed_role="provider_matching",
        error_class="UpstreamTransient",
        transient_attempts=1,
    )
    assert result["action_plan"]["permitted_actions"] == []


@pytest.mark.asyncio
async def test_a_backend_that_permits_nothing_yields_a_plan_that_permits_nothing() -> None:
    result = await classify_error(
        _hand_built(policy=InMemoryPolicy(rows=())),
        failed_role="provider_matching",
        error_class="UpstreamTransient",
        transient_attempts=0,
    )
    plan = result["action_plan"]
    assert plan["permitted_actions"] == []
    assert plan["retry_same_agent"] is True, (
        "the plan still reports the failure; it just may not act"
    )


def test_no_denied_row_can_appear_as_a_permitted_next_action() -> None:
    """§3's forbidden column, checked state by state across the whole table.

    A `(role, tool, state)` row the migration marks `allowed = false` is a capability
    taken away by name. If one of them could appear in `next_allowed_actions`, the
    platform would be recommending the act it forbids — which is the failure mode the
    plan's `permitted_actions` field would carry straight to the state machine.
    """
    denial_states: dict[tuple[str, str], set[str]] = {}
    for denial in hard_denials():
        denial_states.setdefault((denial.role, denial.tool), set()).add(denial.state)
    assert denial_states, "the migration parsed no denials at all; this test would be vacuous"

    policy = InMemoryPolicy()
    for state in all_states():
        permitted = set(next_allowed_actions(policy, state))
        for (role, tool), denied in denial_states.items():
            if state in denied:
                assert f"{role}:{tool}" not in permitted, (
                    f"{state} permits a denied row: {role}/{tool}"
                )


@pytest.mark.asyncio
async def test_the_third_transient_failure_is_reported_as_the_third() -> None:
    result = await recovery_tools(policy=InMemoryPolicy()).invoke(
        "classify_error",
        failed_role="coverage_network",
        error_class="UpstreamTransient",
        transient_attempts=TRANSIENT_ATTEMPT_LIMIT,
    )
    assert result["one_way_door_reached"] is True
    assert result["transient_attempt_limit"] == TRANSIENT_ATTEMPT_LIMIT
    plan = result["action_plan"]
    assert plan["retry_same_agent"] is False
    assert plan["human_handoff_required"] is True
    assert plan["return_state"] == WorkflowState.HUMAN_HANDOFF.value


@pytest.mark.asyncio
async def test_the_door_does_not_change_the_wording_and_that_is_a_known_tension() -> None:
    """The closed door requires a human; the sentence still says "we will continue automatically".

    Both clauses are true of their own subject — recovery does continue — but the pair
    reads as a promise of automatic retry at the exact moment retrying stopped. Pinned
    as it stands: choosing different copy for the closed door is a product-text
    decision, and quietly rewriting a §7 sentence to make a test read better is not one.
    """
    tools = recovery_tools(policy=InMemoryPolicy())
    before = await tools.invoke(
        "classify_error",
        failed_role="coverage_network",
        error_class="UpstreamTransient",
        transient_attempts=1,
    )
    after = await tools.invoke(
        "classify_error",
        failed_role="coverage_network",
        error_class="UpstreamTransient",
        transient_attempts=TRANSIENT_ATTEMPT_LIMIT,
    )
    assert before["patient_message"] == after["patient_message"]
    assert after["patient_message"] == PATIENT_MESSAGES["transient"]
    assert "continue automatically" in after["patient_message"]
    assert before["action_plan"]["retry_same_agent"] is True
    assert after["action_plan"]["retry_same_agent"] is False


@pytest.mark.asyncio
async def test_a_class_the_classifier_does_not_know_still_gets_a_plan() -> None:
    """`classify_error`'s off-table refusal is unreachable, and the reason is the vocabulary.

    The tool raises `UnknownDataError` for a class outside `FAILURE_CLASSES`; the
    classifier cannot produce one — every value in its two mappings is a table member,
    which is what this asserts over the widest input set the tool accepts. The branch
    stays as a backstop for a future class the table has not been extended with, and it
    is deliberately not exercised by a test that would have to fake a table member.
    """
    for error_class in ("KablolamaHatasi", "Exception", "ValueError", "UpstreamTransient"):
        result = await recovery_tools().invoke(
            "classify_error",
            failed_role="scheduling",
            error_class=error_class,
            transient_attempts=0,
        )
        assert result["action_plan"]["failure_class"] in FAILURE_CLASSES
        assert result["patient_message"] in PATIENT_MESSAGES.values()


def test_the_classify_error_surface_has_no_way_to_express_an_override() -> None:
    """The refusal is structural: there is no parameter that could carry "ignore the block"."""
    names = {parameter.name for parameter in TOOL_SPECS["classify_error"].parameters}
    assert names == {"failed_role", "error_class", "transient_attempts", "reason_code"}
    forbidden = ("override", "force", "ignore", "bypass", "anyway", "regardless", "skip")
    assert not [name for name in names if any(token in name for token in forbidden)]


# ── the handoff ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_handoff_without_consent_is_refused() -> None:
    api = StubApi({HANDOFF_ROUTE: {}})
    with pytest.raises(ConsentNotRecorded) as raised:
        await recovery_tools(api=api).invoke(
            "create_handoff",
            reason_class="no_reliable_recovery",
            reason_detail="three transient failures at coverage_network",
            patient_visible_summary="We could not finish checking your coverage.",
            return_state="HUMAN_HANDOFF",
        )
    assert "consent" in str(raised.value)
    assert api.calls == [], "a handoff was posted before its consent was checked"


@pytest.mark.asyncio
async def test_a_handoff_reason_outside_the_classes_is_refused() -> None:
    with pytest.raises(UnknownDataError) as raised:
        await recovery_tools().invoke(
            "create_handoff",
            reason_class="we felt like it",
            reason_detail="a free-text reason",
            patient_visible_summary="A person will look at this.",
            return_state="HUMAN_HANDOFF",
            consent_given_at=CONSENT_AT,
        )
    message = str(raised.value)
    assert all(member.value in message for member in HandoffReasonClass)


@pytest.mark.asyncio
async def test_a_handoff_return_state_outside_the_workflow_is_refused() -> None:
    with pytest.raises(UnknownDataError) as raised:
        await recovery_tools().invoke(
            "create_handoff",
            reason_class="patient_requested",
            reason_detail="the patient asked for a person",
            patient_visible_summary="A person will call you back.",
            return_state="SOMEWHERE_ELSE",
            consent_given_at=CONSENT_AT,
        )
    assert "sixteen workflow states" in str(raised.value)


@pytest.mark.asyncio
async def test_a_handoff_is_filed_against_the_envelope_s_workflow_with_the_consented_summary() -> (
    None
):
    api = StubApi({HANDOFF_ROUTE: {}})
    tools = recovery_tools(api=api)
    result = await tools.invoke(
        "create_handoff",
        reason_class="prolonged_unknown_booking",
        reason_detail="the lookup did not resolve within the reconciliation window",
        patient_visible_summary="We sent your booking request and are still checking with the hospital.",
        return_state="BOOKING_UNVERIFIED",
        consent_given_at=CONSENT_AT,
    )

    method, path, body = api.calls[0]
    assert (method, path) == ("POST", HANDOFF_ROUTE)
    assert body["workflow_id"] == WORKFLOW_ID, "the workflow is the envelope's, not a tool argument"
    assert body["reason_class"] == "prolonged_unknown_booking"
    assert body["patient_visible_summary"].startswith("We sent your booking request")
    assert body["consent_given_at"] == CONSENT_AT
    assert body["return_state"] == "BOOKING_UNVERIFIED"
    assert body["status"] == "queued"
    assert result["final_approval_still_required"] is True
    assert result["handoff"]["workflow_id"] == WORKFLOW_ID


def test_the_handoff_surface_has_no_workflow_parameter() -> None:
    names = {parameter.name for parameter in TOOL_SPECS["create_handoff"].parameters}
    assert "workflow_id" not in names
    assert names == {
        "reason_class",
        "reason_detail",
        "patient_visible_summary",
        "return_state",
        "consent_given_at",
    }


def test_the_handoff_route_does_not_exist_in_the_mock_api(repo_root: Path) -> None:
    """`create_handoff` posts to a route nobody wrote — asserted, so it stays reported.

    `tools/handoff.py` states this gap in its docstring; a docstring is not a fact a CI
    run checks, so the absence is checked here. When the route is added this test fails,
    which is the moment the docstring and the record's storage need revisiting together.
    """
    assert HANDOFF_ROUTE == "/handoffs"
    app = (repo_root / "services" / "mock-api" / "src" / "app.ts").read_text(encoding="utf-8")
    assert "/handoffs" not in app
    assert "handoff" not in app.casefold()


def test_the_handoff_table_exists_even_though_the_route_does_not(repo_root: Path) -> None:
    """The gap is a route gap, not a schema gap: `011_handoff.sql` is where the record lands."""
    migration = repo_root / "migrations" / "011_handoff.sql"
    assert migration.exists()
    assert "handoff" in migration.read_text(encoding="utf-8").casefold()


def test_recovery_s_bound_is_a_row_in_the_table_not_an_absence() -> None:
    """§3's "bounded actions" is written down: booking is denied in RECOVERY by name.

    A capability that exists only because nobody wrote a row would be revoked by
    someone adding one; this asserts the denial is deliberate.
    """
    denials = [row for row in rows_for(role="recovery_handoff") if not row.allowed]
    assert denials, "recovery_handoff has no explicit denial row: §3's bound is not in the table"
    assert {row.tool for row in denials} == {"create_booking"}
    assert {row.state for row in denials} == {"RECOVERY"}
