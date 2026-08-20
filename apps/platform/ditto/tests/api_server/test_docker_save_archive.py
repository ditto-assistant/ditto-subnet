from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

from ditto.api_server.docker_save_archive import config_digest_from_docker_save


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


def test_config_digest_from_classic_kaniko_tar(tmp_path: Path) -> None:
    config = (
        b'{"architecture":"amd64","os":"linux",'
        b'"rootfs":{"type":"layers","diff_ids":[]}}'
    )
    digest = hashlib.sha256(config).hexdigest()
    tar = tmp_path / "image.tar"
    _write_docker_save(
        tar,
        config=config,
        name=f"{digest}.json",
        repo_tags=[
            "ditto-screen/11111111-1111-4111-8111-111111111111-22222222-2222-4222-8222-222222222222:latest"
        ],
    )
    assert config_digest_from_docker_save(tar) == "sha256:" + digest


def test_config_digest_rejects_mismatched_filename(tmp_path: Path) -> None:
    config = b'{"architecture":"amd64"}'
    tar = tmp_path / "image.tar"
    _write_docker_save(
        tar,
        config=config,
        name=f"{'ab' * 32}.json",
        repo_tags=[],
    )
    assert config_digest_from_docker_save(tar) is None


def test_config_digest_from_oci_blob_name(tmp_path: Path) -> None:
    config = b'{"architecture":"amd64","os":"linux"}'
    digest = hashlib.sha256(config).hexdigest()
    tar = tmp_path / "image.tar"
    _write_docker_save(
        tar,
        config=config,
        name=f"blobs/sha256/{digest}",
        repo_tags=[],
    )
    assert config_digest_from_docker_save(tar) == "sha256:" + digest


def test_config_digest_from_gzip_tar(tmp_path: Path) -> None:
    config = b'{"architecture":"amd64"}'
    digest = hashlib.sha256(config).hexdigest()
    tar = tmp_path / "image.tar.gz"
    manifest = [{"Config": f"{digest}.json", "RepoTags": [], "Layers": []}]
    with tarfile.open(tar, "w:gz") as archive:
        _add(archive, "manifest.json", json.dumps(manifest).encode())
        _add(archive, f"{digest}.json", config)
    assert config_digest_from_docker_save(tar) == "sha256:" + digest


def test_config_digest_rejects_garbage(tmp_path: Path) -> None:
    tar = tmp_path / "image.tar"
    tar.write_bytes(b"not a tar")
    assert config_digest_from_docker_save(tar) is None
