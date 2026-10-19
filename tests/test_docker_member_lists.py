"""Every workspace member reaches every image that installs from the workspace.

Two invariants, both enforced by tools that fail *late* — at the second `uv sync`, minutes into a
build, after the dependency layer has been fetched:

1. **Every member's manifest is copied.** uv resolves the workspace from the root manifest and
   refuses a member whose path does not exist, so one missing `COPY …/pyproject.toml` breaks the
   build for every image, not just the one that needed the package.
2. **Every member in the installed package's dependency closure has its source copied.** A manifest
   alone gets past resolution and then fails the install with
   `Distribution not found at: file:///repo/<member>`.

**This file exists because both broke, twice, in the same way.** `services/gateway/Dockerfile`
stopped building when `services/workers` joined the workspace — its own comments record that — and
then broke again when `packages/eval`, `packages/alerts` and `packages/release-registry` joined,
because the fix both times was to append the new member to a hand-maintained list. The second
breakage is the interesting one: the gateway *depends* on `referraleasy-alerts`, so the image was
buildable-from-a-stale-checkout and unbuildable-from-the-repo, and nothing in the suite noticed
because no test read a Dockerfile at all. A list that a phase must remember to extend is a list that
will be forgotten; this is the reminder that does not depend on anyone remembering.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

#: `COPY <src>… <dest>`, with the `--from=`/`--chown=` flags a build stage uses removed first.
_COPY: Final = re.compile(r"^COPY\s+(?!--from)(.*)$")
#: `uv sync … --package <name>`, which names the member whose closure the image must carry.
_SYNC_PACKAGE: Final = re.compile(r"uv sync\b[^\n]*--package\s+(\S+)")


def _dockerfiles() -> list[Path]:
    """Every Dockerfile that installs from the uv workspace, in a stable order."""
    found = [
        path
        for path in sorted(REPO_ROOT.glob("**/Dockerfile"))
        if ".venv" not in path.parts and "node_modules" not in path.parts
    ]
    return [path for path in found if "uv sync" in path.read_text(encoding="utf-8")]


def _workspace() -> tuple[dict[str, Path], dict[str, set[str]]]:
    """Member name → its directory, and name → the workspace deps it declares.

    Paths are repo-relative, because that is the form a `COPY` line uses.
    """
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members: dict[str, Path] = {}
    edges: dict[str, set[str]] = {}
    for member in manifest["tool"]["uv"]["workspace"]["members"]:
        project = tomllib.loads((REPO_ROOT / member / "pyproject.toml").read_text(encoding="utf-8"))
        name = project["project"]["name"]
        members[name] = Path(member)
        edges[name] = {
            dep.split("[")[0].split(">")[0].split("=")[0].strip()
            for dep in project["project"].get("dependencies", [])
            if dep.startswith("referraleasy-")
        }
    return members, edges


def _closure(name: str, edges: dict[str, set[str]]) -> set[str]:
    """`name` and everything it reaches, so a member reached through another is not missed.

    The gateway depends on `referraleasy-alerts`, which depends on `referraleasy-telemetry` — the
    telemetry source was in the list before alerts was in the repo at all, which is luck rather
    than the invariant. A one-hop check would have passed while the transitive one is the property
    the install actually has.
    """
    seen = {name}
    pending = [name]
    while pending:
        for dep in edges.get(pending.pop(), set()):
            if dep not in seen:
                seen.add(dep)
                pending.append(dep)
    return seen


def _copied_sources(text: str) -> list[str]:
    """The source arguments of every plain `COPY` — flags and destinations dropped.

    `COPY --from=build …` is a stage-to-stage copy of something already built and is not evidence
    that a path from the build context was made available.
    """
    out: list[str] = []
    for raw in text.splitlines():
        match = _COPY.match(raw.strip())
        if match is None:
            continue
        parts = [part for part in match.group(1).split() if not part.startswith("--")]
        out.extend(parts[:-1])  # the last argument is the destination
    return out


def _installed_packages(text: str) -> set[str]:
    """The members a Dockerfile installs — the `uv sync` calls that are not `--no-install-workspace`.

    That flag marks the dependency-only warm-up sync, which by design installs no member at all;
    the sync after it is the one whose closure has to be present.
    """
    installed: set[str] = set()
    for line in text.splitlines():
        if "--no-install-workspace" in line:
            continue
        match = _SYNC_PACKAGE.search(line)
        if match is not None:
            installed.add(match.group(1))
    return installed


DOCKERFILES: Final = _dockerfiles()
MEMBERS: Final = _workspace()

assert DOCKERFILES, "no workspace Dockerfiles found — this guard would pass over nothing"


def test_the_guard_found_the_workspace() -> None:
    """A guard over an empty list is the failure mode this repository treats as worse than red."""
    names, edges = MEMBERS
    assert len(names) >= 10, f"only {len(names)} workspace members parsed: {sorted(names)}"
    assert "referraleasy-alerts" in names
    assert edges["referraleasy-gateway"] >= {"referraleasy-alerts"}


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_member_manifest_is_copied(dockerfile: Path) -> None:
    """uv refuses a workspace member whose path is missing, so every image needs every manifest."""
    names, _ = MEMBERS
    text = dockerfile.read_text(encoding="utf-8")
    copied = _copied_sources(text)
    missing = sorted(
        str(member / "pyproject.toml")
        for member in names.values()
        if str(member / "pyproject.toml") not in copied
    )
    assert not missing, (
        f"{dockerfile.relative_to(REPO_ROOT)} does not copy the manifest of "
        f"{', '.join(missing)}. uv resolves the workspace from the root manifest and fails the "
        f"build with 'Distribution not found at: file:///repo/<member>' — add a "
        f"COPY <member>/pyproject.toml line beside the others."
    )


#: `resolve(__dirname, 'index.html')` in a Vite `build.rollupOptions.input` map. The one place a
#: frontend entry point is declared, and therefore the list every HTML entry has to reach.
_VITE_ENTRY: Final = re.compile(r"resolve\(\s*__dirname\s*,\s*['\"]([^'\"]+)['\"]\s*\)")


def test_every_vite_entry_point_reaches_the_web_image() -> None:
    """The frontend's version of the same failure: an entry added to the build and not to `COPY`.

    `apps/web/admin.html` was declared as a third `input` in `vite.config.ts` and left out of the
    Dockerfile's `COPY` line, so the image failed with `Could not resolve entry module
    "admin.html"` — after installing the whole pnpm store. The list of entries lives in the Vite
    config and nowhere else, so this reads it from there rather than repeating it.
    """
    config = (REPO_ROOT / "apps/web" / "vite.config.ts").read_text(encoding="utf-8")
    entries = _VITE_ENTRY.findall(config)
    assert len(entries) >= 2, f"expected the entry map to name each HTML entry, got {entries}"

    dockerfile = REPO_ROOT / "apps/web" / "Dockerfile"
    copied = _copied_sources(dockerfile.read_text(encoding="utf-8"))
    missing = sorted(entry for entry in entries if f"apps/web/{entry}" not in copied)
    assert not missing, (
        f"apps/web/Dockerfile never copies {', '.join(missing)}, but vite.config.ts declares "
        f"{'them' if len(missing) > 1 else 'it'} as a build input. The image fails with "
        f"'Could not resolve entry module' after the dependency stages have run."
    )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_installed_member_has_its_source_copied(dockerfile: Path) -> None:
    """A manifest gets past resolution; only the source gets past the install."""
    names, edges = MEMBERS
    text = dockerfile.read_text(encoding="utf-8")
    sources = [
        source
        for source in _copied_sources(text)
        if not source.endswith("pyproject.toml") and not source.endswith(".lock")
    ]
    for package in sorted(_installed_packages(text)):
        required = _closure(package, edges)
        for name in sorted(required):
            member = names.get(name)
            if member is None:
                continue  # a distribution outside this workspace; not this guard's business
            prefix = f"{member}/"
            assert any(source.startswith(prefix) for source in sources), (
                f"{dockerfile.relative_to(REPO_ROOT)} installs {package}, whose closure includes "
                f"{name}, but copies no source from {member}/. The build fails at "
                f"'Distribution not found at: file:///repo/{member}'."
            )
