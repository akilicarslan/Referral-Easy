"""A small validator for the subset of JSON Schema the manifest schema actually uses.

``packages/skills`` is deliberately dependency-free, so that when ``jsonschema`` happens to be
installed it is not installed *because of this feature*: the manifests are validated against the
shipped ``manifest.schema.json`` with this file, and the real library is used as a cross-check when
it is importable. Two independent implementations accepting and rejecting the same documents is
worth more than either alone.

Supported keywords: ``type``, ``const``, ``enum``, ``properties``, ``required``,
``additionalProperties: false``, ``items``, ``minItems``, ``minLength``, ``maxLength``, ``pattern``.
Anything else in the schema is metadata and ignored.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["validate"]

_TYPE_CHECKS: dict[str, Any] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "null": lambda value: value is None,
}


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of human-readable violations; empty means the instance conforms."""
    errors: list[str] = []
    _check(instance, schema, path, errors)
    return errors


def _check(instance: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")

    declared = schema.get("type")
    if declared is not None:
        check = _TYPE_CHECKS.get(declared)
        if check is None:
            raise ValueError(f"unsupported type in schema: {declared!r}")
        if not check(instance):
            errors.append(f"{path}: expected {declared}, got {type(instance).__name__}")
            return

    if isinstance(instance, str):
        _check_string(instance, schema, path, errors)

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items, has {len(instance)}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                _check(item, item_schema, f"{path}[{index}]", errors)

    if isinstance(instance, dict):
        _check_object(instance, schema, path, errors)


def _check_string(instance: str, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if "minLength" in schema and len(instance) < schema["minLength"]:
        errors.append(f"{path}: shorter than minLength {schema['minLength']}")
    if "maxLength" in schema and len(instance) > schema["maxLength"]:
        errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
    pattern = schema.get("pattern")
    if pattern is not None and not re.search(pattern, instance):
        errors.append(f"{path}: {instance!r} does not match {pattern!r}")


def _check_object(
    instance: dict[str, Any], schema: dict[str, Any], path: str, errors: list[str]
) -> None:
    for key in schema.get("required", []):
        if key not in instance:
            errors.append(f"{path}: missing required key {key!r}")

    properties = schema.get("properties", {})
    for key, subschema in properties.items():
        if key in instance:
            _check(instance[key], subschema, f"{path}.{key}", errors)

    if schema.get("additionalProperties") is False:
        for key in instance:
            if key not in properties:
                errors.append(f"{path}: undeclared key {key!r}")
