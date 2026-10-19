"""Access to the frozen JSON Schema source files.

`packages/contracts/schemas/*.json` is the single source of truth (§1.1). This module only locates
and loads it; it never restates any schema content.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

# .../packages/contracts/python/src/referraleasy_contracts/schemas.py
#   parents[0]=referraleasy_contracts  [1]=src  [2]=python  [3]=contracts
SCHEMA_DIR: Path = Path(__file__).resolve().parents[3] / "schemas"

#: Mirrors `ContractVersion.const` in common.json. A breaking schema change bumps both.
CONTRACT_VERSION = "v1"

#: Load order is irrelevant (refs are by filename) but kept stable for deterministic output.
SCHEMA_FILES: tuple[str, ...] = (
    "common",
    "task",
    "patient",
    "coverage",
    "directory",
    "documents",
    "booking",
    "workflow",
    "sse",
)


@cache
def load_schema(name: str) -> dict[str, Any]:
    """Load one schema by stem, e.g. ``load_schema("booking")``."""
    if name not in SCHEMA_FILES:
        raise KeyError(f"unknown schema {name!r}; known: {', '.join(SCHEMA_FILES)}")
    path = SCHEMA_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"contract schema missing: {path}")
    with path.open(encoding="utf-8") as fh:
        parsed: dict[str, Any] = json.load(fh)
        return parsed


def load_schemas() -> dict[str, dict[str, Any]]:
    """Load every schema, keyed by stem."""
    return {name: load_schema(name) for name in SCHEMA_FILES}


@cache
def ref_resolver() -> Any:
    """A `jsonschema` RefResolver over the schema directory.

    Cross-file refs are written relatively (``"common.json#/$defs/WorkflowId"``), so validation and
    generation both need the whole directory in scope — including for schemas that are only ever
    reached as a ref target.
    """
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    resources = [
        (
            f"{name}.json",
            Resource.from_contents(load_schema(name), default_specification=DRAFT202012),
        )
        for name in SCHEMA_FILES
    ]
    # Bare-stem aliases too, so both "common.json" and "common" resolve.
    aliases = [(name.rsplit(".", 1)[0], res) for name, res in resources]
    return Registry().with_resources([*resources, *aliases])


def _validator(schema: Any, check_formats: bool) -> Any:
    from jsonschema import Draft202012Validator, FormatChecker

    return Draft202012Validator(
        schema,
        registry=ref_resolver(),
        # Without a format checker, `format: uuid` / `date-time` / `date` / `email` are annotations
        # that validate nothing — a malformed id would pass CI and fail at a service boundary
        # instead. They are checked by default; pass check_formats=False to opt out.
        format_checker=FormatChecker() if check_formats else None,
    )


def validate_against(schema_name: str, instance: Any, *, check_formats: bool = True) -> None:
    """Validate ``instance`` against a whole schema document, resolving cross-file refs.

    Raises `jsonschema.exceptions.ValidationError` on failure.
    """
    _validator(load_schema(schema_name), check_formats).validate(instance)


def validate_def(
    schema_name: str, def_name: str, instance: Any, *, check_formats: bool = True
) -> None:
    """Validate ``instance`` against one ``$defs`` entry of a schema."""
    _validator({"$ref": f"{schema_name}.json#/$defs/{def_name}"}, check_formats).validate(instance)
