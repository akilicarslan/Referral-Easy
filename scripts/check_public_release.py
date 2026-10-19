#!/usr/bin/env python3
"""Reject private files and machine-specific paths before publication.

This check prints file locations and categories only. Gitleaks handles credentials.
Ignored local files are excluded unless they have already been added to Git.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

LOCAL_PATH = re.compile(
    rb"/(?:Users|home)/[^/\s\"'<>]+|/(?:private/)?var/folders/|"
    rb"[A-Za-z]:\\Users\\[^\\\s\"'<>]+",
    re.IGNORECASE,
)
CI_PATH = re.compile(rb"/" rb"home/runner(?=/work(?:/|[\s`\"']))")
PRIVATE_DIRECTORIES = {".git", ".aws", ".ssh", ".codex", ".agents", "secrets"}
PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


def check(repo: Path, *, tracked_only: bool = False) -> tuple[int, list[str]]:
    args = ["git", "ls-files", "--cached", "-z"]
    if not tracked_only:
        args.extend(["--others", "--exclude-standard"])
    result = subprocess.run(args, cwd=repo, check=True, capture_output=True)
    paths = sorted(set(result.stdout.decode().split("\0")) - {""})
    issues: list[str] = []
    count = 0
    for relative in paths:
        path = repo / relative
        private_env = path.name == ".env" or (
            path.name.startswith(".env.") and path.name != ".env.example"
        )
        if (
            private_env
            or path.name == "MEMORY.md"
            or path.suffix.lower() in PRIVATE_SUFFIXES
            or PRIVATE_DIRECTORIES.intersection(Path(relative).parts)
        ):
            issues.append(f"{relative}: private file must not be published")
            continue
        if not path.resolve().is_relative_to(repo.resolve()):
            issues.append(f"{relative}: symlink points outside the repository")
            continue
        if not path.is_file():
            continue
        count += 1
        for number, line in enumerate(path.read_bytes().splitlines(), start=1):
            for match in LOCAL_PATH.finditer(line):
                if CI_PATH.match(line, match.start()):
                    continue
                issues.append(f"{relative}:{number}: private machine path")
                break
    return count, issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tracked-only", action="store_true")
    args = parser.parse_args()
    try:
        count, issues = check(args.repo_root, tracked_only=args.tracked_only)
    except (OSError, subprocess.CalledProcessError) as exc:
        # Do not print subprocess output or file contents: they may contain secrets.
        print(f"Public-release privacy check could not run ({type(exc).__name__}).")
        return 2
    if issues:
        print("\n".join(issues))
        return 1
    print(f"Public-release privacy check passed ({count} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
