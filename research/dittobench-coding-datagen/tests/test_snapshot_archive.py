from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from test_snapshot import _source

from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.snapshot import export_sanitized_snapshot
from dittobench_coding_datagen.snapshot_archive import (
    build_snapshot_archive,
    verify_snapshot_archive,
)


def test_snapshot_archive_is_deterministic_and_preserves_modes(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot = export_sanitized_snapshot(_source(tmp_path), snapshot_root)
    archive = snapshot_root / "archive.tar.gz"

    first = build_snapshot_archive(snapshot=snapshot_root, archive=archive)
    first_bytes = archive.read_bytes()
    second = build_snapshot_archive(
        snapshot=snapshot_root, archive=archive, replace=True
    )

    assert first == second == verify_snapshot_archive(archive)
    assert archive.read_bytes() == first_bytes
    assert first.snapshot_tree_sha256 == snapshot.snapshot_tree_sha256
    with tarfile.open(archive, mode="r:gz") as source:
        modes = {member.name: member.mode for member in source.getmembers()}
    assert any(
        name.endswith("/manifest.json") and mode == 0o644
        for name, mode in modes.items()
    )
    assert any(
        name.endswith("/workspace/run.sh") and mode == 0o755
        for name, mode in modes.items()
    )


def test_snapshot_archive_rejects_drift_and_unsafe_output(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    export_sanitized_snapshot(_source(tmp_path), snapshot_root)
    archive = snapshot_root / "archive.tar.gz"
    build_snapshot_archive(snapshot=snapshot_root, archive=archive)

    archive.write_bytes(archive.read_bytes() + b"drift")
    with pytest.raises(CorpusError, match="canonical"):
        verify_snapshot_archive(archive)

    with pytest.raises(CorpusError, match="unsafe"):
        build_snapshot_archive(
            snapshot=snapshot_root,
            archive=snapshot_root / "workspace" / "archive.tar.gz",
        )
