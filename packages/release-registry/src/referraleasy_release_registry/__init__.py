"""The §13 release registry: what is running, and what it was made of.

Phase 3.5 of ``IMPLEMENTATION_TODO.md``, and §13 of ``ReferralEasy_Architecture_v3.md``.
The architecture's claims, and where each one lives:

*the immutable manifest*
    :mod:`referraleasy_release_registry.manifest` builds it from real artifacts and
    refuses to emit one with a field missing. :mod:`referraleasy_release_registry.store`
    writes it into ``release_registry``, which already exists and is already append-only.

*the hashes that make a change visible*
    :mod:`referraleasy_release_registry.artifacts` reads what is actually shipped — the
    cards, the prompts, the tool specs and their handler bodies, the skill packages, the
    sub-agents — and :mod:`referraleasy_release_registry.hashing` gives every one of them a
    content-only, order-free digest. A change to a sub-agent moves its parent's dependency
    hash, and therefore its parent's version requirement.

*the gate*
    :mod:`referraleasy_release_registry.gate` compares two releases component by component
    and fails when an artifact moved and its version did not rise, PATCH included.

*effective versions, pinning, the kill switch and migration*
    :mod:`referraleasy_release_registry.effective` (what a task carries),
    :mod:`referraleasy_release_registry.pinning` (what a conversation is stuck to, and how
    it may move), :mod:`referraleasy_release_registry.canary` (cohorts and rollback, from
    the registry table alone).

*the command*
    :mod:`referraleasy_release_registry.cli`, driven by ``scripts/check_release_versions.py``.

Nothing here imports ``referraleasy_agents`` at import time — the seven agent services are
read through :func:`referraleasy_release_registry.artifacts.rfc_module` when a manifest is
built, so importing this package stays cheap and does not require the agents' dependencies.
"""

from __future__ import annotations

from referraleasy_release_registry.artifacts import (
    AGENT_HASH_GAP,
    SkillArtifact,
    SubAgentArtifact,
    ToolArtifact,
    agent_hash,
    skill_artifacts,
    sub_agent_manifest,
    sub_agent_manifest_hash,
    tool_artifacts,
)
from referraleasy_release_registry.canary import (
    STABLE_COHORT,
    RolloutTarget,
    Routing,
    canary_bucket,
    rollout_targets,
    route,
)
from referraleasy_release_registry.cli import main
from referraleasy_release_registry.effective import (
    LATEST_LABEL,
    EffectiveVersions,
    effective_versions,
    resolve_release,
)
from referraleasy_release_registry.errors import (
    IncompleteManifestError,
    MissingArtifactError,
    RegistryConflictError,
    RegistryError,
    ReleaseRefusedError,
    VersionLedgerError,
    VersionNotAdvancedError,
)
from referraleasy_release_registry.gate import (
    ComponentState,
    Finding,
    GateResult,
    check,
    component_states,
)
from referraleasy_release_registry.hashing import canonical_json, digest, sha256_hex
from referraleasy_release_registry.manifest import (
    BuildInputs,
    RolloutFacts,
    agent_artifact_hash,
    artifact_hash,
    assert_complete,
    build_manifest,
    manifest_components,
    manifest_hash,
    release_manifest_id,
    required_field,
    rollout_facts,
)
from referraleasy_release_registry.pinning import (
    PIN_TABLE_DDL,
    Approval,
    ConversationPin,
    InMemoryPinStore,
    KillSwitch,
    PinStore,
    StopDirective,
    migrate_conversation,
    pin_conversation,
)
from referraleasy_release_registry.store import (
    PostgresRunner,
    RegisteredRelease,
    SqlRunner,
    read_manifest,
    record_event,
    register_release,
    registration_rows,
    release_status,
)
from referraleasy_release_registry.versions import (
    LEDGER_FILENAME,
    VersionLedger,
    is_newer,
    load_ledger,
    parse_semver,
)

__version__ = "0.1.0"

__all__ = [
    "AGENT_HASH_GAP",
    "LATEST_LABEL",
    "LEDGER_FILENAME",
    "PIN_TABLE_DDL",
    "STABLE_COHORT",
    "Approval",
    "BuildInputs",
    "ComponentState",
    "ConversationPin",
    "EffectiveVersions",
    "Finding",
    "GateResult",
    "InMemoryPinStore",
    "IncompleteManifestError",
    "KillSwitch",
    "MissingArtifactError",
    "PinStore",
    "PostgresRunner",
    "RegisteredRelease",
    "RegistryConflictError",
    "RegistryError",
    "ReleaseRefusedError",
    "RolloutFacts",
    "RolloutTarget",
    "Routing",
    "SkillArtifact",
    "SqlRunner",
    "StopDirective",
    "SubAgentArtifact",
    "ToolArtifact",
    "VersionLedger",
    "VersionLedgerError",
    "VersionNotAdvancedError",
    "agent_artifact_hash",
    "agent_hash",
    "artifact_hash",
    "assert_complete",
    "build_manifest",
    "canary_bucket",
    "canonical_json",
    "check",
    "component_states",
    "digest",
    "effective_versions",
    "is_newer",
    "load_ledger",
    "main",
    "manifest_components",
    "manifest_hash",
    "migrate_conversation",
    "parse_semver",
    "pin_conversation",
    "read_manifest",
    "record_event",
    "register_release",
    "registration_rows",
    "release_manifest_id",
    "release_status",
    "required_field",
    "resolve_release",
    "rollout_facts",
    "rollout_targets",
    "route",
    "sha256_hex",
    "skill_artifacts",
    "sub_agent_manifest",
    "sub_agent_manifest_hash",
    "tool_artifacts",
]
