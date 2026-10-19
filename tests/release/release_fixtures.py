"""Shared constants, environment and builders for the §13 release registry tests.

A plain module with a unique name, not ``conftest.py``, for the reason
``tests/skills/skills_fixtures.py`` sets out at length: pytest puts every ``conftest.py`` onto
``sys.path`` under the bare name ``conftest``, so ``from conftest import ...`` silently
resolves to whichever directory pytest imported last, and ``tests/test_test_layout.py``
fails the suite when it sees one. Import the names below from here.

The ``sys.path`` bridge
-----------------------
``packages/release-registry`` was not a member of ``[tool.uv.workspace]`` when this bridge
was written, so ``referraleasy_release_registry`` was not installed into the shared
virtualenv. **It is a member now** — that file also lists it in the root dependencies, and the
``Makefile``'s ``PYTHON_SOURCE_ROOTS`` names its source — so with a synced virtualenv the
bridge already resolves and does nothing. It is kept for the plain-checkout case. The bridge
is the same one ``tests/agents/conftest.py`` documents and ``scripts/check_release_versions.py``
uses: it adds the directory that *contains* the import package and nothing else, and only when
the package does not already resolve.

The environment
---------------
:data:`TEST_ENV` is the smallest environment a manifest can be built from, and every value in
it is a deliberate fiction: ``MODEL_BACKEND=offline`` so no test can reach a live provider,
``test/model`` for the model id, and an all-``f`` git sha. Fictions are legitimate **in a
test**; the registry itself refuses to invent these values, which is the difference the
``test_source_refuses`` test exists to hold. No credential is ever placed in a fixture, and
``.env`` is never copied into a scratch tree.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SRC = REPO_ROOT / "packages" / "release-registry" / "src"
if PACKAGE_SRC.is_dir() and str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from referraleasy_release_registry.artifacts import (  # noqa: E402  (after the bridge)
    bridge_agents_package,
)
from referraleasy_release_registry.manifest import (  # noqa: E402
    BuildInputs,
    build_manifest,
)
from referraleasy_release_registry.versions import (  # noqa: E402
    LEDGER_FILENAME,
    VersionLedger,
    load_ledger,
)

bridge_agents_package(REPO_ROOT)

#: A fixed instant, so ``release_manifest_id`` and ``created_at`` are the same on every run.
#: A test that used ``now()`` would produce a different manifest id each time and could not
#: assert that two runs agree.
BUILT_AT: Final = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)

#: A git sha that is obviously not one. ``deadbeef`` repeated is the conventional stand-in.
TEST_GIT_SHA: Final = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

#: A digest in the shape the field requires. Not a real image — see the module docstring.
TEST_IMAGE_DIGEST: Final = "sha256:" + "0f" * 32

#: The whitelisted environment a manifest needs, with every value a test value.
TEST_ENV: Final[dict[str, str]] = {
    "MODEL_BACKEND": "offline",
    "AGENT_MODEL_PROVIDER": "openrouter",
    "AGENT_MODEL_ID": "test/model",
    "MAX_MODEL_CALLS_PER_TURN": "12",
    "MAX_TOOL_CALLS_PER_TURN": "16",
    "AGENT_ATTEMPT_TIMEOUT_MS": "3000",
    "AGENT_MAX_ATTEMPTS": "3",
    "TURN_DEADLINE_MS": "30000",
    "MAX_DELEGATIONS_PER_TURN": "8",
    "GUARDRAIL_MODE": "three_stage",
    "GUARDRAIL_LOCAL_MODEL": "test/local-model@0000000000000000000000000000000000000000",
    "GUARDRAIL_LOCAL_THRESHOLD": "0.5",
    "EMBEDDING_MODEL_ID": "test/embedding",
    "EMBEDDING_DIMENSIONS": "768",
    "RERANK_MODEL_ID": "test/reranker",
    "RELEASE_GIT_SHA": TEST_GIT_SHA,
    "RELEASE_IMAGE_DIGEST": TEST_IMAGE_DIGEST,
}

#: The seven roles §3 defines, spelled as ``agent_role.code`` spells them.
ROLE_CODES: Final[tuple[str, ...]] = (
    "care_coordinator",
    "referral_intake",
    "coverage_network",
    "provider_matching",
    "referral_readiness",
    "scheduling",
    "recovery_handoff",
)

#: The seven service names, which are also the registry's component names for the agents.
SERVICE_NAMES: Final[tuple[str, ...]] = tuple(f"agent-{role}" for role in ROLE_CODES)

#: The release-as-a-whole component.
RELEASE_COMPONENT: Final = "referraleasy"

#: The eight skill packages §6 defines, by package name. Note that a package has *two*
#: names: the directory (`booking-confirmation`) and the id its manifest publishes to the
#: Agent Card (`scheduling.confirm_booking_with_consent`). The manifest's per-agent
#: ``skill_hashes`` is keyed by the second, so it can be read beside the card; the
#: manifest's ``skills`` section is keyed by the first, so it can be read beside the
#: directories. Tests that mix them up are testing the wrong mapping.
SKILL_PACKAGES: Final[tuple[str, ...]] = (
    "booking-confirmation",
    "constraint-negotiation",
    "coverage-interpretation",
    "directory-grounding",
    "handoff-triage",
    "referral-intake",
    "referral-readiness",
    "uncertain-booking-reconciliation",
)

#: What ``build`` copies into a scratch repository for the CLI end-to-end tests. Only what
#: the build reads: a whole-repo copy would drag in a 100 MB virtualenv and other agents'
#: working files, and a test that copied ``.env`` would copy a live credential.
SCRATCH_TREES: Final[tuple[str, ...]] = (
    "Makefile",
    "pyproject.toml",
    # Both lockfiles: the recorded ``lockfile_hash`` covers every one that exists, so a
    # scratch tree missing one produces a different hash — which is exactly how the
    # "another directory hashes the same" test caught this list being incomplete.
    "uv.lock",
    "pnpm-lock.yaml",
    "registry",
    "services/agents",
    "packages/skills",
    "packages/contracts",
    "packages/rag",
    "packages/guardrails",
)

#: Copied above, then removed: the registry's **build output**.
#:
#: Every scratch repo has to start *unbuilt*, for two reasons and the second is the serious one.
#:
#: * `test_a_missing_baseline_is_not_a_pass` needs a tree with no baseline to compare against, and
#:   it used to get one by accident — `registry/releases/` did not exist in the repository at all,
#:   because no image digest had ever been available to build a manifest with. The repository now
#:   has a real `latest.json`, so the accident stopped holding and the test failed. A test that
#:   depends on the project never having shipped breaks exactly when the project ships.
#: * **A copied baseline makes `build` untestable.** The tests assert that `build` *wrote*
#:   `latest.json` and the per-release manifest; if the fixture had already put one there, those
#:   assertions would pass over a file the build never produced. That is a green run over nothing,
#:   which this repository treats as worse than a red one.
SCRATCH_EXCLUDES: Final[tuple[str, ...]] = ("registry/releases",)


@dataclass(frozen=True, slots=True)
class ScratchRepo:
    """A disposable copy of the tree the build reads, plus the env file it will load."""

    root: Path

    def env_file(self, extra: Mapping[str, str] | None = None) -> Path:
        """Write ``.env`` with the test values, and return its path."""
        values = {**TEST_ENV, **(extra or {})}
        path = self.root / ".env"
        path.write_text(
            "\n".join(f"{key}={value}" for key, value in sorted(values.items())) + "\n",
            encoding="utf-8",
        )
        return path

    def prompt_file(self, role: str) -> Path:
        return self.root / "services" / "agents" / "referraleasy_agents" / "prompts.py"

    def subagent_file(self) -> Path:
        return self.root / "services" / "agents" / "referraleasy_agents" / "subagents.py"

    def ledger_file(self) -> Path:
        return self.root / LEDGER_FILENAME

    def manifest_file(self, release_manifest_id: str) -> Path:
        return self.root / "registry" / "releases" / f"{release_manifest_id}.json"

    def latest_file(self) -> Path:
        return self.root / "registry" / "releases" / "latest.json"


def scratch_repo(tmp_path: Path) -> ScratchRepo:
    """Copy the trees a build reads into ``tmp_path``, and give it an ``.env``.

    Used by the tests that must *change* a source file — mutating a prompt has to happen
    somewhere, and doing it in the working tree would edit a file three other agents share.
    """
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    for relative in SCRATCH_TREES:
        source = REPO_ROOT / relative
        if not source.exists():
            continue
        destination = root / relative
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    for relative in SCRATCH_EXCLUDES:
        # A directory this tree does not have yet is not an error: the point is that the scratch
        # repo ends up without it, and it started without it.
        shutil.rmtree(root / relative, ignore_errors=True)
    repo = ScratchRepo(root=root)
    repo.env_file()
    return repo


def with_versions(
    ledger: VersionLedger,
    *,
    release: str | None = None,
    agents: Mapping[str, str] | None = None,
    prompts: Mapping[str, str] | None = None,
) -> VersionLedger:
    """A copy of ``ledger`` with the named versions replaced.

    The gate compares a *declared version* against a *derived hash*, and the declared version
    is read from the manifest, which reads it from ``BuildInputs.ledger``. So "bump the
    version and re-run" has to mean bumping the ledger the build is given and not nudging the
    value afterwards — a test that did the latter would assert against a number no build ever
    produced. An unknown role is refused rather than ignored, because a typo'd role name would
    leave the version untested and the test green.
    """
    unknown = sorted(set(agents or {}) - set(ledger.agents))
    if unknown:
        raise AssertionError(
            f"{unknown} are not roles this ledger declares; it declares {sorted(ledger.agents)}"
        )
    unknown_prompts = sorted(set(prompts or {}) - set(ledger.prompts))
    if unknown_prompts:
        raise AssertionError(f"{unknown_prompts} are not declared prompts")
    return replace(
        ledger,
        release=release or ledger.release,
        agents={**ledger.agents, **(agents or {})},
        prompts={**ledger.prompts, **(prompts or {})},
    )


def advance_version(version: str, level: str = "patch") -> str:
    """Advance the current release, so gate tests survive a product version bump."""
    major, minor, patch = map(int, version.split("-", 1)[0].split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def build_test_manifest(
    *,
    root: Path = REPO_ROOT,
    env: Mapping[str, str] | None = None,
    built_at: datetime = BUILT_AT,
    ledger: VersionLedger | None = None,
    cohort: str = "stable",
    cohort_percentage: float = 1.0,
    kill_switch: bool = False,
    parent_release_manifest_id: str | None = None,
):
    """Build the manifest for ``root`` from the test environment.

    Session-scoped callers build once and share; a test that needs a *different* manifest
    (a bumped ledger, a mutated prompt) builds its own. ``ledger`` defaults to the one at
    ``root``; a caller supplying one is asking for a release whose declarations differ
    from what is on disk, which is what a version bump looks like before it is committed.
    """
    from referraleasy_contracts.generated.workflow import Rollout

    return build_manifest(
        BuildInputs(
            ledger=ledger or load_ledger(root / LEDGER_FILENAME),
            env={**TEST_ENV, **(env or {})},
            repo_root=root,
            built_at=built_at,
            rollout=Rollout(
                cohort=cohort,
                cohort_percentage=cohort_percentage,
                kill_switch_engaged=kill_switch,
            ),
            parent_release_manifest_id=parent_release_manifest_id,
        )
    )


def agent_named(manifest, agent_name: str):
    """The manifest's record for ``agent_name``, refusing a name it does not carry."""
    for agent in manifest.agents:
        if agent.agent_name == agent_name:
            return agent
    raise AssertionError(
        f"{agent_name!r} is not in the manifest; it carries "
        f"{sorted(agent.agent_name for agent in manifest.agents)}"
    )


def replace_in_file(path: Path, old: str, new: str) -> None:
    """Replace ``old`` with ``new`` in ``path``, asserting the text was there.

    Asserting first is the point: a mutation test whose mutation silently did nothing would
    pass by accident and would keep passing after the code it was testing changed.
    """
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{path} does not contain {old!r}; the fixture is out of date"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
