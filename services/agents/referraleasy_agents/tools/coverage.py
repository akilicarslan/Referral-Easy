"""Coverage & Network's three tools (§3).

What these tools will not do is the point of the file:

* **They never turn a missing rule into a rule.** `requires_referral` and
  `requires_prior_auth_for_specialist` are `bool | None` in the frozen contract
  precisely because a plan row may not say. A `None` here stays `None`; the tool
  has no code path that substitutes `False`.
* **They never let an `unknown` status travel as a guarantee.**
  :func:`check_provider_network` re-checks the two facts that would read as a
  promise — a `status` of `unknown` with `in_network_for_plan` set, or a missing
  `coverage_not_a_guarantee` disclaimer — and refuses with
  :class:`CoverageGuaranteeRefused`. The mock API already returns the disclaimer;
  the check exists so that a *changed* upstream cannot quietly remove it.
* **They do not resolve conflicts.** §2.2 puts conflict resolution in this agent's
  judgement, but the *facts* travel unchanged: `conflicting_evidence_ids` is passed
  through so the reasoning step has both sources, and `winning_evidence_id` is
  never chosen here.
"""

from __future__ import annotations

from typing import Any, Final

from referraleasy_agents.canonical import id_text, id_texts
from referraleasy_agents.errors import CoverageGuaranteeRefused, UnknownDataError
from referraleasy_agents.tools.call import ToolCall
from referraleasy_contracts.generated.coverage import CoverageResult, InsurancePlan

__all__ = [
    "check_plan",
    "check_provider_network",
    "get_coverage_rules",
    "refuse_missing_disclaimer",
    "refuse_unknown_assertion",
    "refuse_unstated_rule",
]


def refuse_unknown_assertion(result: CoverageResult) -> None:
    """Refuse an answer that says "unknown" and one of yes/no in the same breath.

    §3 forbids treating unknown as in-network *or* as out-of-network. The mock API
    does not produce such an answer today; the check exists so a changed upstream
    cannot quietly start, and so the boundary is callable by `forbidden.py` rather
    than described in a comment.
    """
    if result.status.value == "unknown" and result.in_network_for_plan is not None:
        raise CoverageGuaranteeRefused(
            "the coverage answer reports status 'unknown' while asserting "
            f"in_network_for_plan={result.in_network_for_plan}: an unknown network status is not a "
            "yes or a no, and Coverage & Network reports it as neither (§2.2, §3)."
        )


#: The three plan fields this agent reasons with, and the only fields
#: :func:`refuse_unstated_rule` will look at. A whitelist rather than `getattr` on
#: whatever a caller names: the check must not become a way to read arbitrary
#: attributes of a plan.
_RULE_FIELDS: Final = ("requires_referral", "requires_prior_auth_for_specialist", "network_name")


def refuse_unstated_rule(plan: InsurancePlan, *, field: str, value: Any) -> None:
    """Refuse a value substituted for a rule the source does not state (§3).

    Two refusals, because "inventing a rule" has two shapes:

    * the plan is silent (`None`) and a caller supplies a value anyway — the
      invented rule;
    * the plan states one thing and a caller supplies another — the override.

    A value equal to what the plan states is not an invention and returns quietly.

    This function has **no production caller**, and that is stated rather than
    hidden: the production path never substitutes a value, which is the point. It
    exists so the §3 boundary is callable — `forbidden.py` and
    `test_forbidden_capabilities.py` call it — because a boundary that lives only
    in a prompt is a preference, and one that lives only in a code path nobody can
    reach is a comment.
    """
    if field not in _RULE_FIELDS:
        raise UnknownDataError(
            f"{field!r} is not one of the plan rules this agent reads ({sorted(_RULE_FIELDS)})."
        )
    if value is None:
        return
    stated = getattr(plan, field)
    if stated is None:
        raise UnknownDataError(
            f"the plan states nothing about {field} and {value!r} was supplied for it. A rule the "
            "source does not state is unknown, not required and not optional (§3): report it in "
            "`unstated_fields` and leave the value null."
        )
    if stated != value:
        raise UnknownDataError(
            f"the plan states {field}={stated!r} and {value!r} was supplied instead. The source is "
            "the authority on its own rule (§3)."
        )


def refuse_missing_disclaimer(result: CoverageResult) -> None:
    """Refuse a network status that arrives without the not-a-guarantee disclaimer."""
    if not result.disclaimer_key:
        raise CoverageGuaranteeRefused(
            "the coverage answer carries no `coverage_not_a_guarantee` disclaimer; a network "
            "status without it reads as a payment guarantee, which this agent cannot give (§3)."
        )


def _plan_from_rows(rows: Any, plan_id: str) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise UnknownDataError(
            "GET /plans answered without a `plans` list; no plan can be read from this response."
        )
    for row in rows:
        if isinstance(row, dict) and str(row.get("plan_id")) == id_text(plan_id):
            return row
    raise UnknownDataError(
        f"no plan {plan_id!r} exists in the directory; nothing was learned about it."
    )


async def _plans(call: ToolCall) -> list[dict[str, Any]]:
    body = await call.api.get("/plans", budget=call.budget, lease=call.lease)
    rows = body.get("plans")
    if not isinstance(rows, list):
        raise UnknownDataError("GET /plans answered without a `plans` list.")
    return [row for row in rows if isinstance(row, dict)]


async def _coverage_lookup(
    call: ToolCall, *, plan_id: str, provider_id: str | None, hospital_id: str | None
) -> dict[str, Any]:
    """One `/coverage/lookup` call, validated against the published coverage contract.

    Validating rather than passing the dict through is what makes "the upstream
    answered something contract-shaped" a property of this tool rather than a hope.
    """
    if provider_id is None and hospital_id is None:
        raise UnknownDataError(
            "a coverage lookup needs a provider or a hospital: without a subject there is no "
            "network question to ask (§4)."
        )
    body = await call.api.post(
        "/coverage/lookup",
        {"plan_id": plan_id, "provider_id": provider_id, "hospital_id": hospital_id},
        budget=call.budget,
        lease=call.lease,
    )
    result = CoverageResult.model_validate(body)
    refuse_unknown_assertion(result)
    refuse_missing_disclaimer(result)
    return result.model_dump(mode="json")


async def check_plan(
    call: ToolCall, *, plan_id: str, hospital_id: str | None = None
) -> dict[str, Any]:
    """Read what the plan means on its own, and optionally at one hospital.

    §4 allows this alongside provider matching: it needs no provider, so it is
    independent of the doctor-level work. When a hospital is named the answer is
    the hospital-level network fact, which the frozen `CoverageResult` allows with
    `provider_id: null`.
    """
    plan = _plan_from_rows(await _plans(call), plan_id)
    validated = InsurancePlan.model_validate(plan)
    coverage = None
    evidence_ids: list[str] = []
    if hospital_id is not None:
        coverage = await _coverage_lookup(
            call, plan_id=plan_id, provider_id=None, hospital_id=hospital_id
        )
        evidence_ids = id_texts(coverage.get("evidence_ids") or [])
    return {
        "plan": validated.model_dump(mode="json"),
        "coverage": coverage,
        "evidence_ids": evidence_ids,
    }


async def check_provider_network(
    call: ToolCall, *, plan_id: str, provider_id: str, hospital_id: str | None = None
) -> dict[str, Any]:
    """Ask whether one provider is in network for one plan.

    `provider_id` is required and has no default. §4 puts the doctor-level check
    after the doctor is known; a call with no provider would be a *plan*-level
    question wearing a provider-level name, and the policy table refuses that
    combination in `COVERAGE_CHECK` anyway.
    """
    coverage = await _coverage_lookup(
        call, plan_id=plan_id, provider_id=provider_id, hospital_id=hospital_id
    )
    return {
        "coverage": coverage,
        "conflict": bool(coverage.get("conflict")),
        "conflicting_evidence_ids": list(coverage.get("conflicting_evidence_ids") or []),
        "evidence_ids": id_texts(coverage.get("evidence_ids") or []),
    }


async def get_coverage_rules(call: ToolCall, *, plan_id: str) -> dict[str, Any]:
    """Read the plan's own referral and authorization rules, gaps included.

    Returns exactly what the plan row states. Three fields are frequently absent in
    the fixtures, and they arrive as `null` with a `reason_code` saying so — because
    "not stated" and "not required" are different facts, and only one of them can be
    acted on.
    """
    plan = InsurancePlan.model_validate(_plan_from_rows(await _plans(call), plan_id))
    unstated = [
        field
        for field in ("requires_referral", "requires_prior_auth_for_specialist", "network_name")
        if getattr(plan, field) is None
    ]
    return {
        "plan_id": id_text(plan.plan_id),
        "payer_name": plan.payer_name,
        "plan_name": plan.plan_name,
        "plan_type": plan.plan_type.value,
        "network_name": plan.network_name,
        "requires_referral": plan.requires_referral,
        "requires_prior_auth_for_specialist": plan.requires_prior_auth_for_specialist,
        "unstated_fields": unstated,
        "reason_code": "ok" if not unstated else "missing_mandatory_field",
        "evidence_ids": [],
    }
