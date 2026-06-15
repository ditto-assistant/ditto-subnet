"""Shared fixtures + builders for miner_cli unit tests.

Tar fixtures are built at test time via stdlib :mod:`tarfile` so the
test suite stays free of committed binary blobs.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest


def _write_good_tar(dest: Path) -> Path:
    """Build a small valid .tar.gz with a few harness-shaped entries.

    Files do not have to be real harness shape; pre-flight only checks
    structural integrity at this layer. Real manifest / allowlist /
    schema content lands when those validators stop being deferred.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in (
            ("manifest.yaml", b"name: alpha\nversion: 1\n"),
            ("main.go", b"package main\n\nfunc main() {}\n"),
            ("go.mod", b"module ditto-harness\n\ngo 1.22\n"),
        ):
            data = content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    dest.write_bytes(buf.getvalue())
    return dest


def _write_bad_gzip(dest: Path) -> Path:
    """Random bytes that do not start with the gzip magic 0x1F 0x8B."""
    dest.write_bytes(b"NOTGZIP" * 100)
    return dest


def _write_too_large(dest: Path, *, target_bytes: int) -> Path:
    """A valid gzip stream whose payload is forcibly oversize on disk.

    We pad with low-entropy bytes outside the gzip wrapper so the file
    is large but the gzip layer still opens; this isolates the size
    check from the gzip check in tests.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Small valid entry first so the tar would otherwise pass.
        data = b"x"
        info = tarfile.TarInfo(name="ok.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    payload = buf.getvalue()
    padding = b"\0" * max(0, target_bytes - len(payload))
    dest.write_bytes(payload + padding)
    return dest


@pytest.fixture
def good_tar(tmp_path: Path) -> Path:
    return _write_good_tar(tmp_path / "good.tar.gz")


@pytest.fixture
def bad_gzip_tar(tmp_path: Path) -> Path:
    return _write_bad_gzip(tmp_path / "bad_gzip.tar.gz")


@pytest.fixture
def oversize_tar(tmp_path: Path) -> Path:
    # MAX is 2 MiB; build something 3 MiB.
    return _write_too_large(tmp_path / "huge.tar.gz", target_bytes=3 * 1024 * 1024)
