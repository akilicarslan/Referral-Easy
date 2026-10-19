"""The tools against the *running* provider API, not a stub.

Every other file in this suite replaces the transport, which is what makes them
deterministic and what makes them unable to answer one question: does the service the
agents actually call agree with the contracts the tools validate against? This module
asks exactly that, and nothing else — four claims, each one a thing a stub cannot
prove:

* **the directory answers the published contract.** `GET /plans` rows are validated
  with the same `InsurancePlan` model `check_plan` and `get_coverage_rules` validate
  with, so a mock API that drifted from the generated contract fails here rather than
  as an `UnknownDataError` in a container.
* **a rule read through the tool states only what the row states.** §3's "cannot
  invent a rule" is one `None` away from being a substitution somewhere, so the
  tool's answer is compared field by field with the row it came from.
* **the decorated conflict arrives unresolved.** The seeded hospital carries a
  directory blurb that disagrees with the plan record — §20's Network Conflict row —
  and the deployed API answers `status: unknown`, `conflict: true`, both evidence ids
  and a stale freshness. The tool must pass that through: not smoothed to a yes, not
  smoothed to a no, and with no winner chosen for the caller (§2.2, §3, §10).
* **the route Recovery must call is not deployed.** `create_handoff` POSTs to
  `/handoffs`; the running service has no such route and answers 404, which the tool's
  transport surfaces as `UpstreamRejected`. Asserted against the deployed service, not
  by reading `app.ts`, because the point is what a container would experience.

The module is skipped, not failed, when nothing answers on `MOCK_API_URL`: a checkout
without compose running has no business reporting a red integration test. Nothing here
writes to the API — every route exercised is a read, plus the missing-route probe,
which is a 404.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from agents_fixtures import API_URL, API_URL_ENV, AllowAllPolicy, envelope, toolset_for

from referraleasy_agent_runtime import (
    AgentRole,
    BudgetCaps,
    TurnBudget,
    UpstreamRejected,
    project_failure,
)
from referraleasy_agents.tools.client import DirectoryApi
from referraleasy_contracts.generated.common import ResultOutcome
from referraleasy_contracts.generated.coverage import InsurancePlan

pytestmark = pytest.mark.integration

#: The three rule fields a plan may or may not state, as `get_coverage_rules` reads them.
RULE_FIELDS = ("requires_referral", "requires_prior_auth_for_specialist", "network_name")

#: A token for the hop. The mock API does not authenticate; the transport requires one.
TOKEN = "integration-token-not-a-credential"


@pytest.fixture(scope="module")
def live_url() -> str:
    """Where the provider API is, or a skip when nothing is listening.

    The probe is synchronous and short: it has to decide before any test body runs, and
    a hung probe would be a hung suite. A checkout without compose running has no
    business reporting a red integration test.
    """
    url = os.environ.get(API_URL_ENV, API_URL)
    try:
        response = httpx.get(f"{url}/plans", timeout=3.0)
        response.raise_for_status()
    except Exception as error:
        pytest.skip(f"no mock API at {url}: {type(error).__name__}: {error}")
    return url


@asynccontextmanager
async def live_transport(url: str) -> AsyncIterator[DirectoryApi]:
    """One real transport per test, closed with it.

    Per test rather than per module: the client's connections belong to the event loop
    that opened them, and pytest gives each async test a fresh loop — a shared client
    would fail the second test with "Event loop is closed".
    """
    api = DirectoryApi(url, token=TOKEN, caps=BudgetCaps(http_tool_timeout_ms=5000))
    try:
        yield api
    finally:
        await api.aclose()


def budget(role: AgentRole) -> TurnBudget:
    """A turn budget with a transport timeout that is not the point of these tests."""
    return TurnBudget(BudgetCaps(http_tool_timeout_ms=5000), role=role)


def coverage_tools(live_api: DirectoryApi) -> Any:
    """Coverage & Network's tools over the deployed API, permitting every call.

    `AllowAllPolicy` on purpose: these tests are about whether the *service* agrees with
    the contract, and the table's own decisions are asserted over the migration in
    `test_service_asgi.py`. A refusal here would be a second, quieter question.
    """
    _, tools = toolset_for(
        AgentRole.COVERAGE_NETWORK,
        api=live_api,
        policy=AllowAllPolicy(),
        budget=budget(AgentRole.COVERAGE_NETWORK),
        workflow_state="AWAITING_DOCTOR_CHOICE",
    )
    return tools


# ── the contract ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_deployed_directory_answers_the_published_contract(live_url: str) -> None:
    """Every plan row the API serves is a row the tools would accept."""
    async with live_transport(live_url) as live_api:
        body = await live_api.get("/plans", budget=None, lease=None)
    rows = body.get("plans")
    assert isinstance(rows, list) and rows, f"GET /plans served no plan rows: {body!r}"

    for row in rows:
        plan = InsurancePlan.model_validate(row)
        assert plan.synthetic is True, f"{plan.plan_id} is not marked synthetic"


@pytest.mark.asyncio
async def test_a_plan_read_through_the_tool_states_only_what_the_row_states(live_url: str) -> None:
    """§3's "cannot invent a rule", stated as a comparison rather than a promise.

    `unstated_fields` must be exactly the fields the row leaves null, and every
    returned rule must equal the row's. Today's seed states all three rules for all six
    plans, so the null branch of this relation is not exercised by the current data —
    the assertion is written relationally so a row that withheld a rule would be
    checked by the same test rather than by a new one.
    """
    async with live_transport(live_url) as live_api:
        body = await live_api.get("/plans", budget=None, lease=None)
        rows = {str(row["plan_id"]): row for row in body["plans"]}
        tools = coverage_tools(live_api)

        for plan_id, row in rows.items():
            answer = await tools.invoke("get_coverage_rules", plan_id=plan_id)
            assert answer["plan_id"] == plan_id
            assert answer["unstated_fields"] == [
                field for field in RULE_FIELDS if row.get(field) is None
            ]
            for field in RULE_FIELDS:
                assert answer[field] == row.get(field), f"{plan_id}: {field} was substituted"
            expected = "ok" if not answer["unstated_fields"] else "missing_mandatory_field"
            assert answer["reason_code"] == expected


# ── the conflict ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_deployed_api_delivers_the_conflict_unresolved(live_url: str) -> None:
    """§20's Network Conflict row, end to end, through the tool that must not resolve it.

    The hospital's own blurb says it is "in-network with every major plan" while the
    plan record disagrees. The answer is `unknown` with both sources attached, and the
    tool hands both to the caller: `winning_evidence_id` is the *service's* field and
    travels inside the coverage record untouched, while the tool's own summary carries
    the pair rather than one of them.
    """
    async with live_transport(live_url) as live_api:
        plans = (await live_api.get("/plans", budget=None, lease=None))["plans"]
        hospitals = (await live_api.get("/hospitals", budget=None, lease=None))["hospitals"]
        plan_id = str(plans[0]["plan_id"])
        hospital_id = str(hospitals[0]["hospital_id"])

        answer = await coverage_tools(live_api).invoke(
            "check_plan", plan_id=plan_id, hospital_id=hospital_id
        )
    coverage = answer["coverage"]
    assert coverage is not None, f"no coverage for {plan_id} at {hospital_id}: {answer!r}"

    assert coverage["status"] == "unknown", (
        "the seeded conflict is not being served as unknown; if the seed changed, this "
        "test is the one that should be revisited"
    )
    assert coverage["in_network_for_plan"] is None, (
        "an unknown network status was asserted as yes or no"
    )
    assert coverage["conflict"] is True
    assert len(coverage["conflicting_evidence_ids"]) == 2
    assert coverage["freshness"]["state"] == "stale"
    assert coverage["disclaimer_key"] == "coverage_not_a_guarantee"
    assert set(answer["evidence_ids"]) >= set(coverage["conflicting_evidence_ids"]), (
        "the tool's evidence does not include both sources of the conflict it read"
    )


# ── the missing route ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_route_recovery_must_call_is_not_deployed(live_url: str) -> None:
    """The deployment gap, measured against the running service instead of the source.

    `migrations/011_handoff.sql` creates the table, and `tools/handoff.py` files the
    record — but `services/mock-api` registers no `/handoffs` route, so a Recovery turn
    that reaches `create_handoff` gets a 404. The transport maps it to
    `UpstreamRejected` (understood and refused, so not retryable), and the runtime
    projects it as `terminal_error`: the turn stops and a human is required, which is
    the honest outcome for a recovery path whose target is not deployed.
    """
    async with live_transport(live_url) as live_api:
        _, tools = toolset_for(
            AgentRole.RECOVERY_HANDOFF,
            api=live_api,
            policy=AllowAllPolicy(),
            budget=budget(AgentRole.RECOVERY_HANDOFF),
            workflow_state="RECOVERY",
            envelope=envelope(workflow_state="RECOVERY"),
            payload={"failed_role": "coverage_network"},
        )

        with pytest.raises(UpstreamRejected) as raised:
            await tools.invoke(
                "create_handoff",
                reason_class="technical_outage",
                reason_detail="the coverage service answered 503 three times",
                patient_visible_summary=(
                    "We could not reach the coverage service; a person will follow up."
                ),
                return_state="RECOVERY",
                consent_given_at="2026-01-05T09:00:00+00:00",
            )

    assert raised.value.status_code == 404, (
        f"the handoff route answered {raised.value.status_code}; if it now exists, this "
        "test should assert the filed record instead of the gap"
    )
    assert project_failure(raised.value).outcome is ResultOutcome.terminal_error
