from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from test_public_staging import _intake

from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_pack_v2 import compile_public_v2_pack
from dittobench_coding_datagen.public_v2_release import (
    build_public_v2_release,
    verify_public_v2_release,
)


def _pack(root: Path) -> Path:
    intake = _intake(root / "staging")
    pack = root / "pack"
    compile_public_v2_pack(staging_root=intake.parent, intake_path=intake, output=pack)
    return pack


def test_public_v2_release_is_deterministic_and_verifiable(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    one = build_public_v2_release(pack=pack, output=first)
    two = build_public_v2_release(pack=pack, output=second)

    assert one == two
    archive = next(first.glob("*.tar.gz"))
    descriptor = next(first.glob("*.release.json"))
    assert verify_public_v2_release(archive=archive, descriptor=descriptor) == one
    with tarfile.open(archive, mode="r:gz") as release:
        modes = {member.name: member.mode for member in release.getmembers()}
    assert any(
        name.endswith("/visible/workspace/app.txt") and mode == 0o755
        for name, mode in modes.items()
    )
    assert any(
        name.endswith("/grader/test.txt") and mode == 0o755
        for name, mode in modes.items()
    )
    assert any(
        name.endswith("/manifest.json") and mode == 0o644
        for name, mode in modes.items()
    )


def test_public_v2_release_rejects_archive_drift(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    output = tmp_path / "release"
    build_public_v2_release(pack=pack, output=output)
    archive = next(output.glob("*.tar.gz"))
    descriptor = next(output.glob("*.release.json"))
    archive.write_bytes(archive.read_bytes() + b"drift")
    with pytest.raises(CorpusError, match="authority"):
        verify_public_v2_release(archive=archive, descriptor=descriptor)
