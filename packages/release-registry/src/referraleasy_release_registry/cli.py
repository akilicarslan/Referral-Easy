"""``check_release_versions``: build the §13 manifest, and gate a change against the last one.

Two commands and four exit codes, because a CI job needs to be able to tell *the change is
wrong* apart from *the check is broken*. A check that reported both the same way would be
routed around the first time the environment was missing a variable.

===========  ============================================================================
exit code     meaning
===========  ============================================================================
0             the gate passed
1             the gate failed: an artifact changed and its version did not increase (§13)
2             usage error: the arguments do not name a command this script has
3             the gate could not run: a missing artifact, an unset environment value, an
              incomplete manifest, or no baseline to compare against
===========  ============================================================================

**3 is not 0.** A missing baseline means the gate did not run, and a CI job that treated
"could not run" as "passed" would pass forever after the first time someone deleted the
file. ``--initial`` exists for the genuinely-first release and has to be asked for.

What ``build`` writes, and why each file exists
-----------------------------------------------
``registry/releases/<release_manifest_id>.json``
    The manifest, immutable. One file per release, named by its own id, so the file name
    and the document inside it cannot disagree.
``registry/releases/latest.json``
    The most recently built release. This is what ``make eval`` reads.
``registry/releases/index.json``
    One line per release: id, hashes, build time, git sha, cohort — and the **release
    version**, which the manifest itself does not carry (§13 lists a version for every
    artifact it names and none for the release as a whole; the contract has no field for
    one and this work may not edit it). The gate reads it back from here, which is the
    only reason it is written rather than derived.
``registry/agents/<card-name>.json``
``registry/prompts/<role>.json``
    The per-artifact views. Both directories existed and were empty; these make them
    answer a question — what is this agent's version and hash — without loading a
    three-hundred-line document to find out.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from referraleasy_contracts.generated.workflow import ReleaseManifest, Rollout, RolloutCohort
from referraleasy_release_registry.artifacts import (
    bridge_agents_package,
    card_name,
    rfc_module,
)
from referraleasy_release_registry.errors import RegistryError
from referraleasy_release_registry.gate import (
    ComponentState,
    GateResult,
    check,
    component_states,
)
from referraleasy_release_registry.manifest import (
    BuildInputs,
    artifact_hash,
    build_manifest,
    manifest_hash,
    rollout_facts,
)
from referraleasy_release_registry.versions import LEDGER_FILENAME, VersionLedger, load_ledger

__all__ = [
    "EXIT_GATE_FAILED",
    "EXIT_OK",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE",
    "main",
    "registry_paths",
]

EXIT_OK: Final = 0
EXIT_GATE_FAILED: Final = 1
EXIT_USAGE: Final = 2
EXIT_UNAVAILABLE: Final = 3

#: Where the generated release files live, relative to the repository root.
RELEASES_DIR: Final = Path("registry/releases")
AGENTS_DIR: Final = Path("registry/agents")
PROMPTS_DIR: Final = Path("registry/prompts")

#: The variables ``build`` may be handed by CI, in the priority ``manifest.source_from``
#: documents. Kept here as well so ``--help`` can name them.
SOURCE_ENV: Final[tuple[str, ...]] = ("RELEASE_GIT_SHA", "RELEASE_IMAGE_DIGEST")


@dataclass(frozen=True, slots=True)
class RegistryPaths:
    """The generated registry, resolved once so every writer agrees on where it is."""

    root: Path

    @property
    def releases(self) -> Path:
        return self.root / RELEASES_DIR

    @property
    def agents(self) -> Path:
        return self.root / AGENTS_DIR

    @property
    def prompts(self) -> Path:
        return self.root / PROMPTS_DIR

    @property
    def ledger(self) -> Path:
        return self.root / LEDGER_FILENAME

    @property
    def latest(self) -> Path:
        return self.releases / "latest.json"

    @property
    def index(self) -> Path:
        return self.releases / "index.json"


def registry_paths(root: Path) -> RegistryPaths:
    return RegistryPaths(root=root)


# ── the environment, as the deployment will see it ──────────────────────────────────


def _parse_env_file(path: Path) -> dict[str, str]:
    """A minimal ``KEY=VALUE`` reader.

    The fourth copy of fifteen lines in this repository, and the least bad option: the
    canonical readers live in ``scripts/validate_env.py`` and
    ``referraleasy_guardrails.config``, and ``referraleasy_guardrails`` is not a uv
    workspace member, so importing it would make a release build depend on a package the
    environment does not install. A future extraction into ``referraleasy-domain`` would
    delete all four.

    Nothing here is ever printed or recorded: :func:`referraleasy_release_registry.artifacts.env_snapshot`
    reads a whitelist, and the whitelist contains no credential.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    # The real environment wins, so `RELEASE_GIT_SHA=... script` overrides the file.
    values.update(os.environ)
    return values


def resolve_repo_root(start: Path | None = None) -> Path:
    """The directory holding both ``Makefile`` and ``pyproject.toml``.

    Searched upward rather than hard-coded: the script is run from the repository root by
    CI and from anywhere at all by a person, and both should work.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "Makefile").is_file() and (candidate / "pyproject.toml").is_file():
            return candidate
    raise RegistryError(
        f"no repository root found at or above {here}: a directory containing both Makefile "
        "and pyproject.toml is required"
    )


# ── building ────────────────────────────────────────────────────────────────────────


def _rollout(args: argparse.Namespace) -> Rollout:
    """The rollout block, validated here so the refusal names the flag.

    ``cohort_percentage`` is a **fraction** (``0..1``) because that is what the contract
    says (``packages/contracts/schemas/workflow.json``: ``"minimum": 0, "maximum": 1``).
    Passing ``5`` and meaning five percent is the mistake this check exists to catch, and
    pydantic would catch it too — but with a validation error rather than a sentence
    telling the operator which flag to fix.
    """
    share = float(args.cohort_percentage)
    if not 0.0 <= share <= 1.0:
        raise RegistryError(
            f"--cohort-percentage={args.cohort_percentage} is not a fraction. The contract "
            "bounds cohort_percentage to 0..1, so 5% is --cohort-percentage 0.05."
        )
    return Rollout(
        cohort=RolloutCohort(args.cohort),
        cohort_percentage=share,
        kill_switch_engaged=bool(args.kill_switch),
    )


def _built_at(args: argparse.Namespace) -> datetime:
    if not args.built_at:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(args.built_at)
    except ValueError as error:
        raise RegistryError(f"--built-at={args.built_at!r} is not an ISO-8601 timestamp") from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def build(
    *,
    repo_root: Path,
    args: argparse.Namespace,
    ledger: VersionLedger,
    env: Mapping[str, str],
) -> ReleaseManifest:
    """Assemble the manifest for this tree. Raises rather than defaulting anything.

    ``env`` is a parameter rather than read here so that a test can build two manifests
    from two environments in one process — the only way to prove that a manifest follows
    the deployment rather than the ambient shell.
    """
    env = {**env}
    if args.git_sha:
        env["RELEASE_GIT_SHA"] = args.git_sha
    if args.image_digest:
        env["RELEASE_IMAGE_DIGEST"] = args.image_digest
    bridge_agents_package(repo_root)
    return build_manifest(
        BuildInputs(
            ledger=ledger,
            env=env,
            repo_root=repo_root,
            built_at=_built_at(args),
            rollout=_rollout(args),
        )
    )


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write canonical, sorted, newline-terminated JSON.

    Sorted and stable so two builds of the same release produce a byte-identical file: a
    registry whose diffs are noise cannot be reviewed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
    path.write_text(f"{text}\n", encoding="utf-8")


def _artifact_views(manifest: ReleaseManifest) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(per-agent files, per-prompt files)``, keyed by the file name each should have."""
    roles = rfc_module("referraleasy_agent_runtime.roles")
    agents: dict[str, Any] = {}
    prompts: dict[str, Any] = {}
    by_name = {agent.agent_name: agent for agent in manifest.agents}
    for role in roles.AgentRole:
        name = card_name(role)
        record = by_name[name]
        agents[f"{name}.json"] = {
            "release_manifest_id": manifest.release_manifest_id.root,
            "agent_name": record.agent_name,
            "role": role.value,
            "service": roles.service_name(role),
            "version": record.version,
            "agent_hash": record.agent_hash,
            "instruction_hash": record.instruction_hash,
            "sub_agent_manifest_hash": record.sub_agent_manifest_hash,
            "a2a_card_hash": record.a2a_card_hash,
            "tool_description_hashes": record.tool_description_hashes,
            "skill_hashes": record.skill_hashes,
        }
        prompts[f"{role.value}.json"] = {
            "release_manifest_id": manifest.release_manifest_id.root,
            "role": role.value,
            "agent_name": name,
            "prompt_version": manifest.prompts[name],
            "instruction_hash": record.instruction_hash,
        }
    return agents, prompts


def _index_entry(manifest: ReleaseManifest, ledger: VersionLedger) -> dict[str, Any]:
    # The index carries the cohort because a reader who has only the index — the admin page,
    # `make eval`, a reviewer diffing two releases — must be able to see which cohort a
    # release is in without loading the manifest. `rollout_facts` refuses a release whose
    # rollout nobody wrote down, rather than filing it under a default cohort.
    rollout = rollout_facts(manifest)
    return {
        "release_manifest_id": manifest.release_manifest_id.root,
        "release_version": ledger.release,
        "artifact_hash": artifact_hash(manifest),
        "manifest_hash": manifest_hash(manifest),
        "created_at": manifest.created_at.root.astimezone(UTC).isoformat(),
        "git_sha": manifest.source.git_sha,
        "image_digest": manifest.source.image_digest,
        "cohort": rollout.cohort,
        "cohort_percentage": rollout.share,
        "file": f"{manifest.release_manifest_id.root}.json",
    }


def _read_index(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"latest": None, "releases": []}
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("releases"), list):
        raise RegistryError(f"{path} is not a release index")
    return raw


def write_release(
    manifest: ReleaseManifest, ledger: VersionLedger, *, paths: RegistryPaths
) -> Path:
    """Persist one release: the manifest, the pointer, the index and the per-artifact views.

    Files are written in order of usefulness rather than atomically as a set: if this dies
    half-way the manifest exists and the index is stale, which is a state the next `build`
    repairs. A half-written *manifest* is the state worth avoiding, and a single
    ``write_text`` of a fully built document is not.
    """
    document = manifest.model_dump(mode="json")
    release_path = paths.releases / f"{manifest.release_manifest_id.root}.json"
    _write_json(release_path, document)
    _write_json(paths.latest, document)

    index = _read_index(paths.index)
    entry = _index_entry(manifest, ledger)
    entries = [
        existing
        for existing in index["releases"]
        if isinstance(existing, dict)
        and existing.get("release_manifest_id") != entry["release_manifest_id"]
    ]
    entries.append(entry)
    entries.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    _write_json(
        paths.index,
        {
            "latest": entry["release_manifest_id"],
            "latest_entry": entry,
            "releases": entries,
        },
    )

    agents, prompts = _artifact_views(manifest)
    for name, payload in agents.items():
        _write_json(paths.agents / name, payload)
    for name, payload in prompts.items():
        _write_json(paths.prompts / name, payload)
    return release_path


# ── checking ────────────────────────────────────────────────────────────────────────


def _baseline_states(
    *, paths: RegistryPaths, baseline_path: Path
) -> tuple[tuple[ComponentState, ...], ReleaseManifest]:
    """The baseline's component states, with its release version read from the index.

    The release-level version is the one number the gate needs that the manifest does not
    hold (see the module docstring), so it comes from ``index.json`` — the same file the
    version was written to. A missing entry is a refusal, not an assumed version: guessing
    "probably unchanged" is exactly how a gate stops being able to fail.
    """
    raw: Any = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline = ReleaseManifest.model_validate(raw)
    index = _read_index(paths.index)
    entry = next(
        (
            item
            for item in index["releases"]
            if isinstance(item, dict)
            and item.get("release_manifest_id") == baseline.release_manifest_id.root
        ),
        None,
    )
    if entry is None or not isinstance(entry.get("release_version"), str):
        raise RegistryError(
            f"{paths.index} records no release version for "
            f"{baseline.release_manifest_id.root}. The manifest carries no release version of "
            "its own (§13 names one for every artifact but the release), so the gate cannot "
            "compare it; re-run `build` to regenerate the index."
        )
    ledger = load_ledger(paths.ledger)
    states = component_states(baseline, _ledger_with_release(ledger, str(entry["release_version"])))
    return states, baseline


def _ledger_with_release(ledger: VersionLedger, release_version: str) -> VersionLedger:
    """The current ledger with only its ``release`` field replaced.

    The baseline's *components* keep the versions recorded in its own manifest — they are
    read from the manifest, not from the ledger. Only the release-level row needs the
    historical value, and it is the one thing the ledger cannot supply for a past release.
    """
    return VersionLedger(
        release=release_version,
        schema=ledger.schema,
        agents=ledger.agents,
        prompts=ledger.prompts,
        skills=ledger.skills,
    )


def render(result: GateResult, *, baseline_id: str, current_id: str) -> str:
    """A report a person can act on, rather than a status word."""
    lines = [f"baseline: {baseline_id}", f"current:  {current_id}"]
    if result.changed:
        lines.append(f"artifacts changed: {', '.join(result.changed)}")
    if result.need_evaluation:
        lines.append(f"evaluation required for: {', '.join(result.need_evaluation)}")
    if result.findings:
        lines.append("")
        for finding in result.findings:
            lines.append(f"ERROR [{finding.code}] {finding.component}: {finding.detail}")
    else:
        lines.append("no findings")
    return "\n".join(lines)


# ── argument parsing ────────────────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_release_versions",
        description=(
            "Build the §13 release manifest and fail when an artifact changed without its "
            "version increasing."
        ),
        epilog=(
            "exit codes: 0 ok, 1 gate failed (a version did not increase), 2 usage error, "
            "3 the gate could not run (missing artifact, environment value or baseline)."
        ),
    )
    parser.add_argument("command", choices=("check", "build"), nargs="?", default="check")
    parser.add_argument("--repo-root", type=Path, help="defaults to the nearest Makefile")
    parser.add_argument("--baseline", type=Path, help="defaults to registry/releases/latest.json")
    parser.add_argument(
        "--initial",
        action="store_true",
        help="allow `check` to pass with no baseline. Required explicitly: a missing baseline "
        "otherwise exits 3, because a gate that passes when its input is absent is not a gate.",
    )
    parser.add_argument(
        "--git-sha",
        help=f"the commit this release was built from (otherwise {'/'.join(SOURCE_ENV[:1])}, "
        "otherwise `git rev-parse HEAD`)",
    )
    parser.add_argument("--image-digest", help="the built image's digest (RELEASE_IMAGE_DIGEST)")
    parser.add_argument(
        "--built-at", help="ISO-8601 build time; fixed, this makes output reproducible"
    )
    parser.add_argument(
        "--cohort",
        default=RolloutCohort.stable.value,
        choices=[cohort.value for cohort in RolloutCohort],
        help="rollout cohort (default: stable)",
    )
    parser.add_argument(
        "--cohort-percentage",
        type=float,
        default=1.0,
        help="rollout share as a fraction, 0..1 (default: 1.0)",
    )
    parser.add_argument("--kill-switch", action="store_true", help="mark the rollout as stopped")
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    return parser


def main(argv: Sequence[str] | None = None, *, repo_root: Path | None = None) -> int:
    """Run the CLI and return the exit code. Never raises: a traceback is not a gate report."""
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        root = (args.repo_root or repo_root or resolve_repo_root()).resolve()
        paths = registry_paths(root)
        ledger = load_ledger(paths.ledger)
        current = build(
            repo_root=root, args=args, ledger=ledger, env=_parse_env_file(root / ".env")
        )
        if args.command == "build":
            written = write_release(current, ledger, paths=paths)
            print(f"wrote {written}")
            print(f"release: {current.release_manifest_id.root}  version: {ledger.release}")
            return EXIT_OK

        baseline_path = args.baseline or paths.latest
        if not baseline_path.is_file():
            if args.initial:
                print(
                    f"no baseline at {baseline_path}: nothing to compare. --initial was passed, "
                    "so this gate reports nothing rather than passing."
                )
                return EXIT_OK
            print(
                f"error: no baseline release at {baseline_path}. The gate compares this build "
                "against the last one, and it cannot compare against nothing (pass --initial "
                "for the first release of a new registry).",
                file=sys.stderr,
            )
            return EXIT_UNAVAILABLE
        baseline_states, baseline = _baseline_states(paths=paths, baseline_path=baseline_path)
        current_states = component_states(current, ledger)
        result = check(baseline_states, current_states)
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": result.ok,
                        "failed": result.failed,
                        "baseline": baseline.release_manifest_id.root,
                        "current": current.release_manifest_id.root,
                        "changed": list(result.changed),
                        "need_evaluation": list(result.need_evaluation),
                        "findings": [
                            {
                                "code": finding.code,
                                "component": finding.component,
                                "detail": finding.detail,
                            }
                            for finding in result.findings
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            report = render(
                result,
                baseline_id=baseline.release_manifest_id.root,
                current_id=current.release_manifest_id.root,
            )
            print(report, file=sys.stderr if result.failed else sys.stdout)
        return EXIT_GATE_FAILED if result.failed else EXIT_OK
    except (RegistryError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except Exception as error:
        # A pydantic validation error from a malformed manifest, an import failure in one
        # of the seven agent packages, anything at all: a CI job needs an exit code and a
        # sentence, not a stack trace. 3 rather than 1 — "the check could not run" is a
        # different problem from "the change is wrong", and reporting the second when the
        # first happened is how a team learns to ignore the check.
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
