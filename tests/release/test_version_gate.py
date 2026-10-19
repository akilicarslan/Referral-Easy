"""The version gate's own logic, tested without a repository, an environment or a database.

``check`` takes two tuples of :class:`ComponentState` — a component name, a declared version
and a derived hash — and returns findings. That signature is deliberate: §13's rule is about
two numbers, so the rule can be exercised exhaustively by constructing the pairs, including
the ones a real build rarely produces (a version that rose while the content stood still, a
component that disappeared, a version the ledger cannot parse).

Four of the cases below are the whole contract, and each is written as the sentence a person
would say:

===========================================  ==========================================
content changed, version did not increase     **fail** — §13's rule, and the check is
                                              worthless if this case is not tested
content changed, version increased            evaluation required
version increased, content unchanged          evaluation required, no failure: a version
                                              that rose without a change is bookkeeping
the run is a rebuild of the same content      nothing, at all
===========================================  ==========================================

The last one is not decoration. A gate that fires on every rebuild is a gate someone turns
off, and the check that protects a release would go with it — which is why the release-level
state is keyed on the metadata-free :func:`artifact_hash` and this module asserts the
consequence of that choice directly.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from release_fixtures import (
    RELEASE_COMPONENT,
    REPO_ROOT,
    SERVICE_NAMES,
    build_test_manifest,
)

from referraleasy_release_registry.errors import MissingArtifactError, VersionLedgerError
from referraleasy_release_registry.gate import (
    COMPONENT_REMOVED,
    FAILING_CODES,
    VERSION_NOT_ADVANCED,
    ComponentState,
    check,
    component_states,
)
from referraleasy_release_registry.manifest import artifact_hash
from referraleasy_release_registry.versions import load_ledger

pytestmark = pytest.mark.contract

#: The committed ledger. A module-level constant rather than a fixture, so the pure tests
#: above and the real-build tests below read the same declaration.
LEDGER_PATH = REPO_ROOT / "registry" / "versions.json"

AGENT = "agent-scheduling"
OTHER = "agent-provider_matching"


def state(component: str, version: str, content: str) -> ComponentState:
    return ComponentState(component=component, version=version, hash=content)


def test_the_failure_and_the_harmless_cases_are_both_named() -> None:
    """``FAILING_CODES`` is read as a set, so a new code cannot be added and forgotten."""
    assert {VERSION_NOT_ADVANCED, COMPONENT_REMOVED} == FAILING_CODES
    assert COMPONENT_REMOVED in FAILING_CODES, (
        "a component that disappears is a change of a different kind, and silence about it "
        "would let a service be deleted by an edit that never mentioned it"
    )


def test_a_moved_hash_with_a_standing_version_fails() -> None:
    """§13's rule, at its smallest. This is the test the whole gate exists to pass."""
    before = (state(AGENT, "1.0.0", "aaa"),)
    after = (state(AGENT, "1.0.0", "bbb"),)
    result = check(before, after)
    assert result.failed and not result.ok
    assert result.findings[0].code == VERSION_NOT_ADVANCED
    assert result.findings[0].component == AGENT
    assert result.changed == (AGENT,)
    assert result.need_evaluation == (), "a failed gate does not hand out an evaluation to run"


def test_a_patch_bump_is_an_increase_and_a_lower_version_is_not() -> None:
    """Both directions, because "changed at all" is not what §13 asks for."""
    before = (state(AGENT, "1.0.0", "aaa"),)
    assert check(before, (state(AGENT, "1.0.1", "bbb"),)).ok
    assert check(before, (state(AGENT, "2.0.0", "bbb"),)).ok
    assert check(before, (state(AGENT, "0.9.9", "bbb"),)).failed
    assert check(before, (state(AGENT, "1.0.0-rc.1", "bbb"),)).failed


def test_a_moved_hash_with_a_risen_version_requires_evaluation() -> None:
    """A bump is the trigger for §13's evaluation run, and the result has to name it."""
    result = check((state(AGENT, "1.0.0", "aaa"),), (state(AGENT, "1.1.0", "bbb"),))
    assert result.ok and not result.failed
    assert result.changed == (AGENT,)
    assert result.need_evaluation == (AGENT,)


def test_a_risen_version_with_an_unchanged_hash_requires_evaluation_but_does_not_fail() -> None:
    """*"Even a PATCH counts as a behaviour change"* — any bump triggers evaluation.

    A version that rose while the content stood still is a bookkeeping change rather than a
    behaviour change, so it is not a failure; it is still a bump, so it still runs the
    evaluation. Silence would be the one wrong answer.
    """
    result = check((state(AGENT, "1.0.0", "aaa"),), (state(AGENT, "1.2.0", "aaa"),))
    assert result.ok
    assert result.changed == (), "nothing changed; the hash is the content"
    assert result.need_evaluation == (AGENT,)


def test_an_identical_release_is_silent() -> None:
    """Nothing moved, so there is nothing to say. Contrast the rebuild test below."""
    states = (state(AGENT, "1.0.0", "aaa"), state(OTHER, "1.0.0", "ccc"))
    result = check(states, states)
    assert result.ok and result.changed == () and result.need_evaluation == ()


def test_a_new_component_is_not_a_version_gate_question() -> None:
    """A component with no predecessor has no version to have failed to rise from."""
    result = check(
        (state(AGENT, "1.0.0", "aaa"),),
        (state(AGENT, "1.0.0", "aaa"), state(OTHER, "1.0.0", "ccc")),
    )
    assert result.ok
    assert result.changed == ()
    assert result.need_evaluation == (OTHER,)


def test_a_removed_component_fails() -> None:
    result = check(
        (state(AGENT, "1.0.0", "aaa"), state(OTHER, "1.0.0", "ccc")),
        (state(AGENT, "1.0.0", "aaa"),),
    )
    assert result.failed
    assert [finding.code for finding in result.findings] == [COMPONENT_REMOVED]
    assert result.findings[0].component == OTHER
    assert "1.0.0" in result.findings[0].detail


def test_an_empty_baseline_passes_nothing() -> None:
    """``check`` refuses rather than returning "no findings", which would look like success."""
    with pytest.raises(MissingArtifactError) as refusal:
        check((), (state(AGENT, "1.0.0", "aaa"),))
    assert "against nothing" in str(refusal.value)


def test_a_version_the_ledger_cannot_parse_is_refused_rather_than_treated_as_equal() -> None:
    """The failure mode a silent fallback would produce is a build that quietly passes."""
    with pytest.raises(VersionLedgerError):
        check((state(AGENT, "1.0.0", "aaa"),), (state(AGENT, "not-a-version", "bbb"),))
    with pytest.raises(VersionLedgerError):
        check((state(AGENT, "not-a-version", "aaa"),), (state(AGENT, "1.0.0", "bbb"),))


def test_every_finding_is_reported_not_only_the_first() -> None:
    """A build that is wrong in two ways says so in two ways, in a stable order.

    Stopping at the first finding would make the gate a one-round-trip-at-a-time affair, and
    an unstable order would make CI output nobody can diff between runs. The detail text is
    deliberately *not* asserted to be unique — ``cli.render`` prints the component beside it,
    so two components that failed the same way read as two lines — but each finding must name
    its own component, which is what a caller acts on.
    """
    before = (
        state(AGENT, "1.0.0", "aaa"),
        state(OTHER, "1.0.0", "ccc"),
        state("agent-referral-intake", "1.0.0", "eee"),
    )
    after = (
        state(AGENT, "1.0.0", "bbb"),
        state(OTHER, "1.0.0", "ddd"),
        state("agent-referral-intake", "0.9.0", "fff"),
    )
    result = check(before, after)
    assert result.failed
    assert [finding.component for finding in result.findings] == [
        AGENT,
        OTHER,
        "agent-referral-intake",
    ]
    assert len({(finding.component, finding.code) for finding in result.findings}) == 3
    assert all(finding.detail for finding in result.findings), "a finding explains itself"


# ── the gate against real builds ────────────────────────────────────────────────────


def test_a_rebuild_of_the_same_tree_is_not_a_release(manifest) -> None:
    """The consequence of keying the release on ``artifact_hash``, asserted at the gate.

    Two builds of one tree at two instants have two ids, two ``created_at`` values and two
    ``manifest_hash`` values. If the gate compared those, every re-run of CI on an unchanged
    commit would demand a version bump, and the honest response to that would be to stop
    running it.
    """
    again = build_test_manifest(built_at=manifest.created_at.root + timedelta(days=1))
    ledger = load_ledger(LEDGER_PATH)
    assert again.release_manifest_id.root != manifest.release_manifest_id.root
    assert artifact_hash(again) == artifact_hash(manifest)
    result = check(component_states(manifest, ledger), component_states(again, ledger))
    assert result.ok, [finding.detail for finding in result.findings]
    assert result.changed == () and result.need_evaluation == ()


def test_the_gate_covers_every_deployed_component(manifest) -> None:
    """Seven services and the release: a component outside the gate cannot be gated."""
    ledger = load_ledger(LEDGER_PATH)
    states = component_states(manifest, ledger)
    assert sorted(s.component for s in states) == sorted([*SERVICE_NAMES, RELEASE_COMPONENT])
    assert {s.version for s in states} == {*ledger.agents.values(), ledger.release}
    assert len({s.component for s in states}) == len(states)


def test_a_component_the_manifest_cannot_produce_is_refused(manifest) -> None:
    """``component_states`` reads the manifest, so a missing agent is an error, not a gap.

    Deleting an agent's record would otherwise mean deleting its gate.
    """
    ledger = load_ledger(LEDGER_PATH)
    pruned = manifest.model_copy(update={"agents": manifest.agents[:-1]})
    with pytest.raises(MissingArtifactError):
        component_states(pruned, ledger)
