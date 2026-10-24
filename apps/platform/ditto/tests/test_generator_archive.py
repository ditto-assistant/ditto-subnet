"""Platform's archive check must agree with the Go contract.

``challenge/testdata/archive_vector.json`` is written by the Go contract's
``TestArchiveVectorGolden``.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

from ditto.generator_archive import (
    MAX_SOURCE_FILES,
    ArchiveRejected,
    inspect_archive,
    tree_digest,
)

_VECTOR = (
    Path(__file__).resolve().parents[4]
    / "research/dittobench-datagen/challenge/testdata/archive_vector.json"
)


def test_matches_go_archive_vector() -> None:
    vector = json.loads(_VECTOR.read_text())
    info = inspect_archive(base64.b64decode(vector["archive_base64"]))
    want = vector["expected"]
    assert info.sha256 == want["sha256"]
    assert info.tree_sha256 == want["tree_sha256"]
    assert info.module == want["module"]
    assert info.go_version == want["go_version"]
    assert info.files == want["files"]
    assert info.bytes == want["bytes"]
    assert info.vendored == want["vendored"]


def _archive(entries: list[tuple[str, bytes | None, bytes]]) -> bytes:
    """entries: (name, data or None for a non-file, tar type)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/passwd"
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return gzip.compress(buf.getvalue(), mtime=0)


GOMOD = b"module x\n\ngo 1.23\n"
MAIN = b"package main\nfunc main(){}\n"


def test_accepts_minimal_tree_and_ignores_packing_details() -> None:
    a = _archive(
        [("go.mod", GOMOD, tarfile.REGTYPE), ("main.go", MAIN, tarfile.REGTYPE)]
    )
    b = _archive(
        [
            ("./main.go", MAIN, tarfile.REGTYPE),
            ("./", None, tarfile.DIRTYPE),
            ("./go.mod", GOMOD, tarfile.REGTYPE),
        ]
    )
    info_a, info_b = inspect_archive(a), inspect_archive(b)
    assert info_a.sha256 != info_b.sha256
    assert info_a.tree_sha256 == info_b.tree_sha256
    assert info_a.tree_sha256 == tree_digest({"go.mod": GOMOD, "main.go": MAIN})


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (
            [("go.mod", GOMOD, tarfile.REGTYPE), ("../main.go", MAIN, tarfile.REGTYPE)],
            "escapes",
        ),
        (
            [("go.mod", GOMOD, tarfile.REGTYPE), ("/main.go", MAIN, tarfile.REGTYPE)],
            "relative",
        ),
        (
            [("go.mod", GOMOD, tarfile.REGTYPE), ("main.go", None, tarfile.SYMTYPE)],
            "regular file",
        ),
        (
            [
                ("go.mod", GOMOD, tarfile.REGTYPE),
                ("main.go", MAIN, tarfile.REGTYPE),
                ("./main.go", MAIN, tarfile.REGTYPE),
            ],
            "repeats",
        ),
        ([("main.go", MAIN, tarfile.REGTYPE)], "go.mod"),
        (
            [
                ("go.mod", GOMOD, tarfile.REGTYPE),
                ("cmd/main.go", MAIN, tarfile.REGTYPE),
            ],
            "root",
        ),
        (
            [
                ("go.mod", b"module x\n\ngo 1.99\n", tarfile.REGTYPE),
                ("main.go", MAIN, tarfile.REGTYPE),
            ],
            "builder runs",
        ),
        (
            [
                (
                    "go.mod",
                    b"module x\n\ngo 1.23\n\nrequire example.com/y v1.0.0\n",
                    tarfile.REGTYPE,
                ),
                ("main.go", MAIN, tarfile.REGTYPE),
            ],
            "vendor",
        ),
        (
            [
                ("go.mod", GOMOD, tarfile.REGTYPE),
                ("main.go", MAIN, tarfile.REGTYPE),
                ("gen.go", MAIN, tarfile.REGTYPE),
                ("gen.go/extra.go", MAIN, tarfile.REGTYPE),
            ],
            "under file|also a directory",
        ),
        (
            [
                ("go.mod", GOMOD, tarfile.REGTYPE),
                ("main.go", MAIN, tarfile.REGTYPE),
                ("gen.go/extra.go", MAIN, tarfile.REGTYPE),
                ("gen.go", MAIN, tarfile.REGTYPE),
            ],
            "under file|also a directory",
        ),
        (
            [
                ("go.mod", GOMOD, tarfile.REGTYPE),
                ("main.go", MAIN, tarfile.REGTYPE),
                ("a/", None, tarfile.DIRTYPE),
                ("a", MAIN, tarfile.REGTYPE),
            ],
            "also a directory",
        ),
        (
            [
                ("go.mod", GOMOD, tarfile.REGTYPE),
                ("main.go", MAIN, tarfile.REGTYPE),
                ("a", MAIN, tarfile.REGTYPE),
                ("a/", None, tarfile.DIRTYPE),
            ],
            "also a file",
        ),
        (
            [
                ("go.mod", b'module x"\n\ngo 1.23\n', tarfile.REGTYPE),
                ("main.go", MAIN, tarfile.REGTYPE),
            ],
            "module",
        ),
    ],
)
def test_rejects_unsafe_or_unbuildable_trees(
    entries: list[tuple[str, bytes | None, bytes]], message: str
) -> None:
    with pytest.raises(ArchiveRejected, match=message):
        inspect_archive(_archive(entries))


def test_rejects_non_gzip_and_bombs() -> None:
    with pytest.raises(ArchiveRejected, match="gzip"):
        inspect_archive(b"not gzip")
    bomb = gzip.compress(b"\0" * (64 << 20), mtime=0)
    assert len(bomb) < (4 << 20)
    with pytest.raises(ArchiveRejected, match="expands past"):
        inspect_archive(bomb)


def test_requires_exactly_one_complete_gzip_member() -> None:
    good = _archive(
        [("go.mod", GOMOD, tarfile.REGTYPE), ("main.go", MAIN, tarfile.REGTYPE)]
    )
    inspect_archive(good)
    with pytest.raises(ArchiveRejected, match="incomplete"):
        inspect_archive(good[:-4])
    with pytest.raises(ArchiveRejected, match="after its gzip member"):
        inspect_archive(good + good)


def test_accepts_a_quoted_module_path() -> None:
    info = inspect_archive(
        _archive(
            [
                ("go.mod", b'module "x"\n\ngo 1.23\n', tarfile.REGTYPE),
                ("main.go", MAIN, tarfile.REGTYPE),
            ]
        )
    )
    assert info.module == "x"


def test_the_root_entry_does_not_extend_the_file_limit() -> None:
    entries = [("go.mod", GOMOD, tarfile.REGTYPE)] + [
        (f"f{i}.go", b"package main\n", tarfile.REGTYPE)
        for i in range(1, MAX_SOURCE_FILES + 1)
    ]
    with pytest.raises(ArchiveRejected, match="more than"):
        inspect_archive(_archive(entries))
