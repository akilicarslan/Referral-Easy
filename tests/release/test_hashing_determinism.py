"""The hashes are reproducible, and that is a property rather than a hope.

§13's manifest is only worth having if the same inputs produce the same digest. If a rebuild
moved a hash, every rebuild would look like a release, the version gate would fire on
artifacts nobody touched, and the first thing anyone would do is stop reading it. So the two
properties are tested directly:

**content-only** — nothing looks at ``mtime``, at a file's size, or at where the repository
happens to be checked out. ``touch``-ing the tree, or building the same commit on a CI runner
in ``/home/runner/work``, must produce the same hash. The second of those is what
:func:`test_a_copy_of_the_tree_in_another_directory_hashes_the_same` exists for.

**order-free** — mappings are canonicalised before hashing, so a dictionary built in a
different order produces the same bytes. Python's mapping order is an implementation detail
that a refactor can change without anyone intending to, and a hash that followed it would
turn a tidy-up into a release.

And two tests on the other side, because a determinism suite that only showed *stability*
could be satisfied by a constant: the build must follow the environment, and a released
artifact must be able to differ.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, timedelta

import pytest
from release_fixtures import (
    BUILT_AT,
    agent_named,
    build_test_manifest,
    scratch_repo,
)

from referraleasy_contracts.generated.workflow import Rollout
from referraleasy_release_registry.hashing import (
    canonical_json,
    digest,
    file_digest,
    text_digest,
    tree_digest,
)
from referraleasy_release_registry.manifest import (
    RELEASE_METADATA_FIELDS,
    agent_artifact_hash,
    artifact_hash,
    manifest_hash,
    release_manifest_id,
)

pytestmark = pytest.mark.contract


# ── order-free ──────────────────────────────────────────────────────────────────────


def test_a_mapping_hashes_the_same_whatever_order_it_was_built_in() -> None:
    """The headline property, at its smallest."""
    first = {"b": 2, "a": 1, "c": {"z": 26, "y": 25}}
    second = {"c": {"y": 25, "z": 26}, "a": 1, "b": 2}
    assert first == second
    assert digest(first) == digest(second)
    assert canonical_json(first) == canonical_json(second)
    assert canonical_json(first) == '{"a":1,"b":2,"c":{"y":25,"z":26}}'


def test_reordering_a_manifests_nested_mapping_does_not_move_its_hash(manifest) -> None:
    """The same property one level up, on a real manifest.

    ``provider.params`` is a plain ``dict``, and a dict built in another order is the *same*
    dict: the hash must not follow the order it happened to iterate in.
    """
    shuffled = manifest.model_copy(
        update={
            "provider": manifest.provider.model_copy(
                update={"params": dict(reversed(list(manifest.provider.params.items())))}
            )
        }
    )
    assert shuffled.provider.params == manifest.provider.params
    assert artifact_hash(shuffled) == artifact_hash(manifest)
    assert manifest_hash(shuffled) == manifest_hash(manifest)


def test_reordering_the_agent_list_does_move_the_hash_and_the_build_fixes_the_order(
    manifest,
) -> None:
    """A *list* is ordered content, and this package does not pretend otherwise.

    ``json.dumps(sort_keys=True)`` sorts keys, not elements, so reversing ``agents`` genuinely
    changes the document — claiming otherwise in a test would be claiming the order of a list
    does not matter. What matters is that the order cannot drift by accident: ``_agents``
    walks ``AgentRole``, so the manifest's order is the enum's order and the only way to
    change it is to change the enum, which is a real change that should move a hash.
    """
    from referraleasy_agent_runtime.roles import AgentRole
    from referraleasy_release_registry.artifacts import card_name

    reversed_agents = manifest.model_copy(update={"agents": list(reversed(manifest.agents))})
    assert artifact_hash(reversed_agents) != artifact_hash(manifest)
    assert [agent.agent_name for agent in manifest.agents] == [
        card_name(role) for role in AgentRole
    ], "the built manifest follows §3's role order, so its order cannot drift by itself"


def test_tree_digest_is_order_free_and_name_sensitive(tmp_path) -> None:
    one = tmp_path / "one.txt"
    two = tmp_path / "two.txt"
    one.write_text("one", encoding="utf-8")
    two.write_text("two", encoding="utf-8")
    assert tree_digest({"a": one, "b": two}) == tree_digest({"b": two, "a": one})
    # The *name* is part of the hash: a renamed file is a content change.
    assert tree_digest({"a": one, "b": two}) != tree_digest({"a": two, "b": one})


# ── content-only ────────────────────────────────────────────────────────────────────


def test_a_file_digest_does_not_depend_on_its_mtime(tmp_path) -> None:
    path = tmp_path / "artifact.txt"
    path.write_text("same bytes", encoding="utf-8")
    before = file_digest(path)
    os.utime(path, (time.time() + 3600, time.time() + 3600))
    assert file_digest(path) == before
    assert file_digest(path) == text_digest("same bytes")


def test_two_builds_of_the_same_inputs_agree(manifest) -> None:
    """The whole manifest, twice, from the same tree and the same instant."""
    again = build_test_manifest()
    assert artifact_hash(again) == artifact_hash(manifest)
    assert manifest_hash(again) == manifest_hash(manifest)
    assert again.release_manifest_id.root == manifest.release_manifest_id.root
    assert canonical_json(again.model_dump(mode="json")) == canonical_json(
        manifest.model_dump(mode="json")
    )


def test_a_copy_of_the_tree_in_another_directory_hashes_the_same(tmp_path) -> None:
    """Where the checkout lives must not be part of a release's identity.

    A CI runner checks out into a path nobody chose, and a hash that followed the path would
    make the same commit a different release on every machine. This is why the implementation
    digest is taken over ``inspect.getsource`` of the handler rather than over the file, and
    why skill content hashes use package-relative names.
    """
    scratch = scratch_repo(tmp_path / "elsewhere")
    elsewhere = build_test_manifest(root=scratch.root)
    here = build_test_manifest()
    assert artifact_hash(elsewhere) == artifact_hash(here)
    assert manifest_hash(elsewhere) == manifest_hash(here)
    for agent in here.agents:
        assert agent_artifact_hash(agent) == agent_artifact_hash(
            agent_named(elsewhere, agent.agent_name)
        )


# ── the two hashes, and why they differ ─────────────────────────────────────────────


def test_the_artifact_hash_ignores_the_release_metadata(manifest) -> None:
    """§13's hashes move for the right reasons, and this is the boundary.

    ``created_at``, the id and the rollout are *about* a release rather than part of one: a
    canary moving from 5% to 25% is a deployment decision, and two builds of one commit are
    two releases. The content hash excludes all four and the stored hash includes them, which
    is why a registry row can say "the same artifact again" without the gate misfiring.
    """
    later = manifest.model_copy(
        update={
            "created_at": type(manifest.created_at)(manifest.created_at.root + timedelta(hours=3)),
            "release_manifest_id": manifest.release_manifest_id,
            "rollout": Rollout(cohort="canary", cohort_percentage=0.05, kill_switch_engaged=False),
        }
    )
    assert artifact_hash(later) == artifact_hash(manifest)
    assert manifest_hash(later) != manifest_hash(manifest)
    assert set(RELEASE_METADATA_FIELDS) == {
        "release_manifest_id",
        "created_at",
        "parent_release_manifest_id",
        "rollout",
    }


def test_the_release_id_is_derived_from_the_content_and_the_date(manifest) -> None:
    """``rel-<YYYYMMDD>-<first seven of the artifact hash>``, so the two cannot disagree."""
    artefact = artifact_hash(manifest)
    assert manifest.release_manifest_id.root == release_manifest_id(artefact, built_at=BUILT_AT)
    assert manifest.release_manifest_id.root == f"rel-20261015-{artefact[:7]}"
    # And the contract's own pattern, which the database does not enforce and the id must.
    import re

    assert re.fullmatch(r"rel-\d{8}-[a-z0-9]{7}", manifest.release_manifest_id.root)


def test_the_same_tree_on_another_day_is_another_release(manifest) -> None:
    """Same content, different release date: a different id, the same content prefix.

    The prefix is what makes the id useful in a log and still content-derived; the date is
    what keeps two registrations of one artifact from colliding on ``UNIQUE (release_id)``.
    """
    tomorrow = build_test_manifest(built_at=BUILT_AT + timedelta(days=1))
    assert artifact_hash(tomorrow) == artifact_hash(manifest)
    assert manifest_hash(tomorrow) != manifest_hash(manifest)
    assert tomorrow.release_manifest_id.root != manifest.release_manifest_id.root
    assert tomorrow.release_manifest_id.root[4:12] == "20261016"
    assert tomorrow.release_manifest_id.root[13:] == manifest.release_manifest_id.root[13:]


# ── the other side: the build must be able to differ ────────────────────────────────


def test_the_manifest_follows_the_deployment_it_was_built_from(manifest) -> None:
    """A determinism suite that could be satisfied by a constant would be worthless.

    Two environments, one tree: the hashes must differ. This is the test that fails if
    someone makes the provider block a module-level constant "for simplicity".
    """
    other = build_test_manifest(env={"AGENT_MODEL_ID": "another/model"})
    assert other.provider.agent_model_id == "another/model"
    assert artifact_hash(other) != artifact_hash(manifest)
    assert other.release_manifest_id.root != manifest.release_manifest_id.root
    assert (
        agent_named(other, "scheduling").agent_hash
        == agent_named(manifest, "scheduling").agent_hash
    ), "the provider is not part of an agent's own hash; the release hash carries it"


def test_a_release_can_carry_its_parent(manifest) -> None:
    child = build_test_manifest(parent_release_manifest_id=manifest.release_manifest_id.root)
    assert child.parent_release_manifest_id is not None
    assert child.parent_release_manifest_id.root == manifest.release_manifest_id.root
    assert artifact_hash(child) == artifact_hash(manifest)


def test_the_two_hashes_are_both_sha256_hex(manifest) -> None:
    from referraleasy_release_registry.hashing import is_sha256_hex

    assert is_sha256_hex(artifact_hash(manifest))
    assert is_sha256_hex(manifest_hash(manifest))
    assert is_sha256_hex(agent_artifact_hash(manifest.agents[0]))
    assert artifact_hash(manifest) != manifest_hash(manifest)


def test_built_at_is_a_real_instant_not_an_ambient_one(manifest) -> None:
    """The build takes its clock as an argument, which is why this suite can assert anything.

    ``BuildInputs.built_at`` is required and aware. A manifest that read ``datetime.now()``
    somewhere inside would be unreproducible no matter what the callers passed, and the
    "two builds agree" test above would fail intermittently rather than always.
    """
    assert manifest.created_at.root == BUILT_AT
    assert manifest.created_at.root.tzinfo is not None
    assert manifest.created_at.root.astimezone(UTC) == BUILT_AT


# ── the content hash is about content, and provenance is not content ────────────────


def test_a_rebuild_of_one_tree_is_the_same_artifact_whatever_built_it(manifest) -> None:
    """Two builds, one tree: a new commit sha or a new image digest is not a content change.

    This is the property the version gate rests on, and it was only almost true. `source` sat inside
    the content hash, so **every commit** changed it — the gate would have demanded a version bump
    for a README edit — and every *rebuild* changed it too, because an image digest is assigned by
    whatever built the image and two builds of one tree do not produce one digest. `gate.py` names
    where that ends: "A gate that fired on a rebuild would be turned off within a week, and the check
    that protects the release would go with it."

    Asserted with the fields `artifact_hash` is allowed to ignore, one at a time, so a future change
    that puts either back is caught here rather than in CI.
    """
    rebuilt = build_test_manifest(
        env={
            "RELEASE_GIT_SHA": "c0ffee" * 7 + "c0",
            "RELEASE_IMAGE_DIGEST": "sha256:" + "ab" * 32,
        }
    )

    assert rebuilt.source.git_sha != manifest.source.git_sha, "the fixture did not vary the sha"
    assert rebuilt.source.image_digest != manifest.source.image_digest
    assert artifact_hash(rebuilt) == artifact_hash(manifest), (
        "a rebuild of the same tree produced a different content hash, so the version gate would "
        "fire on every commit and every rebuild"
    )
    # The stored hash still moves, because it covers the manifest as written — that is the point of
    # having two hashes, and it is why the registry row can say "the same artifact, built again".
    assert manifest_hash(rebuilt) != manifest_hash(manifest)


def test_a_changed_dependency_resolution_is_a_content_change(manifest) -> None:
    """`source.lockfile_hash` stays in the content hash, and that is deliberate.

    A lockfile can change behaviour without a source file moving, which is why §13 records it. The
    exclusion above is by field and not by block precisely so this one is not swept along with the
    two that had to go.
    """
    # `Source` is a generated Pydantic model, so this is `model_copy` rather than
    # `dataclasses.replace` — the registry's own types are dataclasses and its contract types are
    # not, which is exactly the kind of mix-up a passing test would not have revealed.
    other = manifest.model_copy(
        update={"source": manifest.source.model_copy(update={"lockfile_hash": "0" * 64})}
    )

    assert artifact_hash(other) != artifact_hash(manifest), (
        "a changed dependency resolution did not change the content hash; the gate would not ask "
        "for a version bump when the resolved set moved"
    )
