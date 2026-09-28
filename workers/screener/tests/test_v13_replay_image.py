"""A replay archive must match both Platform's tar SHA and its actual image ID."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import time
from pathlib import Path

import pytest

from ditto_screener.v13_replay_image import (
    ReplayImageUnavailable,
    verify_replay_image_archive,
)


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def _image() -> tuple[bytes, str]:
    layer = _tar({"app/ready.txt": b"ready\n"})
    compressed = gzip.compress(layer, mtime=0)
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {
                "type": "layers",
                "diff_ids": ["sha256:" + hashlib.sha256(layer).hexdigest()],
            },
        },
        separators=(",", ":"),
    ).encode()
    config_sha = hashlib.sha256(config).hexdigest()
    layer_sha = hashlib.sha256(compressed).hexdigest()
    manifest = json.dumps(
        [
            {
                "Config": f"blobs/sha256/{config_sha}",
                "RepoTags": None,
                "Layers": [f"blobs/sha256/{layer_sha}"],
            }
        ],
        separators=(",", ":"),
    ).encode()
    return (
        _tar(
            {
                "manifest.json": manifest,
                f"blobs/sha256/{config_sha}": config,
                f"blobs/sha256/{layer_sha}": compressed,
            }
        ),
        "sha256:" + config_sha,
    )


def test_replay_archive_verifies_tar_and_real_config_id(tmp_path: Path) -> None:
    raw, image_id = _image()
    archive = tmp_path / "image.tar"
    archive.write_bytes(raw)
    portable = tmp_path / "portable.tar"
    verified = verify_replay_image_archive(
        archive_path=archive,
        portable_path=portable,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        expected_size_bytes=len(raw),
        expected_image_id=image_id,
        deadline=time.monotonic() + 30,
    )
    assert verified.image_id == image_id
    assert portable.is_file()
    with tarfile.open(portable, mode="r:") as normalized:
        assert normalized.getnames()[0] == "manifest.json"


@pytest.mark.parametrize("alter", ["tar", "size", "image_id"])
def test_replay_archive_rejects_changed_commitment(tmp_path: Path, alter: str) -> None:
    raw, image_id = _image()
    archive = tmp_path / "image.tar"
    archive.write_bytes(raw)
    portable = tmp_path / "portable.tar"
    sha = hashlib.sha256(raw).hexdigest()
    size = len(raw)
    if alter == "tar":
        archive.write_bytes(raw + b"changed")
    elif alter == "size":
        size += 1
    else:
        image_id = "sha256:" + "f" * 64
    with pytest.raises(ReplayImageUnavailable):
        verify_replay_image_archive(
            archive_path=archive,
            portable_path=portable,
            expected_sha256=sha,
            expected_size_bytes=size,
            expected_image_id=image_id,
            deadline=time.monotonic() + 30,
        )
    assert not portable.exists()


def test_replay_archive_refuses_symlink(tmp_path: Path) -> None:
    raw, image_id = _image()
    real = tmp_path / "real.tar"
    real.write_bytes(raw)
    link = tmp_path / "image.tar"
    link.symlink_to(real)
    with pytest.raises(ReplayImageUnavailable):
        verify_replay_image_archive(
            archive_path=link,
            portable_path=tmp_path / "portable.tar",
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            expected_size_bytes=len(raw),
            expected_image_id=image_id,
            deadline=time.monotonic() + 30,
        )
