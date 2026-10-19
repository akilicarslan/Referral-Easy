"""The artifacts §13 hashes, read from where they actually live.

This module is the *only* place that decides what "an agent's artifact" is, and the
decision is made by reading the shipped objects rather than by restating their shape:

======================================  ==========================================
§13 field                               read from
======================================  ==========================================
agent version                           ``registry/versions.json`` (declared)
agent hash                              ``cards.agent_hash(role)`` — the number the
                                        card itself publishes, not a second one
instruction version / hash              ``prompts.build_instruction`` plus the
                                        sub-agent directive the assembler appends
agent description                       the card's ``description``
tool description                        ``tools/specs.py`` ``ToolSpec`` — the text
                                        ADK turns into the model-visible function
                                        declaration
tool schema / implementation digest     the ``ToolSpec`` parameters, and the bytes
                                        of the handler ``inspect.getsource`` returns
skill version / hash                    ``packages/skills`` ``manifest.json`` and the
                                        package's own files
sub-agent manifest hash                 ``subagents.py``: its names, its tools, its two
                                        instructions and the module's own digest
A2A card hash                           the canonical JSON of the published card
======================================  ==========================================

Three deliberate choices, each of which someone will otherwise "fix" later:

**The card hash is the card's.** ``Agent.agent_hash`` is
``cards.agent_hash(role)`` — the value the Agent Card publishes over
``/.well-known/agent-card.json``. Recomputing it here would give the registry its own
opinion, and a registry whose agent hash disagrees with the hash a peer is handed is
worse than no registry. ``a2a_card_hash`` is the *other* number: the digest of the
whole card document.

**The instruction hash is the instruction the model is given.** The assembler appends
:data:`SUB_AGENT_DIRECTIVE` to Provider Matching's prompt
(``assembly.build_agent``), so this module appends it too, read from
``subagents`` at call time rather than copied. The card's ``agent_hash`` covers the
directive only indirectly (through the sub-agent *names*); this field covers it
directly. Recorded as a finding in ``docs/adr/ADR-0037``: the card hash has a gap, the
manifest does not, and the version gate keys on the manifest.

**A skill's version is not part of its content hash.** Bumping a version changes
nothing a model reads, so a version bump must not by itself make an agent look
changed. The hash is over the manifest *minus* ``version``, plus the instruction body
and every reference file the package ships.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from referraleasy_release_registry.errors import MissingArtifactError
from referraleasy_release_registry.hashing import digest, file_digest, text_digest

__all__ = [
    "AGENTS_ROLE_DIRS",
    "AGENTS_SRC",
    "AGENT_HASH_GAP",
    "SKILLS_SRC",
    "SkillArtifact",
    "SubAgentArtifact",
    "ToolArtifact",
    "agent_card_payload",
    "agent_hash",
    "agent_role_enum",
    "bridge_agents_package",
    "full_instruction",
    "load_module",
    "role_entry_points",
    "skill_artifacts",
    "sub_agent_manifest",
    "sub_agent_manifest_hash",
    "tool_artifacts",
]

#: Where the seven agent services live, relative to the repository root.
AGENTS_SRC: Final = "services/agents"

#: Where the eight skill packages live, relative to the repository root.
SKILLS_SRC: Final = "packages/skills"

#: The seven service directories, each of which declares its own ``ROLE``.
AGENTS_ROLE_DIRS: Final[tuple[str, ...]] = (
    "coordinator",
    "intake",
    "coverage",
    "matching",
    "readiness",
    "scheduling",
    "recovery",
)

#: Named so a reader of the code meets the caveat here, not only in an ADR. The Agent
#: Card's own ``agent_hash`` covers description, instruction, tools, skills and which
#: sub-agents exist — but **not** the sub-agent directive or the sub-agents' own
#: instructions, because ``cards._hash_payload`` never reads them. The manifest closes
#: that with ``instruction_hash`` and ``sub_agent_manifest_hash``, and the version gate
#: keys on the whole agent record. Renaming this constant is a deliberate act.
AGENT_HASH_GAP: Final = (
    "cards.agent_hash does not cover the sub-agent directive or the sub-agents' own "
    "instructions; instruction_hash and sub_agent_manifest_hash do"
)


def bridge_agents_package(root: Path) -> None:
    """Make ``import referraleasy_agents`` resolve, from any working directory.

    ``services/agents`` **is** a uv workspace member (root ``pyproject.toml``), so this is
    normally a no-op: ``uv run`` has already installed the package. The bridge exists for
    the two cases where it has not — a plain ``python scripts/check_release_versions.py``
    outside the virtualenv, and a checkout whose environment predates the last member being
    added — and it is the same one ``tests/agents/conftest.py`` documents.

    It adds the *directory that contains* the import package and nothing else, so a module
    that reaches outside the package still fails, and it is idempotent, so an environment
    that already resolves the package is unaffected.
    """
    entry = str(root / AGENTS_SRC)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def load_module(path: Path, *, name: str) -> ModuleType:
    """Load a Python file by path, under ``name``.

    Used for the seven ``<role>/__main__.py`` entry points: each declares
    ``ROLE = AgentRole.<X>``, which is the only statement in the tree that ties a
    directory to a role, and reading it beats restating the mapping here where it could
    drift. Executing the module body is safe — every one of them ends in
    ``if __name__ == "__main__": raise SystemExit(main(...))``, which does not run.
    """
    if not path.is_file():
        raise MissingArtifactError(f"no module at {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise MissingArtifactError(f"{path} is not importable as a module")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise MissingArtifactError(f"{path} could not be imported: {error}") from error
    return module


def rfc_module(name: str) -> ModuleType:
    """Import a module of ``referraleasy_agents``, naming it if it is absent."""
    try:
        return importlib.import_module(name)
    except ImportError as error:
        raise MissingArtifactError(
            f"{name} is not importable: {error}. If this is a plain checkout, call "
            "bridge_agents_package(repo_root) first."
        ) from error


def agent_role_enum() -> Any:
    """``referraleasy_agent_runtime.AgentRole``, imported rather than restated."""
    return importlib.import_module("referraleasy_agent_runtime.roles").AgentRole


def role_entry_points(root: Path) -> Mapping[Any, Path]:
    """``{AgentRole: path to its ``__main__.py``}``, read from each module's own ``ROLE``.

    A directory whose entry point declares no ``ROLE``, or a role with no directory, is
    a :class:`MissingArtifactError`: the manifest carries a per-role entry point, and a
    role nobody deploys has no artifact to version.
    """
    bridge_agents_package(root)
    enum = agent_role_enum()
    found: dict[Any, Path] = {}
    declared_dirs = sorted(
        entry.parent.name
        for entry in (root / AGENTS_SRC).glob("*/__main__.py")
        if entry.parent.name != "referraleasy_agents"
    )
    for directory in declared_dirs:
        path = root / AGENTS_SRC / directory / "__main__.py"
        module = load_module(path, name=f"referraleasy_registry_entry_{directory}")
        role = getattr(module, "ROLE", None)
        if role is None:
            raise MissingArtifactError(f"{path} declares no ROLE, so it cannot be attributed")
        found[role] = path
    missing = [role.value for role in enum if role not in found]
    if missing:
        raise MissingArtifactError(
            f"no entry point found for {', '.join(sorted(missing))} under {root / AGENTS_SRC}"
        )
    return found


# ── cards, prompts, tools ────────────────────────────────────────────────────────────


def agent_card_payload(role: Any) -> dict[str, Any]:
    """The published Agent Card document for ``role``, as JSON-ready data."""
    cards = rfc_module("referraleasy_agents.cards")
    return dict(cards.card_payload(role))


def agent_hash(role: Any) -> str:
    """The hash the card publishes for ``role`` — computed by the card module, not here."""
    cards = rfc_module("referraleasy_agents.cards")
    return str(cards.agent_hash(role))


def card_name(role: Any) -> str:
    """The protocol card name for ``role`` (``referral-intake``, hyphenated)."""
    cards = rfc_module("referraleasy_agents.cards")
    return str(cards.CARD_NAME_BY_ROLE[role])


def full_instruction(role: Any) -> str:
    """The instruction the model for ``role`` is actually given.

    Provider Matching's prompt is the role briefing **plus** the sub-agent directive the
    assembler appends; every other role's is the briefing alone. The directive is read
    from ``subagents`` at call time, so a test that patches it on the module moves this
    function's answer too — which is the only way "the directive is covered by the hash"
    can be demonstrated rather than asserted.
    """
    prompts = rfc_module("referraleasy_agents.prompts")
    base = str(prompts.build_instruction(role, skills=""))
    subagents = rfc_module("referraleasy_agents.subagents")
    matching_role = importlib.import_module(
        "referraleasy_agent_runtime.roles"
    ).AgentRole.PROVIDER_MATCHING
    if role is matching_role:
        return f"{base}\n{subagents.SUB_AGENT_DIRECTIVE}"
    return base


def _tool_specs() -> Mapping[str, Any]:
    specs = rfc_module("referraleasy_agents.tools.specs")
    return dict(specs.TOOL_SPECS)


@dataclass(frozen=True, slots=True)
class ToolArtifact:
    """One tool's two digests, plus the description a model reads.

    ``description_hash`` and ``schema_hash`` look redundant and are not. The
    description is prose ADK shows the model; the schema is the parameter list it fills
    in. A tool can change one without the other — a renamed parameter with the same
    sentence, or a reworded sentence with the same parameters — and §13 asks for both.
    """

    name: str
    description: str
    description_hash: str
    schema_hash: str
    implementation_digest: str
    source_file: str

    def as_contract(self) -> dict[str, str]:
        return {
            "schema_hash": self.schema_hash,
            "implementation_digest": self.implementation_digest,
        }


def _implementation_source(name: str) -> tuple[str, str]:
    """``(normalised source text, file path)`` for a tool handler.

    The handler's *own* function body, not its whole module: hashing
    ``tools/scheduling.py`` would make a comment added above ``list_slots`` invalidate
    ``create_booking``'s digest too, and a gate that fires on unchanged artefacts gets
    switched off. ``inspect.getsource`` is used rather than a byte range so a
    reformatting outside the function cannot move the digest.
    """
    handlers = rfc_module("referraleasy_agents.tools.handlers")
    handler = handlers.handler_for(name)
    try:
        source = inspect.getsource(handler)
        path = inspect.getsourcefile(handler) or str(getattr(handler, "__module__", "?"))
    except (OSError, TypeError) as error:
        raise MissingArtifactError(
            f"cannot read the implementation of tool {name!r}: {error}. §13 requires an "
            "implementation digest, and an unavailable one is not an empty one."
        ) from error
    # `textwrap.dedent` via inspect's own cleaning: leading indentation is a property of
    # where the function sits, not of what it does.
    lines = source.splitlines()
    if lines:
        indent = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
        source = "\n".join(line[indent:] if line.strip() else "" for line in lines)
    return source, str(path)


def tool_artifacts() -> Mapping[str, ToolArtifact]:
    """Every §3 tool, keyed by the name the allowlist, the policy table and the cards use."""
    specs = _tool_specs()
    if not specs:
        raise MissingArtifactError("the tool table is empty; there is nothing to digest")
    artifacts: dict[str, ToolArtifact] = {}
    for name, spec in sorted(specs.items()):
        parameters = [
            {
                "name": parameter.name,
                "type": parameter.annotation_text(),
                "description": parameter.description,
                "required": parameter.required,
            }
            for parameter in spec.parameters
        ]
        source, path = _implementation_source(name)
        artifacts[name] = ToolArtifact(
            name=name,
            description=spec.summary,
            description_hash=text_digest(spec.summary),
            schema_hash=digest({"name": name, "parameters": parameters}),
            implementation_digest=text_digest(source),
            source_file=path,
        )
    return artifacts


# ── skills ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SkillArtifact:
    """One skill package: its declared version, its content hash, and its owner."""

    name: str
    version: str
    owner_agent: str
    content_hash: str
    agent_card_skill_id: str
    files: tuple[str, ...]

    def content_payload(self) -> dict[str, Any]:
        """What :attr:`content_hash` is taken over — exposed so a test can move one part."""
        return {"name": self.name, "content_hash": self.content_hash}


def skill_artifacts(root: Path | None = None) -> Mapping[str, SkillArtifact]:
    """Every skill package under ``packages/skills``, keyed by package name.

    Read through the package's own loader (``referraleasy_skills``), so the eight
    packages remain the loader's business and this module never re-implements
    ``manifest.json`` validation.

    ``root`` is the checkout being described. It matters because the loader resolves the
    skills directory from **its own installed location** — ``packages/skills`` in whatever
    tree ``referraleasy_skills`` was imported from — and has no root parameter. Building a
    manifest for a *copy* of the tree while hashing the original's ``SKILL.md`` files would
    be the same tree-labelling bug :func:`bridge_agents_package` exists to prevent: a
    release that says "these are the skills" about skills it never read. When ``root`` is
    given, its ``packages/skills`` must exist — falling back to the installed copy would
    hash a tree the manifest does not describe, and the omission would be invisible at
    report time. The override is the loader's own documented environment variable, set and
    restored around the reads rather than left behind, so importing this module has no
    side effects on an embedding process.

    Without ``root`` the loader's own resolution stands, which is what a caller who has no
    checkout in hand (a reader of the installed distribution) wants.
    """
    loader = importlib.import_module("referraleasy_skills")
    with _skills_root_from(root):
        names: Sequence[str] = list(loader.list_skills())
        if not names:
            raise MissingArtifactError("no skill packages found; is packages/skills on the path?")
        artifacts: dict[str, SkillArtifact] = {}
        for name in names:
            manifest = loader.load_manifest(name)
            skill = loader.load_skill(name)
            manifest_content = {
                key: value for key, value in manifest.raw.items() if key != "version"
            }
            references = {path: skill.references[path] for path in sorted(skill.references)}
            content_hash = digest(
                {
                    "manifest": manifest_content,
                    "instructions": skill.instructions,
                    "references": references,
                }
            )
            files = (
                loader.MANIFEST_FILENAME,
                loader.INSTRUCTIONS_FILENAME,
                *sorted(f"references/{path}" for path in skill.references),
            )
            artifacts[name] = SkillArtifact(
                name=manifest.name,
                version=manifest.version,
                owner_agent=manifest.owner_agent,
                content_hash=content_hash,
                agent_card_skill_id=manifest.agent_card_skill_id,
                files=tuple(files),
            )
        return artifacts


@contextmanager
def _skills_root_from(root: Path | None) -> Iterator[None]:
    """Point the skills loader at ``root/packages/skills`` for the duration of the block."""
    if root is None:
        yield
        return
    loader = importlib.import_module("referraleasy_skills.loader")
    override: str = loader.SKILLS_ROOT_ENV
    packages = root / SKILLS_SRC
    if not packages.is_dir():
        raise MissingArtifactError(
            f"no {SKILLS_SRC} under {root}; the manifest records each skill's hash and a build "
            "must not read the installed tree's skills while describing this one"
        )
    previous = os.environ.get(override)
    os.environ[override] = str(packages)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(override, None)
        else:
            os.environ[override] = previous


# ── sub-agents ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SubAgentArtifact:
    """One local ADK sub-agent, as the parent's dependency hash sees it."""

    node_name: str
    protocol_name: str
    description: str
    instruction_hash: str
    tools: tuple[str, ...]
    output_key: str

    def as_record(self) -> dict[str, Any]:
        return {
            "node_name": self.node_name,
            "protocol_name": self.protocol_name,
            "description": self.description,
            "instruction_hash": self.instruction_hash,
            "tools": list(self.tools),
            "output_key": self.output_key,
        }


def _subagent_record(node_name: str, module: ModuleType) -> SubAgentArtifact:
    """One sub-agent's manifest, read from the ``Agent(...)`` call that builds it.

    Read from the builder rather than by guessing constant names. An earlier version of
    this function looked up ``_CANDIDATE_RESEARCHER_INSTRUCTION`` — a name that does not
    exist, because the module calls it ``_RESEARCHER_INSTRUCTION`` — and the failure is
    the lesson: a manifest that names an artifact by a scheme it invented will break the
    next time someone renames a constant, and it will break by refusing, which is at least
    loud. Walking the call site has no scheme to get wrong.

    The alternative, building the ADK ``Agent`` objects and reading their attributes,
    needs a live ``ToolSet`` bound to an allowlist, a policy backend and a transport — a
    manifest build would then depend on a database and a model provider.
    """
    keywords = _agent_call_keywords(node_name, module)
    instruction = keywords.get("instruction")
    if not isinstance(instruction, str) or not instruction:
        raise MissingArtifactError(
            f"the Agent(...) in subagents.py's {node_name} passes no readable instruction; "
            "§13 hashes the sub-agent's instruction into its parent's dependency hash, and an "
            "instruction that cannot be read is not an empty one"
        )
    tools = keywords.get("tools")
    if not isinstance(tools, (list, tuple)) or not tools:
        raise MissingArtifactError(
            f"the Agent(...) in subagents.py's {node_name} passes no readable tool list"
        )
    output_key = keywords.get("output_key")
    if not isinstance(output_key, str) or not output_key:
        raise MissingArtifactError(
            f"the Agent(...) in subagents.py's {node_name} passes no output_key; the "
            "sub-agent's state key is part of what its parent depends on"
        )
    return SubAgentArtifact(
        node_name=node_name,
        protocol_name=str(module.protocol_name(node_name)),
        description=str(keywords.get("description") or ""),
        instruction_hash=text_digest(instruction),
        tools=tuple(str(tool) for tool in tools),
        output_key=output_key,
    )


#: The ``Agent(...)`` keywords §13's sub-agent manifest is made of. The rest — ``model``,
#: ``name``, ``output_key``'s siblings — are either injected at call time from the parent's
#: ``ToolSet``/``BaseLlm`` or already covered by the *global* provider block, so they are
#: skipped rather than resolved. Two reasons, both practical: a builder's
#: ``model=model`` argument is a function parameter no module-level lookup can reach, and a
#: manifest that refused every sub-agent because of it would be useless.
_SUBAGENT_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"description", "instruction", "tools", "output_key"}
)


def _agent_call_keywords(node_name: str, module: ModuleType) -> Mapping[str, Any]:
    """The resolved keyword arguments of the ``Agent(...)`` call in ``node_name``.

    ``ast`` rather than a regex, and a name lookup rather than a literal-only check: the
    builders pass module constants (``instruction=_RESEARCHER_INSTRUCTION``), so a parser
    that only understood literals would report every sub-agent as unreadable.

    Only :data:`_SUBAGENT_KEYWORDS` are resolved, and one of those that cannot be resolved
    is refused rather than skipped — §13's hash must cover the description, the instruction
    and the tool list, and a value this function cannot evaluate is a value the hash would
    silently not cover.
    """
    import ast
    import textwrap

    builder = getattr(module, node_name, None)
    if builder is None:
        raise MissingArtifactError(f"subagents.py declares no builder {node_name!r}")
    declared = frozenset(_tool_specs())
    tree = ast.parse(textwrap.dedent(inspect.getsource(builder)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Agent":
            keywords = {
                str(keyword.arg): keyword.value
                for keyword in node.keywords
                if keyword.arg in _SUBAGENT_KEYWORDS
            }
            resolved: dict[str, Any] = {}
            for key, value in keywords.items():
                if key == "tools":
                    resolved[key] = _subagent_tools(value, module, node_name, declared)
                else:
                    resolved[key] = _resolve_ast_value(value, module, node_name)
            return resolved
    raise MissingArtifactError(
        f"no Agent(...) call found in subagents.py's {node_name}; the sub-agent manifest "
        "cannot describe a sub-agent whose construction cannot be read"
    )


def _subagent_tools(
    node: Any, module: ModuleType, where: str, declared: frozenset[str]
) -> tuple[str, ...]:
    """The tools a sub-agent is handed, named by the tool table rather than by the call.

    The builders do not pass a literal list: they pass
    ``tools=_tools_named(tools, RESEARCHER_TOOLS, owner=...)`` — a call whose first
    argument is a ``ToolSet`` injected at runtime and whose second is the constant that
    names what the helper will bind. So the strings are collected from the expression and
    then intersected with the declared tool table.

    Intersecting is what makes this a reading rather than a guess. The alternative —
    accepting every string in the expression — would put the ``owner=`` argument into the
    tool list; the alternative on the other side, refusing the call because one of its
    arguments is a parameter, would make every sub-agent unreadable. The tool table is the
    authority on what a tool name *is*.
    """
    names = sorted({name for name in _collect_strings(node, module) if name in declared})
    if not names:
        raise MissingArtifactError(
            f"the tools expression in subagents.py's {where} resolves to no tool this project "
            f"declares; §13's sub-agent manifest hash must cover the tool list"
        )
    return tuple(names)


def _collect_strings(node: Any, module: ModuleType) -> set[str]:
    """Every literal string reachable in ``node`` without executing anything.

    A name that the module does not define is skipped rather than refused: it is a
    runtime injection (a ``ToolSet``, a ``BaseLlm``), and it is not a string this hash
    could have covered. A literal is taken as it is.
    """
    import ast

    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.Name):
        try:
            resolved = getattr(module, node.id)
        except AttributeError:
            return set()
        if isinstance(resolved, str):
            return {resolved}
        if isinstance(resolved, (list, tuple, frozenset)):
            return {item for item in resolved if isinstance(item, str)}
        return set()
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        found: set[str] = set()
        for element in node.elts:
            found |= _collect_strings(element, module)
        return found
    if isinstance(node, ast.Call):
        found = set()
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            found |= _collect_strings(argument, module)
        return found
    return set()


def _resolve_ast_value(node: Any, module: ModuleType, where: str) -> Any:
    """A literal, a module constant, or a list/tuple/dict of those — never a guess."""
    import ast

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        resolved = getattr(module, node.id, None)
        if resolved is None:
            raise MissingArtifactError(
                f"{where} passes {node.id!r}, which subagents.py does not define"
            )
        return resolved
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_resolve_ast_value(element, module, where) for element in node.elts]
    if isinstance(node, ast.Dict):
        return {
            _resolve_ast_value(key, module, where): _resolve_ast_value(value, module, where)
            for key, value in zip(node.keys, node.values, strict=True)
        }
    if isinstance(node, ast.JoinedStr):
        # An f-string is what the care coordinator's instruction uses. Its parts are
        # resolvable when they are literals or names, and that is enough for the two
        # sub-agents; anything else is refused rather than approximated.
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Name):
                parts.append(str(getattr(module, value.value.id, "")))
            else:
                raise MissingArtifactError(
                    f"{where} builds a string this module cannot evaluate; §13's hash must "
                    "cover it exactly, and an approximation would not"
                )
        return "".join(parts)
    raise MissingArtifactError(
        f"{where} passes a {type(node).__name__} that cannot be resolved to a value; §13's "
        "sub-agent manifest hash may not silently omit it"
    )


def sub_agent_manifest(role: Any) -> Mapping[str, Any] | None:
    """The sub-agent manifest for ``role``, or ``None`` when it has no sub-agents.

    ``None`` is a *statement* here, not a gap: §3 gives sub-agents to Provider Matching
    and to no one else, so for six roles the answer is "there are none". It is never
    returned by accident — :func:`sub_agent_manifest_hash` turns it into a digest of the
    empty list, so an agent that lost its sub-agents cannot hash like one that never
    had them.
    """
    cards = rfc_module("referraleasy_agents.cards")
    declared = cards.SUB_AGENT_NAMES.get(role)
    if not declared:
        return None
    module = rfc_module("referraleasy_agents.subagents")
    node_names = tuple(module.sub_agent_names())
    records = [_subagent_record(node_name, module) for node_name in node_names]
    protocol = tuple(str(name) for name in declared)
    if tuple(record.protocol_name for record in records) != protocol:
        raise MissingArtifactError(
            f"the card declares sub-agents {protocol} but subagents.py builds "
            f"{tuple(record.protocol_name for record in records)}"
        )
    module_path = Path(str(module.__file__))
    return {
        "sub_agents": [record.as_record() for record in records],
        "directive_hash": text_digest(str(module.SUB_AGENT_DIRECTIVE)),
        "module_digest": file_digest(module_path),
    }


def sub_agent_manifest_hash(role: Any) -> str:
    """The 64-hex dependency hash §13 requires the parent to carry.

    Always a digest. For the six roles §3 gives no sub-agents the hash is taken over the
    canonical empty list, so "no sub-agents" and "the sub-agent manifest failed to load"
    are different values rather than both ``None``.
    """
    return digest(sub_agent_manifest(role) or {"sub_agents": [], "module_digest": None})


def env_snapshot(env: Mapping[str, str], keys: Iterable[str]) -> Mapping[str, str]:
    """The named environment keys and their values, refusing to return a partial set.

    A whitelist, never a dump: the process environment carries credentials (this
    repository's ``.env`` holds a live provider key), and §5's manifest is stored, hashed
    and served to an admin page. A missing key is an error, because a manifest that
    quietly records an empty model id describes a deployment nobody ran.
    """
    missing = [key for key in keys if not env.get(key, "").strip()]
    if missing:
        raise MissingArtifactError(
            f"the environment does not define {', '.join(sorted(missing))}; §13's manifest "
            "records the resolved provider and model, and an unset one is not a value"
        )
    return {key: env[key].strip() for key in keys}


def environ() -> Mapping[str, str]:
    """The process environment — injectable everywhere else, wrapped once here."""
    return os.environ
