"""The publication guard must detect private data without echoing its values."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_public_release.py"


@pytest.mark.parametrize(
    ("filename", "content", "tracked", "expected"),
    [
        ("notes.md", "/" + "Users/private-person/work/project", False, 1),
        ("notes.md", "/" + "private/" + "var/" + "folders/private-machine/report.html", True, 1),
        ("notes.md", "C:" + "\\Users\\private-person\\work", True, 1),
        ("notes.md", "/" + "home/runner/work/project", True, 0),
        ("notes.md", "/" + "home/runner/private", True, 1),
        (".env", "PRIVATE_VALUE=must-never-be-printed", True, 1),
        (".env", "PRIVATE_VALUE=must-never-be-printed", False, 0),
        (".env.example", "PRIVATE_VALUE=CHANGEME", True, 0),
        ("MEMORY.md", "private working notes", True, 1),
    ],
)
def test_publication_boundary(
    tmp_path: Path, filename: str, content: str, tracked: bool, expected: int
) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / filename).write_text(content)
    if tracked:
        subprocess.run(["git", "add", "--force", filename], cwd=tmp_path, check=True)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected
    assert "private-person" not in result.stdout
    assert "private-machine" not in result.stdout
    assert "must-never-be-printed" not in result.stdout


def test_an_external_symlink_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    outside = tmp_path / "private.txt"
    outside.write_text("must-never-be-printed")
    (repo / "notes.md").symlink_to(outside)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "outside the repository" in result.stdout
    assert "must-never-be-printed" not in result.stdout
