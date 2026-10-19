"""``scripts/check_release_versions.py``, run as the CI job runs it — and shown to fail.

This is the module the whole of §13's gate exists for. §13: *"CI fails when an affected agent's
version did not increase."* A check that cannot fail is not a check, so the sequence below is not
a happy path with assertions bolted on: it is the failure, produced by editing a prompt in a
throwaway copy of the tree, and then the repair.

============================  ==================================================
1. ``build``                  writes the manifest, the pointer, the index and
                              the per-artifact views
2. ``check``                  exit 0 — the baseline is this build
3. reword a sentence in       exit **1**, ``version_not_advanced``, naming the
   ``prompts.py``             agent that changed *and* the release that carries it
4. bump that agent's version  exit **1** still, naming only the release
5. bump the release version   exit **0**, ``evaluation required for:`` both
============================  ==================================================

Every run is a subprocess, so what is tested is the file CI will call — its argument parsing,
its exit codes, its path resolution, its stdout — and not a function that happens to sit behind
it. The mutations happen in a ``tmp_path`` copy of the tree (``release_fixtures.scratch_repo``):
three other agents share the working tree, and a test that edited a real prompt to prove the
gate works would be a test that edits the thing it is protecting.

No network, no database, no model call: ``MODEL_BACKEND=offline`` in the fixture environment and
nothing here imports a provider. The environment handed to the subprocess is explicit and
minimal, so the test cannot pick up a live ``.env`` from the developer's shell — ``uv run``
loads one, and a gate test that read real credentials would be a very bad gate test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from release_fixtures import (
    BUILT_AT,
    REPO_ROOT,
    SKILL_PACKAGES,
    TEST_ENV,
    advance_version,
    replace_in_file,
    scratch_repo,
)

pytestmark = pytest.mark.contract

SCRIPT = REPO_ROOT / "scripts" / "check_release_versions.py"

#: A fixed build time, so two ``build`` runs of one tree are byte-identical — the property
#: ``--built-at`` exists for and the one that makes a registry's diffs reviewable.
BUILT_AT_FLAG = BUILT_AT.isoformat()

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3


def run_script(root: Path, *args: str, env: dict[str, str] | None = None):
    """Run the CI entry point with ``--repo-root <root>`` and return the completed process.

    The environment is built rather than inherited: ``uv run`` loads ``.env``, and a gate test
    that saw the developer's provider key would be a gate test that read a credential.
    """
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        **(env or {}),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--repo-root", str(root)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(root),
    )


def build_once(root: Path, *extra: str):
    result = run_script(root, "build", "--built-at", BUILT_AT_FLAG, *extra)
    assert result.returncode == EXIT_OK, f"{result.stdout}\n{result.stderr}"
    return result


def bump_ledger(root: Path, *, agent_versions: dict[str, str], release: str | None) -> None:
    """Edit the scratch ledger the way a person would: a JSON file, a version, a diff."""
    path = root / "registry" / "versions.json"
    ledger: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    ledger["agents"].update(agent_versions)
    if release is not None:
        ledger["release"] = release
    path.write_text(
        json.dumps(ledger, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# ── the four exit codes ────────────────────────────────────────────────────────────


def test_the_script_documents_the_exit_codes_it_returns() -> None:
    """A CI job has to tell "the change is wrong" apart from "the check is broken".

    Reported the same way, the second trains a team to ignore the first.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    for code, sentence in (
        ("0", "the gate passed"),
        ("1", "version did not increase"),
        ("2", "usage error"),
        ("3", "the gate could not run"),
    ):
        assert sentence in text, f"the exit-code table does not document {code}: {sentence}"
    assert len({EXIT_OK, EXIT_GATE_FAILED, EXIT_USAGE, EXIT_UNAVAILABLE}) == 4, (
        "the four outcomes must be four codes; the tests below assert which one each case "
        "returns, and that is the part that matters"
    )
    assert "--repo-root" in text and "check_release_versions.py check" in text


def test_a_missing_baseline_is_not_a_pass(tmp_path: Path) -> None:
    """The one failure mode that would make the whole check decorative."""
    scratch = scratch_repo(tmp_path)
    missing = run_script(scratch.root, "check")
    assert missing.returncode == EXIT_UNAVAILABLE
    assert "no baseline" in missing.stderr and "cannot compare against nothing" in missing.stderr

    initial = run_script(scratch.root, "check", "--initial")
    assert initial.returncode == EXIT_OK
    assert "nothing to compare" in initial.stdout, (
        "--initial reports that it compared nothing rather than claiming a pass"
    )


def test_an_argument_this_script_does_not_have_is_a_usage_error(tmp_path: Path) -> None:
    scratch = scratch_repo(tmp_path)
    assert run_script(scratch.root, "deploy").returncode == EXIT_USAGE


def test_an_environment_the_manifest_needs_but_does_not_have_cannot_run(tmp_path: Path) -> None:
    """Exit 3, with the missing variable named — not exit 1 and not a traceback.

    ``RELEASE_GIT_SHA`` is removed and the scratch tree is not a git repository, so there is no
    sha to be had. A registry that invented one would make every recorded sha a guess, so the
    build refuses; and a CI job needs to be told the check could not run.
    """
    scratch = scratch_repo(tmp_path)
    env_file = scratch.root / ".env"
    env_file.write_text(
        "\n".join(
            f"{key}={value}" for key, value in sorted(TEST_ENV.items()) if key != "RELEASE_GIT_SHA"
        )
        + "\n",
        encoding="utf-8",
    )
    result = run_script(scratch.root, "build")
    assert result.returncode == EXIT_UNAVAILABLE, result.stdout
    assert "git sha" in result.stderr.lower()
    assert "Traceback" not in result.stderr

    # And supplying it explicitly is enough, which is what CI does.
    supplied = run_script(scratch.root, "build", "--git-sha", "a" * 40, "--built-at", BUILT_AT_FLAG)
    assert supplied.returncode == EXIT_OK, supplied.stderr


# ── build ──────────────────────────────────────────────────────────────────────────


def test_build_writes_the_manifest_the_pointer_and_the_index(tmp_path: Path) -> None:
    """§13's registry files, and the pointer ``make eval`` and a future CI job both read."""
    scratch = scratch_repo(tmp_path)
    build_once(scratch.root)
    releases = sorted((scratch.root / "registry" / "releases").glob("*.json"))
    manifests = [
        path for path in releases if path.name != "latest.json" and path.name != "index.json"
    ]
    assert len(manifests) == 1
    document = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert document["release_manifest_id"] == manifests[0].stem
    assert manifests[0].stem.startswith("rel-2026")
    assert scratch.latest_file().read_text(encoding="utf-8") == manifests[0].read_text(
        encoding="utf-8"
    ), "latest.json is the same document, so a reader cannot get a different answer"

    index = json.loads((scratch.root / "registry" / "releases" / "index.json").read_text("utf-8"))
    assert index["latest"] == document["release_manifest_id"]
    entry = index["releases"][0]
    assert entry["release_manifest_id"] == index["latest"]
    ledger = json.loads(scratch.ledger_file().read_text())
    assert entry["release_version"] == ledger["release"], (
        "the release version is the one number the manifest does not carry, so the index"
        " records it and the gate reads it back from there"
    )
    assert entry["artifact_hash"] != entry["manifest_hash"]
    assert entry["file"] == f"{index['latest']}.json"

    agents = sorted(path.name for path in (scratch.root / "registry" / "agents").glob("*.json"))
    prompts = sorted(path.name for path in (scratch.root / "registry" / "prompts").glob("*.json"))
    assert len(agents) == len(prompts) == 7
    assert agents[0] == "care-coordinator.json"
    assert prompts[0] == "care_coordinator.json"
    for path in [
        *manifests,
        scratch.latest_file(),
        *(scratch.root / "registry" / "agents").glob("*.json"),
    ]:
        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n"), f"{path} is not newline-terminated"
        assert json.loads(text) == json.loads(
            json.dumps(json.loads(text), sort_keys=True, indent=2, ensure_ascii=False)
        ), f"{path} is not written in the canonical form a diff can be reviewed in"


def test_two_builds_of_one_tree_are_byte_identical(tmp_path: Path) -> None:
    """``--built-at`` is what makes this true, and a registry nobody can diff is a registry
    nobody reviews."""
    scratch = scratch_repo(tmp_path)
    build_once(scratch.root)
    first = scratch.latest_file().read_bytes()
    build_once(scratch.root)
    assert scratch.latest_file().read_bytes() == first
    index = json.loads((scratch.root / "registry" / "releases" / "index.json").read_text("utf-8"))
    assert len(index["releases"]) == 1, "the same release twice is one entry, not two"


def test_build_records_the_rollout_it_was_given(tmp_path: Path) -> None:
    """Cohorts come from the registry row (§13), so ``build`` has to be able to write one."""
    from referraleasy_contracts.generated.workflow import Rollout

    scratch = scratch_repo(tmp_path)
    build_once(scratch.root, "--cohort", "canary", "--cohort-percentage", "0.05")
    document = json.loads(scratch.latest_file().read_text(encoding="utf-8"))
    rollout = document["rollout"]
    assert rollout["cohort"] == Rollout(cohort="canary", cohort_percentage=0.05).cohort.value
    assert rollout["cohort_percentage"] == 0.05
    assert rollout["kill_switch_engaged"] is False
    assert (
        document["release_manifest_id"]
        == json.loads((scratch.root / "registry" / "releases" / "index.json").read_text("utf-8"))[
            "latest"
        ]
    )

    # And the mistake the check exists to catch is caught with a sentence, not a schema error.
    wrong = run_script(scratch.root, "build", "--cohort-percentage", "5")
    assert wrong.returncode == EXIT_UNAVAILABLE
    assert "is not a fraction" in wrong.stderr and "0.05" in wrong.stderr


def test_the_manifest_is_complete_enough_to_serve_as_a_baseline(tmp_path: Path) -> None:
    """Read back what ``build`` wrote, through the registry's own reader, and assert the fields
    a baseline must carry."""
    scratch = scratch_repo(tmp_path)
    build_once(scratch.root)
    document = json.loads(scratch.latest_file().read_text(encoding="utf-8"))
    assert document["source"]["git_sha"] == TEST_ENV["RELEASE_GIT_SHA"]
    assert document["source"]["image_digest"] == TEST_ENV["RELEASE_IMAGE_DIGEST"]
    assert set(document["skills"]) == set(SKILL_PACKAGES)
    assert len(document["tools"]) == 19
    assert len(document["agents"]) == 7
    assert all(
        len(agent["instruction_hash"]) == 64 and len(agent["sub_agent_manifest_hash"]) == 64
        for agent in document["agents"]
    )


# ── the gate, failing and then passing ─────────────────────────────────────────────


def test_the_gate_passes_against_its_own_build(tmp_path: Path) -> None:
    scratch = scratch_repo(tmp_path)
    build_once(scratch.root)
    # `check` builds a manifest of its own, so its release id is dated by the clock unless
    # `--built-at` pins it. Unpinned, this test asserted that a build and a check landing on the
    # same UTC date produce one id — true, and true only until midnight, when it started failing
    # against a fixture constant dated the previous day. `test_a_rebuild_of_the_unchanged_tree_…`
    # is the one that says the interesting thing: a rebuild on a later day is a new release and
    # still passes. Here the clock is pinned so that the equality being asserted is about the
    # tree, not about when the suite ran.
    result = run_script(scratch.root, "check", "--built-at", BUILT_AT_FLAG)
    assert result.returncode == EXIT_OK, result.stderr
    assert "no findings" in result.stdout

    as_json = run_script(scratch.root, "check", "--json", "--built-at", BUILT_AT_FLAG)
    payload = json.loads(as_json.stdout)
    assert payload["ok"] is True and payload["failed"] is False
    assert payload["changed"] == [] and payload["findings"] == []
    assert payload["baseline"] == payload["current"]


@pytest.mark.parametrize("mutation", ["prompt", "tool_description", "sub_agent", "skill"])
def test_the_check_fails_when_an_artifact_changes_without_a_bump(
    tmp_path: Path, mutation: str
) -> None:
    """§13's rule, four ways, each one an edit that changes what a model does.

    The prompt is the case §13 names; the other three are the fields a naive manifest would
    have left out — a tool description, a sub-agent's instruction, a skill's body — and a gate
    that missed any of them would be a gate that could be walked around by moving the edit one
    directory over.
    """
    scratch = scratch_repo(tmp_path)
    build_once(scratch.root)
    mutate(scratch.root, mutation)

    result = run_script(scratch.root, "check")
    assert result.returncode == EXIT_GATE_FAILED, f"{result.stdout}\n{result.stderr}"
    assert "ERROR [version_not_advanced]" in result.stderr
    # The component is named as the ledger names it — ``agent-<role>`` — so the person reading
    # the CI output can go straight to the line in ``registry/versions.json`` to edit.
    assert "ERROR [version_not_advanced] agent-provider_matching:" in result.stderr
    assert "Bump the declared version and re-run" in result.stderr

    as_json = run_script(scratch.root, "check", "--json")
    payload = json.loads(as_json.stdout)
    assert payload["failed"] is True
    assert {finding["component"] for finding in payload["findings"]} >= {
        "agent-provider_matching",
        # The release carries the agent's record, so it changed too and its version must rise
        # with it. A person who bumps only the agent meets this second finding next.
        "referraleasy",
    }
    assert payload["need_evaluation"] == [], "a failed gate hands out no evaluation to run"


def mutate(root: Path, which: str) -> None:
    """Change exactly one shipped artifact in the scratch tree, in place.

    Each case edits real shipped text, and ``replace_in_file`` asserts the text was there — a
    mutation that silently did nothing would make the test pass by accident and keep passing
    after the artifact it was testing stopped being read.
    """
    agents = root / "services" / "agents" / "referraleasy_agents"
    if which == "prompt":
        replace_in_file(
            agents / "prompts.py",
            '"which search strategy fits the constraint set",',
            '"which search strategy fits the constraint set, and in what order",',
        )
    elif which == "tool_description":
        replace_in_file(
            agents / "tools" / "specs.py",
            '"Build the provider candidate pool for a specialty and an origin. Distance, filtering, "',
            '"Build a provider candidate pool for a specialty and an origin. Distance, filtering, "',
        )
    elif which == "sub_agent":
        # The directive the parent appends to its own prompt: the one field this manifest
        # carries that the Agent Card's own hash does not (artifacts.AGENT_HASH_GAP).
        replace_in_file(
            agents / "subagents.py",
            "The pool is already filtered, de-duplicated and ranked by distance in code before it\n"
            "reaches you; do not re-derive any of it, and do not choose for the patient. Your own\n"
            "answer is the decision, and it cites the evidence ids the tools returned.",
            "The pool is already filtered, de-duplicated and ranked in code before it reaches you;\n"
            "do not re-derive any of it. Cite the evidence ids the tools returned.",
        )
    elif which == "skill":
        replace_in_file(
            root / "packages" / "skills" / "directory-grounding" / "SKILL.md",
            "- A patient asks whether a specific doctor is available, or whether a particular "
            "appointment is free.",
            "- A patient asks whether a particular doctor is available at a particular site.",
        )
    else:  # pragma: no cover - the parametrisation above is closed
        raise AssertionError(which)


def test_the_gate_passes_once_the_versions_rise_and_names_what_to_evaluate(tmp_path: Path) -> None:
    """The repair: two version bumps, and the evaluation §13 asks for is named in the output.

    Both components, because both changed — and the first half of this test is the reason the
    message has to say which one, rather than "something changed".
    """
    scratch = scratch_repo(tmp_path)
    build_once(scratch.root)
    mutate(scratch.root, "prompt")

    ledger = json.loads(scratch.ledger_file().read_text())
    bump_ledger(
        scratch.root,
        agent_versions={
            "provider_matching": advance_version(ledger["agents"]["provider_matching"])
        },
        release=None,
    )
    half = run_script(scratch.root, "check")
    assert half.returncode == EXIT_GATE_FAILED, "the release version still stands still"
    # Only the release is still standing still: the agent appears in the changed list — it did
    # change — but it is not reported as needing a version bump any more, and the ERROR line
    # naming exactly one component is what says so.
    assert "artifacts changed: agent-provider_matching, referraleasy" in half.stderr
    assert "ERROR [version_not_advanced] agent-provider_matching:" not in half.stderr
    assert "ERROR [version_not_advanced] referraleasy:" in half.stderr

    bump_ledger(scratch.root, agent_versions={}, release=advance_version(ledger["release"]))
    fixed = run_script(scratch.root, "check")
    assert fixed.returncode == EXIT_OK, f"{fixed.stdout}\n{fixed.stderr}"
    assert "artifacts changed:" in fixed.stdout
    assert "evaluation required for: agent-provider_matching, referraleasy" in fixed.stdout
    assert "no findings" in fixed.stdout

    as_json = json.loads(run_script(scratch.root, "check", "--json").stdout)
    assert as_json["ok"] is True and as_json["failed"] is False
    assert as_json["changed"] == ["agent-provider_matching", "referraleasy"]
    assert as_json["need_evaluation"] == ["agent-provider_matching", "referraleasy"]


def test_a_rebuild_of_the_unchanged_tree_still_passes(tmp_path: Path) -> None:
    """The property that keeps the check switched on: CI re-running on an unchanged commit.

    A rebuild has a new id and a new ``created_at`` — it is a second release of the same
    artifact — and it must not demand a version bump. If it did, the honest response would be
    to stop running the gate.
    """
    scratch = scratch_repo(tmp_path)
    build_once(scratch.root)
    later = run_script(scratch.root, "build", "--built-at", "2026-10-16T07:00:00+00:00")
    assert later.returncode == EXIT_OK, later.stderr
    assert "wrote" in later.stdout
    index = json.loads((scratch.root / "registry" / "releases" / "index.json").read_text("utf-8"))
    assert len(index["releases"]) == 2, "two releases of one artifact are two releases"
    # The pointer moved to the newer release, and the gate compares against *it* — same
    # artifact, so there is nothing to report.
    result = run_script(scratch.root, "check")
    assert result.returncode == EXIT_OK, result.stderr
    assert "no findings" in result.stdout


def test_a_ledger_that_drops_a_role_cannot_run_the_check(tmp_path: Path) -> None:
    """A missing declaration is exit 3, not a silent narrowing of the gate.

    Worth being exact about, because it is easy to assume otherwise: ``COMPONENT_REMOVED``
    exists in :mod:`...gate` for a caller that supplies its own component states (and is tested
    in ``test_version_gate.py``), but it is **not reachable from this CLI today**. The gate's
    components are derived from the current ``AgentRole`` enum, so a role that disappeared takes
    its baseline component with it, and the ledger refuses to load without all seven roles
    before any of that. The failure below is therefore the honest answer to "what happens if a
    service goes": the check stops, loudly.
    """
    scratch = scratch_repo(tmp_path)
    build_once(scratch.root)
    ledger = json.loads((scratch.root / "registry" / "versions.json").read_text("utf-8"))
    del ledger["agents"]["recovery_handoff"]
    (scratch.root / "registry" / "versions.json").write_text(
        json.dumps(ledger, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    result = run_script(scratch.root, "check")
    assert result.returncode == EXIT_UNAVAILABLE
    assert "recovery_handoff" in result.stderr
    assert "Traceback" not in result.stderr
