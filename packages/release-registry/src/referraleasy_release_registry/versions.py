"""The version ledger: the one place a release version is *declared*.

§13 needs two different things and it is worth being precise about which is which:

* **a hash**, which is *derived* from artifacts and cannot be wrong — change a prompt
  and the hash moves, whether or not anyone wanted it to;
* **a version**, which is *declared* by a person and cannot be derived — no function
  can know whether a prompt edit is a patch, a minor bump or a major one.

:mod:`referraleasy_release_registry.artifacts` produces the first.
``registry/versions.json``, read by this module, holds the second. The version-bump
gate is the join of the two: *this artifact's hash moved, and its declared version
did not.*

Why a checked-in file rather than a constant in each module
-----------------------------------------------------------
A version declared inside the module it versions is a version that moves as a side
effect of editing that module — which is precisely the edit being reviewed. Keeping
the declaration in one file next to the release history means a version bump is a
line in a diff a reviewer reads, and the gate compares two files rather than a file
against itself.

Where the ledger is *not* authoritative
---------------------------------------
Skills already declare a version in their own ``manifest.json`` (ADR-0028). The
ledger is checked against those rather than trusted over them: a mismatch is a
:class:`~referraleasy_release_registry.errors.VersionLedgerError`, because two
answers to "what version is this skill?" is the drift §13 exists to prevent. For
agents and prompts nothing in the tree declares a version, so the ledger is the only
source and there is nothing to disagree with.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from referraleasy_release_registry.errors import VersionLedgerError

__all__ = [
    "LEDGER_FILENAME",
    "ROLE_KEYS",
    "VersionLedger",
    "is_newer",
    "load_ledger",
    "parse_semver",
]

#: Where the ledger lives, relative to the repository root.
LEDGER_FILENAME: Final = "registry/versions.json"

#: §3's seven roles, spelled as `agent_role.code` spells them (migration 014). Copied
#: rather than imported from `referraleasy_agent_runtime`: the ledger is read by the
#: check script before anything else is on the path, and a ledger whose keys came from
#: the thing it is checking could not notice a role going missing.
ROLE_KEYS: Final[tuple[str, ...]] = (
    "care_coordinator",
    "referral_intake",
    "coverage_network",
    "provider_matching",
    "referral_readiness",
    "scheduling",
    "recovery_handoff",
)

#: Accepts `1.2.3` and `1.2.3-rc.1`. Build metadata (`+sha`) is deliberately not
#: accepted: it describes a build, and the ledger describes a release.
_SEMVER: Final = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


def parse_semver(text: str) -> tuple[int, int, int, str]:
    """``text`` as ``(major, minor, patch, prerelease)``.

    Raises rather than guessing: a version the gate cannot compare is a version the
    gate cannot enforce, and silently treating it as "not newer" would fail every
    build for a reason nobody could read.
    """
    match = _SEMVER.match(text.strip())
    if match is None:
        raise VersionLedgerError(
            f"{text!r} is not a version this ledger accepts (major.minor.patch)"
        )
    major, minor, patch, prerelease = match.groups()
    return int(major), int(minor), int(patch), prerelease or ""


def is_newer(candidate: str, baseline: str) -> bool:
    """Whether ``candidate`` is a strictly later release than ``baseline``.

    Strictly, not merely different. §13 asks whether the version *increased*, and a
    version that moved sideways — `1.0.0` to `1.0.0-rc.1`, or
    a re-tag — has not increased. A prerelease sorts *before* its own release, so
    `1.0.0-rc.1 < 1.0.0 < 1.0.1`.
    """
    candidate_parts = parse_semver(candidate)
    baseline_parts = parse_semver(baseline)
    if candidate_parts[:3] != baseline_parts[:3]:
        return candidate_parts[:3] > baseline_parts[:3]
    candidate_pre, baseline_pre = candidate_parts[3], baseline_parts[3]
    if candidate_pre == baseline_pre:
        return False
    if not candidate_pre:
        return True  # a release outranks its own prerelease
    if not baseline_pre:
        return False
    return _prerelease_key(candidate_pre) > _prerelease_key(baseline_pre)


def _prerelease_key(prerelease: str) -> tuple[tuple[int, Any], ...]:
    """A comparable key for a prerelease string.

    Split on dots, with numeric identifiers compared numerically and everything else
    lexically — semver's own rule. Mixed comparisons are ordered by a type tag rather
    than by Python's refusal to compare `int` and `str`.
    """
    parts: list[tuple[int, Any]] = []
    for identifier in prerelease.split("."):
        if identifier.isdigit():
            parts.append((0, int(identifier)))
        else:
            parts.append((1, identifier))
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class VersionLedger:
    """The declared versions of one release.

    ``release`` is the version of the release as a whole; it is what a
    ``release_registry`` row for ``component = 'referraleasy'`` carries. The three
    mappings are the per-component declarations.
    """

    release: str
    schema: str
    agents: Mapping[str, str]
    prompts: Mapping[str, str]
    skills: Mapping[str, str]

    def agent(self, role: str) -> str:
        return self._declared(self.agents, role, "agents")

    def prompt(self, role: str) -> str:
        return self._declared(self.prompts, role, "prompts")

    def skill(self, name: str) -> str:
        return self._declared(self.skills, name, "skills")

    @staticmethod
    def _declared(declared: Mapping[str, str], key: str, section: str) -> str:
        try:
            return declared[key]
        except KeyError as error:
            raise VersionLedgerError(
                f"the version ledger declares no version for {key!r} in its {section!r} section; "
                "an undeclared component cannot be gated"
            ) from error


def load_ledger(path: Path) -> VersionLedger:
    """Read and validate the ledger at ``path``.

    Validation is the point, not a formality: an unknown key is refused (a typo'd role
    name would otherwise silently declare a version for nobody), a missing one is
    refused, and every value must be a version :func:`parse_semver` accepts.
    """
    if not path.is_file():
        raise VersionLedgerError(
            f"no version ledger at {path}. §13's gate compares a derived artifact hash "
            "against a declared version; without the declaration there is nothing to compare."
        )
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise VersionLedgerError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise VersionLedgerError(f"{path} must contain a JSON object, not {type(raw).__name__}")

    _reject_unknown(raw, {"release", "schema", "agents", "prompts", "skills"}, path)
    release = _required_version(raw, "release", path)
    # `schema` is deliberately *not* semver. It records the contract generation the release
    # is expressed in, and the contracts package declares its own (`CONTRACT_VERSION`, "v1")
    # — a value this module cannot compare against semver and must not restate. It is read
    # as text here and checked against the installed contracts when a manifest is built
    # (``manifest.build_manifest``), which is where the authority for it lives.
    schema = _required_text(raw, "schema", path)
    agents = _section(raw, "agents", ROLE_KEYS, path)
    prompts = _section(raw, "prompts", ROLE_KEYS, path)
    skills = _section(raw, "skills", None, path)

    for section in (agents, prompts, skills):
        for value in section.values():
            parse_semver(value)  # raises VersionLedgerError naming the offending value

    return VersionLedger(
        release=release, schema=schema, agents=agents, prompts=prompts, skills=skills
    )


def _reject_unknown(raw: Mapping[str, Any], known: set[str], path: Path) -> None:
    unknown = sorted(set(raw) - known)
    if unknown:
        raise VersionLedgerError(
            f"{path} carries sections this ledger does not define: {', '.join(unknown)}. "
            f"The defined sections are {', '.join(sorted(known))}."
        )


def _required_text(raw: Mapping[str, Any], key: str, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise VersionLedgerError(f"{path} needs a non-empty {key!r}")
    return value.strip()


def _required_version(raw: Mapping[str, Any], key: str, path: Path) -> str:
    """A required field that must also be a version :func:`parse_semver` accepts."""
    value = _required_text(raw, key, path)
    parse_semver(value)
    return value


def _section(
    raw: Mapping[str, Any], key: str, required_keys: Sequence[str] | None, path: Path
) -> Mapping[str, str]:
    section = raw.get(key)
    if not isinstance(section, dict) or not section:
        raise VersionLedgerError(f"{path} needs a non-empty {key!r} object")
    declared: dict[str, str] = {}
    for name, version in section.items():
        if not isinstance(version, str):
            raise VersionLedgerError(f"{path}: {key}.{name} must be a version string")
        declared[str(name)] = version.strip()
    if required_keys is not None:
        missing = [name for name in required_keys if name not in declared]
        extra = sorted(set(declared) - set(required_keys))
        if missing:
            raise VersionLedgerError(
                f"{path}: {key} is missing {', '.join(missing)}; every one of §3's seven roles "
                "must be declared or the gate cannot see it change"
            )
        if extra:
            raise VersionLedgerError(
                f"{path}: {key} declares {', '.join(extra)}, which is not one of §3's seven roles"
            )
    return declared
