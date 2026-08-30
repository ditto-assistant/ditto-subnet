from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_release import (
    build_public_practice_release,
    verify_public_practice_release,
)

ROOT = Path(__file__).parents[1]
PACK = ROOT / "practice/v1"


def _artifact(output: Path, suffix: str) -> Path:
    return next(output.glob(f"*{suffix}"))


def test_public_practice_release_is_deterministic_and_verifiable(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_descriptor = build_public_practice_release(PACK, first)
    second_descriptor = build_public_practice_release(PACK, second)

    assert first_descriptor == second_descriptor
    assert sorted(path.name for path in first.iterdir()) == [
        "RELEASE.md",
        "coding-practice-3x3-v1.release.json",
        "coding-practice-3x3-v1.tar.gz",
        "manifest.json",
    ]
    for name in (
        "RELEASE.md",
        "coding-practice-3x3-v1.release.json",
        "coding-practice-3x3-v1.tar.gz",
        "manifest.json",
    ):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    assert (
        verify_public_practice_release(
            archive=_artifact(first, ".tar.gz"),
            descriptor=_artifact(first, ".release.json"),
        )
        == first_descriptor
    )

    with tarfile.open(_artifact(first, ".tar.gz"), mode="r:gz") as archive:
        members = archive.getmembers()
    assert members
    assert all(
        member.isfile()
        and member.mode == 0o644
        and member.mtime == 0
        and member.uid == 0
        and member.gid == 0
        and not member.uname
        and not member.gname
        for member in members
    )


def test_public_practice_release_rejects_tampered_artifacts_and_unsafe_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "release"
    build_public_practice_release(PACK, output)
    archive = _artifact(output, ".tar.gz")
    descriptor = _artifact(output, ".release.json")
    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(CorpusError, match="does not match archive"):
        verify_public_practice_release(archive=archive, descriptor=descriptor)

    output = tmp_path / "release-with-manifest-drift"
    build_public_practice_release(PACK, output)
    (output / "manifest.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(CorpusError, match="does not match manifest copy"):
        verify_public_practice_release(
            archive=_artifact(output, ".tar.gz"),
            descriptor=_artifact(output, ".release.json"),
        )

    with pytest.raises(CorpusError, match="inside the practice pack"):
        build_public_practice_release(PACK, PACK / "release")

    with pytest.raises(CorpusError, match="root or mountpoint"):
        build_public_practice_release(PACK, Path("/"), replace=True)


def test_public_practice_release_rejects_descriptor_identity_drift(
    tmp_path: Path,
) -> None:
    output = tmp_path / "release"
    build_public_practice_release(PACK, output)
    archive = _artifact(output, ".tar.gz")
    descriptor = _artifact(output, ".release.json")
    body = json.loads(descriptor.read_bytes())
    body["practice_pack_id"] = "../../not-a-pack"
    descriptor.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(CorpusError, match="pack identity"):
        verify_public_practice_release(archive=archive, descriptor=descriptor)


def test_public_practice_release_rejects_oversized_descriptor(
    tmp_path: Path,
) -> None:
    output = tmp_path / "release"
    build_public_practice_release(PACK, output)
    archive = _artifact(output, ".tar.gz")
    descriptor = _artifact(output, ".release.json")
    descriptor.write_bytes(b"{" + (b"x" * (1 << 20)) + b"}")
    with pytest.raises(CorpusError, match="exceeds its byte bound"):
        verify_public_practice_release(archive=archive, descriptor=descriptor)
