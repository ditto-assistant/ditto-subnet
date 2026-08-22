from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest

from screener_capacity.targon_screen_contract import (
    busybox_contract_rental_script,
    inspect_docker_save,
    kaniko_argv,
    kaniko_destination,
    named_config_digest,
    pack_source_tar,
    parse_starter_kit_probe_logs,
    scoring_ref,
    starter_kit_rental_script,
    validate_screened_archive,
)

TINY = Path(__file__).resolve().parent / "testdata" / "tiny-miner"


def _write_docker_save(
    path: Path, *, config: bytes, name: str, repo_tags: list[str]
) -> None:
    manifest = [{"Config": name, "RepoTags": repo_tags, "Layers": []}]
    with tarfile.open(path, "w:") as archive:
        _add(archive, "manifest.json", json.dumps(manifest).encode())
        _add(archive, name, config)


def _add(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def test_kaniko_destination_is_attempt_scoped_and_argv_matches_production() -> None:
    agent_id = str(uuid4())
    attempt_id = str(uuid4())
    destination = kaniko_destination(agent_id, attempt_id)
    assert destination == f"ditto-screen/{agent_id}-{attempt_id}:latest"
    assert scoring_ref(agent_id) == f"ditto-screen/{agent_id}:latest"
    argv = kaniko_argv(destination=destination)
    assert argv[0] == "/kaniko/executor"
    assert "--no-push" in argv
    assert "--tar-path=/kaniko/image.tar" in argv
    assert "--ignore-path=/workspace" in argv
    assert "--ignore-path=/etc/resolv.conf" in argv
    assert "--ignore-path=/etc/hosts" in argv
    assert f"--destination={destination}" in argv
    assert "--digest-file=/kaniko/manifest-digest" in argv


def test_pack_source_tar_puts_dockerfile_at_root(tmp_path: Path) -> None:
    output = tmp_path / "source.tar.gz"
    digest = pack_source_tar(TINY, output)
    assert len(digest) == 64
    with tarfile.open(output, "r:gz") as archive:
        names = archive.getnames()
    assert "Dockerfile" in names


def test_named_config_digest_accepts_kaniko_gcr_filename() -> None:
    digest = "ab" * 32
    assert named_config_digest(f"sha256:{digest}") == digest
    assert named_config_digest(f"{digest}.json") == digest
    assert named_config_digest(f"blobs/sha256/{digest}") == digest


def test_validate_accepts_attempt_scoped_kaniko_tar(tmp_path: Path) -> None:
    agent_id = str(uuid4())
    attempt_id = str(uuid4())
    config = b'{"architecture":"amd64","os":"linux"}'
    digest = hashlib.sha256(config).hexdigest()
    tar = tmp_path / "image.tar"
    _write_docker_save(
        tar,
        config=config,
        name=f"sha256:{digest}",
        repo_tags=[kaniko_destination(agent_id, attempt_id)],
    )
    result = validate_screened_archive(
        path=tar, agent_id=agent_id, attempt_id=attempt_id
    )
    assert result["ok"] is True
    assert result["image_id"] == "sha256:" + digest
    assert inspect_docker_save(tar)["image_id"] == result["image_id"]


def test_validate_rejects_manifest_digest_identity(tmp_path: Path) -> None:
    agent_id = str(uuid4())
    attempt_id = str(uuid4())
    config = b'{"architecture":"amd64"}'
    digest = hashlib.sha256(config).hexdigest()
    wrong = "cd" * 32
    tar = tmp_path / "image.tar"
    _write_docker_save(
        tar,
        config=config,
        name=f"{wrong}.json",
        repo_tags=[kaniko_destination(agent_id, attempt_id)],
    )
    with pytest.raises(ValueError, match="config digest is missing"):
        inspect_docker_save(tar)
    # Filename digest must match bytes; that is the production scoring failure.
    assert named_config_digest(f"{digest}.json") == digest


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is required")
def test_tiny_miner_docker_save_matches_kaniko_contract(tmp_path: Path) -> None:
    agent_id = str(uuid4())
    attempt_id = str(uuid4())
    destination = kaniko_destination(agent_id, attempt_id)
    image_tar = tmp_path / "image.tar"
    build = subprocess.run(
        ["docker", "build", "--tag", destination, str(TINY)],
        check=False,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.skip(build.stderr[-400:] or "docker build failed")
    try:
        saved = subprocess.run(
            ["docker", "save", destination, "-o", str(image_tar)],
            check=False,
            capture_output=True,
            text=True,
        )
        if saved.returncode != 0:
            pytest.skip(saved.stderr[-400:] or "docker save failed")
        result = validate_screened_archive(
            path=image_tar, agent_id=agent_id, attempt_id=attempt_id
        )
        assert result["ok"] is True
        assert result["image_id"].startswith("sha256:")
        assert result["repo_tags"] == [destination]
    finally:
        subprocess.run(["docker", "image", "rm", "-f", destination], check=False)


def test_busybox_contract_rental_script_uses_production_kaniko_flags() -> None:
    agent_id = str(uuid4())
    attempt_id = str(uuid4())
    script = busybox_contract_rental_script(agent_id=agent_id, attempt_id=attempt_id)
    destination = kaniko_destination(agent_id, attempt_id)
    assert "--no-push" in script
    assert "--tar-path=/workspace/image.tar" in script
    assert "--ignore-path=/workspace" in script
    assert f"--destination={destination}" in script
    assert "FROM busybox:1.37.0" in script
    assert "wget" not in script
    logs = (
        "DITTO_SCREEN_DESTINATION=" + destination + "\n"
        "DITTO_SCREEN_MANIFEST_BEGIN\n"
        + json.dumps(
            [{"Config": "deadbeef.json", "RepoTags": [destination], "Layers": []}]
        )
        + "\nDITTO_SCREEN_MANIFEST_END\n"
        "KANIKO_PROBE_AVAILABLE\n"
    )
    parsed = parse_starter_kit_probe_logs(logs)
    assert parsed["ok"] is True
    assert parsed["repo_tags"] == [destination]


def test_starter_kit_rental_script_uses_production_kaniko_flags() -> None:
    agent_id = str(uuid4())
    attempt_id = str(uuid4())
    sha = "a" * 40
    script = starter_kit_rental_script(
        source_sha=sha, agent_id=agent_id, attempt_id=attempt_id
    )
    destination = kaniko_destination(agent_id, attempt_id)
    assert "--no-push" in script
    assert "--tar-path=/kaniko/image.tar" in script
    assert "--ignore-path=/workspace" in script
    assert f"--destination={destination}" in script
    assert "git://github.com/ditto-assistant/dittobench-starter-kit.git#" in script
    assert "ditto-subnet.git" not in script
    assert "wget" not in script
    assert "KANIKO_STARTER_PROBE_AVAILABLE" in script
    assert "KANIKO_STARTER_PROBE_FAILED" in script
    assert "/bin/sleep 600" in script
    logs = (
        "DITTO_SCREEN_DESTINATION=" + destination + "\n"
        "DITTO_SCREEN_MANIFEST_BEGIN\n"
        + json.dumps(
            [{"Config": "deadbeef.json", "RepoTags": [destination], "Layers": []}]
        )
        + "\nDITTO_SCREEN_MANIFEST_END\n"
        "KANIKO_STARTER_PROBE_AVAILABLE\n"
    )
    parsed = parse_starter_kit_probe_logs(logs)
    assert parsed["ok"] is True
    assert parsed["repo_tags"] == [destination]
