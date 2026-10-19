"""The effective versions a runtime task carries (§13).

§13: **runtime tasks carry their effective versions, and the label** ``latest`` **is not
sufficient for reporting.** (ADR-0037 quotes the sentences as written.)

The reason is worth stating plainly, because it is the whole point of this module. When
a task records ``latest``, the record is a claim about the *present*: a report produced
next month cannot recover which prompt, which tool implementation or which knowledge
index the task actually ran with, because ``latest`` has moved on. When it records the
versions themselves, the record is a claim about the *task*, and it stays true.

So :class:`EffectiveVersions` is a **resolved** record: every field is a version, a hash
or an id that was true at the moment the task started. Nothing in it is a pointer.

Two of them, for one task
-------------------------
A task starts on a release and may outlive it. So the record carries the release it was
built from (``release_manifest_id``, ``manifest_hash``) *and* the values it is running
with — and the values are copied, not looked up. A report that reads the manifest later
is reading a document; a report that reads the task's own record is reading what ran.

What this module deliberately does not do is read the database. It is the value a caller
persists, not the persistence: the release registry stores manifests, and where a task
record lives is the task's own schema, which §13 does not define and this package does
not own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from referraleasy_contracts.generated.workflow import Agent, ReleaseManifest
from referraleasy_release_registry.errors import MissingArtifactError, ReleaseRefusedError
from referraleasy_release_registry.manifest import manifest_hash, required_field, rollout_facts

__all__ = [
    "LATEST_LABEL",
    "EffectiveVersions",
    "effective_versions",
    "resolve_release",
]

#: The label that is not a version. Named so the refusal below can quote it and so a
#: caller grepping for the word finds the rule rather than a string literal.
LATEST_LABEL: Final = "latest"


def _agent(manifest: ReleaseManifest, agent_name: str) -> Agent:
    for agent in manifest.agents:
        if agent.agent_name == agent_name:
            return agent
    known = ", ".join(sorted(agent.agent_name for agent in manifest.agents))
    raise MissingArtifactError(
        f"{agent_name!r} is not an agent in {manifest.release_manifest_id.root}; the manifest "
        f"carries {known}"
    )


def _prompt_version(manifest: ReleaseManifest, agent_name: str) -> str:
    try:
        return manifest.prompts[agent_name]
    except KeyError as error:
        raise MissingArtifactError(
            f"{manifest.release_manifest_id.root} declares no prompt version for {agent_name!r}"
        ) from error


@dataclass(frozen=True, slots=True)
class EffectiveVersions:
    """What a running task is executing, resolved and copied (§13).

    Frozen, and ``to_payload`` produces only JSON primitives, so a task record cannot
    hold a live object that changes under it. A mutable effective-versions record would
    reproduce exactly the defect it exists to fix.
    """

    release_manifest_id: str
    manifest_hash: str
    agent_name: str
    agent_version: str
    agent_hash: str
    prompt_version: str
    instruction_hash: str
    tool_versions: Mapping[str, str]
    skill_versions: Mapping[str, str]
    sub_agent_manifest_hash: str
    model_provider: str
    model_id: str
    guardrail_classifier_hash: str
    knowledge_version: str
    git_sha: str
    cohort: str

    def to_payload(self) -> dict[str, Any]:
        """The record as JSON-ready primitives, ready to attach to a task or a span."""
        return {
            "release_manifest_id": self.release_manifest_id,
            "manifest_hash": self.manifest_hash,
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "agent_hash": self.agent_hash,
            "prompt_version": self.prompt_version,
            "instruction_hash": self.instruction_hash,
            "tool_versions": dict(self.tool_versions),
            "skill_versions": dict(self.skill_versions),
            "sub_agent_manifest_hash": self.sub_agent_manifest_hash,
            "model_provider": self.model_provider,
            "model_id": self.model_id,
            "guardrail_classifier_hash": self.guardrail_classifier_hash,
            "knowledge_version": self.knowledge_version,
            "git_sha": self.git_sha,
            "cohort": self.cohort,
        }


def effective_versions(manifest: ReleaseManifest, *, agent_name: str) -> EffectiveVersions:
    """Resolve the versions ``agent_name`` runs with under ``manifest``.

    An agent that is not in the manifest raises rather than producing a partially filled
    record. §13's failure mode is an omission that is invisible at report time, so the
    refusal happens here — at task start, where it can still be fixed — rather than in a
    report six weeks later.
    """
    agent = _agent(manifest, agent_name)
    # Read through `required_field`: the contract types these as optional, and a stored
    # manifest that lost one of them must fail here — at task start — rather than produce an
    # effective-versions record with a hole in it that only a report would notice.
    where = f"release {manifest.release_manifest_id.root}, agent {agent_name}"
    return EffectiveVersions(
        release_manifest_id=manifest.release_manifest_id.root,
        manifest_hash=manifest_hash(manifest),
        agent_name=agent.agent_name,
        agent_version=agent.version,
        agent_hash=agent.agent_hash,
        prompt_version=_prompt_version(manifest, agent_name),
        instruction_hash=required_field(
            agent.instruction_hash, field="instruction_hash", where=where
        ),
        tool_versions=dict(
            required_field(
                agent.tool_description_hashes, field="tool_description_hashes", where=where
            )
        ),
        skill_versions=dict(required_field(agent.skill_hashes, field="skill_hashes", where=where)),
        sub_agent_manifest_hash=required_field(
            agent.sub_agent_manifest_hash, field="sub_agent_manifest_hash", where=where
        ),
        model_provider=manifest.provider.agent_model_provider,
        model_id=manifest.provider.agent_model_id,
        guardrail_classifier_hash=manifest.guardrail_policy.classifier_hash,
        knowledge_version=manifest.rag.knowledge_version,
        git_sha=manifest.source.git_sha,
        cohort=rollout_facts(manifest).cohort,
    )


def resolve_release(pin: str, releases: Mapping[str, ReleaseManifest]) -> ReleaseManifest:
    """Resolve a release pin to a manifest, **refusing the label ``latest``**.

    ``releases`` is keyed by ``release_manifest_id``, which is what a recorded pin is.

    There is no branch that resolves ``latest`` to the newest key. That is the entire
    point: a resolution that answered ``latest`` would make every report that used it
    silently about whichever release happened to be newest when the report ran. A pin
    that cannot be resolved is a failure, and a failure is readable; a pin that resolves
    to the wrong release is not.
    """
    requested = pin.strip()
    if not requested:
        raise ReleaseRefusedError("an empty pin names no release; §13 pins conversations to one")
    if requested == LATEST_LABEL:
        raise ReleaseRefusedError(
            f"{LATEST_LABEL!r} is not a release. §13 requires a task to carry the versions it "
            "ran with; resolving the label would make the record a claim about the present "
            f"rather than about the task. Pin to one of: {', '.join(sorted(releases)) or 'none registered'}"
        )
    try:
        return releases[requested]
    except KeyError as error:
        raise ReleaseRefusedError(
            f"no release {requested!r} is registered; known releases: "
            f"{', '.join(sorted(releases)) or 'none'}"
        ) from error
