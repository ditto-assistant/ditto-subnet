from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
VERSION_CHECK = ROOT / "research/dittobench-datagen/scripts/verify-version-bump.sh"
VERSION_FILE = Path("research/dittobench-datagen/internal/version/version.go")


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", VERSION_FILE.as_posix())
    _git(repository, "commit", "-qm", message)
    return _git(repository, "rev-parse", "HEAD")


def test_datagen_changes_require_a_component_version_bump(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Datagen Release Test")
    path = tmp_path / VERSION_FILE
    path.parent.mkdir(parents=True)
    path.write_text('package version\n\nconst Version = "0.13.2"\n')
    base = _commit(tmp_path, "initial version")

    unchanged = subprocess.run(
        [VERSION_CHECK, base], cwd=tmp_path, capture_output=True, text=True
    )
    assert unchanged.returncode != 0
    assert "bump" in unchanged.stderr

    path.write_text('package version\n\nconst Version = "0.13.3"\n')
    _commit(tmp_path, "bump version")
    subprocess.run([VERSION_CHECK, base], cwd=tmp_path, check=True)


def test_first_datagen_release_accepts_the_zero_base(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    path = tmp_path / VERSION_FILE
    path.parent.mkdir(parents=True)
    path.write_text('package version\n\nconst Version = "0.13.2"\n')

    subprocess.run([VERSION_CHECK, "0" * 40], cwd=tmp_path, check=True)
