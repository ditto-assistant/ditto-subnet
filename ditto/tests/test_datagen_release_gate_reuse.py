"""A successful source gate can replace duplicate tests, never changed code."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "research/dittobench-datagen/scripts/verify-generate-service-release.sh"
)


@pytest.mark.parametrize("change", ["stamp", "code", "version_code", "no_gate"])
def test_reuse_requires_unchanged_tested_source(tmp_path: Path, change: str) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    module = tmp_path / "research/dittobench-datagen"
    version = module / "internal/version/version.go"
    version.parent.mkdir(parents=True)
    version.write_text('package version\nconst Version = "1.2.3"\n')
    source = module / "main.go"
    source.write_text("package main\n")
    git("add", ".")
    git("commit", "-qm", "gate source")
    verified = git("rev-parse", "HEAD")
    version.write_text('package version\nconst Version = "1.2.4"\n')
    if change == "code":
        source.write_text("package main\n// new untested code\n")
    elif change == "version_code":
        version.write_text(version.read_text() + "func newCode() {}\n")
    git("add", ".")
    git("commit", "-qm", "release")
    git("tag", "v1.2.4")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    # Keep mocks outside the checkout so the clean-tree check stays real.
    tools = tmp_path.parent / (tmp_path.name + "-tools")
    tools.mkdir()
    calls = tools / "calls"
    for name in ("go", "docker"):
        tool = tools / name
        tool.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$TEST_CALLS"\n')
        tool.chmod(0o755)
    env = os.environ | {
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "TEST_CALLS": str(calls),
    }
    if change != "no_gate":
        env["DATAGEN_VERIFIED_SOURCE_SHA"] = verified
    result = subprocess.run(
        ["bash", str(SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    if change in ("code", "version_code"):
        assert result.returncode != 0
        assert not calls.exists()
    else:
        assert result.returncode == 0, result.stderr
        commands = calls.read_text()
        assert ("test -timeout 20m ./..." in commands) == (change == "no_gate")
        assert "TestV7KnownVector" in commands
        assert "build --build-arg SOURCE_COMMIT=" in commands
