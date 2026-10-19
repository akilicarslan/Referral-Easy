"""Directory candidates require evidence for each factual constraint."""

import json

import pytest
from agents_fixtures import AllowAllPolicy, StubApi, toolset_for

from referraleasy_agent_runtime import AgentRole
from referraleasy_agents.errors import UnknownDataError


def matching_tools(root):
    responses = {
        f"/{name}": {
            name: json.loads((root / "fixtures" / "directory" / f"{name}.json").read_text())
        }
        for name in ("providers", "hospitals", "locations")
    }
    responses["/evidence"] = {"records": json.loads((root / "fixtures/evidence.json").read_text())}
    return toolset_for(
        AgentRole.PROVIDER_MATCHING,
        api=StubApi(responses),
        policy=AllowAllPolicy(),
        workflow_state="PROVIDER_MATCHING",
    )[1]


ORIGIN = {"latitude": 41.8955, "longitude": -87.635}


@pytest.mark.asyncio
async def test_unknown_distance_cannot_satisfy_a_hard_radius(repo_root, monkeypatch):
    monkeypatch.setattr(
        "referraleasy_agents.tools.matching.hospital_distance", lambda *a, **k: None
    )
    baseline = await matching_tools(repo_root).invoke(
        "find_candidates", specialty="cardiology", origin=ORIGIN
    )
    assert baseline["candidates"]
    bounded = await matching_tools(repo_root).invoke(
        "find_candidates", specialty="cardiology", origin=ORIGIN, max_distance_miles=50
    )
    assert bounded["candidates"] == []


@pytest.mark.asyncio
async def test_accessibility_is_checked_at_the_selected_hospital(repo_root):
    answer = await matching_tools(repo_root).invoke(
        "find_candidates",
        specialty="cardiology",
        origin=ORIGIN,
        constraints=[{"field": "accessibility", "value": "hearing_loop"}],
    )
    hospitals = json.loads((repo_root / "fixtures/directory/hospitals.json").read_text())
    supported = {
        h["hospital_id"] for h in hospitals if "hearing_loop" in h["accessibility_features"]
    }
    assert answer["candidates"]
    assert {c["hospital_id"] for c in answer["candidates"]} <= supported


@pytest.mark.asyncio
async def test_a_string_cannot_change_the_new_patient_constraint(repo_root):
    with pytest.raises(UnknownDataError, match="requires a boolean"):
        await matching_tools(repo_root).invoke(
            "find_candidates",
            specialty="cardiology",
            origin=ORIGIN,
            constraints=[{"field": "accepts_new_patients", "value": "false"}],
        )
