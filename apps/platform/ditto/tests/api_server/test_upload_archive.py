"""Upload-time gzip/tar structure checks."""

from __future__ import annotations

import ast
import io
import tarfile
from pathlib import Path

import pytest

from ditto.api_server import source_inspect
from ditto.api_server.source_inspect import (
    UPLOAD_MAX_MEMBERS,
    UPLOAD_MAX_UNPACKED_BYTES,
    SourceInspectError,
    validate_upload_archive,
)

SCREENER_GATE = (
    Path(__file__).resolve().parents[5] / "workers/screener/ditto_screener/gate.py"
)


def _archive(members: list[tuple[str, bytes, bytes]]) -> bytes:
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


@pytest.mark.parametrize("directory", ["src/", "./src/"])
def test_accepts_a_directory_with_trailing_slash(directory: str) -> None:
    validate_upload_archive(
        _archive(
            [
                ("Dockerfile", b"FROM scratch\n", tarfile.REGTYPE),
                (directory, b"", tarfile.DIRTYPE),
                ("src/lib.rs", b"fn main() {}\n", tarfile.REGTYPE),
            ]
        )
    )


def test_rejects_duplicate_directory_after_normalization() -> None:
    blob = _archive(
        [
            ("Dockerfile", b"FROM scratch\n", tarfile.REGTYPE),
            ("src/", b"", tarfile.DIRTYPE),
            ("./src/", b"", tarfile.DIRTYPE),
        ]
    )
    with pytest.raises(SourceInspectError) as raised:
        validate_upload_archive(blob)
    assert raised.value.code == "archive-duplicate-path"


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


def test_rejects_too_many_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_inspect, "UPLOAD_MAX_MEMBERS", 3)
    members = [("Dockerfile", b"FROM scratch\n", tarfile.REGTYPE)]
    members.extend((f"f{index}", b"x", tarfile.REGTYPE) for index in range(3))
    with pytest.raises(SourceInspectError) as raised:
        validate_upload_archive(_archive(members))
    assert raised.value.code == "artifact-too-many-members"


def test_rejects_an_archive_that_expands_past_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_inspect, "UPLOAD_MAX_UNPACKED_BYTES", 16)
    blob = _archive(
        [
            ("Dockerfile", b"FROM scratch\n", tarfile.REGTYPE),
            ("big", b"x" * 16, tarfile.REGTYPE),
        ]
    )
    with pytest.raises(SourceInspectError) as raised:
        validate_upload_archive(blob)
    assert raised.value.code == "artifact-too-large"


def test_rejects_a_non_utf8_dockerfile() -> None:
    blob = _archive([("Dockerfile", b"FROM scratch\n\xff\xfe", tarfile.REGTYPE)])
    with pytest.raises(SourceInspectError) as raised:
        validate_upload_archive(blob)
    assert raised.value.code == "archive-dockerfile-unreadable"


def test_accepts_a_dockerfile_larger_than_one_read_chunk() -> None:
    # The screener decodes the whole Dockerfile; a multi-byte character split
    # across the chunk boundary must still decode.
    body = b"FROM scratch\n# " + "\u00e9".encode() * (64 * 1024) + b"\n"
    validate_upload_archive(_archive([("Dockerfile", body, tarfile.REGTYPE)]))


def _screener_constant(name: str) -> object:
    for node in ast.parse(SCREENER_GATE.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            expression = ast.Expression(node.value)
            # Only integer arithmetic such as ``64 * 1024 * 1024`` is evaluated.
            assert all(
                isinstance(
                    part, (ast.Expression, ast.BinOp, ast.Constant, ast.operator)
                )
                for part in ast.walk(expression)
            ), f"{name} is not a constant arithmetic expression"
            return eval(compile(expression, name, "eval"), {"__builtins__": {}})
    raise AssertionError(f"{name} not found in {SCREENER_GATE}")


def test_upload_limits_match_the_screener_contract() -> None:
    """Upload must never be stricter than the screener it gates for."""
    assert _screener_constant("_MAX_ARCHIVE_MEMBERS") == UPLOAD_MAX_MEMBERS
    assert _screener_constant("_MAX_UNPACKED_BYTES") == UPLOAD_MAX_UNPACKED_BYTES
