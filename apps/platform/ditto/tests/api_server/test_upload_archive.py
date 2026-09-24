"""Upload-time gzip/tar structure checks."""

from __future__ import annotations

import io
import tarfile

import pytest

from ditto.api_server.source_inspect import (
    MAX_MEMBERS,
    SourceInspectError,
    validate_upload_archive,
)


def _archive(members: list[tuple[str, bytes, int]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, typ in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.type = typ
            tar.addfile(info, io.BytesIO(data) if typ == tarfile.REGTYPE else None)
    return buf.getvalue()


def _ok() -> bytes:
    return _archive([("Dockerfile", b"FROM scratch\n", tarfile.REGTYPE)])


def test_accepts_a_root_dockerfile() -> None:
    validate_upload_archive(_ok())


def test_rejects_bytes_without_gzip_magic() -> None:
    with pytest.raises(SourceInspectError, match="gzip") as raised:
        validate_upload_archive(b"not-gzip")
    assert raised.value.code == "archive-not-gzip"


def test_rejects_gzip_that_is_not_a_tar() -> None:
    with pytest.raises(SourceInspectError) as raised:
        validate_upload_archive(b"\x1f\x8b" + b"x" * 32)
    assert raised.value.code == "archive-unreadable"


def test_rejects_a_missing_root_dockerfile() -> None:
    blob = _archive([("src/lib.rs", b"fn main() {}\n", tarfile.REGTYPE)])
    with pytest.raises(SourceInspectError) as raised:
        validate_upload_archive(blob)
    assert raised.value.code == "archive-missing-dockerfile"


def test_rejects_a_symlink() -> None:
    blob = _archive(
        [
            ("Dockerfile", b"FROM scratch\n", tarfile.REGTYPE),
            ("link", b"", tarfile.SYMTYPE),
        ]
    )
    with pytest.raises(SourceInspectError) as raised:
        validate_upload_archive(blob)
    assert raised.value.code == "archive-special-file"


def test_rejects_parent_traversal() -> None:
    blob = _archive(
        [
            ("Dockerfile", b"FROM scratch\n", tarfile.REGTYPE),
            ("../outside", b"x", tarfile.REGTYPE),
        ]
    )
    with pytest.raises(SourceInspectError) as raised:
        validate_upload_archive(blob)
    assert raised.value.code == "archive-unsafe-path"


def test_rejects_too_many_members() -> None:
    members = [("Dockerfile", b"FROM scratch\n", tarfile.REGTYPE)]
    members.extend(
        (f"f{index}", b"x", tarfile.REGTYPE) for index in range(MAX_MEMBERS)
    )
    with pytest.raises(SourceInspectError) as raised:
        validate_upload_archive(_archive(members))
    assert raised.value.code == "artifact-too-many-members"
