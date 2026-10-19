"""Skill selection: load what the task needs, record what was loaded.

The skill packages live in ``packages/skills`` (eight of them, each a
``manifest.json`` beside a ``SKILL.md``), and this module is the only part of the
runtime that reads them. It codes against three functions and nothing else —
``list_skills()``, ``load_manifest(name)``, ``load_body(name)`` — so the package
layout stays the skill author's business, and so this file is testable without it.

Four rules, each with a reason:

**Selection never reads a body.** Listing eight skills and reading eight
``SKILL.md`` files to run one turn is the difference between a fast turn and a
budget-shaped hole. Selection reads manifests; :func:`load_instructions` reads
bodies, once, only for the skills that were actually selected.

**A skill grants no tool permission.** A manifest may *declare* the tools it talks
about — and the shipped loader marks such manifests invalid — but a declaration is
recorded and then ignored. Authority comes from ``roles.py`` and the database
policy table, never from a document that ships next to a prompt. The test
``test_a_skill_grants_no_tool_permission`` pins this against a skill that claims
``create_booking``.

**The effective list goes into the trace.** Not the available list: what this turn
actually loaded, per skill, with the reason every other candidate was left out —
which is what makes "why did the agent not know X?" answerable afterwards.

**Read manifests, not object models.** A manifest may arrive as a mapping or as a
dataclass; :meth:`SkillManifest.from_source` reads both, so the runtime never needs
to import the loader it is reading from.

Where the shipped loader differs
--------------------------------
``referraleasy_skills`` names its three ends ``list_skills``, ``load_manifest`` and
``load_skill`` (a full package, whose body is ``Skill.instructions``), and offers
its own lexical ``select()`` for *relevance*. :class:`SkillsPackageSource` bridges
the naming with no import at module scope, and :func:`gate_manifests` applies this
runtime's *entitlement* gate to a list the loader already ranked. Entitlement and
relevance stay separate decisions: a skill is never loaded because it scored well.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

from referraleasy_agent_runtime.errors import SkillSourceError
from referraleasy_agent_runtime.roles import AgentRole

__all__ = [
    "MANIFEST_ROLE_KEYS",
    "MANIFEST_STATE_KEYS",
    "MANIFEST_TAG_KEYS",
    "MANIFEST_TOOL_KEYS",
    "SKILLS_PACKAGE",
    "EmptySkillSource",
    "SkillManifest",
    "SkillSelection",
    "SkillSource",
    "SkillsPackageSource",
    "gate_manifests",
    "load_instructions",
    "select_skills",
]

#: Canonical manifest keys, and the aliases accepted because the skills package is
#: authored separately. Anything unrecognised means "no claim", and a skill that
#: claims nothing is selected only when a task names it explicitly.
MANIFEST_ROLE_KEYS: Final[tuple[str, ...]] = ("roles", "agents", "owner_agent")
MANIFEST_STATE_KEYS: Final[tuple[str, ...]] = ("states", "workflow_states", "applies_to")
MANIFEST_TAG_KEYS: Final[tuple[str, ...]] = ("tags", "keywords")
MANIFEST_TOOL_KEYS: Final[tuple[str, ...]] = ("tools", "declared_tools")

#: The distribution providing the skill packages, imported lazily by the adapter.
SKILLS_PACKAGE: Final = "referraleasy_skills"

_JOIN_SEPARATOR: Final = "\n\n---\n\n"


@runtime_checkable
class SkillSource(Protocol):
    """The three-function interface a skill source provides.

    Narrow on purpose: a source that lists, loads a manifest and loads a body can
    be a filesystem tree, an embedded bundle or a database row, and none of that
    changes here.
    """

    def list_skills(self) -> Sequence[str]:
        """Names of every available skill, without loading any of them."""
        ...

    def load_manifest(self, name: str) -> object:
        """The skill's manifest. Must not require reading ``SKILL.md``."""
        ...

    def load_body(self, name: str) -> str:
        """The skill's instructions (``SKILL.md``). Read only when building a prompt."""
        ...


class EmptySkillSource:
    """A source with no skills, for roles that carry none yet."""

    def list_skills(self) -> Sequence[str]:
        return ()

    def load_manifest(self, name: str) -> object:
        raise SkillSourceError(f"no skills are available; cannot load manifest for {name!r}")

    def load_body(self, name: str) -> str:
        raise SkillSourceError(f"no skills are available; cannot load body for {name!r}")


class SkillsPackageSource:
    """Adapter over the shipped ``referraleasy_skills`` loader.

    ``load_body`` is the bridge: the loader exposes the body as
    ``load_skill(name).instructions``. The import happens in the constructor, so
    this package never depends on the skills package at import time — a deployment
    that ships no skills still runs, and this module's tests stay offline.
    """

    def __init__(self, module: Any | None = None) -> None:
        self._module: Any = (
            module if module is not None else importlib.import_module(SKILLS_PACKAGE)
        )

    def list_skills(self) -> Sequence[str]:
        return list(self._module.list_skills())

    def load_manifest(self, name: str) -> object:
        return self._module.load_manifest(name)

    def load_body(self, name: str) -> str:
        return str(self._module.load_skill(name).instructions)

    def select(self, query: str = "", *, owner_agent: str | None = None) -> Sequence[object]:
        """The loader's own lexical ranking. Relevance only — never entitlement.

        A caller that wants the most relevant skills should pass the result to
        :func:`gate_manifests` before using it, so the entitlement rules apply to
        whatever relevance proposed.
        """
        return list(self._module.select(query, owner_agent=owner_agent))


def _as_text_set(value: object) -> frozenset[str] | None:
    """Normalise a manifest field to a lower-cased set of non-empty strings.

    ``None`` means "this key is absent, try the next alias". A bare string is a
    one-element set (the shipped manifests write ``owner_agent: scheduling``); any
    other iterable is read item by item. Sets count: a manifest that carries a
    ``frozenset`` — as this module's own :class:`SkillManifest` does — must not be
    read as claiming nothing, or gating it would silently reject everything.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return frozenset({value.strip().lower()}) if value.strip() else frozenset()
    if isinstance(value, Mapping):
        return None
    if isinstance(value, Iterable):
        return frozenset(str(item).strip().lower() for item in value if str(item).strip())
    return None


def _text_set(payload: Mapping[str, Any], keys: Sequence[str]) -> frozenset[str]:
    """First recognised key wins; values are stringified and lower-cased for comparison."""
    for key in keys:
        parsed = _as_text_set(payload.get(key))
        if parsed is not None:
            return parsed
    return frozenset()


def _attr_set(source: object, keys: Sequence[str]) -> frozenset[str]:
    for key in keys:
        parsed = _as_text_set(getattr(source, key, None))
        if parsed is not None:
            return parsed
    return frozenset()


def _text_of(source: object, keys: Sequence[str]) -> str | None:
    for key in keys:
        value = getattr(source, key, None)
        if isinstance(value, str) and value.strip():
            return value
    return None


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """A skill's manifest, as the runtime reads it.

    ``declared_tools`` is faithfully recorded and deliberately never granted:
    a manifest is documentation, and documentation does not hold authority.
    """

    name: str
    version: str | None = None
    description: str = ""
    roles: frozenset[str] = frozenset()
    states: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    declared_tools: frozenset[str] = frozenset()
    #: What the manifest *said* about its own authority. Recorded so the trace shows
    #: that the runtime saw the claim and did not act on it.
    grants_tool_permissions_claimed: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_source(cls, name: str, payload: object) -> SkillManifest:
        """Read a manifest defensively, from a mapping or a manifest object.

        A shape the runtime cannot read claims nothing, which makes it ineligible
        for automatic selection — the same default-deny posture as the tool policy.
        """
        if isinstance(payload, Mapping):
            return cls._from_mapping(name, payload)
        return cls._from_object(name, payload)

    @classmethod
    def _from_mapping(cls, name: str, payload: Mapping[str, Any]) -> SkillManifest:
        declared_name = payload.get("name")
        version = payload.get("version")
        description = payload.get("description") or payload.get("summary")
        claim = payload.get("grants_tool_permissions")
        return cls(
            name=str(declared_name).strip()
            if isinstance(declared_name, str) and declared_name.strip()
            else name,
            version=str(version) if isinstance(version, str) and version.strip() else None,
            description=str(description) if isinstance(description, str) else "",
            roles=_text_set(payload, MANIFEST_ROLE_KEYS),
            states=_text_set(payload, MANIFEST_STATE_KEYS),
            tags=_text_set(payload, MANIFEST_TAG_KEYS),
            declared_tools=_text_set(payload, MANIFEST_TOOL_KEYS),
            grants_tool_permissions_claimed=bool(claim),
            raw=MappingProxyType(dict(payload)),
        )

    @classmethod
    def _from_object(cls, name: str, payload: object) -> SkillManifest:
        return cls(
            name=_text_of(payload, ("name",)) or name,
            version=_text_of(payload, ("version",)),
            description=_text_of(payload, ("summary", "description", "title")) or "",
            roles=_attr_set(payload, MANIFEST_ROLE_KEYS),
            states=_attr_set(payload, MANIFEST_STATE_KEYS),
            tags=_attr_set(payload, MANIFEST_TAG_KEYS),
            declared_tools=_attr_set(payload, MANIFEST_TOOL_KEYS),
            grants_tool_permissions_claimed=bool(
                getattr(payload, "grants_tool_permissions", False)
            ),
            raw={},
        )

    def as_trace_entry(self) -> dict[str, Any]:
        return {
            "skill": self.name,
            "version": self.version,
            "roles": sorted(self.roles),
            "states": sorted(self.states),
            "tags": sorted(self.tags),
            "declared_tools_not_granted": sorted(self.declared_tools),
            "manifest_claimed_tool_permissions": self.grants_tool_permissions_claimed,
        }


@dataclass(frozen=True, slots=True)
class SkillSelection:
    """What this turn loaded, and why nothing else was loaded."""

    role: AgentRole
    workflow_state: str | None
    selected: tuple[SkillManifest, ...] = ()
    considered: tuple[str, ...] = ()
    rejected: Mapping[str, str] = field(default_factory=dict)

    @property
    def names(self) -> tuple[str, ...]:
        """The **effective** skill list for this turn."""
        return tuple(manifest.name for manifest in self.selected)

    @property
    def declared_tools(self) -> frozenset[str]:
        """Tools the selected skills merely *mention*. Recorded; never granted."""
        return frozenset().union(*(manifest.declared_tools for manifest in self.selected))

    def trace_entry(self) -> dict[str, Any]:
        """The trace record: effective skills, candidates considered, rejections."""
        return {
            "role": self.role.value,
            "workflow_state": self.workflow_state,
            "effective_skills": list(self.names),
            "selected": [manifest.as_trace_entry() for manifest in self.selected],
            "considered": list(self.considered),
            "rejected": dict(self.rejected),
            "declared_tools_not_granted": sorted(self.declared_tools),
        }


def _rejection_reason(
    manifest: SkillManifest,
    *,
    role: AgentRole,
    workflow_state: str | None,
    wanted_tags: set[str],
) -> str | None:
    """Why this manifest is not usable for this role in this state, or ``None``.

    Comparisons are case-folded on both sides: manifest values are stored lower-cased
    by :func:`_as_text_set`, and workflow states are conventionally upper-cased at
    the call site, so comparing either raw form would refuse a skill that actually
    applies. The reason string reports the *manifest's* spelling so the trace reads
    like the file an author would open.
    """
    if not manifest.roles:
        return "manifest declares no roles"
    if role.value not in manifest.roles:
        return f"manifest applies to {sorted(manifest.roles)}, not {role.value}"
    state = (workflow_state or "").strip().lower()
    if manifest.states and state not in manifest.states:
        return (
            f"manifest applies to states {sorted(state.upper() for state in manifest.states)}, "
            f"not {workflow_state}"
        )
    if wanted_tags and not (manifest.tags & wanted_tags):
        return f"manifest tags {sorted(manifest.tags)} do not intersect {sorted(wanted_tags)}"
    return None


def _as_role(role: AgentRole | str) -> AgentRole:
    return role if isinstance(role, AgentRole) else AgentRole(role)


def select_skills(
    source: SkillSource,
    *,
    role: AgentRole | str,
    workflow_state: str | None = None,
    tags: Sequence[str] = (),
    requested: Sequence[str] = (),
) -> SkillSelection:
    """Choose the skills this task needs, reading manifests and nothing else.

    ``requested`` names are honoured unconditionally — a task that asks for a skill
    either gets it or fails loudly (:class:`SkillSourceError`), because silently
    dropping an explicit instruction is worse than refusing the turn.

    Everything else must match on role **and** state (when the manifest claims
    states) **and** tag (when the task passes tags). A manifest that claims nothing
    is not selected by default.
    """
    agent_role = _as_role(role)
    available = tuple(source.list_skills())
    wanted_tags = {tag.strip().lower() for tag in tags if tag.strip()}
    selected: list[SkillManifest] = []
    rejected: dict[str, str] = {}

    for name in requested:
        if name not in available:
            raise SkillSourceError(
                f"the task requested skill {name!r}, which the skill source does not provide "
                f"(available: {sorted(available)})"
            )
        if name in {manifest.name for manifest in selected}:
            continue
        selected.append(SkillManifest.from_source(name, source.load_manifest(name)))

    explicitly_requested = set(requested)
    for name in available:
        if name in explicitly_requested:
            continue
        manifest = SkillManifest.from_source(name, source.load_manifest(name))
        reason = _rejection_reason(
            manifest,
            role=agent_role,
            workflow_state=workflow_state,
            wanted_tags=wanted_tags,
        )
        if reason is None:
            selected.append(manifest)
        else:
            rejected[name] = reason

    return SkillSelection(
        role=agent_role,
        workflow_state=workflow_state,
        selected=tuple(selected),
        considered=available,
        rejected=MappingProxyType(rejected),
    )


def gate_manifests(
    manifests: Sequence[object],
    *,
    role: AgentRole | str,
    workflow_state: str | None = None,
    tags: Sequence[str] = (),
) -> SkillSelection:
    """Apply this runtime's entitlement gate to manifests someone else chose.

    The companion to a ranked list from the skills loader: ranking decides what is
    *relevant*, this decides what is *allowed*. A skill that fails the gate is
    recorded as rejected with its reason, never silently dropped.
    """
    agent_role = _as_role(role)
    wanted_tags = {tag.strip().lower() for tag in tags if tag.strip()}
    selected: list[SkillManifest] = []
    rejected: dict[str, str] = {}
    considered: list[str] = []
    for payload in manifests:
        fallback = str(getattr(payload, "name", "unnamed"))
        manifest = SkillManifest.from_source(fallback, payload)
        considered.append(manifest.name)
        reason = _rejection_reason(
            manifest, role=agent_role, workflow_state=workflow_state, wanted_tags=wanted_tags
        )
        if reason is None:
            selected.append(manifest)
        else:
            rejected[manifest.name] = reason
    return SkillSelection(
        role=agent_role,
        workflow_state=workflow_state,
        selected=tuple(selected),
        considered=tuple(considered),
        rejected=MappingProxyType(rejected),
    )


def load_instructions(source: SkillSource, selection: SkillSelection) -> str:
    """Read the selected skills' bodies and join them into a prompt fragment.

    The only caller of ``load_body`` in the runtime: bodies are read when a prompt
    is actually built, for the skills that were actually selected, and never to
    answer "which skills exist?".
    """
    return _JOIN_SEPARATOR.join(source.load_body(name) for name in selection.names)
