"""The version-bump gate (§13): a check that can fail, and does.

§13: **CI fails when an affected agent's version did not increase, and even a PATCH
counts as a behaviour change, which triggers evaluation.** (The Turkish sentences,
quoted as written, are in ``docs/adr/ADR-0037-release-registry-version-gate.md``.)

What that means mechanically
----------------------------
Two manifests are compared, component by component. For each component the gate has a
**derived hash** and a **declared version**:

* hash moved, version did not rise  → **failure**. This is the rule §13 states.
* hash moved, version rose         → **evaluation required**.
* version rose, hash did not move  → **evaluation required** (any bump triggers it),
  and no failure: a version that rose without a content change is a booking-keeping
  error, not a behaviour change.
* the released component's own hash is the *set* of its parts.

The unit is the whole per-agent record, not the card's ``agent_hash``
(:func:`referraleasy_release_registry.manifest.agent_artifact_hash`), and that choice is
load-bearing. The card hash does not cover the sub-agent directive or the sub-agents' own
instructions (``artifacts.AGENT_HASH_GAP``), so a gate keyed on it would let a sub-agent's
instruction change while its parent's version stood still. Keying on the record makes
*"if a sub-agent changes, the parent's dependency hash and version rise"* true by
construction rather than by everyone remembering.

A finding is not a warning
--------------------------
:class:`Finding` objects are collected and returned; the caller decides how to print
them, but :attr:`GateResult.failed` is what the exit code is derived from. There is no
severity field and no "soft failure", because the one thing this check must not be is a
check that cannot fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from referraleasy_contracts.generated.workflow import ReleaseManifest
from referraleasy_release_registry.artifacts import card_name, rfc_module
from referraleasy_release_registry.errors import MissingArtifactError
from referraleasy_release_registry.manifest import agent_artifact_hash, artifact_hash
from referraleasy_release_registry.versions import VersionLedger, is_newer, parse_semver

__all__ = [
    "COMPONENT_REMOVED",
    "VERSION_NOT_ADVANCED",
    "ComponentState",
    "Finding",
    "GateResult",
    "check",
    "component_states",
]

#: An artifact's hash moved while its declared version did not rise. §13's failure.
VERSION_NOT_ADVANCED: Final = "version_not_advanced"

#: A component that was in the baseline and is not in the current build. A service that
#: disappeared is a change of a different kind, and a gate that stayed quiet about it
#: would let a component be deleted by an edit that never mentioned it.
COMPONENT_REMOVED: Final = "component_removed"

#: Codes that make the gate fail. Read as a set rather than a chain of conditions so a
#: new code cannot be added and forgotten in the failure test.
FAILING_CODES: Final[frozenset[str]] = frozenset({VERSION_NOT_ADVANCED, COMPONENT_REMOVED})


@dataclass(frozen=True, slots=True)
class ComponentState:
    """One component's two numbers: the derived hash and the declared version."""

    component: str
    version: str
    hash: str


@dataclass(frozen=True, slots=True)
class Finding:
    """One reason the gate is unhappy. ``detail`` is written for the person who must fix it."""

    code: str
    component: str
    detail: str


@dataclass(frozen=True, slots=True)
class GateResult:
    """The gate's answer, and the three lists a CI job needs to act on it.

    ``changed`` and ``need_evaluation`` are reported separately from ``findings`` because
    they are not problems: a legitimate release has both, and the evaluation run is what
    §13 asks for next. A caller that only looked at ``failed`` would skip the evaluation.
    """

    findings: tuple[Finding, ...]
    changed: tuple[str, ...]
    need_evaluation: tuple[str, ...]

    @property
    def failed(self) -> bool:
        return any(finding.code in FAILING_CODES for finding in self.findings)

    @property
    def ok(self) -> bool:
        return not self.findings


def component_states(
    manifest: ReleaseManifest, ledger: VersionLedger
) -> tuple[ComponentState, ...]:
    """The gate's view of a release: one entry per deployed component.

    The release-level entry is keyed on :func:`artifact_hash` — the content hash with the
    release metadata removed — rather than on ``manifest_hash``, which changes with
    ``created_at`` and would make every rebuild of an unchanged tree look like a change.
    A gate that fired on a rebuild would be turned off within a week, and the check that
    protects the release would go with it.
    """
    parse_semver(ledger.release)
    roles = rfc_module("referraleasy_agent_runtime.roles")
    by_name = {agent.agent_name: agent for agent in manifest.agents}
    states: list[ComponentState] = []
    for role in roles.AgentRole:
        name = card_name(role)
        agent = by_name.get(name)
        if agent is None:
            raise MissingArtifactError(f"the manifest carries no agent record for {name!r}")
        states.append(
            ComponentState(
                component=roles.service_name(role),
                version=agent.version,
                hash=agent_artifact_hash(agent),
            )
        )
    states.append(
        ComponentState(
            component="referraleasy",
            version=ledger.release,
            hash=artifact_hash(manifest),
        )
    )
    return tuple(states)


def check(baseline: tuple[ComponentState, ...], current: tuple[ComponentState, ...]) -> GateResult:
    """Compare two releases, component by component.

    ``baseline`` is the release that is already registered; ``current`` is the build under
    review. Both are plain values, so this function is pure — the whole gate can be tested
    by constructing two tuples, with no repository, no environment and no database.
    """
    before = {state.component: state for state in baseline}
    after = {state.component: state for state in current}
    if not before:
        raise MissingArtifactError(
            "the baseline has no components; there is nothing to compare against, and a gate "
            "against nothing passes everything"
        )
    findings: list[Finding] = []
    changed: list[str] = []
    evaluating: list[str] = []

    for component, now in after.items():
        was = before.get(component)
        if was is None:
            # A new component. Not a version-gate question: it has no predecessor to have
            # failed to rise from, and its version is whatever the ledger declares.
            evaluating.append(component)
            continue
        if now.hash != was.hash:
            changed.append(component)
            if is_newer(now.version, was.version):
                evaluating.append(component)
            else:
                findings.append(
                    Finding(
                        code=VERSION_NOT_ADVANCED,
                        component=component,
                        detail=(
                            f"the artifact changed but the version did not increase: "
                            f"{was.version} -> {now.version}. §13 counts even a PATCH as a "
                            "behaviour change. Bump the declared version and re-run."
                        ),
                    )
                )
        elif now.version != was.version:
            evaluating.append(component)

    for component in sorted(set(before) - set(after)):
        findings.append(
            Finding(
                code=COMPONENT_REMOVED,
                component=component,
                detail=(
                    f"{component} was registered as {before[component].version} and is not in "
                    "this build. A component that disappears is a change; remove it from the "
                    "ledger deliberately if that is intended."
                ),
            )
        )

    return GateResult(
        findings=tuple(findings),
        changed=tuple(sorted(changed)),
        need_evaluation=tuple(sorted(set(evaluating))),
    )
