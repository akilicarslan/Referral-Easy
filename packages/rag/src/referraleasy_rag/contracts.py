"""Validated access to the fixture corpus.

Fixtures are read through the frozen contract bindings, not parsed as loose JSON. A fixture
that drifts from `packages/contracts/schemas/directory.json` then fails at index time, loudly,
instead of being chunked into a plausible-sounding document that mis-states a phone number.

The generated bindings ship a `py.typed` marker, so this import is typed. The generated model
types still stop here: everything crossing the boundary is cast to a plain mapping, so the rest
of the package depends on the *data*, not on a code generator's class names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from referraleasy_contracts.generated.coverage import CoverageResult
from referraleasy_contracts.generated.directory import (
    Hospital,
    Location,
    Provider,
    Slot,
)

Row = dict[str, Any]


class FixtureError(RuntimeError):
    """A fixture file is missing, unparseable, or violates its schema."""


def _read_json_rows(path: Path) -> list[Row]:
    if not path.is_file():
        raise FixtureError(f"fixture file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FixtureError(f"fixture file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise FixtureError(f"fixture file must contain a JSON array of records: {path}")
    return [cast("Row", row) for row in payload]


def _validate(path: Path, model: Any) -> list[Row]:
    rows = _read_json_rows(path)
    validated: list[Row] = []
    for index, row in enumerate(rows):
        try:
            # mode="json" matters: UUIDs, enums and datetimes become plain JSON values, which is
            # what the chunker, the index documents and the committed seed corpus all expect.
            validated.append(cast("Row", model.model_validate(row).model_dump(mode="json")))
        except Exception as exc:  # pydantic ValidationError, kept as a domain error
            raise FixtureError(
                f"{path.name}[{index}] does not satisfy its contract: {exc}"
            ) from exc
    return validated


def load_providers(fixtures_dir: Path) -> list[Row]:
    """Providers, validated against the `Provider` contract type."""
    return _validate(fixtures_dir / "directory" / "providers.json", Provider)


def load_hospitals(fixtures_dir: Path) -> list[Row]:
    """Hospitals, validated against the `Hospital` contract type."""
    return _validate(fixtures_dir / "directory" / "hospitals.json", Hospital)


def load_locations(fixtures_dir: Path) -> list[Row]:
    """Locations, validated against the `Location` contract type."""
    return _validate(fixtures_dir / "directory" / "locations.json", Location)


def load_slots(fixtures_dir: Path) -> list[Row]:
    """Slots, validated against the `Slot` contract type.

    Loaded for completeness (the booking flow needs them) but deliberately **not** indexed:
    see :mod:`referraleasy_rag.chunking`.
    """
    return _validate(fixtures_dir / "directory" / "slots.json", Slot)


def load_coverage_results(fixtures_dir: Path) -> list[Row]:
    """Coverage results — the structured record that outranks anything retrieved as text."""
    return _validate(fixtures_dir / "coverage" / "coverage_results.json", CoverageResult)
