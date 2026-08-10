from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/publish-compat-runtime-index.sh"
REPOSITORY = "ghcr.io/ditto-assistant/ditto-subnet-validator"
CANONICAL_DIGEST = "sha256:" + "a" * 64
RUNTIME_DIGEST = "sha256:" + "f" * 64
AMD64_DIGEST = "sha256:" + "1" * 64
ARM64_DIGEST = "sha256:" + "2" * 64


def _manifest(digest: str, architecture: str, os_name: str = "linux") -> dict:
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": digest,
        "size": 123,
        "platform": {"architecture": architecture, "os": os_name},
    }


def _index(*manifests: dict) -> str:
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": list(manifests),
        }
    )


@pytest.fixture
def fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as stream:
    stream.write("\\0".join(args) + "\\n")
if args[:3] == ["buildx", "imagetools", "create"]:
    raise SystemExit(0)
if args[:3] != ["buildx", "imagetools", "inspect"]:
    raise SystemExit("unexpected fake docker call: " + repr(args))
if "--raw" in args:
    reference = args[-1]
    if "@sha256:" in reference:
        print(os.environ["FAKE_CANONICAL_RAW"])
    else:
        print(os.environ["FAKE_RUNTIME_RAW"])
    raise SystemExit(0)
if "--format" in args:
    print('{"digest":"' + os.environ["FAKE_RUNTIME_DIGEST"] + '"}')
    raise SystemExit(0)
raise SystemExit("unexpected fake docker inspect: " + repr(args))
"""
    )
    docker.chmod(0o755)
    return fake_bin, log


def _run(
    fake_docker: tuple[Path, Path],
    *,
    runtime_raw: str,
    platforms: tuple[str, ...] = ("linux/amd64", "linux/arm64"),
) -> subprocess.CompletedProcess[str]:
    fake_bin, log = fake_docker
    jq = shutil.which("jq")
    assert jq is not None
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{Path(jq).parent}:/usr/bin:/bin",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_CANONICAL_RAW": _index(
            _manifest(AMD64_DIGEST, "amd64"),
            _manifest(ARM64_DIGEST, "arm64"),
            _manifest("sha256:" + "3" * 64, "unknown", "unknown"),
        ),
        "FAKE_RUNTIME_RAW": runtime_raw,
        "FAKE_RUNTIME_DIGEST": RUNTIME_DIGEST,
    }
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            REPOSITORY,
            CANONICAL_DIGEST,
            "compat-runtime-2-revision",
            *platforms,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_publishes_only_the_exact_canonical_platform_children(
    fake_docker: tuple[Path, Path],
) -> None:
    result = _run(
        fake_docker,
        runtime_raw=_index(
            _manifest(AMD64_DIGEST, "amd64"),
            _manifest(ARM64_DIGEST, "arm64"),
        ),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == RUNTIME_DIGEST + "\n"
    calls = fake_docker[1].read_text().splitlines()
    create = next(call.split("\0") for call in calls if "\0create\0" in call)
    assert "--prefer-index=true" in create
    assert f"{REPOSITORY}@{AMD64_DIGEST}" in create
    assert f"{REPOSITORY}@{ARM64_DIGEST}" in create
    assert all("3" * 64 not in argument for argument in create)


def test_rejects_an_attachment_manifest_in_the_runtime_index(
    fake_docker: tuple[Path, Path],
) -> None:
    result = _run(
        fake_docker,
        runtime_raw=_index(
            _manifest(AMD64_DIGEST, "amd64"),
            _manifest(ARM64_DIGEST, "arm64"),
            _manifest("sha256:" + "3" * 64, "unknown", "unknown"),
        ),
    )

    assert result.returncode == 1
    assert "attachment or unexpected platform manifests" in result.stderr


def test_rejects_a_runtime_index_that_substitutes_a_platform_child(
    fake_docker: tuple[Path, Path],
) -> None:
    result = _run(
        fake_docker,
        runtime_raw=_index(
            _manifest("sha256:" + "9" * 64, "amd64"),
            _manifest(ARM64_DIGEST, "arm64"),
        ),
    )

    assert result.returncode == 1
    assert "changed the canonical linux/amd64 child" in result.stderr


def test_rejects_duplicate_runtime_platforms_before_publication(
    fake_docker: tuple[Path, Path],
) -> None:
    result = _run(
        fake_docker,
        runtime_raw=_index(_manifest(AMD64_DIGEST, "amd64")),
        platforms=("linux/amd64", "linux/amd64"),
    )

    assert result.returncode == 2
    assert "duplicate runtime platform" in result.stderr
    assert "\0create\0" not in fake_docker[1].read_text()
