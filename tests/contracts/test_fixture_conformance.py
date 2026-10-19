"""Fixture conformance: every fixture validates against the frozen schemas, and the eight demo
scenarios each have the data they need.

`packages/contracts/schemas/*.json` is the single source of truth (§1.1). Fixtures are the demo's
imported content, not a second contract, so they are validated *against* the schemas rather than
described alongside them: no field is added here that the schema does not declare.

The second half of this file is deliberately scenario-shaped rather than file-shaped. A fixture set
that validates perfectly can still fail to make Scenario 2 reproducible, so each of the eight
scenarios gets its own named test whose failure message says which scenario is unsupported.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from referraleasy_contracts.schemas import load_schema, validate_def

pytestmark = pytest.mark.contract

# .../tests/contracts/test_fixture_conformance.py -> [0]=contracts [1]=tests [2]=repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures"
FAULT_DIR = FIXTURES / "fault-scenarios"

#: Files that live under `fixtures/` but are not records of a schema definition.
MANIFEST_FILES: frozenset[str] = frozenset({"scenarios.json"})

#: Every `fault` type the mock API understands. Closed set: a new fault type is a decision.
FAULT_TYPES: frozenset[str] = frozenset(
    {
        "latency",
        "rate_limit_429",
        "service_unavailable_503",
        "malformed_schema",
        "empty_valid_result",
        "stale_slot_409",
        "timeout_before_commit",
        "timeout_after_commit",
        "duplicate_event",
        "stale_coverage_evidence",
        "notification_failure",
    }
)

#: The eight demo scenarios the fixture set exists to make reproducible.
SCENARIO_IDS: tuple[str, ...] = (
    "standard_booking",
    "multi_constraint_search",
    "network_conflict",
    "missing_document",
    "timeout_after_commit",
    "three_transient_errors",
    "no_specialist_found",
    "version_regression",
)

#: The fault type each fault file must declare, keyed by file stem. One file per fault type.
FAULT_TYPE_BY_FILE: dict[str, str] = {name: name for name in FAULT_TYPES}

#: Fridays in the fixture window. Availability is keyed by weekday, and these are the dates that
#: exercise it.
FRIDAY_DATES: frozenset[str] = frozenset(
    {"2026-10-09", "2026-10-16", "2026-10-23", "2026-10-30", "2026-11-06", "2026-11-13"}
)


@dataclass(frozen=True)
class FixtureSpec:
    """One fixture file, the schema definition its records implement, and how to reach the records."""

    key: str
    path: str
    schema: str
    definition: str
    id_field: str | None = None
    #: Path inside each top-level record to the records actually being described, for definitions
    #: that the schema nests (a Constraint lives inside PatientPreferences.constraints).
    nested: tuple[str, ...] = ()
    #: What actually has to be unique. Defaults to `id_field`. Overridden where a row is versioned:
    #: a superseded preference set keeps its workflow id on purpose.
    identity: tuple[str, ...] | None = None


FIXTURE_SPECS: tuple[FixtureSpec, ...] = (
    FixtureSpec("locations", "directory/locations.json", "directory", "Location", "location_id"),
    FixtureSpec("hospitals", "directory/hospitals.json", "directory", "Hospital", "hospital_id"),
    FixtureSpec("providers", "directory/providers.json", "directory", "Provider", "provider_id"),
    FixtureSpec("slots", "directory/slots.json", "directory", "Slot", "slot_id"),
    FixtureSpec(
        "availability_results",
        "directory/availability_results.json",
        "directory",
        "AvailabilityResult",
    ),
    FixtureSpec(
        "provider_candidates",
        "directory/provider_candidates.json",
        "directory",
        "ProviderCandidate",
    ),
    FixtureSpec("plans", "coverage/plans.json", "coverage", "InsurancePlan", "plan_id"),
    FixtureSpec(
        "network_records", "coverage/network_records.json", "coverage", "NetworkRecord", "record_id"
    ),
    FixtureSpec(
        "coverage_results",
        "coverage/coverage_results.json",
        "coverage",
        "CoverageResult",
        "coverage_id",
    ),
    FixtureSpec(
        "referral_requirements",
        "coverage/referral_requirements.json",
        "coverage",
        "ReferralRequirement",
        "requirement_id",
    ),
    FixtureSpec(
        "evidence_snapshots",
        "coverage/evidence_snapshots.json",
        "coverage",
        "CoverageEvidenceSnapshot",
        "snapshot_id",
    ),
    FixtureSpec("evidence", "evidence.json", "common", "Evidence", "evidence_id"),
    FixtureSpec(
        "patient_details",
        "patients/patient_details.json",
        "patient",
        "PatientDetails",
        "patient_details_id",
    ),
    # Patient identity is handed to agents as an opaque id; the same rows carry it.
    FixtureSpec(
        "patients", "patients/patient_details.json", "patient", "PatientDetails", "patient_id"
    ),
    FixtureSpec(
        "referral_intakes",
        "patients/referral_intake.json",
        "patient",
        "ReferralIntake",
        "referral_id",
    ),
    FixtureSpec(
        "preferences",
        "patients/preferences.json",
        "patient",
        "PatientPreferences",
        "workflow_id",
        identity=("patient_id", "version"),
    ),
    FixtureSpec(
        "constraints",
        "patients/preferences.json",
        "patient",
        "Constraint",
        "constraint_id",
        nested=("constraints",),
    ),
    FixtureSpec(
        "alternative_options",
        "patients/alternative_options.json",
        "patient",
        "AlternativeOption",
        "option_id",
    ),
    FixtureSpec(
        "change_options",
        "patients/constraint_changes.json",
        "patient",
        "AlternativeOption",
        "option_id",
        nested=("presented_options",),
    ),
    FixtureSpec(
        "constraint_changes", "patients/constraint_changes.json", "patient", "ConstraintChange"
    ),
    FixtureSpec("patient_choices", "patients/patient_choices.json", "patient", "PatientChoice"),
    FixtureSpec("missing_fields", "patients/missing_fields.json", "common", "MissingField"),
    FixtureSpec(
        "documents", "documents/documents.json", "documents", "DocumentFixture", "document_id"
    ),
    FixtureSpec("handoffs", "workflow/handoffs.json", "workflow", "Handoff", "handoff_id"),
    FixtureSpec(
        "human_queue_tickets",
        "workflow/human_queue_tickets.json",
        "workflow",
        "HumanQueueTicket",
        "ticket_id",
    ),
)

SPEC_BY_KEY: dict[str, FixtureSpec] = {spec.key: spec for spec in FIXTURE_SPECS}

#: Reference field name -> the fixture key its values must resolve to. Explicit on purpose: guessing
#: from name suffixes would quietly skip the fields that matter.
REFERENCE_FIELDS: dict[str, str] = {
    "evidence_id": "evidence",
    "evidence_ids": "evidence",
    "conflicting_evidence_ids": "evidence",
    "winning_evidence_id": "evidence",
    "invalidated_by": "evidence",
    "provenance": "evidence",
    "provider_id": "providers",
    "hospital_id": "hospitals",
    "hospital_ids": "hospitals",
    "accepted_plan_ids": "plans",
    "plan_id": "plans",
    "slot_id": "slots",
    "patient_id": "patients",
    "document_id": "documents",
    "document_ids": "documents",
    "location_id": "locations",
    "handoff_id": "handoffs",
    "workflow_id": "preferences",
    "constraint_id": "constraints",
    "superseded_by": "constraints",
    "relaxes_constraint_id": "constraints",
    "accepted_option_id": "alternative_options",
    "record_id": "network_records",
}


def _load(relative: str) -> Any:
    with (FIXTURES / relative).open(encoding="utf-8") as handle:
        return json.load(handle)


def _records(spec: FixtureSpec) -> list[dict[str, Any]]:
    """The records a spec describes, flattening one level of nesting where the schema nests them."""
    rows = _load(spec.path)
    if not spec.nested:
        return list(rows)
    collected: list[dict[str, Any]] = []
    for row in rows:
        for nested_key in spec.nested:
            collected.extend(row.get(nested_key) or [])
    return collected


def _ids(spec: FixtureSpec) -> set[str]:
    if spec.id_field is None:
        return set()
    values = []
    for record in _records(spec):
        value = record.get(spec.id_field)
        if value is None:
            continue
        values.append(value if isinstance(value, str) else str(value))
    return set(values)


def _all_json_fixture_files() -> list[str]:
    return sorted(
        str(path.relative_to(FIXTURES))
        for path in FIXTURES.rglob("*.json")
        if path.parent != FAULT_DIR and str(path.relative_to(FIXTURES)) not in MANIFEST_FILES
    )


def _haversine_miles(origin: dict[str, Any], target: dict[str, Any]) -> float:
    """Great-circle distance between two GeoPoints, so a distance limit can be checked, not assumed."""
    earth_radius_miles = 3958.8
    lat1, lon1 = math.radians(origin["latitude"]), math.radians(origin["longitude"])
    lat2, lon2 = math.radians(target["latitude"]), math.radians(target["longitude"])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * earth_radius_miles * math.asin(math.sqrt(a))


def _walk(node: Any) -> Any:
    """Yield every (key, value) pair in a fixture document, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


# ── the fixture set as a whole ──────────────────────────────────────────────────────────────


def test_every_fixture_file_is_registered() -> None:
    """A fixture file that no spec covers would never be validated."""
    registered = {spec.path for spec in FIXTURE_SPECS}
    unregistered = [path for path in _all_json_fixture_files() if path not in registered]
    assert not unregistered, f"fixture files not covered by FIXTURE_SPECS: {unregistered}"


@pytest.mark.parametrize("spec", FIXTURE_SPECS, ids=lambda spec: spec.key)
def test_definition_exists_in_the_frozen_schema(spec: FixtureSpec) -> None:
    """A typo in a definition name would make the validation below vacuous."""
    definitions = load_schema(spec.schema)["$defs"]
    assert spec.definition in definitions, (
        f"{spec.key}: {spec.definition!r} is not a $defs entry of {spec.schema}.json"
    )


@pytest.mark.parametrize("spec", FIXTURE_SPECS, ids=lambda spec: spec.key)
def test_fixture_validates_against_the_frozen_schema(spec: FixtureSpec) -> None:
    records = _records(spec)
    assert records, f"{spec.key}: {spec.path} describes no records"
    for index, record in enumerate(records):
        try:
            validate_def(spec.schema, spec.definition, record)
        except Exception as exc:
            raise AssertionError(
                f"{spec.key}[{index}] fails {spec.schema}.json#/$defs/{spec.definition}: {exc}"
            ) from exc


def test_fixture_ids_are_unique() -> None:
    """Identity, not "id": a superseded preference set repeats its workflow id by design, so the
    identity of a versioned row is the pair (patient, version), and nested records are scoped to
    their parent."""
    duplicates: dict[str, list[str]] = {}
    for spec in FIXTURE_SPECS:
        identity = spec.identity or ((spec.id_field,) if spec.id_field else ())
        if not identity:
            continue
        if spec.nested:
            groups = [
                [record for record in (parent.get(key) or [])]
                for parent in _load(spec.path)
                for key in spec.nested
            ]
        else:
            groups = [_records(spec)]
        for index, group in enumerate(groups):
            values = [tuple(record.get(field) for field in identity) for record in group]
            repeated = {value for value in values if values.count(value) > 1}
            if repeated:
                duplicates[f"{spec.key}[{index}]"] = sorted(str(value) for value in repeated)
    assert not duplicates, f"duplicate fixture identities: {duplicates}"


def test_every_synthetic_flag_is_set_where_the_schema_declares_one() -> None:
    """`const: true` on `synthetic` is the schemas' way of saying every row here is invented."""
    missing: list[str] = []
    for spec in FIXTURE_SPECS:
        properties = load_schema(spec.schema)["$defs"][spec.definition].get("properties", {})
        if "synthetic" not in properties:
            continue
        for index, record in enumerate(_records(spec)):
            if record.get("synthetic") is not True:
                missing.append(f"{spec.key}[{index}]")
    assert not missing, (
        f"records whose schema declares synthetic but which do not set it: {missing}"
    )


# ── references resolve inside the fixture set ───────────────────────────────────────────────


def test_every_reference_resolves_to_a_fixture_record() -> None:
    """A dangling id would make the demo fail at run time on data that looks complete."""
    known = {key: _ids(SPEC_BY_KEY[key]) for key in REFERENCE_FIELDS.values()}
    broken: list[str] = []
    for spec in FIXTURE_SPECS:
        for key, value in _walk(_load(spec.path)):
            target = REFERENCE_FIELDS.get(key)
            if target is None:
                continue
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if candidate is None or not isinstance(candidate, str):
                    continue
                if candidate not in known[target]:
                    broken.append(f"{spec.path}: {key}={candidate} is not a {target} id")
    assert not broken, "unresolved fixture references:\n" + "\n".join(sorted(set(broken)))


# ── documents, including the deliberate injection fixture ───────────────────────────────────


def test_document_fixtures_cover_all_three_kinds() -> None:
    kinds = {record["kind"] for record in _records(SPEC_BY_KEY["documents"])}
    assert kinds == {"referral_letter", "prior_report", "authorization_reference"}, kinds


def test_document_text_is_multi_line_and_marked_synthetic() -> None:
    for record in _records(SPEC_BY_KEY["documents"]):
        assert record["text"].count("\n") >= 8, f"{record['filename']} is not multi-line prose"
        assert "SYNTHETIC FIXTURE" in record["text"], f"{record['filename']} lacks its marker"


def test_injection_fixture_exists_and_is_flagged() -> None:
    flagged = [
        record
        for record in _records(SPEC_BY_KEY["documents"])
        if record["contains_injection_attempt"] is True
    ]
    assert flagged, "no document fixture is flagged contains_injection_attempt"
    assert len(flagged) == 1, f"expected exactly one flagged fixture, got {len(flagged)}"
    text = flagged[0]["text"].lower()
    assert "as an ai assistant" in text, "the flagged fixture does not address an assistant"
    assert "in_network_for_plan" in text, "the flagged fixture does not attempt the coverage claim"
    assert "already been verified" in text, "the flagged fixture does not claim prior verification"


def test_injection_fixture_is_contradicted_by_the_structured_coverage_record() -> None:
    """The injection is a demo artefact: the data it argues against is present and authoritative."""
    contradicted = [
        result
        for result in _records(SPEC_BY_KEY["coverage_results"])
        if result["conflict"] is True and result["winning_evidence_id"] is not None
    ]
    assert len(contradicted) >= 2, "expected both the provider-level and hospital-level conflicts"
    assert any(result["status"] == "unknown" for result in contradicted)
    assert any(
        result["status"] == "out_of_network" for result in _records(SPEC_BY_KEY["coverage_results"])
    )


# ── fault scenarios: shape the mock API consumes ────────────────────────────────────────────


def _fault_files() -> list[Path]:
    return sorted(FAULT_DIR.glob("*.json"))


def test_one_fault_file_per_fault_type() -> None:
    declared = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in _fault_files()}
    assert set(declared) == set(FAULT_TYPE_BY_FILE), (
        f"fault files {sorted(set(declared) ^ set(FAULT_TYPE_BY_FILE))} do not match the fault types"
    )
    for stem, document in declared.items():
        assert document["default_fault"] == stem, (
            f"{stem}.json declares default_fault={document['default_fault']!r}"
        )


@pytest.mark.parametrize("path", _fault_files(), ids=lambda path: path.stem)
def test_fault_scenario_has_the_shape_the_mock_api_consumes(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document) >= {"scenario_id", "description", "fault_seed", "default_fault", "steps"}
    assert document["scenario_id"] in SCENARIO_IDS, (
        f"{path.name}: scenario_id {document['scenario_id']!r} does not name one of the eight"
    )
    assert isinstance(document["fault_seed"], int) and not isinstance(
        document["fault_seed"], bool
    ), f"{path.name}: fault_seed must be an integer so a faulted run is replayable"
    assert document["description"].strip()
    assert document["steps"], f"{path.name}: no steps"
    for step in document["steps"]:
        assert set(step) >= {"match", "occurrence", "fault"}, step
        assert isinstance(step["occurrence"], int) and step["occurrence"] >= 1, step
        assert set(step["match"]) >= {"method", "path"}, step
        assert step["match"]["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE"}, step
        assert step["match"]["path"].startswith("/"), step
        assert step["fault"]["type"] in FAULT_TYPES, (
            f"{path.name}: fault type {step['fault']['type']!r} is outside the closed set"
        )


def test_every_fault_type_appears_in_the_fixture_set() -> None:
    seen = set()
    for path in _fault_files():
        document = json.loads(path.read_text(encoding="utf-8"))
        seen.add(document["default_fault"])
        seen.update(step["fault"]["type"] for step in document["steps"])
    assert seen == FAULT_TYPES, f"fault types never injected: {sorted(FAULT_TYPES - seen)}"


# ── the scenario manifest ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    key: str
    data: dict[str, Any] = field(default_factory=dict)


def _scenarios() -> list[dict[str, Any]]:
    return _load("scenarios.json")["scenarios"]


def _scenario(scenario_id: str) -> dict[str, Any]:
    matches = [entry for entry in _scenarios() if entry["scenario_id"] == scenario_id]
    assert len(matches) == 1, f"scenarios.json must describe {scenario_id!r} exactly once"
    return matches[0]


def test_manifest_describes_every_scenario_exactly_once() -> None:
    ids = [entry["scenario_id"] for entry in _scenarios()]
    assert sorted(ids) == sorted(SCENARIO_IDS), ids


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_scenario_references_resolve(scenario_id: str) -> None:
    entry = _scenario(scenario_id)
    assert entry["assertions"], f"{scenario_id}: no assertions recorded"
    for key, values in entry["references"].items():
        if key not in SPEC_BY_KEY:
            continue
        known = _ids(SPEC_BY_KEY[key])
        unresolved = sorted(value for value in values if value not in known)
        assert not unresolved, f"{scenario_id}: {key} ids not present in fixtures: {unresolved}"
    for filename in entry["fault_scenarios"]:
        assert (FAULT_DIR / filename).is_file(), f"{scenario_id}: missing fault file {filename}"
        document = json.loads((FAULT_DIR / filename).read_text(encoding="utf-8"))
        assert document["scenario_id"] == scenario_id, (
            f"{scenario_id}: {filename} is labelled {document['scenario_id']!r}"
        )


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_scenario_expected_result_kinds_are_present(scenario_id: str) -> None:
    expected = _scenario(scenario_id)["expected_result_kinds"]
    if not expected:
        pytest.skip(f"{scenario_id} has no availability-results dependency")
    available = {record["result_kind"] for record in _records(SPEC_BY_KEY["availability_results"])}
    missing = sorted(set(expected) - available)
    assert not missing, f"{scenario_id}: no availability fixture with result_kind in {missing}"


# ── one named assertion per scenario ────────────────────────────────────────────────────────


def test_scenario_1_standard_booking_is_supported() -> None:
    hospitals = {record["hospital_id"]: record for record in _records(SPEC_BY_KEY["hospitals"])}
    adapters = {record["adapter_type"] for record in hospitals.values()}
    assert adapters == {"instant_confirm", "review_required"}, (
        "scenario 1: the golden path needs both adapters, so CONFIRMED and PENDING_REVIEW are "
        f"both reachable; found {adapters}"
    )

    instant = [h for h in hospitals.values() if h["adapter_type"] == "instant_confirm"]
    review = [h for h in hospitals.values() if h["adapter_type"] == "review_required"]
    assert len(instant) >= 2 and len(review) >= 2, "scenario 1: need at least two of each adapter"
    assert len({h["timezone"] for h in hospitals.values()}) >= 2, (
        "scenario 1: hospitals must span at least two IANA zones"
    )

    slots = {record["slot_id"]: record for record in _records(SPEC_BY_KEY["slots"])}
    chosen = slots["40000000-0000-4000-8000-000000000001"]
    assert chosen["status"] == "available" and chosen["version"] >= 1, (
        "scenario 1: the approved slot must be available and versioned so approval can bind to it"
    )
    assert chosen["display_timezone"] == "America/Chicago"

    requests = {
        (record["hospital_id"], record["visit_type"]): record
        for record in _records(SPEC_BY_KEY["referral_requirements"])
    }
    hospital_one_first = requests[("20000000-0000-4000-8000-000000000001", "first_visit")]
    assert hospital_one_first["required_documents"] == ["referral_letter"]
    documents = _records(SPEC_BY_KEY["documents"])
    avery = "b0000000-0000-4000-8000-000000000001"
    assert any(
        record["patient_id"] == avery and record["kind"] == "referral_letter"
        for record in documents
    ), "scenario 1: the referral letter the chosen hospital requires is missing for this patient"

    choices = _records(SPEC_BY_KEY["patient_choices"])
    sequence = [record["choice_type"] for record in choices]
    assert sequence[:4] == ["hospital", "doctor", "slot", "final_approval"], (
        f"scenario 1: the four-step approval sequence is not recorded; found {sequence}"
    )
    assert choices[3]["confirmed_by_patient"] is True, (
        "scenario 1: a booking without explicit patient confirmation is not a booking"
    )


def test_scenario_2_multi_constraint_search_is_supported() -> None:
    specs = _records(SPEC_BY_KEY["preferences"])
    maria = "b0000000-0000-4000-8000-000000000003"
    first_version = min(
        (record for record in specs if record["patient_id"] == maria),
        key=lambda record: record["version"],
    )
    constraints = first_version["constraints"]
    assert constraints, "scenario 2: the all-hard preference set is missing"
    assert {constraint["kind"] for constraint in constraints} == {"hard"}, (
        "scenario 2: this patient's preferences must all be hard, so nothing may be relaxed silently"
    )
    by_field = {constraint["field"]: constraint for constraint in constraints}
    assert by_field["max_distance_miles"]["value"] == 15
    assert by_field["provider_language"]["value"] == "es"
    assert by_field["preferred_day"]["value"] == "friday"
    assert by_field["accepts_new_patients"]["value"] is True
    assert first_version["search_radius_miles"] == 15, "scenario 2: the radius starts un-widened"

    providers = _records(SPEC_BY_KEY["providers"])
    slots = _records(SPEC_BY_KEY["slots"])
    hospitals = {record["hospital_id"]: record for record in _records(SPEC_BY_KEY["hospitals"])}
    locations = {record["location_id"]: record for record in _records(SPEC_BY_KEY["locations"])}

    spanish = [
        provider
        for provider in providers
        if any(skill["language"] == "es" for skill in provider["languages"])
    ]
    assert len(spanish) >= 3, "scenario 2: the language filter needs Spanish-speaking providers"

    origin = first_version["search_origin"]
    assert origin, "scenario 2: the search origin the radius is measured from is missing"

    def distance_miles(provider: dict[str, Any]) -> float:
        return min(
            _haversine_miles(origin, locations[hospitals[hospital_id]["location_id"]]["geo"])
            for hospital_id in provider["hospital_ids"]
        )

    def fridays(provider_id: str, visit_type: str = "first_visit") -> bool:
        return any(
            slot["provider_id"] == provider_id
            and slot["status"] == "available"
            and slot["visit_type"] == visit_type
            and (slot["start_at"].split("T")[0] in FRIDAY_DATES)
            for slot in slots
        )

    # Everything the patient asked for except the day, inside the radius: these must exist, or the
    # search fails for the wrong reason (nothing matched at all) and the scenario loses its point.
    in_radius = [
        provider
        for provider in spanish
        if provider["specialty"] == "cardiology"
        and provider["accepts_new_patients"]
        and distance_miles(provider) <= 15
    ]
    assert in_radius, (
        "scenario 2: no Spanish-speaking cardiologist accepting new patients within 15 miles"
    )
    assert not any(fridays(provider["provider_id"]) for provider in in_radius), (
        "scenario 2: a Spanish-speaking cardiologist inside the radius has a Friday first-visit "
        "slot, so the search would simply succeed and the scenario would not reproduce"
    )

    # The options that would work if a hard constraint were relaxed: these must be real and reachable
    # only by relaxing, so the workflow has something honest to offer.
    beyond_radius = [
        provider
        for provider in spanish
        if provider["specialty"] == "cardiology"
        and provider["accepts_new_patients"]
        and distance_miles(provider) > 15
    ]
    assert any(fridays(provider["provider_id"]) for provider in beyond_radius), (
        "scenario 2: nothing satisfies the relaxed radius, so there is no honest alternative to offer"
    )

    # Each remaining constraint must be defeated by a different provider, so no single relaxation
    # unlocks the search. Otherwise the demo proves the wrong thing.
    friday_first_visits = [
        provider
        for provider in providers
        if provider["specialty"] == "cardiology" and fridays(provider["provider_id"])
    ]
    assert any(
        not any(skill["language"] == "es" for skill in provider["languages"])
        and distance_miles(provider) <= 15
        for provider in friday_first_visits
    ), (
        "scenario 2: no in-radius Friday provider fails on language alone, so the language "
        "constraint is not what the failing search turns on"
    )
    closed_cardiology = [
        provider
        for provider in providers
        if provider["specialty"] == "cardiology" and not provider["accepts_new_patients"]
    ]
    assert closed_cardiology, "scenario 2: the closed-panel trap is missing"
    assert all(
        not fridays(provider["provider_id"], "first_visit") for provider in closed_cardiology
    ), (
        "scenario 2: a closed cardiology panel offers a booking-able first visit, which would let "
        "the workflow book a provider the patient cannot see"
    )

    candidates = _records(SPEC_BY_KEY["provider_candidates"])
    assert any(
        fit["satisfied"] is None for candidate in candidates for fit in candidate["constraint_fit"]
    ), "scenario 2: unknown is never exercised as unknown"

    options = _records(SPEC_BY_KEY["alternative_options"])
    assert len(options) >= 4, "scenario 2: at least four evidenced alternatives are expected"
    bad = [option["option_id"] for option in options if option["relaxes_constraint_id"] is not None]
    assert not bad, (
        "scenario 2: every option addresses a hard constraint, so relaxes_constraint_id must be "
        f"null; these are not: {bad}"
    )
    for option in options:
        assert option["evidence_ids"], f"scenario 2: option {option['option_id']} has no evidence"
        assert option["candidate_ids"], (
            f"scenario 2: option {option['option_id']} has no candidates"
        )
    assert any(option["kind"] == "wider_radius" for option in options), (
        "scenario 2: the distance alternatives that break the limit must be shown, not applied"
    )

    changes = _records(SPEC_BY_KEY["constraint_changes"])
    for change in changes:
        assert change["presented_options"], (
            "scenario 2: a preference moved without the alternatives being offered first"
        )
    distance_change = [
        change
        for change in changes
        if change["constraint_id"] == "e0000000-0000-4000-8000-000000000002"
    ]
    assert not distance_change, "scenario 2: the hard 15 mile limit was moved"


def test_scenario_3_network_conflict_is_supported() -> None:
    results = {
        record["coverage_id"]: record for record in _records(SPEC_BY_KEY["coverage_results"])
    }
    dual = results["80000000-0000-4000-8000-000000000002"]
    assert dual["accepts_insurance"] is True, (
        "scenario 3: this case is the 'accepts in general' half"
    )
    assert dual["in_network_for_plan"] is None, "scenario 3: the plan-level answer must be unknown"
    assert dual["status"] == "unknown", "scenario 3: unknown must not be rounded to a verdict"
    assert dual["conflict"] is True and len(dual["conflicting_evidence_ids"]) == 2
    assert dual["winning_evidence_id"] in dual["conflicting_evidence_ids"]
    assert (
        dual["freshness"]["state"] in {"fresh", "stale", "unknown"}
        and dual["freshness"]["age_seconds"] >= 0
    ), "scenario 3: unknown is shown with its freshness"
    assert dual["disclaimer_key"] == "coverage_not_a_guarantee"

    records = {record["record_id"]: record for record in _records(SPEC_BY_KEY["network_records"])}
    stale = records["70000000-0000-4000-8000-000000000002"]
    current = records["70000000-0000-4000-8000-000000000003"]
    assert stale["status"] == "in_network" and stale["effective_to"] is not None
    assert current["status"] == "unknown" and current["effective_from"] > stale["effective_to"], (
        "scenario 3: the contradiction must be date-explainable"
    )
    assert any(record["status"] == "out_of_network" for record in records.values()), (
        "scenario 3: the out-of-network shape must also exist"
    )

    blurb = results["80000000-0000-4000-8000-000000000009"]
    assert blurb["conflict"] is True and blurb["provider_id"] is None
    evidence = {record["evidence_id"]: record for record in _records(SPEC_BY_KEY["evidence"])}
    stale_claim = evidence[blurb["conflicting_evidence_ids"][0]]
    winning = evidence[blurb["winning_evidence_id"]]
    assert stale_claim["source"] == "rag_directory" and stale_claim["authoritative"] is False
    assert winning["source"] == "provider_directory" and winning["authoritative"] is True, (
        "scenario 3: the structured record must win over the stale text"
    )
    hospital = next(
        record
        for record in _records(SPEC_BY_KEY["hospitals"])
        if record["hospital_id"] == blurb["hospital_id"]
    )
    assert hospital["directory_blurb"] and any(
        plan not in hospital["accepted_plan_ids"]
        for plan in ("60000000-0000-4000-8000-000000000002",)
    ), "scenario 3: the stale blurb does not actually contradict the structured plan list"

    injected = [
        record
        for record in _records(SPEC_BY_KEY["documents"])
        if record["contains_injection_attempt"]
    ]
    assert injected and injected[0]["patient_id"] == "b0000000-0000-4000-8000-000000000005", (
        "scenario 3: the injecting document must belong to the conflicting-coverage patient"
    )


def test_scenario_4_missing_document_is_supported() -> None:
    patient = "b0000000-0000-4000-8000-000000000002"
    intake = next(
        record
        for record in _records(SPEC_BY_KEY["referral_intakes"])
        if record["patient_id"] == patient
    )
    documents = {record["document_id"]: record for record in _records(SPEC_BY_KEY["documents"])}
    attached = [documents[document_id] for document_id in intake["document_ids"]]
    assert attached, "scenario 4: the referral letter itself must be present"
    assert not any(record["kind"] == "prior_report" for record in attached), (
        "scenario 4: the prior report must be genuinely absent, not merely unreadable"
    )

    late = [
        record
        for record in documents.values()
        if record["patient_id"] == patient and record["kind"] == "prior_report"
    ]
    assert late, "scenario 4: no synthetic prior report exists to attach when it arrives"
    assert late[0]["issued_at"] > min(record["issued_at"] for record in attached), (
        "scenario 4: the report must arrive after the referral it completes"
    )

    requirement = next(
        record
        for record in _records(SPEC_BY_KEY["referral_requirements"])
        if record["requirement_id"] == "90000000-0000-4000-8000-000000000003"
    )
    assert set(requirement["required_documents"]) == {"referral_letter", "prior_report"}
    assert requirement["requires_prior_auth_reference"] is True
    assert requirement["max_document_age_days"] == 365

    fields = _records(SPEC_BY_KEY["missing_fields"])
    assert fields, "scenario 4: the contradictory referral data is not itemised"
    assert {field["reason_code"] for field in fields} >= {"document_missing", "ambiguous_input"}, (
        "scenario 4: both a missing document and an ambiguity must be represented"
    )
    assert any(field["mandatory"] for field in fields)

    ticket = next(
        record
        for record in _records(SPEC_BY_KEY["human_queue_tickets"])
        if record["ticket_id"] == "f2000000-0000-4000-8000-000000000003"
    )
    assert ticket["return_state"] == "READINESS_CHECK", "scenario 4: the resume point is readiness"
    assert ticket["final_approval_still_required"] is True


def test_scenario_5_timeout_after_commit_is_supported() -> None:
    after = json.loads((FAULT_DIR / "timeout_after_commit.json").read_text(encoding="utf-8"))
    step = after["steps"][0]
    assert step["fault"]["type"] == "timeout_after_commit"
    assert step["fault"]["delay_ms"] >= 1000, (
        "scenario 5: the response must be lost, not merely slow, so the delay exceeds any deadline"
    )
    assert step["match"]["method"] == "POST" and step["match"]["path"] == "/appointments"
    assert step["occurrence"] == 1, (
        "scenario 5: only the first attempt may be lost; a second attempt is the bug being prevented"
    )

    before = json.loads((FAULT_DIR / "timeout_before_commit.json").read_text(encoding="utf-8"))
    assert before["steps"][0]["fault"]["type"] == "timeout_before_commit"
    assert after["fault_seed"] != before["fault_seed"] or (
        after["default_fault"] != before["default_fault"]
    ), "scenario 5: the committed and uncommitted cases must be distinguishable"

    duplicate = json.loads((FAULT_DIR / "duplicate_event.json").read_text(encoding="utf-8"))
    assert duplicate["default_fault"] == "duplicate_event"
    assert any(step["fault"].get("deliveries", 1) > 1 for step in duplicate["steps"]), (
        "scenario 5: the duplicate delivery is not actually duplicated"
    )

    handoff = next(
        record
        for record in _records(SPEC_BY_KEY["handoffs"])
        if record["handoff_id"] == "f1000000-0000-4000-8000-000000000002"
    )
    assert handoff["reason_class"] == "prolonged_unknown_booking"
    assert (
        handoff["status"] in {"queued", "claimed", "resolved"} and handoff["status"] != "resolved"
    ), "scenario 5: an unknown booking outcome must not be marked resolved by the fixture"
    ticket = next(
        record
        for record in _records(SPEC_BY_KEY["human_queue_tickets"])
        if record["handoff_id"] == handoff["handoff_id"]
    )
    assert ticket["return_state"] == "BOOKING_UNVERIFIED"
    assert ticket["final_approval_still_required"] is True
    assert ticket["added_alternatives"] == [], "scenario 5: no alternative may be invented for this"


def test_scenario_6_three_transient_errors_is_supported() -> None:
    transient = []
    for filename in ("latency.json", "rate_limit_429.json", "service_unavailable_503.json"):
        document = json.loads((FAULT_DIR / filename).read_text(encoding="utf-8"))
        assert document["scenario_id"] == "three_transient_errors", filename
        transient.append(document["default_fault"])
    assert sorted(transient) == ["latency", "rate_limit_429", "service_unavailable_503"], transient
    assert (
        len(
            {
                document["fault_seed"]
                for document in (
                    json.loads((FAULT_DIR / name).read_text(encoding="utf-8"))
                    for name in (
                        "latency.json",
                        "rate_limit_429.json",
                        "service_unavailable_503.json",
                    )
                )
            }
        )
        == 3
    ), "scenario 6: each transient fault needs its own seed"

    malformed = json.loads((FAULT_DIR / "malformed_schema.json").read_text(encoding="utf-8"))
    fault = malformed["steps"][0]["fault"]
    assert fault["body_is_valid_json"] is True and fault["http_status"] == 200, (
        "scenario 6: malformed means 'valid JSON, wrong contract', not 'unparseable'"
    )
    assert fault["missing_fields"], "scenario 6: the malformed response omits nothing"

    kinds = {record["result_kind"] for record in _records(SPEC_BY_KEY["availability_results"])}
    assert "upstream_error" in kinds, (
        "scenario 6: without an upstream_error fixture a fault can be reported as 'no specialist'"
    )
    assert "no_slots_for_constraints" in kinds, (
        "scenario 6: the genuine empty result must exist beside the fault so they are distinguishable"
    )
    upstream = next(
        record
        for record in _records(SPEC_BY_KEY["availability_results"])
        if record["result_kind"] == "upstream_error"
    )
    assert upstream["partial"] is True and upstream["slots"] == []
    evidence = {record["evidence_id"]: record for record in _records(SPEC_BY_KEY["evidence"])}
    assert evidence[upstream["evidence_id"]]["authoritative"] is False, (
        "scenario 6: a failed call is not authoritative evidence"
    )

    outage = next(
        record
        for record in _records(SPEC_BY_KEY["handoffs"])
        if record["reason_class"] == "technical_outage"
    )
    assert outage["status"] == "withdrawn", (
        "scenario 6: recovery succeeded, so the outage handoff must be withdrawn"
    )


def test_scenario_7_no_specialist_found_is_supported() -> None:
    patient = "b0000000-0000-4000-8000-000000000006"
    intake = next(
        record
        for record in _records(SPEC_BY_KEY["referral_intakes"])
        if record["patient_id"] == patient
    )
    requested = intake["specialty"]
    specialties = {record["specialty"] for record in _records(SPEC_BY_KEY["providers"])}
    assert requested not in specialties, (
        f"scenario 7: a provider already lists {requested!r}, so the directory is not empty for it"
    )
    assert all(
        requested not in provider.get("subspecialties", [])
        for provider in _records(SPEC_BY_KEY["providers"])
    ), f"scenario 7: a provider lists {requested!r} as a subspecialty"

    handoff = next(
        record
        for record in _records(SPEC_BY_KEY["handoffs"])
        if record["workflow_id"] == "f0000000-0000-4000-8000-000000000006"
    )
    assert handoff["reason_class"] == "no_match" and handoff["status"] == "claimed"
    assert handoff["patient_visible_summary"].strip()

    ticket = next(
        record
        for record in _records(SPEC_BY_KEY["human_queue_tickets"])
        if record["handoff_id"] == handoff["handoff_id"]
    )
    assert (
        ticket["status"] == "claimed" and ticket["claimed_by"] and ticket["claim_generation"] >= 1
    )
    assert ticket["final_approval_still_required"] is True, (
        "scenario 7: a safe return must not skip the patient's final approval"
    )
    assert ticket["return_state"] in set(load_schema("common")["$defs"]["WorkflowState"]["enum"]), (
        "scenario 7: the return state must be a real workflow state"
    )

    option = next(
        record
        for record in _records(SPEC_BY_KEY["alternative_options"])
        if record["option_id"] == "a1000000-0000-4000-8000-000000000005"
    )
    assert (
        option["kind"] == "human_handoff" and option["candidate_ids"] and option["evidence_ids"]
    ), "scenario 7: the handoff offered must name a candidate and carry evidence"

    search_evidence = next(
        record
        for record in _records(SPEC_BY_KEY["evidence"])
        if record["evidence_id"] == option["evidence_ids"][0]
    )
    assert "functional_neurosurgery" in search_evidence["source_id"], (
        "scenario 7: the evidence must be the search that came back empty"
    )


def test_scenario_8_version_regression_is_supported() -> None:
    entry = _scenario("version_regression")
    pinned = entry["pinned_inputs"]
    assert pinned["fault_seed"] == 42, "scenario 8: the pinned seed must be concrete"
    assert pinned["patient_id"] in _ids(SPEC_BY_KEY["patients"])
    assert pinned["hospital_id"] in _ids(SPEC_BY_KEY["hospitals"])
    assert pinned["provider_id"] in _ids(SPEC_BY_KEY["providers"])
    assert pinned["plan_id"] in _ids(SPEC_BY_KEY["plans"])

    hospitals = {record["hospital_id"]: record for record in _records(SPEC_BY_KEY["hospitals"])}
    providers = {record["provider_id"]: record for record in _records(SPEC_BY_KEY["providers"])}
    for pin in entry["knowledge_version_pins"]:
        record = hospitals.get(pin["record"]) or providers.get(pin["record"])
        assert record is not None, (
            f"scenario 8: knowledge version pinned to unknown {pin['record']}"
        )
        assert record["knowledge_version"] == pin["knowledge_version"], (
            f"scenario 8: {pin['record']} knowledge_version drifted from the pin"
        )

    assert entry["non_fixture_dependencies"], (
        "scenario 8: the agent description change lives outside fixtures and must be declared"
    )
    for path in _fault_files():
        document = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(document["fault_seed"], int), (
            f"scenario 8: {path.name} has no integer seed, so a faulted run is not replayable"
        )
