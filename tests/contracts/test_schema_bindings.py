"""The frozen JSON Schemas and the generated Pydantic bindings must not disagree.

`packages/contracts/schemas/*.json` is the single source of truth (§1.1); the Pydantic models are a
projection of it. These tests assert the projection is faithful — and, where the generator cannot be
faithful, that the gap is *recorded and compensated* rather than discovered later in production.
"""

from __future__ import annotations

import importlib
import re
from enum import Enum
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from referraleasy_contracts.constraints import ContractViolation, validate_result_envelope
from referraleasy_contracts.generated.booking import BookingRequest
from referraleasy_contracts.generated.common import BookingState, ConstraintKind
from referraleasy_contracts.generated.coverage import CoverageResult
from referraleasy_contracts.generated.task import ResultEnvelope
from referraleasy_contracts.schemas import (
    SCHEMA_DIR,
    SCHEMA_FILES,
    load_schema,
    load_schemas,
    validate_def,
)

pytestmark = pytest.mark.contract

UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
UUID_C = "33333333-3333-4333-8333-333333333333"


# ── the schema source itself ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", SCHEMA_FILES)
def test_schema_is_a_valid_draft_2020_12_schema(name: str) -> None:
    Draft202012Validator.check_schema(load_schema(name))


def test_all_cross_file_refs_resolve() -> None:
    """A ref that does not resolve would silently disable validation for that field."""
    defs = {name: set(load_schema(name).get("$defs", {})) for name in SCHEMA_FILES}
    unresolved: list[str] = []

    def walk(node: Any, path: str, fname: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    target, _, frag = value.partition("#")
                    # Refs are written as "common.json#/..." while `defs` is keyed by stem.
                    target = (target or fname).removesuffix(".json")
                    match = re.fullmatch(r"/\$defs/(.+)", frag)
                    if not match or target not in defs or match.group(1) not in defs[target]:
                        unresolved.append(f"{fname}{path}: {value}")
                elif isinstance(value, (dict, list)):
                    walk(value, f"{path}/{key}", fname)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]", fname)

    for name in SCHEMA_FILES:
        walk(load_schema(name), "", name)

    assert unresolved == [], f"unresolved $refs: {unresolved}"


def test_no_reserved_keyword_appears_inside_properties() -> None:
    """Regression: `$comment` inside `properties` makes it look like a property named `$comment`.

    Property values must be schemas, and a string is not one — so the schema is invalid and every
    code generator chokes. This bit us once; it should not bite again.
    """
    offenders: list[str] = []

    def walk(node: Any, path: str, fname: str) -> None:
        if isinstance(node, dict):
            for container in ("properties", "$defs"):
                sub = node.get(container)
                if isinstance(sub, dict):
                    offenders.extend(
                        f"{fname}{path}/{container}/{key}" for key in sub if key.startswith("$")
                    )
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    walk(value, f"{path}/{key}", fname)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]", fname)

    for name in SCHEMA_FILES:
        walk(load_schema(name), "", name)

    assert offenders == [], f"reserved keys used as names: {offenders}"


# ── schema <-> Pydantic agreement ───────────────────────────────────────────────────────────


def _model_for(schema_name: str, def_name: str) -> type[BaseModel]:
    module = importlib.import_module(f"referraleasy_contracts.generated.{schema_name}")
    model = getattr(module, def_name, None)
    if model is None or not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise AssertionError(
            f"{schema_name}.{def_name} has no generated model — "
            f"run `pnpm gen:contracts` and check the definition name."
        )
    return model


def _schema_accepts(schema_name: str, def_name: str, instance: Any) -> bool:
    try:
        validate_def(schema_name, def_name, instance)
    except Exception:
        return False
    return True


def _pydantic_accepts(schema_name: str, def_name: str, instance: Any) -> bool:
    try:
        _model_for(schema_name, def_name).model_validate(instance)
    except ValidationError:
        return False
    return True


VALID_COVERAGE: dict[str, Any] = {
    "coverage_id": UUID_A,
    "plan_id": UUID_B,
    "accepts_insurance": True,
    # "Accepts insurance, but this plan's network record is unknown" — Scenario 3 (§20).
    "in_network_for_plan": None,
    "status": "unknown",
    "referral_required": None,
    "prior_auth_required": None,
    "freshness": {"state": "stale", "age_seconds": 86400},
    "evidence_ids": [],
    "synthetic": True,
}

VALID_CONSTRAINT: dict[str, Any] = {
    "constraint_id": UUID_A,
    "field": "max_distance_miles",
    "kind": "hard",
    "value": 15,
    "source": "patient_stated",
    "recorded_at": "2026-10-14T12:00:00+00:00",
}

CORPUS: list[tuple[str, str, dict[str, Any], bool, str]] = [
    # (schema, def, instance, should_validate, why)
    ("coverage", "CoverageResult", VALID_COVERAGE, True, "valid, with unknown network"),
    (
        "coverage",
        "CoverageResult",
        {**VALID_COVERAGE, "in_network_for_plan": False},
        True,
        "explicit out-of-network is valid and distinct from unknown",
    ),
    (
        "coverage",
        "CoverageResult",
        {k: v for k, v in VALID_COVERAGE.items() if k != "in_network_for_plan"},
        False,
        "required field omitted",
    ),
    (
        "coverage",
        "CoverageResult",
        {**VALID_COVERAGE, "synthetic": False},
        False,
        "synthetic is const true (§1: all data is synthetic)",
    ),
    (
        "coverage",
        "CoverageResult",
        {**VALID_COVERAGE, "unexpected_field": 1},
        False,
        "additionalProperties is false",
    ),
    ("patient", "Constraint", VALID_CONSTRAINT, True, "hard distance constraint"),
    (
        "patient",
        "Constraint",
        {**VALID_CONSTRAINT, "kind": "mandatory"},
        False,
        "kind is hard|soft only",
    ),
    (
        "patient",
        "Constraint",
        {k: v for k, v in VALID_CONSTRAINT.items() if k != "recorded_at"},
        False,
        "recorded_at is required",
    ),
]


@pytest.mark.parametrize(
    ("schema_name", "def_name", "instance", "should_validate", "why"),
    CORPUS,
    ids=[f"{c[0]}.{c[1]}-{'ok' if c[3] else 'bad'}-{c[4]}" for c in CORPUS],
)
def test_schema_and_pydantic_agree(
    schema_name: str, def_name: str, instance: dict[str, Any], should_validate: bool, why: str
) -> None:
    by_schema = _schema_accepts(schema_name, def_name, instance)
    by_pydantic = _pydantic_accepts(schema_name, def_name, instance)

    assert by_schema is should_validate, f"JSON Schema disagreed: {why}"
    assert by_pydantic is by_schema, (
        f"bindings drifted from the frozen schema for {schema_name}.{def_name} ({why}): "
        f"jsonschema={by_schema}, pydantic={by_pydantic}"
    )


def test_bindings_cover_every_definition() -> None:
    """Every `$def` must be bound to something — a model for objects, an enum for enums."""
    unbound: list[str] = []
    for name in SCHEMA_FILES:
        module = importlib.import_module(f"referraleasy_contracts.generated.{name}")
        for def_name, definition in load_schema(name).get("$defs", {}).items():
            candidate = getattr(module, def_name, None)
            is_model = isinstance(candidate, type) and issubclass(candidate, BaseModel)
            is_enum = isinstance(candidate, type) and issubclass(candidate, Enum)
            if not (is_model or is_enum):
                unbound.append(f"{name}.{def_name} ({definition.get('type', '?')})")
    assert unbound == [], f"definitions with no binding: {unbound}"


# ── invariant-carrying behaviours ───────────────────────────────────────────────────────────


def test_pending_review_survives_a_round_trip_as_pending_review() -> None:
    """§2.5: `PENDING_REVIEW` is never silently promoted to `CONFIRMED`.

    A parser that coerced unknown values, or a schema that dropped a member, would let a
    not-yet-confirmed request reach the patient as a confirmed appointment.
    """
    for state in BookingState:
        assert BookingState(state.value) is state

    request = BookingRequest.model_validate(
        {
            "booking_request_id": UUID_A,
            "workflow_id": UUID_B,
            "command_id": UUID_C,
            "idempotency_key": "idem-key-0001",
            "payload_hash": "a" * 64,
            "payload": {
                "patient_id": UUID_A,
                "provider_id": UUID_B,
                "hospital_id": UUID_C,
                "slot_id": UUID_A,
                "visit_type": "first_visit",
            },
            "state": "PENDING_REVIEW",
            "adapter_type": "review_required",
            "attempt_count": 1,
            "created_at": "2026-10-14T12:00:00+00:00",
            "updated_at": "2026-10-14T12:00:00+00:00",
        }
    )
    assert request.state is BookingState.PENDING_REVIEW
    # Survives a full serialize/parse cycle without being promoted to CONFIRMED.
    reparsed = BookingRequest.model_validate_json(request.model_dump_json())
    assert reparsed.state is BookingState.PENDING_REVIEW
    # Compared by value, not identity: the point is the wire form, and an identity check between
    # two distinct enum members is a type error the checker rightly rejects.
    assert reparsed.state.value == "PENDING_REVIEW"


def test_unknown_coverage_is_null_and_never_false() -> None:
    """§2.2: `unknown` must be representable. `False` would assert out-of-network wrongly."""
    unknown = CoverageResult.model_validate(VALID_COVERAGE)
    assert unknown.in_network_for_plan is None
    assert unknown.accepts_insurance is True
    assert unknown.referral_required is None
    assert unknown.status.value == "unknown"

    out_of_network = CoverageResult.model_validate(
        {**VALID_COVERAGE, "in_network_for_plan": False, "status": "out_of_network"}
    )
    assert out_of_network.in_network_for_plan is False
    assert out_of_network.in_network_for_plan is not None


def test_constraint_kind_has_exactly_hard_and_soft() -> None:
    """A third kind would make "never relax a hard constraint" unenforceable."""
    assert set(ConstraintKind) == {ConstraintKind.hard, ConstraintKind.soft}


# ── recorded divergences between the two layers ─────────────────────────────────────────────


def test_result_envelope_cross_field_rule_is_a_recorded_divergence() -> None:
    """`allOf` + `if`/`then` is not translated into Pydantic validators.

    The schema rejects an error outcome with no `error`; the generated model alone accepts it. That
    gap is closed by `validate_result_envelope`, not by weakening the schema.
    """
    envelope = {
        "task_id": UUID_A,
        "workflow_id": UUID_B,
        "command_id": UUID_C,
        "outcome": "terminal_error",
        "a2a_state": "failed",
        "release_manifest_id": "rel-20261014-abc1234",
        "synthetic": True,
    }

    assert _schema_accepts("task", "ResultEnvelope", envelope) is False, (
        "the schema should reject an error outcome with no error object"
    )
    # Documented divergence: the bindings alone do not catch it.
    assert _pydantic_accepts("task", "ResultEnvelope", envelope) is True

    # ...and the compensating check does.
    model = ResultEnvelope.model_validate(envelope)
    with pytest.raises(ContractViolation, match="requires an `error` object"):
        validate_result_envelope(model)


def test_valid_empty_result_is_not_an_error() -> None:
    """§8: `no_match` is a successful call whose answer is "nothing matched"."""
    envelope = {
        "task_id": UUID_A,
        "workflow_id": UUID_B,
        "command_id": UUID_C,
        "outcome": "no_match",
        "a2a_state": "completed",
        "release_manifest_id": "rel-20261014-abc1234",
        "synthetic": True,
    }
    validate_result_envelope(ResultEnvelope.model_validate(envelope))


def test_pydantic_coerces_where_the_schema_does_not() -> None:
    """A second, recorded divergence: pydantic coerces, JSON Schema does not.

    Consequence for callers: validate untrusted input with the JSON Schema (via `validate_def`),
    not by constructing a model — model construction is a convenience, not the contract.
    """
    coerced = {
        **VALID_COVERAGE,
        # A string where the schema demands an integer.
        "freshness": {"state": "fresh", "age_seconds": "0"},
    }
    assert _schema_accepts("coverage", "CoverageResult", coerced) is False
    assert _pydantic_accepts("coverage", "CoverageResult", coerced) is True
    assert CoverageResult.model_validate(coerced).freshness.age_seconds == 0


def test_format_is_enforced_not_treated_as_an_annotation() -> None:
    """`format: uuid` must actually be checked.

    Without a format checker, `format` is a pure annotation that validates nothing, so a malformed
    id sails through a contract test and fails at a service boundary instead. This pins that the
    checks are live, and that the structural-only opt-out still exists for callers that want it.
    """
    malformed = {**VALID_COVERAGE, "coverage_id": "not-a-uuid"}
    assert _schema_accepts("coverage", "CoverageResult", malformed) is False

    # The documented opt-out: structure without formats.
    validate_def("coverage", "CoverageResult", malformed, check_formats=False)


def test_schema_dir_points_at_the_real_directory() -> None:
    assert SCHEMA_DIR.is_dir()
    assert (SCHEMA_DIR / "common.json").is_file()
    assert len(load_schemas()) == len(SCHEMA_FILES)
