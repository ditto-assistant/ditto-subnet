from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import PurePosixPath

import pytest

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_private_snapshot import (
    PrivateSnapshotError,
    validate_private_snapshot_capsule,
)


def _json(value: dict | list) -> bytes:
    return coding_canonical_json_bytes(value, maximum_bytes=8 << 20, label="fixture")


def _capsule(
    *,
    path: str = "app.py",
    mode: int = 0o644,
    duplicate: bool = False,
    extra: bool = False,
    bad_tree: bool = False,
) -> tuple[bytes, str]:
    source = b"print('synthetic')\n"
    entries = [
        {
            "path": path,
            "mode": 0o644,
            "sha256": hashlib.sha256(source).hexdigest(),
            "size_bytes": len(source),
        }
    ]
    tree = hashlib.sha256(_json(entries)).hexdigest()
    manifest = _json(
        {
            "schema": "dittobench-coding-sanitized-snapshot-v2",
            "files": entries,
            "source_tree_sha256": "0" * 64 if bad_tree else tree,
            "snapshot_tree_sha256": tree,
            "excluded_root_entries": [],
        }
    )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        files = [
            ("manifest.json", manifest, 0o644),
            ("workspace/" + path, source, mode),
        ]
        if duplicate:
            files.append(files[-1])
        if extra:
            files.append(("workspace/PRIVATE_MARKER", b"extra", 0o644))
        for name, body, file_mode in files:
            info = tarfile.TarInfo(name)
            info.mode = file_mode
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return output.getvalue(), hashlib.sha256(manifest).hexdigest()


def test_snapshot_capsule_requires_manifest_binding_and_real_modes() -> None:
    body, digest = _capsule()
    validate_private_snapshot_capsule(body, expected_manifest_sha256=digest)
    with pytest.raises(PrivateSnapshotError):
        validate_private_snapshot_capsule(body, expected_manifest_sha256="f" * 64)


@pytest.mark.parametrize("mode", [0o600, 0o777, 0o4755])
def test_mode_drift_is_rejected_even_when_contents_match(mode: int) -> None:
    body, digest = _capsule(mode=mode)
    with pytest.raises(PrivateSnapshotError):
        validate_private_snapshot_capsule(body, expected_manifest_sha256=digest)


@pytest.mark.parametrize(
    "path",
    ["../PRIVATE_MARKER", ".", ".git/config", ".env", "a/../b", "/absolute", "a\\b"],
)
def test_unsafe_paths_fail_without_echoing_names(path: str) -> None:
    body, _ = _capsule(path=path)
    with pytest.raises(PrivateSnapshotError) as caught:
        validate_private_snapshot_capsule(body)
    assert "PRIVATE_MARKER" not in str(caught.value)


@pytest.mark.parametrize("change", ["duplicate", "extra", "bad_tree"])
def test_snapshot_file_set_and_tree_are_bound(change: str) -> None:
    body, _ = _capsule(
        duplicate=change == "duplicate",
        extra=change == "extra",
        bad_tree=change == "bad_tree",
    )
    with pytest.raises(PrivateSnapshotError):
        validate_private_snapshot_capsule(body)


def test_nonzero_trailer_and_links_are_rejected() -> None:
    body, _ = _capsule()
    with pytest.raises(PrivateSnapshotError):
        validate_private_snapshot_capsule(
            body + b"PRIVATE_MARKER".ljust(tarfile.RECORDSIZE, b"\x00")
        )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        info = tarfile.TarInfo("workspace/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../secret"
        archive.addfile(info)
    with pytest.raises(PrivateSnapshotError):
        validate_private_snapshot_capsule(output.getvalue())


def test_nested_path_order_matches_the_snapshot_exporter() -> None:
    paths = sorted(["a.py", "a/child.py"], key=PurePosixPath)
    entries = [
        {
            "path": path,
            "mode": 0o644,
            "sha256": hashlib.sha256(b"x").hexdigest(),
            "size_bytes": 1,
        }
        for path in paths
    ]
    tree = hashlib.sha256(_json(entries)).hexdigest()
    manifest = _json(
        {
            "schema": "dittobench-coding-sanitized-snapshot-v2",
            "files": entries,
            "source_tree_sha256": tree,
            "snapshot_tree_sha256": tree,
            "excluded_root_entries": [],
        }
    )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, value in [
            ("manifest.json", manifest),
            *[("workspace/" + path, b"x") for path in sorted(paths)],
        ]:
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    validate_private_snapshot_capsule(output.getvalue())
