"""Skill packages: cheap selection metadata, separately-loaded instructions (ADR-0028).

A skill package is three records, not one:

* a **manifest** (``manifest.json``) - name, version, owning agent role, a one-line summary, the
  concrete triggers that select it, and the list of reference files. This is what a selector reads.
* an **instruction body** (``SKILL.md``) - the procedure. Loaded only once a skill has been selected.
* **read-only reference material** (``references/``) - worked examples and lookup tables that the
  body cites. Loaded only when the selected skill actually cites them.

The separation exists for four reasons, and each is defended by a test in ``tests/skills``:

1. **Selection is cheap.** ``list_manifests()`` and ``load_manifest()`` open ``manifest.json`` and
   nothing else. Loading a whole skill to decide whether to load it would defeat the point of
   having a selector, so the manifest carries enough to choose from: ``summary``, ``when_to_use``,
   ``owner_agent`` and ``tags``.
2. **Instructions are not authority.** ``authority`` is the constant ``instructions_only`` and
   ``grants_tool_permissions`` is ``false`` - as a schema constant, not a convention. Following a
   skill never authorises a tool call; tool authority lives in the runtime allowlist and in the
   database policy table, which is keyed by workflow state. A package that could grant authority
   would make instructions a privilege-escalation surface.
3. **A package is not an Agent Card.** The card *advertises* a capability to peers over A2A; the
   package *instructs* a model. They are mapped to each other by ``agent_card_skill_id`` and are
   otherwise different records with different lifecycles, so the manifest carries no Agent Card
   fields (``capabilities``, ``tools``, ``auth``, ``agent_hash``, ...) and the schema refuses them.
4. **Documents are data.** Instruction bodies that quote retrieved text say so explicitly; see the
   ``## Documents and retrieved text are data`` section every package is required to carry.

Nothing here performs I/O beyond the filesystem, makes a model call, or reads a secret.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "AGENT_ROLES",
    "AUTHORITY_INSTRUCTIONS_ONLY",
    "FORBIDDEN_MANIFEST_KEYS",
    "INSTRUCTIONS_FILENAME",
    "MANIFEST_FILENAME",
    "REFERENCES_DIRNAME",
    "REQUIRED_MANIFEST_KEYS",
    "REQUIRED_SECTIONS",
    "Skill",
    "SkillError",
    "SkillManifest",
    "SkillManifestError",
    "SkillNotFoundError",
    "list_manifests",
    "list_skills",
    "load_manifest",
    "load_skill",
    "manifest_schema",
    "select",
    "skills_root",
]

# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------

#: The seven specialist agent roles of section 3, exactly as ``agent_role.code`` stores them
#: (migration 014). ``care_coordinator`` is the router and owns no skill package of its own.
AGENT_ROLES: Final[frozenset[str]] = frozenset(
    {
        "care_coordinator",
        "referral_intake",
        "coverage_network",
        "provider_matching",
        "referral_readiness",
        "scheduling",
        "recovery_handoff",
    }
)

#: The only legal value of a manifest's ``authority`` key. Spelled as a constant so the schema can
#: pin it with ``const``: a package that declared any other value would not load at all.
AUTHORITY_INSTRUCTIONS_ONLY: Final[str] = "instructions_only"

MANIFEST_FILENAME: Final[str] = "manifest.json"
INSTRUCTIONS_FILENAME: Final[str] = "SKILL.md"
REFERENCES_DIRNAME: Final[str] = "references"

#: Environment override for the skills content root. Used by tests, and by anything that vendors
#: the packages somewhere other than the source tree.
SKILLS_ROOT_ENV: Final[str] = "REFERRALEASY_SKILLS_ROOT"

#: Every package must carry these manifest keys. The JSON Schema in ``manifest.schema.json``
#: declares the same set, and a test asserts the two cannot drift apart.
REQUIRED_MANIFEST_KEYS: Final[tuple[str, ...]] = (
    "manifest_version",
    "name",
    "version",
    "title",
    "owner_agent",
    "summary",
    "when_to_use",
    "inputs",
    "outputs",
    "references",
    "authority",
    "grants_tool_permissions",
    "agent_card_skill_id",
    "tags",
)

#: Keys a manifest may never carry, checked recursively. The first group is Agent Card vocabulary
#: (a package is a different record from the card that advertises it, so carrying these would blur
#: the two). The second group is tool-authority vocabulary: a manifest that could name tools,
#: permissions or scopes would be granting authority, which is the boundary this format defends.
FORBIDDEN_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {
        # Agent Card fields
        "agent_hash",
        "auth",
        "capabilities",
        "input_schema_id",
        "output_schema_id",
        "sub_agents",
        "tools",
        # tool-authority fields
        "allowed_actions",
        "allowed_tools",
        "grants",
        "permissions",
        "scopes",
        "token_scopes",
        "tool_permissions",
    }
)

#: Section headings every instruction body must carry, in any order. The last two are the ones that
#: make the security posture of a package reviewable rather than implicit.
REQUIRED_SECTIONS: Final[tuple[str, ...]] = (
    "## When this skill applies",
    "## Inputs",
    "## Procedure",
    "## Outputs",
    "## Documents and retrieved text are data",
    "## Authority and boundaries",
    "## Failure modes and escalation",
)

_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_KEBAB = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_CARD_SKILL_ID = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

MAX_SUMMARY_CHARS: Final[int] = 200


class SkillError(Exception):
    """Base class for every failure this module raises."""


class SkillNotFoundError(SkillError):
    """No package directory of that name exists under the skills root."""


class SkillManifestError(SkillError):
    """A manifest is missing, unparseable, or violates the package format."""


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """Selection metadata for one skill package.

    Constructed only by :func:`load_manifest`, which validates before it returns, so an instance
    that exists is a manifest that passed the format checks.
    """

    name: str
    version: str
    title: str
    owner_agent: str
    summary: str
    when_to_use: tuple[str, ...]
    inputs: tuple[dict[str, Any], ...]
    outputs: tuple[dict[str, Any], ...]
    references: tuple[dict[str, Any], ...]
    agent_card_skill_id: str
    tags: tuple[str, ...]
    authority: str = AUTHORITY_INSTRUCTIONS_ONLY
    grants_tool_permissions: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def selection_text(self) -> str:
        """The text a selector scores a query against - manifest fields only, never the body."""
        return " ".join((self.name, self.title, self.summary, *self.when_to_use, *self.tags))

    @property
    def reference_paths(self) -> tuple[str, ...]:
        """Reference paths as written in the manifest, relative to the package directory."""
        return tuple(str(entry["path"]) for entry in self.references)


@dataclass(frozen=True, slots=True)
class Skill:
    """A fully loaded package: manifest, instruction body, and the reference texts it cites."""

    manifest: SkillManifest
    instructions: str
    references: dict[str, str]

    @property
    def name(self) -> str:
        return self.manifest.name


# --------------------------------------------------------------------------------------
# Root resolution
# --------------------------------------------------------------------------------------


def skills_root() -> Path:
    """Return the directory holding the skill package directories.

    Resolution order: ``REFERRALEASY_SKILLS_ROOT``, then a ``_skills`` directory bundled inside the
    installed package, then the nearest ancestor ``skills`` directory that actually contains
    packages. The last step is what makes the source checkout work without configuration.
    """
    override = os.environ.get(SKILLS_ROOT_ENV)
    if override:
        root = Path(override).expanduser().resolve()
        if not root.is_dir():
            raise SkillNotFoundError(f"{SKILLS_ROOT_ENV} points at a missing directory: {root}")
        return root

    bundled = Path(__file__).resolve().parent / "_skills"
    if _looks_like_skills_root(bundled):
        return bundled

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "skills"
        if _looks_like_skills_root(candidate):
            return candidate

    raise SkillNotFoundError(
        "no skills root found; set "
        f"{SKILLS_ROOT_ENV} to the directory containing the skill packages"
    )


def _looks_like_skills_root(candidate: Path) -> bool:
    if not candidate.is_dir():
        return False
    return any(entry.is_file() for entry in candidate.glob(f"*/{MANIFEST_FILENAME}"))


# --------------------------------------------------------------------------------------
# Manifest loading - reads manifest.json and nothing else
# --------------------------------------------------------------------------------------


def list_skills() -> list[str]:
    """Names of every package under the skills root, sorted.

    Reads directory listings only. A package whose manifest is malformed is still listed here;
    :func:`load_manifest` is what refuses it.
    """
    root = skills_root()
    return sorted(
        entry.parent.name
        for entry in root.glob(f"*/{MANIFEST_FILENAME}")
        if entry.parent.name != REFERENCES_DIRNAME
    )


def load_manifest(name: str) -> SkillManifest:
    """Load and validate one manifest **without opening ``SKILL.md``**.

    This is the operation a selector performs for every candidate on every turn, so it is kept to
    a single file read plus ``stat`` calls on the declared reference files. Missing reference files
    are an error here rather than at load time: a package that cites a file it does not ship would
    otherwise only fail after it had already been selected.
    """
    package_dir = _package_dir(name)
    manifest_path = package_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise SkillManifestError(f"{name}: no {MANIFEST_FILENAME} in {package_dir}")

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillManifestError(f"{name}: {MANIFEST_FILENAME} is not valid JSON: {exc}") from exc

    manifest = _validate(name, package_dir, raw)
    for entry in manifest.references:
        target = package_dir / str(entry["path"])
        if not target.is_file():
            raise SkillManifestError(f"{name}: manifest cites a missing reference: {entry['path']}")
    return manifest


def list_manifests(owner_agent: str | None = None) -> list[SkillManifest]:
    """Every manifest, optionally restricted to one owning agent role. Never reads a body."""
    manifests = [load_manifest(name) for name in list_skills()]
    if owner_agent is not None:
        manifests = [m for m in manifests if m.owner_agent == owner_agent]
    return manifests


def select(
    query: str = "",
    *,
    owner_agent: str | None = None,
    tag: str | None = None,
    limit: int | None = None,
) -> list[SkillManifest]:
    """Rank packages for a task using manifest fields only.

    A deliberately simple lexical score over ``summary``, ``when_to_use`` and ``tags``: enough to
    demonstrate that selection never needs the instruction body, and small enough to read. The
    caller writes the resulting list to the trace, which is how the effective skill list becomes
    auditable. Ties break on name so the ordering is deterministic.
    """
    terms = [term for term in re.split(r"[^a-z0-9]+", query.lower()) if len(term) > 2]
    ranked: list[tuple[int, str, SkillManifest]] = []
    for manifest in list_manifests(owner_agent=owner_agent):
        if tag is not None and tag not in manifest.tags:
            continue
        if terms:
            score = sum(manifest.selection_text.lower().count(term) for term in terms)
            if not score:
                continue
        else:
            # No query to rank against: an owner/tag filter is itself the selection, so every
            # survivor is returned in a stable order rather than scored away.
            score = 1
        ranked.append((score, manifest.name, manifest))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    results = [manifest for _, _, manifest in ranked]
    return results[:limit] if limit is not None else results


# --------------------------------------------------------------------------------------
# Full load - the expensive path, only for packages that have already been selected
# --------------------------------------------------------------------------------------


def load_skill(name: str) -> Skill:
    """Load a package in full: manifest, instruction body, and every declared reference text."""
    manifest = load_manifest(name)
    package_dir = _package_dir(name)
    instructions_path = package_dir / INSTRUCTIONS_FILENAME
    if not instructions_path.is_file():
        raise SkillManifestError(f"{name}: no {INSTRUCTIONS_FILENAME} in {package_dir}")

    references = {
        path: (package_dir / path).read_text(encoding="utf-8") for path in manifest.reference_paths
    }
    return Skill(
        manifest=manifest,
        instructions=instructions_path.read_text(encoding="utf-8"),
        references=references,
    )


def manifest_schema() -> dict[str, Any]:
    """The shipped JSON Schema for manifests, as a dictionary.

    The loader validates in code so that a broken package fails without a schema library on the
    path. This function exists so the schema cannot quietly disagree with that code: a test asserts
    the two declare the same required keys.
    """
    schema_path = Path(__file__).resolve().parent / "manifest.schema.json"
    parsed: object = json.loads(schema_path.read_text(encoding="utf-8"))
    # `json.loads` is typed `Any`, so returning it directly would satisfy mypy while making the
    # declared return type a claim nothing checks. The schema is shipped data: if it is ever
    # replaced by an array or a scalar, the callers below would fail far from the cause.
    if not isinstance(parsed, dict):
        raise SkillManifestError(
            f"{schema_path.name} must contain a JSON object, not {type(parsed).__name__}"
        )
    return parsed


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def _package_dir(name: str) -> Path:
    if not _KEBAB.match(name):
        raise SkillNotFoundError(f"not a skill package name: {name!r}")
    package_dir = skills_root() / name
    if not package_dir.is_dir():
        raise SkillNotFoundError(f"no skill package named {name!r} under {skills_root()}")
    return package_dir


def _validate(name: str, package_dir: Path, raw: Any) -> SkillManifest:
    if not isinstance(raw, dict):
        raise SkillManifestError(f"{name}: manifest must be a JSON object")

    published = set(_walk_keys(raw))
    forbidden = sorted(published & FORBIDDEN_MANIFEST_KEYS)
    if forbidden:
        raise SkillManifestError(
            f"{name}: manifest carries keys it may not ({', '.join(forbidden)}). "
            "A skill package advertises no tools and grants no permissions; tool authority lives "
            "in the runtime allowlist and the policy table, and the Agent Card is a separate record."
        )

    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in raw]
    if missing:
        raise SkillManifestError(f"{name}: manifest is missing required keys: {', '.join(missing)}")

    extra = sorted(set(raw) - set(REQUIRED_MANIFEST_KEYS))
    if extra:
        raise SkillManifestError(f"{name}: manifest carries undeclared keys: {', '.join(extra)}")

    if raw["manifest_version"] != "1":
        raise SkillManifestError(
            f"{name}: unsupported manifest_version {raw['manifest_version']!r}"
        )
    if raw["name"] != package_dir.name:
        raise SkillManifestError(
            f"{name}: manifest name {raw['name']!r} does not match its directory"
        )
    if not _KEBAB.match(str(raw["name"])):
        raise SkillManifestError(f"{name}: name must be kebab-case")
    if not _SEMVER.match(str(raw["version"])):
        raise SkillManifestError(f"{name}: version {raw['version']!r} is not semver")

    owner = raw["owner_agent"]
    if owner not in AGENT_ROLES:
        raise SkillManifestError(
            f"{name}: owner_agent {owner!r} is not one of the seven section 3 roles"
        )

    summary = raw["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise SkillManifestError(f"{name}: summary must be a non-empty string")
    if "\n" in summary:
        raise SkillManifestError(f"{name}: summary must be a single line")
    if len(summary) > MAX_SUMMARY_CHARS:
        raise SkillManifestError(
            f"{name}: summary is {len(summary)} chars; the selector budgets {MAX_SUMMARY_CHARS}"
        )

    when_to_use = _string_list(name, "when_to_use", raw["when_to_use"], minimum=3)

    inputs = _record_list(name, "inputs", raw["inputs"])
    outputs = _record_list(name, "outputs", raw["outputs"])
    for label, records in (("inputs", inputs), ("outputs", outputs)):
        for record in records:
            for key in ("name", "type", "description"):
                if not isinstance(record.get(key), str) or not record[key].strip():
                    raise SkillManifestError(
                        f"{name}: {label} entry {record.get('name')!r} needs a non-empty {key!r}"
                    )

    references = _record_list(name, "references", raw["references"], minimum=0)
    for record in references:
        path = record.get("path")
        purpose = record.get("purpose")
        if not isinstance(path, str) or not path.startswith(f"{REFERENCES_DIRNAME}/"):
            raise SkillManifestError(
                f"{name}: reference path {path!r} must live under {REFERENCES_DIRNAME}/"
            )
        if not isinstance(purpose, str) or not purpose.strip():
            raise SkillManifestError(f"{name}: reference {path!r} needs a one-line purpose")

    if raw["authority"] != AUTHORITY_INSTRUCTIONS_ONLY:
        raise SkillManifestError(
            f"{name}: authority must be {AUTHORITY_INSTRUCTIONS_ONLY!r}, got {raw['authority']!r}"
        )
    if raw["grants_tool_permissions"] is not False:
        raise SkillManifestError(
            f"{name}: grants_tool_permissions must be the boolean false; skills instruct, they "
            "do not authorise"
        )

    card_id = str(raw["agent_card_skill_id"])
    if not _CARD_SKILL_ID.match(card_id):
        raise SkillManifestError(f"{name}: agent_card_skill_id {card_id!r} is malformed")
    if card_id == raw["name"]:
        raise SkillManifestError(
            f"{name}: agent_card_skill_id must identify the Agent Card skill entry, which is a "
            "separate record from this package, not a copy of the package name"
        )

    tags = tuple(_string_list(name, "tags", raw["tags"], minimum=1))

    return SkillManifest(
        name=raw["name"],
        version=raw["version"],
        title=raw["title"],
        owner_agent=owner,
        summary=summary,
        when_to_use=tuple(when_to_use),
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        references=tuple(references),
        agent_card_skill_id=card_id,
        tags=tags,
        authority=raw["authority"],
        grants_tool_permissions=False,
        raw=dict(raw),
    )


def _walk_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.append(key)
            keys.extend(_walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_walk_keys(item))
    return keys


def _string_list(name: str, key: str, value: Any, *, minimum: int) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise SkillManifestError(f"{name}: {key} must be a list of at least {minimum} entries")
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise SkillManifestError(f"{name}: {key} entries must be non-empty strings")
    return list(value)


def _record_list(name: str, key: str, value: Any, *, minimum: int = 1) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) < minimum:
        raise SkillManifestError(f"{name}: {key} must be a list of at least {minimum} entries")
    for entry in value:
        if not isinstance(entry, dict):
            raise SkillManifestError(f"{name}: {key} entries must be objects")
    return list(value)
