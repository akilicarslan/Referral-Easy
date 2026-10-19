"""§3's split inside Provider Matching: what is code, and what the sub-agents may not do.

§3 draws the line precisely: *distance, filtering and uniqueness are implemented in
code; simple ranking is done directly in code; the sub-agent interprets ambiguous or
contradictory textual conditions and the alternatives to put to the user.* The four
claims this file holds the implementation to, each with a way to fail loudly:

* **there is no numeric score anywhere in the analyst's schema.** Not "the prompt
  says not to score": the field does not exist, so a score would have to be invented,
  and inventing it is a validation error (`extra="forbid"`). §3: *"bulunmayan veriyi
  puanla örtemez."*
* **a non-null verdict without evidence is refused** — `satisfied: true` with nothing
  behind it is a claim about a hospital, and the analyst may emit `null` instead.
* **distance, filtering, uniqueness and ranking are code** — measured from
  coordinates, filtered case-folded and exactly, de-duplicated with provenance
  unioned, ordered by distance with unknown last.
* **the two sub-agents share the parent's turn budget by construction** — every
  callable in the tree comes from one `ToolSet` holding one `TurnBudget`, and the
  test spends the budget through a sub-agent's own tool and watches the parent's
  snapshot move.

The last block is a **finding, not a defect report**: the authored
`fixtures/directory/provider_candidates.json` distances do not match any great-circle
distance from the hospital locations, so the fixture and §3 cannot both be satisfied.
§3 wins (distance is computed), and the discrepancy is pinned here by name so nobody
quietly "fixes" the code to agree with the fixture.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Union, cast, get_args, get_origin

import pytest
from agents_fixtures import (
    EVIDENCE_ID,
    HOSPITAL_ID,
    PROVIDER_ID,
    AllowAllPolicy,
    StubApi,
    envelope,
    payload_for,
)
from pydantic import ValidationError

from referraleasy_agent_runtime import AgentRole, BudgetCaps, TurnBudget, allowed_tools
from referraleasy_agents import subagents
from referraleasy_agents.assembly import build_agent
from referraleasy_agents.canonical import id_text
from referraleasy_agents.cards import SUB_AGENT_NAMES
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.matching_code import (
    dedupe_candidates,
    filter_providers,
    haversine_miles,
    hospital_distance,
    locations_by_id,
    rank_candidates,
    validate_analysis,
)
from referraleasy_agents.offline_model import TextStep, ToolCallStep, scripted_model
from referraleasy_agents.schemas import CandidateConstraintAnalysis
from referraleasy_agents.tools.toolbox import ToolBox
from referraleasy_contracts.generated.common import EvidenceId, HospitalId, ProviderId
from referraleasy_contracts.generated.directory import (
    ConstraintFitItem,
    Hospital,
    Location,
    Provider,
    ProviderCandidate,
)

pytestmark = pytest.mark.contract

CONSTRAINT_A = "e0000000-0000-4000-8000-000000000001"
CONSTRAINT_B = "e0000000-0000-4000-8000-000000000002"

#: The patient's fixture search origin, from `fixtures/patients/preferences.json`.
PATIENT_ORIGIN = {"latitude": 41.8955, "longitude": -87.635}


def _provider_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "provider_id": PROVIDER_ID,
        "display_name": "Elena Marquez",
        "credentials": "MD",
        "specialty": "cardiology",
        "subspecialties": [],
        "languages": [
            {"language": "en", "fluency": "native"},
            {"language": "es-MX", "fluency": "native"},
        ],
        "accepts_new_patients": True,
        "accessibility_features": [],
        "first_visit_duration_minutes": 60,
        "return_visit_duration_minutes": 20,
        "hospital_ids": [HOSPITAL_ID],
        "accepts_insurance": ["Northbridge Health"],
        "profile_text": "A synthetic cardiologist.",
        "synthetic": True,
    }
    row.update(overrides)
    return row


def _provider(**overrides: Any) -> Provider:
    return Provider.model_validate(_provider_row(**overrides))


def _fit(constraint_id: str = CONSTRAINT_A, *, satisfied: bool | None = True) -> ConstraintFitItem:
    return ConstraintFitItem(
        constraint_id=constraint_id, satisfied=satisfied, reason_code="ok", detail=None
    )


def _analysis(
    *,
    fits: Sequence[ConstraintFitItem] | None = None,
    evidence_by_constraint: Mapping[str, list[str]] | None = None,
) -> CandidateConstraintAnalysis:
    return CandidateConstraintAnalysis(
        provider_id=ProviderId(PROVIDER_ID),
        fits=list(fits if fits is not None else [_fit()]),
        evidence_by_constraint={
            key: [EvidenceId(item) for item in value]
            for key, value in (evidence_by_constraint or {}).items()
        },
    )


# ── no score, on purpose ─────────────────────────────────────────────────────────────────────


def _atomic_types(annotation: Any, seen: set[type]) -> set[type]:
    """Every leaf type in ``annotation``, expanding containers and nested models."""
    origin = get_origin(annotation)
    if origin in (list, tuple, set, frozenset, dict, Mapping) or origin is Union:
        found: set[type] = set()
        for argument in get_args(annotation):
            if argument is type(None):
                continue
            found |= _atomic_types(argument, seen)
        return found
    if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
        if annotation in seen:
            return set()
        seen.add(annotation)
        found = set()
        for field in annotation.model_fields.values():
            found |= _atomic_types(field.annotation, seen)
        return found
    return {annotation} if isinstance(annotation, type) else set()


def test_the_analysis_schema_has_no_numeric_field_at_all() -> None:
    """The analyst cannot cover missing data with a score, because there is no field to put one in."""
    numeric = {int, float, complex}
    seen: set[type] = {CandidateConstraintAnalysis}
    for name, field in CandidateConstraintAnalysis.model_fields.items():
        found = _atomic_types(field.annotation, seen)
        assert not (found & numeric), (
            f"CandidateConstraintAnalysis.{name} can carry "
            f"{sorted(t.__name__ for t in found & numeric)}: "
            "§3 forbids the Constraint Analyst from covering absent data with a score, so no numeric "
            "field may exist for one to hide in"
        )


def test_inventing_a_score_is_a_validation_error_not_an_ignored_key() -> None:
    """`extra='forbid'`: a field nobody declared does not get to travel as a result."""
    with pytest.raises(ValidationError):
        CandidateConstraintAnalysis.model_validate(
            {
                "provider_id": PROVIDER_ID,
                "fits": [],
                "question_for_patient": None,
                "score": 0.87,
            }
        )


def test_a_verdict_without_evidence_is_refused() -> None:
    with pytest.raises(UnknownDataError) as raised:
        validate_analysis(_analysis())
    assert "scored over" in str(raised.value)


def test_a_null_verdict_needs_no_evidence_and_is_a_real_answer() -> None:
    validate_analysis(_analysis(fits=[_fit(satisfied=None)]))


def test_a_verdict_with_evidence_passes() -> None:
    validate_analysis(
        _analysis(evidence_by_constraint={CONSTRAINT_A: [EVIDENCE_ID]}),
        constraints={CONSTRAINT_A: None},  # type: ignore[dict-item]
    )


def test_a_fit_for_a_constraint_that_was_not_asked_about_is_refused() -> None:
    with pytest.raises(UnknownDataError) as raised:
        validate_analysis(
            _analysis(
                fits=[_fit(constraint_id=CONSTRAINT_B)],
                evidence_by_constraint={CONSTRAINT_B: [EVIDENCE_ID]},
            ),
            constraints={CONSTRAINT_A: None},  # type: ignore[dict-item]
        )
    assert CONSTRAINT_B in str(raised.value)


# ── distance, filtering, uniqueness and ranking are code ─────────────────────────────────────


def test_distance_is_great_circle_from_coordinates_and_rounded() -> None:
    origin = {"latitude": 41.8955, "longitude": -87.635}
    hospital_one = {"latitude": 41.8955, "longitude": -87.621}
    computed = haversine_miles(origin, hospital_one)
    assert computed == haversine_miles(hospital_one, origin), "distance is symmetric"
    assert round(computed, 2) == computed
    assert 0.6 < computed < 0.8


def test_a_hospital_with_no_location_row_has_an_unknown_distance_not_a_zero() -> None:
    hospital = Hospital.model_validate(
        json.loads(json.dumps(_hospital_row(), default=str))
        | {"location_id": "10000000-0000-4000-8000-0000000000ff"}
    )
    assert hospital_distance(hospital, origin=PATIENT_ORIGIN, locations={}) is None


def _hospital_row() -> dict[str, Any]:
    return {
        "hospital_id": HOSPITAL_ID,
        "name": "Lakeview Regional Medical Center",
        "location_id": "10000000-0000-4000-8000-000000000001",
        "timezone": "America/Chicago",
        "adapter_type": "instant_confirm",
        "accepts_new_patients": True,
        "accessibility_features": [],
        "languages_supported": ["en"],
        "accepted_plan_ids": [],
        "synthetic": True,
    }


def test_filtering_is_exact_and_case_folded_and_never_fuzzy() -> None:
    """A substring match would let "cardiology" select "cardiothoracic surgery"."""
    cardiac = _provider()
    surgeon = _provider(
        provider_id="30000000-0000-4000-8000-000000000099", specialty="cardiothoracic surgery"
    )
    assert filter_providers([cardiac, surgeon], specialty="CARDIOLOGY") == [cardiac]
    assert filter_providers([cardiac, surgeon], specialty="cardiothoracic surgery") == [surgeon]


def test_a_language_constraint_matches_by_bcp47_prefix() -> None:
    """`es` selects `es-MX`; the comparison reads the tag's text, not the wrapper's repr."""
    spanish = _provider()
    english_only = _provider(
        provider_id="30000000-0000-4000-8000-000000000098",
        languages=[{"language": "en", "fluency": "native"}],
    )
    assert filter_providers([spanish, english_only], languages=["es"]) == [spanish]
    assert filter_providers([spanish, english_only], languages=["fr"]) == []


def test_dedupe_unions_provenance_and_does_not_depend_on_input_order() -> None:
    first = ProviderCandidate(
        provider_id=ProviderId(PROVIDER_ID),
        hospital_id=HospitalId(HOSPITAL_ID),
        distance_miles=3.2,
        constraint_fit=[],
        provenance=[EvidenceId(EVIDENCE_ID)],
        evidence_ids=[EvidenceId(EVIDENCE_ID)],
    )
    other_evidence = "0f0f0f0f-0000-4000-8000-0000000000ee"
    second = ProviderCandidate(
        provider_id=ProviderId(PROVIDER_ID),
        hospital_id=HospitalId(HOSPITAL_ID),
        distance_miles=None,
        constraint_fit=[],
        provenance=[EvidenceId(other_evidence)],
        evidence_ids=[EvidenceId(other_evidence)],
    )
    forwards = dedupe_candidates([first, second])
    backwards = dedupe_candidates([second, first])
    assert len(forwards) == 1
    assert [id_text(item.provider_id) for item in forwards] == [
        id_text(item.provider_id) for item in backwards
    ], "the pool's ordering must not depend on retrieval order"
    # The *set* of evidence is order-independent; the order within one merged entry
    # follows first-seen retrieval order, which is the order the ledger records.
    merged_evidence = forwards[0].evidence_ids
    assert merged_evidence is not None
    assert {id_text(item) for item in merged_evidence} == {EVIDENCE_ID, other_evidence}
    provenance = forwards[0].provenance
    assert provenance is not None
    assert {id_text(item) for item in provenance} == {EVIDENCE_ID, other_evidence}
    assert forwards[0].distance_miles == 3.2, "a duplicate with a distance wins over one without"


def test_ranking_is_by_distance_with_unknown_last() -> None:
    def candidate(provider: str, distance: float | None) -> ProviderCandidate:
        return ProviderCandidate(
            provider_id=ProviderId(provider),
            hospital_id=HospitalId(HOSPITAL_ID),
            distance_miles=distance,
            constraint_fit=[],
            provenance=[EvidenceId(EVIDENCE_ID)],
            evidence_ids=[EvidenceId(EVIDENCE_ID)],
        )

    near = candidate("30000000-0000-4000-8000-000000000001", 1.0)
    far = candidate("30000000-0000-4000-8000-000000000002", 9.0)
    unknown = candidate("30000000-0000-4000-8000-000000000003", None)
    ranked = rank_candidates([unknown, far, near])
    assert [item.rank for item in ranked] == [1, 2, 3]
    assert [item.provider_id.root for item in ranked][-1] == unknown.provider_id.root


def test_the_fixtures_authored_distance_is_not_geometric(repo_root: Path) -> None:
    """The authored 3.2 miles is not a great-circle distance — a finding, pinned rather than tuned.

    §3 requires distance to be *computed*; the fixture was authored with a different
    (or reused) number. Whoever reconciles the two should fix
    `fixtures/directory/provider_candidates.json`, not this function.
    """
    candidates = json.loads((repo_root / "fixtures/directory/provider_candidates.json").read_text())
    authored = next(
        row
        for row in candidates
        if row["provider_id"] == "30000000-0000-4000-8000-000000000001"
        and row["hospital_id"] == "20000000-0000-4000-8000-000000000001"
    )
    locations = locations_by_id(
        Location.model_validate(row)
        for row in json.loads((repo_root / "fixtures/directory/locations.json").read_text())
    )
    hospital = Hospital.model_validate(
        next(
            row
            for row in json.loads((repo_root / "fixtures/directory/hospitals.json").read_text())
            if row["hospital_id"] == authored["hospital_id"]
        )
    )
    computed = hospital_distance(hospital, origin=PATIENT_ORIGIN, locations=locations)
    assert computed is not None
    assert computed != authored["distance_miles"], (
        "the authored distance now agrees with a great-circle distance from the patient's search "
        "origin. If the fixtures were fixed, delete this test and the finding in "
        "`matching_code.py`; do not keep it as a pass-through."
    )
    assert abs(computed - authored["distance_miles"]) > 0.5


# ── the two sub-agents ───────────────────────────────────────────────────────────────────────


def test_the_protocol_names_are_the_node_names_with_dashes() -> None:
    assert subagents.protocol_name(subagents.CANDIDATE_RESEARCHER) == "candidate-researcher"
    assert subagents.sub_agent_names() == (
        subagents.CANDIDATE_RESEARCHER,
        subagents.CONSTRAINT_ANALYST,
    )
    assert SUB_AGENT_NAMES[AgentRole.PROVIDER_MATCHING] == (
        "candidate-researcher",
        "constraint-analyst",
    )


def test_each_sub_agent_holds_a_subset_of_the_parent_s_allowlist() -> None:
    parent = allowed_tools(AgentRole.PROVIDER_MATCHING)
    assert set(subagents.RESEARCHER_TOOLS) <= parent
    assert set(subagents.ANALYST_TOOLS) <= parent
    assert "find_candidates" not in subagents.ANALYST_TOOLS, (
        "the analyst must not be able to rebuild the pool — it reviews what the researcher found"
    )


def test_a_sub_agent_tool_the_surface_lacks_is_a_configuration_error() -> None:
    tools = _matching_toolset()
    with pytest.raises(ValueError) as raised:
        subagents._tools_named(tools, ("create_booking",), owner="a sub-agent")
    assert "create_booking" in str(raised.value)


@pytest.mark.asyncio
async def test_a_sub_agent_tool_call_charges_the_parent_s_budget() -> None:
    """Shared budget by construction: one `ToolSet`, one `TurnBudget`, one counter."""
    tools = _matching_toolset()
    researcher = subagents.candidate_researcher(tools=tools, model=scripted_model(()))
    exposed = cast("Sequence[Any]", tools.exposed())
    by_name = {callable_.__name__: callable_ for callable_ in exposed}
    researcher_tools = cast("Sequence[Any]", researcher.tools)
    assert [item.__name__ for item in researcher_tools] == list(subagents.RESEARCHER_TOOLS)
    search = next(item for item in researcher_tools if item.__name__ == "search_directory")
    assert search.__qualname__.startswith("ToolSet."), (
        "the callable must be the parent's own tool wrapper, not a re-implementation"
    )
    assert set(by_name) >= set(subagents.RESEARCHER_TOOLS)

    before = tools.budget.snapshot()
    await search(query="a cardiologist who speaks Spanish")
    after = tools.budget.snapshot()
    assert after.tool_calls == before.tool_calls + 1, (
        "a sub-agent's call must be charged to the parent's turn; a sub-agent with its own "
        "counter could spend budget the parent never allocated"
    )


def test_only_provider_matching_exposes_sub_agents_as_tools() -> None:
    tools = _matching_toolset()
    matching = build_agent(
        AgentRole.PROVIDER_MATCHING,
        tools=tools,
        instruction="briefing",
        model=scripted_model(()),
        sub_agent_models={name: scripted_model(()) for name in subagents.sub_agent_names()},
    )
    from google.adk.tools.base_tool import BaseTool

    extra = [tool for tool in matching.tools if isinstance(tool, BaseTool)]
    assert sorted(tool.name for tool in extra) == sorted(subagents.sub_agent_names())

    other = build_agent(
        AgentRole.COVERAGE_NETWORK,
        tools=_coverage_toolset(),
        instruction="briefing",
        model=scripted_model(()),
    )
    assert not [tool for tool in other.tools if isinstance(tool, BaseTool)], (
        "only Provider Matching has sub-agents (§3)"
    )


def _matching_toolset() -> Any:
    box = ToolBox(
        role=AgentRole.PROVIDER_MATCHING,
        api=StubApi({"/directory/search": _DIRECTORY_ANSWER}),
        policy=AllowAllPolicy(),
    )
    return box.start(
        budget=TurnBudget(BudgetCaps(), role=AgentRole.PROVIDER_MATCHING),
        envelope=envelope(workflow_state="PROVIDER_MATCHING"),
        payload=payload_for(AgentRole.PROVIDER_MATCHING),
        workflow_state="PROVIDER_MATCHING",
    )


def _coverage_toolset() -> Any:
    box = ToolBox(role=AgentRole.COVERAGE_NETWORK, api=StubApi(), policy=AllowAllPolicy())
    return box.start(
        budget=TurnBudget(BudgetCaps(), role=AgentRole.COVERAGE_NETWORK),
        envelope=envelope(workflow_state="COVERAGE_CHECK"),
        workflow_state="COVERAGE_CHECK",
    )


#: One directory answer, with provenance: the researcher's call must cite something.
_DIRECTORY_ANSWER: Mapping[str, Any] = {
    "candidates": [{"provider_id": PROVIDER_ID, "display_name": "Elena Marquez"}],
    "result_kind": "candidates_found",
    "evidence_id": EVIDENCE_ID,
}


@pytest.mark.asyncio
async def test_a_matching_turn_runs_both_sub_agents_and_keeps_what_they_wrote() -> None:
    """The composition ADR-0033 records, through a real `Runner`: researcher, analyst, decision."""
    from agents_fixtures import handler_for, run_turn

    from referraleasy_contracts.generated.common import ResultOutcome

    pool: dict[str, Any] = {
        "candidates": [],
        "strategy": "directory_search",
        "filters_applied": ["specialty=cardiology"],
    }
    analysis: dict[str, Any] = {"candidates": [], "alternatives": []}
    parent_answer: dict[str, Any] = {
        "candidates": [],
        "alternatives": [],
        "constraint_change": None,
        "mandatory_conditions": {
            "specialty_identified": True,
            "approval_present": False,
            "slot_valid": False,
        },
        "decision": {
            "action": "provider_matching:find_candidates",
            "reason_code": "no_candidate_matched",
            "evidence_ids": [EVIDENCE_ID],
            "missing_fields": [],
            "next_allowed_actions": ["provider_matching:find_candidates"],
        },
    }
    api = StubApi({"/directory/search": dict(_DIRECTORY_ANSWER)})
    sessions = None
    handler = handler_for(
        AgentRole.PROVIDER_MATCHING,
        model=scripted_model(
            [
                ToolCallStep(tool="candidate_researcher", args={"request": "build the pool"}),
                ToolCallStep(tool="constraint_analyst", args={"request": "review the pool"}),
                TextStep(text=json.dumps(parent_answer)),
            ]
        ),
        api=api,
        policy=AllowAllPolicy(),
        sub_agent_models={
            subagents.CANDIDATE_RESEARCHER: scripted_model(
                [
                    ToolCallStep(
                        tool="search_directory", args={"query": "spanish-speaking cardiologists"}
                    ),
                    TextStep(text=json.dumps(pool)),
                ]
            ),
            subagents.CONSTRAINT_ANALYST: scripted_model([TextStep(text=json.dumps(analysis))]),
        },
    )
    del sessions
    report = await run_turn(
        AgentRole.PROVIDER_MATCHING,
        script=[],
        workflow_state="PROVIDER_MATCHING",
        handler=handler,
        api=api,
    )

    assert report.result.outcome is ResultOutcome.no_match
    assert report.trace["tools"] == [
        {
            "tool": "search_directory",
            "timeout_ms": report.trace["tools"][0]["timeout_ms"],
            "attempt": report.trace["tools"][0]["attempt"],
            "evidence_ids": [EVIDENCE_ID],
        }
    ]
    sub_agent_state = report.trace["sub_agent_state"]
    assert sub_agent_state["candidate_pool"] == "present"
    assert sub_agent_state["constraint_analysis"] == "present", (
        "each sub-agent writes its own output_key into the session the parent is using"
    )
    state = await handler.service.sessions.get_session(
        app_name="agent-provider_matching",
        user_id=report.trace["session"]["user_id"],
        session_id=report.trace["session"]["session_id"],
    )
    assert state is not None
    assert json.loads(state.state["candidate_pool"]) == pool
    assert json.loads(state.state["constraint_analysis"]) == analysis
