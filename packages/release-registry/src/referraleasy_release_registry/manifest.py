"""§13's ``release_manifest``: built from real artifacts, and refused when incomplete.

The contract already exists — ``referraleasy_contracts.generated.workflow.ReleaseManifest``,
generated from ``packages/contracts/schemas/workflow.json`` — so this module's job is not
to invent a shape but to *fill* one, from the tree, and to refuse when it cannot. The
mapping is one-to-one with §13's list:

=====================================  =================================================
§13 field                              manifest field
=====================================  =================================================
agent version/hash                     ``agents[].version`` / ``agents[].agent_hash``
instruction/prompt version             ``agents[].instruction_hash``, ``prompts``
agent and tool descriptions            ``agents[].agent_hash`` (covers the description);
                                       ``agents[].tool_description_hashes``
tool schema and implementation digest  ``tools[<name>].schema_hash`` / ``.implementation_digest``
skill version/hash                     ``agents[].skill_hashes``, ``skills``
sub-agent manifest hash                ``agents[].sub_agent_manifest_hash``
A2A card hash                          ``agents[].a2a_card_hash``
provider/model/params                  ``provider``
guardrail policy/classifier/threshold  ``guardrail_policy``
RAG/embedding/index/reranker/knowledge ``rag``
Git SHA/image/lockfile hash            ``source``
=====================================  =================================================

Two hashes, and the difference matters
--------------------------------------
``artifact_hash`` is taken over the manifest **with the release metadata removed** —
``release_manifest_id``, ``created_at``, ``parent_release_manifest_id`` and ``rollout``.
It is what "the same inputs produce the same release" means, and it is what the version
gate compares between two builds.

``manifest_hash`` is taken over the whole document as stored, which is what the
``release_registry.manifest_hash`` column documents: *"sha256 hex of the manifest as
deployed. If it changes, the artifact changed."* Two builds of the same commit produce
the same ``artifact_hash`` and different ``manifest_hash`` values, because they are two
releases; that is not a bug and is asserted by a test so nobody "fixes" it.

``rollout`` is excluded from ``artifact_hash`` on the same principle: a canary moving
from 5% to 25% is a deployment decision about an artifact, not a change to the artifact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from referraleasy_contracts.generated import common
from referraleasy_contracts.generated.workflow import (
    Agent,
    GuardrailPolicy,
    Provider,
    Rag,
    ReleaseManifest,
    Rollout,
    Source,
    Tools,
)
from referraleasy_release_registry.artifacts import (
    SkillArtifact,
    ToolArtifact,
    agent_card_payload,
    agent_hash,
    card_name,
    full_instruction,
    rfc_module,
    role_entry_points,
    skill_artifacts,
    sub_agent_manifest_hash,
    tool_artifacts,
)
from referraleasy_release_registry.errors import (
    IncompleteManifestError,
    MissingArtifactError,
    VersionLedgerError,
)
from referraleasy_release_registry.hashing import digest, file_digest, is_sha256_hex, text_digest
from referraleasy_release_registry.versions import VersionLedger, parse_semver

__all__ = [
    "GIT_SHA_ENV",
    "IMAGE_DIGEST_ENV",
    "LOCKFILES",
    "RELEASE_METADATA_FIELDS",
    "RELEASE_PROVENANCE_FIELDS",
    "BuildInputs",
    "RolloutFacts",
    "agent_artifact_hash",
    "artifact_hash",
    "assert_complete",
    "build_manifest",
    "manifest_components",
    "manifest_hash",
    "release_manifest_id",
    "required_field",
    "rollout_facts",
    "source_from",
]

#: Where a git sha comes from, in priority order: an explicit release pin, then the
#: convention CI uses, then the working tree's own HEAD. Nothing else — a manifest that
#: invented a sha would make every ``git_sha`` in the registry a guess.
GIT_SHA_ENV: Final[tuple[str, ...]] = ("RELEASE_GIT_SHA", "GIT_SHA")

#: The image digest is supplied by whatever built the image. There is no other source:
#: a digest is not derivable from a Dockerfile, and a tag is mutable, so an absent one
#: is reported as a finding rather than substituted with the tag.
IMAGE_DIGEST_ENV: Final = "RELEASE_IMAGE_DIGEST"

#: §13's "lockfile hash". Both lockfiles: the Python resolution and the Node one are
#: each capable of changing behaviour without a source file moving.
LOCKFILES: Final[tuple[str, ...]] = ("uv.lock", "pnpm-lock.yaml")

#: The release-level fields that are *about* a release rather than part of one.
RELEASE_METADATA_FIELDS: Final[tuple[str, ...]] = (
    "release_manifest_id",
    "created_at",
    "parent_release_manifest_id",
    "rollout",
)

#: Fields *inside* ``source`` that record how this build was produced rather than what it contains.
#:
#: Separate from :data:`RELEASE_METADATA_FIELDS` rather than folded into it because the two say
#: different things and the tests pin them separately: those four are about the release *event*, and
#: these two are about the release's *provenance*. Both are excluded from the content hash, and
#: `source.lockfile_hash` is deliberately in neither — see :func:`artifact_hash`.
RELEASE_PROVENANCE_FIELDS: Final[tuple[str, ...]] = ("git_sha", "image_digest")


@dataclass(frozen=True, slots=True)
class BuildInputs:
    """Everything :func:`build_manifest` needs that is not the tree itself.

    Passed explicitly rather than read from the ambient environment so that a test can
    build two manifests from two different "environments" in one process, and so that
    nothing in the build depends on the order a mapping happened to iterate.
    """

    ledger: VersionLedger
    env: Mapping[str, str]
    repo_root: Path
    built_at: datetime
    rollout: Rollout
    parent_release_manifest_id: str | None = None


@dataclass(frozen=True, slots=True)
class RolloutFacts:
    """One release's rollout, as the three values routing and reporting need.

    A plain value rather than the contract's ``Rollout``: every field of that model is
    optional, so handing it to a caller puts "which cohort is this" one ``.cohort`` away
    from a ``None`` that type-checks and routes wrongly.
    """

    cohort: str
    share: float
    kill_switch_engaged: bool


# ── ids and hashes ──────────────────────────────────────────────────────────────────


def release_manifest_id(artifact: str, *, built_at: datetime) -> str:
    """``rel-<YYYYMMDD>-<first seven of the artifact hash>``.

    The shape is the contract's own (``^rel-[0-9]{8}-[a-z0-9]{7}$``, *"Immutable release
    identifier (§13). Conversations are pinned to one."*). Date plus content prefix, so
    an id is readable in a log and still content-derived; two builds of the same tree on
    one day produce the same id, and ``release_registry_release_id_key`` then refuses the
    second registration. That refusal is correct — it is the same release.
    """
    if not is_sha256_hex(artifact):
        raise IncompleteManifestError(f"not a sha256 hex digest: {artifact!r}")
    return f"rel-{built_at.astimezone(UTC).strftime('%Y%m%d')}-{artifact[:7]}"


def artifact_hash(manifest: ReleaseManifest) -> str:
    """The release's content hash: what the release *is*, not where or when it was built.

    Deterministic across rebuilds of the same inputs, which is the property the version gate and the
    determinism tests both rely on — and the property that was, until now, only *almost* true: the
    `source` block was inside the hash, and two of its three fields cannot be stable.

    * ``source.git_sha`` moves on every commit. Inside the content hash that means **every commit
      demands a version bump**, which is not what §13 asks for: the gate exists so a changed
      *artifact* advances its version, and a commit that touches a README changes none.
    * ``source.image_digest`` is assigned by whatever built the image. Two builds of one tree produce
      two digests — the ADR-0037 measurement recorded a *local* image id, which is exactly the kind
      of value that differs per build — so leaving it in makes every rebuild look like a content
      change. ``gate.py`` says where that ends: "A gate that fired on a rebuild would be turned off
      within a week, and the check that protects the release would go with it."

    ``source.lockfile_hash`` **stays**. A dependency resolution is content: it can change behaviour
    without a source file moving, which is why §13 records it at all.

    The three fields are removed by path rather than by excluding `source` wholesale, so the removal
    is one reviewable list and a fourth provenance field added later is visibly not in it.
    """
    payload = manifest.model_dump(mode="json")
    for field in RELEASE_METADATA_FIELDS:
        payload.pop(field, None)
    source = payload.get("source")
    if isinstance(source, dict):
        for field in RELEASE_PROVENANCE_FIELDS:
            source.pop(field, None)
    return digest(payload)


def manifest_hash(manifest: ReleaseManifest) -> str:
    """The hash of the manifest **as stored** — the ``release_registry.manifest_hash``."""
    return digest(manifest.model_dump(mode="json"))


def required_field[T](value: T | None, *, field: str, where: str) -> T:
    """A field §13 requires, **refused when absent** rather than defaulted.

    The generated contract marks most fields optional — it has to, because it also
    describes documents a builder is still filling in — so a manifest read back from the
    registry table or from ``registry/releases/<id>.json`` type-checks with ``None`` in
    places this package's own reader cannot work with. Defaulting one of those to ``""``
    would produce a record that looks complete and describes nothing, which is precisely
    the failure §13's "a field that is missing is worse than an error" rule names. So the
    read sites below ask for the value and get an error naming the field instead.
    """
    if value is None:
        raise IncompleteManifestError(
            f"{where}.{field} is absent; §13's manifest carries it and a document that lost "
            "it cannot be resolved into effective versions"
        )
    return value


def rollout_facts(manifest: ReleaseManifest) -> RolloutFacts:
    """A manifest's rollout as plain values: cohort name, share, kill switch.

    Read through :func:`required_field`, so a manifest that carries no rollout — or a
    rollout missing its cohort — is refused here rather than routed on a default. Routing
    on an absent cohort would put a release in ``stable`` because nobody said otherwise,
    and "defaulted to stable" and "declared stable" are not the same statement.
    """
    where = f"release {manifest.release_manifest_id.root}"
    rollout = required_field(manifest.rollout, field="rollout", where=where)
    cohort = required_field(rollout.cohort, field="rollout.cohort", where=where)
    share = required_field(
        rollout.cohort_percentage, field="rollout.cohort_percentage", where=where
    )
    return RolloutFacts(
        cohort=str(cohort.value),
        share=float(share),
        kill_switch_engaged=bool(rollout.kill_switch_engaged),
    )


def agent_artifact_hash(agent: Agent) -> str:
    """One agent's whole §13 record, as a single digest. The version gate's unit.

    Not the same number as ``agent.agent_hash``, and deliberately so.
    ``agent_hash`` is the card's published value; this covers *every* per-agent field,
    including the two the card's own hash does not reach (``instruction_hash`` and
    ``sub_agent_manifest_hash``, see ``artifacts.AGENT_HASH_GAP``). Keying the gate on
    the whole record is what makes "if a sub-agent changes, the parent rises" true even
    for the parts of a sub-agent the card hash never saw.
    """
    return digest(agent.model_dump(mode="json"))


# ── building ────────────────────────────────────────────────────────────────────────


def _agents(
    inputs: BuildInputs, skills: Mapping[str, SkillArtifact], tools: Mapping[str, ToolArtifact]
) -> list[Agent]:
    """One :class:`Agent` per §3 role, in the enum's order so the manifest is stable."""
    enum = rfc_module("referraleasy_agent_runtime.roles").AgentRole
    allowed = rfc_module("referraleasy_agent_runtime.roles").allowed_tools
    entry_points = role_entry_points(inputs.repo_root)
    records: list[Agent] = []
    for role in enum:
        if role not in entry_points:
            raise MissingArtifactError(f"no service entry point for {role.value}")
        role_skills = sorted(
            (skill for skill in skills.values() if skill.owner_agent == role.value),
            key=lambda skill: skill.agent_card_skill_id,
        )
        tool_names = sorted(allowed(role))
        missing_tools = [name for name in tool_names if name not in tools]
        if missing_tools:
            raise MissingArtifactError(
                f"{role.value} may call {missing_tools}, which have no tool artifact"
            )
        records.append(
            Agent(
                agent_name=card_name(role),
                version=inputs.ledger.agent(role.value),
                agent_hash=agent_hash(role),
                instruction_hash=text_digest(full_instruction(role)),
                tool_description_hashes={name: tools[name].description_hash for name in tool_names},
                # Keyed by the card's own skill id, so this mapping can be read beside the
                # published card without a translation step.
                skill_hashes={
                    skill.agent_card_skill_id: skill.content_hash for skill in role_skills
                },
                sub_agent_manifest_hash=sub_agent_manifest_hash(role),
                a2a_card_hash=digest(agent_card_payload(role)),
            )
        )
    return records


def _prompts(ledger: VersionLedger) -> dict[str, str]:
    """``{card name: declared prompt version}``.

    Keyed by card name, not by role code: ``prompts`` is read next to ``agents``, whose
    names are the protocol's, and a reader comparing the two should not have to know that
    ``provider_matching`` and ``provider-matching`` are one thing.
    """
    enum = rfc_module("referraleasy_agent_runtime.roles").AgentRole
    return {card_name(role): ledger.prompt(role.value) for role in enum}


def _skills(skills: Mapping[str, SkillArtifact], ledger: VersionLedger) -> dict[str, str]:
    """``{skill package: version}``, with the ledger checked against each manifest.

    The check is the point: a skill already declares its version (ADR-0028), so the
    ledger agreeing with it is what keeps the two from becoming two answers.
    """
    declared = dict(ledger.skills)
    missing = sorted(set(skills) - set(declared))
    extra = sorted(set(declared) - set(skills))
    if missing or extra:
        raise VersionLedgerError(
            "the version ledger and the shipped skill packages disagree: "
            f"undeclared={missing or '[]'} unknown={extra or '[]'}"
        )
    for name, artifact in skills.items():
        if declared[name] != artifact.version:
            raise VersionLedgerError(
                f"skill {name!r} is declared {declared[name]!r} in the ledger and "
                f"{artifact.version!r} in its own manifest.json; §13 has one version per artifact"
            )
    return {name: artifact.version for name, artifact in skills.items()}


def _provider(env: Mapping[str, str]) -> Provider:
    """§8's provider block, read from the same variables the runtime resolves."""
    from referraleasy_release_registry.artifacts import env_snapshot

    core = env_snapshot(env, ("AGENT_MODEL_PROVIDER", "AGENT_MODEL_ID"))
    budget_keys = (
        "MODEL_BACKEND",
        "MAX_MODEL_CALLS_PER_TURN",
        "MAX_TOOL_CALLS_PER_TURN",
        "AGENT_ATTEMPT_TIMEOUT_MS",
        "AGENT_MAX_ATTEMPTS",
        "TURN_DEADLINE_MS",
        "MAX_DELEGATIONS_PER_TURN",
    )
    params: dict[str, Any] = dict(env_snapshot(env, budget_keys))
    params.update(_json_object(env.get("AGENT_MODEL_PARAMS", ""), name="AGENT_MODEL_PARAMS"))
    overrides = _json_object(env.get("AGENT_MODEL_OVERRIDES", ""), name="AGENT_MODEL_OVERRIDES")
    return Provider(
        agent_model_provider=core["AGENT_MODEL_PROVIDER"],
        agent_model_id=core["AGENT_MODEL_ID"],
        params=params,
        # `{}` rather than `None`: "no per-agent overrides" is a statement, and `None` is
        # indistinguishable from a producer that forgot to look (§8 records them, because
        # an override hidden in the environment is an unmeasured release).
        per_agent_overrides={str(k): dict(v) for k, v in overrides.items()} if overrides else {},
    )


def _json_object(raw: str, *, name: str) -> dict[str, Any]:
    """Parse a JSON object from the environment, or ``{}`` when it is unset.

    Malformed JSON is an error rather than an empty mapping: the runtime refuses it too
    (``model._json_object``), and a manifest that recorded "no params" for an
    unparseable value would describe a deployment that could not have started.
    """
    import json

    text = raw.strip()
    if not text:
        return {}
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError as error:
        raise MissingArtifactError(f"{name} is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise MissingArtifactError(f"{name} must be a JSON object, not {type(parsed).__name__}")
    return dict(parsed)


def _guardrail_policy(env: Mapping[str, str], repo_root: Path) -> GuardrailPolicy:
    """§9's policy block: mode, the classifier's artifact hash, and the recorded thresholds.

    The classifier hash covers *both* halves §9 names — the artifact pin
    (``GUARDRAIL_LOCAL_MODEL``, which carries its own revision) and the code that applies
    it — because a threshold change and a code change have the same effect on a decision
    and must therefore have the same effect on the hash.

    The threshold value is read from the environment but is *not* trusted as an
    independent number here: ADR-0005's rule is that the environment must equal the
    recorded measurement, and this module records what the deployment will enforce.
    """
    from referraleasy_release_registry.artifacts import env_snapshot

    core = env_snapshot(
        env, ("GUARDRAIL_MODE", "GUARDRAIL_LOCAL_MODEL", "GUARDRAIL_LOCAL_THRESHOLD")
    )
    classifier_source = (
        repo_root
        / "packages"
        / "guardrails"
        / "src"
        / "referraleasy_guardrails"
        / "layer_classifier.py"
    )
    if not classifier_source.is_file():
        raise MissingArtifactError(
            f"no guardrail classifier at {classifier_source}; §9 requires a recorded artifact "
            "hash, and a hash of nothing would look identical for every release"
        )
    try:
        threshold = float(core["GUARDRAIL_LOCAL_THRESHOLD"])
    except ValueError as error:
        raise MissingArtifactError(
            f"GUARDRAIL_LOCAL_THRESHOLD={core['GUARDRAIL_LOCAL_THRESHOLD']!r} is not a number"
        ) from error
    return GuardrailPolicy(
        mode=core["GUARDRAIL_MODE"],
        classifier_hash=digest(
            {
                "artifact": core["GUARDRAIL_LOCAL_MODEL"],
                "implementation": file_digest(classifier_source),
            }
        ),
        thresholds={"local_classifier": threshold},
    )


def _rag(env: Mapping[str, str], repo_root: Path) -> Rag:
    """§10's retrieval block.

    ``knowledge_version`` is read from the seed that produced the index
    (``packages/rag/.../data/seed_directory.v1.json``), not invented: §10's rule is that a
    knowledge version moves with the content, and the only place that knows it is the
    seed itself.
    """
    import json

    from referraleasy_release_registry.artifacts import env_snapshot

    core = env_snapshot(env, ("EMBEDDING_MODEL_ID", "EMBEDDING_DIMENSIONS", "RERANK_MODEL_ID"))
    seed_path = (
        repo_root
        / "packages"
        / "rag"
        / "src"
        / "referraleasy_rag"
        / "data"
        / "seed_directory.v1.json"
    )
    if not seed_path.is_file():
        raise MissingArtifactError(f"no directory seed at {seed_path}")
    seed: Any = json.loads(seed_path.read_text(encoding="utf-8"))
    if not isinstance(seed, dict) or not isinstance(seed.get("knowledge_version"), str):
        raise MissingArtifactError(f"{seed_path} carries no knowledge_version")
    try:
        dimensions = int(core["EMBEDDING_DIMENSIONS"])
    except ValueError as error:
        raise MissingArtifactError(
            f"EMBEDDING_DIMENSIONS={core['EMBEDDING_DIMENSIONS']!r} is not an integer"
        ) from error
    index_name = env.get("DIRECTORY_INDEX_NAME", "").strip() or _default_index_name()
    return Rag(
        embedding_model_id=core["EMBEDDING_MODEL_ID"],
        embedding_dimensions=dimensions,
        index_name=index_name,
        reranker_model_id=core["RERANK_MODEL_ID"],
        knowledge_version=str(seed["knowledge_version"]),
    )


def _assert_schema_agrees(ledger: VersionLedger) -> None:
    """The ledger's ``schema`` must name the contract generation actually installed.

    §13 hashes a manifest that is itself an instance of the workflow contract, so a
    registry entry is only readable if it says which generation it is an instance of.
    Saying so in a file nothing checks is decoration; this compares it against the real
    ``CONTRACT_VERSION`` of the installed contracts package, which is the authority.
    """
    import referraleasy_contracts

    installed = str(referraleasy_contracts.CONTRACT_VERSION)
    if ledger.schema != installed:
        raise VersionLedgerError(
            f"the version ledger declares releases are expressed in contract "
            f"{ledger.schema!r}, but the installed referraleasy-contracts is {installed!r}. "
            "The ledger is describing a schema this checkout does not have. Update "
            "registry/versions.json deliberately, as part of the contract change."
        )


def _default_index_name() -> str:
    """``referraleasy_rag.config.DEFAULT_INDEX_NAME``, so the manifest and the index agree."""
    config = rfc_module("referraleasy_rag.config")
    return str(config.DEFAULT_INDEX_NAME)


def source_from(env: Mapping[str, str], repo_root: Path) -> Source:
    """§13's ``source`` block: git sha, image digest, lockfile hash.

    Both of the first two are *supplied*, never derived, and an absent one raises. There
    is no honest default: a git sha cannot be computed from a working tree, and a tag is
    mutable so it is not a digest. When neither the environment nor the repository can
    answer, the build stops and says which value is missing.
    """
    lockfile_digests = {
        name: file_digest(repo_root / name) for name in LOCKFILES if (repo_root / name).is_file()
    }
    if not lockfile_digests:
        raise MissingArtifactError(
            f"none of {', '.join(LOCKFILES)} is present under {repo_root}; §13 asks for the "
            "resolved dependency set, and an empty one is not the same as an unhashed one"
        )
    git_sha = _git_sha(env, repo_root)
    image_digest = env.get(IMAGE_DIGEST_ENV, "").strip()
    if not image_digest:
        raise MissingArtifactError(
            f"{IMAGE_DIGEST_ENV} is not set. §13 records the image digest; a tag is mutable and "
            "a fabricated digest would make every entry in the registry a guess. Set "
            f"{IMAGE_DIGEST_ENV} to the digest CI recorded when it built the image."
        )
    return Source(
        git_sha=git_sha, image_digest=image_digest, lockfile_hash=digest(lockfile_digests)
    )


def _git_sha(env: Mapping[str, str], repo_root: Path) -> str:
    """The commit the release was built from, or a refusal naming where to get one."""
    for key in GIT_SHA_ENV:
        value = env.get(key, "").strip()
        if value:
            return value
    import subprocess

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise MissingArtifactError(
            f"no git sha available: neither {GIT_SHA_ENV[0]} nor {GIT_SHA_ENV[1]} is set, and "
            f"`git rev-parse HEAD` failed in {repo_root} ({error}). §13 records the commit; a "
            "working tree with no commit has no sha to record, and inventing one would put a "
            "false value in an append-only registry."
        ) from error
    return completed.stdout.strip()


def build_manifest(inputs: BuildInputs) -> ReleaseManifest:
    """Build §13's manifest from the tree, or raise naming what is missing.

    Nothing is defaulted. Every value comes from an artifact, the ledger, or the
    environment the deployment will actually run with.
    """
    _assert_schema_agrees(inputs.ledger)
    skill_map = skill_artifacts(inputs.repo_root)
    tool_map = tool_artifacts()
    manifest = ReleaseManifest(
        # Filled in below, once the content hash exists — an id derived from the content
        # is the only kind that cannot disagree with it.
        release_manifest_id=common.ReleaseManifestId(_PLACEHOLDER_ID),
        created_at=common.IsoTimestamp(inputs.built_at.astimezone(UTC)),
        parent_release_manifest_id=(
            common.ReleaseManifestId(inputs.parent_release_manifest_id)
            if inputs.parent_release_manifest_id
            else None
        ),
        agents=_agents(inputs, skill_map, tool_map),
        prompts=_prompts(inputs.ledger),
        tools={name: Tools(**tool.as_contract()) for name, tool in sorted(tool_map.items())},
        skills=_skills(skill_map, inputs.ledger),
        provider=_provider(inputs.env),
        guardrail_policy=_guardrail_policy(inputs.env, inputs.repo_root),
        rag=_rag(inputs.env, inputs.repo_root),
        source=source_from(inputs.env, inputs.repo_root),
        rollout=inputs.rollout,
    )
    _assert_skills_accounted_for(manifest, skill_map)
    artefact = artifact_hash(manifest)
    manifest.release_manifest_id = common.ReleaseManifestId(
        release_manifest_id(artefact, built_at=inputs.built_at)
    )
    assert_complete(manifest)
    return manifest


def _assert_skills_accounted_for(
    manifest: ReleaseManifest, skills: Mapping[str, SkillArtifact]
) -> None:
    """Every shipped skill package must appear under exactly one agent.

    The one check that makes an empty ``skill_hashes`` safe. §13 records each skill's hash
    on the agent that owns it, so a skill package that no agent names would be a skill the
    manifest never mentions — invisible, and precisely the omission that is worse than an
    error. Comparing the two sets catches both directions: a package the loader missed, and
    an id no package declares.
    """
    shipped = {artifact.agent_card_skill_id for artifact in skills.values()}
    # An absent mapping reads as "no skills recorded", which makes the two sets disagree and
    # raises below — the right outcome, and the reason `or {}` is not a silent default here.
    recorded = {skill_id for agent in manifest.agents for skill_id in (agent.skill_hashes or {})}
    if shipped != recorded:
        missing = sorted(shipped - recorded)
        unknown = sorted(recorded - shipped)
        raise IncompleteManifestError(
            "the manifest does not account for every shipped skill package: "
            f"unattributed={missing or '[]'} unknown={unknown or '[]'}"
        )


#: A syntactically valid id used only between constructing the model and computing the
#: content hash that replaces it. Never returned: :func:`build_manifest` assigns the real
#: one before the manifest leaves the function.
_PLACEHOLDER_ID: Final = "rel-19700101-voidxxx"


# ── completeness ────────────────────────────────────────────────────────────────────


def assert_complete(manifest: ReleaseManifest) -> None:
    """Refuse a manifest that is missing any §13 field.

    Checked on the finished object rather than enforced by construction, so the rule is
    exhaustive: a field added to the contract later fails here until it is filled. *"A
    manifest that omits a field is worse than one that errors, since the omission is
    invisible at report time"* — this is the error.

    Empty is not the same as absent. ``per_agent_overrides = {}`` states "none";
    ``None`` is refused, and so is an empty mapping for anything that must have content.
    """
    problems: list[str] = []

    if not manifest.agents:
        problems.append("agents: empty")
    seen: set[str] = set()
    for agent in manifest.agents:
        where = f"agents[{agent.agent_name}]"
        if agent.agent_name in seen:
            problems.append(f"{where}: duplicated agent_name")
        seen.add(agent.agent_name)
        for field, present in (
            ("version", agent.version),
            ("agent_hash", agent.agent_hash),
            ("a2a_card_hash", agent.a2a_card_hash),
        ):
            if not present:
                problems.append(f"{where}.{field}: missing")
        for field, digest_value in (
            ("agent_hash", agent.agent_hash),
            ("a2a_card_hash", agent.a2a_card_hash),
            ("instruction_hash", agent.instruction_hash),
            ("sub_agent_manifest_hash", agent.sub_agent_manifest_hash),
        ):
            if not isinstance(digest_value, str) or not is_sha256_hex(digest_value):
                problems.append(f"{where}.{field}: not a sha256 hex digest")
        if not agent.tool_description_hashes:
            problems.append(f"{where}.tool_description_hashes: absent or empty")
        # `skill_hashes` is allowed to be empty and `tool_description_hashes` is not, and
        # the difference is not a detail: the care coordinator owns no skill package,
        # because §3 gives it only `delegate_task`, and demanding a non-empty mapping here
        # would force a skill to be invented for it. An empty skill mapping *states*
        # "this agent owns no skill"; `build_manifest` then checks the union of all
        # agents' skill ids against the packages that ship, so a loader that returned
        # nothing could not hide behind it.
        for field, mapping in (
            ("tool_description_hashes", agent.tool_description_hashes),
            ("skill_hashes", agent.skill_hashes),
        ):
            # ``None`` here is already a problem recorded above, for the mapping that must
            # have content; iterating ``or {}`` keeps this collector collecting rather than
            # raising on the first absent field and hiding the rest behind it.
            for name, value in (mapping or {}).items():
                if not is_sha256_hex(value):
                    problems.append(f"{where}.{field}[{name}]: not a sha256 hex digest")

    if not manifest.prompts:
        problems.append("prompts: empty")
    if not manifest.tools:
        problems.append("tools: empty")
    for name, tool in manifest.tools.items():
        if not tool.schema_hash or not is_sha256_hex(tool.schema_hash):
            problems.append(f"tools[{name}].schema_hash: not a sha256 hex digest")
        if not tool.implementation_digest or not is_sha256_hex(tool.implementation_digest):
            problems.append(f"tools[{name}].implementation_digest: not a sha256 hex digest")
    if not manifest.skills:
        problems.append("skills: empty")

    provider = manifest.provider
    if not provider.agent_model_provider or not provider.agent_model_id:
        problems.append("provider: provider or model id missing")
    if not provider.params:
        problems.append("provider.params: empty")
    if provider.per_agent_overrides is None:
        problems.append("provider.per_agent_overrides: None (use {} to state 'none')")

    if not manifest.guardrail_policy.mode:
        problems.append("guardrail_policy.mode: missing")
    if not is_sha256_hex(manifest.guardrail_policy.classifier_hash):
        problems.append("guardrail_policy.classifier_hash: not a sha256 hex digest")
    if not manifest.guardrail_policy.thresholds:
        problems.append("guardrail_policy.thresholds: empty")

    rag = manifest.rag
    for field, value in (
        ("embedding_model_id", rag.embedding_model_id),
        ("index_name", rag.index_name),
        ("reranker_model_id", rag.reranker_model_id),
        ("knowledge_version", rag.knowledge_version),
    ):
        if not value:
            problems.append(f"rag.{field}: missing")
    if rag.embedding_dimensions <= 0:
        problems.append("rag.embedding_dimensions: not positive")

    source = manifest.source
    for field, value in (
        ("git_sha", source.git_sha),
        ("image_digest", source.image_digest),
        ("lockfile_hash", source.lockfile_hash),
    ):
        if not value:
            problems.append(f"source.{field}: missing")

    if problems:
        raise IncompleteManifestError(
            "the manifest is missing §13 fields and will not be emitted: " + "; ".join(problems)
        )


def manifest_components(
    manifest: ReleaseManifest, *, release_version: str
) -> Sequence[tuple[str, str]]:
    """``[(component, version)]`` for every versioned artifact in the release.

    The components are the deployed units — the seven agent services — plus the release
    itself. Tools and skills are not components: they are not deployed on their own, and
    they move an agent's artifact hash, which is what carries their version requirement
    to the gate.

    ``release_version`` is a parameter and not a manifest field, which is itself a
    finding: §13 lists a version for every artifact it names but none for the release as
    a whole, and the contract — generated from ``packages/contracts/schemas``, which this
    work may not edit — has no field for one. The release's identity is
    ``release_manifest_id``; the ledger's ``release`` version is what a human reads. Both
    are recorded rather than one being derived from the other.
    """
    parse_semver(release_version)  # refuses a version the registry cannot order
    roles = rfc_module("referraleasy_agent_runtime.roles")
    by_name = {agent.agent_name: agent for agent in manifest.agents}
    entries: list[tuple[str, str]] = []
    for role in roles.AgentRole:
        name = card_name(role)
        if name not in by_name:
            raise IncompleteManifestError(f"the manifest carries no agent record for {name!r}")
        # `service_name` rather than a name made up here: the registry's component is the
        # deployed unit, and `service_name` is the name that unit already answers to in
        # logs, traces and the compose file. Two names for one process is how a
        # dashboard ends up with a component nothing reports against.
        entries.append((roles.service_name(role), by_name[name].version))
    return [*entries, ("referraleasy", release_version)]
