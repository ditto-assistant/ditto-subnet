from __future__ import annotations

import json
from pathlib import Path

import pytest

from dittobench_coding_datagen.cli import main
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_pack_v2 import validate_public_v2_pack
from dittobench_coding_datagen.public_v2_release import (
    build_public_v2_release,
    unpack_public_v2_release,
)
from dittobench_coding_datagen.public_workspace import prepare_public_workspace

RELEASE = Path(__file__).parents[1] / "practice/v2"
ARCHIVE = RELEASE / "coding-public-v2-2026-09-04-r2.tar.gz"
DESCRIPTOR = RELEASE / "coding-public-v2-2026-09-04-r2.release.json"


def test_repository_release_roundtrip_and_visible_workspace(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    authority = unpack_public_v2_release(
        archive=ARCHIVE, descriptor=DESCRIPTOR, output=pack
    )
    assert validate_public_v2_pack(pack)["task_count"] == 10
    rebuilt = build_public_v2_release(pack=pack, output=tmp_path / "release")
    assert authority == rebuilt
    result = prepare_public_workspace(
        pack=pack, task_id="PUBLIC-GIN-2121", output=tmp_path / "task"
    )
    assert result["authoritative"] is False
    assert (tmp_path / "task/workspace/binding/form_mapping.go").is_file()
    assert not (tmp_path / "task/grader").exists()
    assert {p.name for p in (tmp_path / "task").iterdir()} == {
        "workspace",
        "issue.json",
        "memory.json",
        "runtime-policy.json",
    }
    with pytest.raises(CorpusError):
        prepare_public_workspace(
            pack=pack, task_id="../grader", output=tmp_path / "bad"
        )
    with pytest.raises(CorpusError):
        unpack_public_v2_release(archive=ARCHIVE, descriptor=DESCRIPTOR, output=pack)


def test_tampered_archive_is_rejected_before_output(tmp_path: Path) -> None:
    corrupted = tmp_path / "bad.tar.gz"
    corrupted.write_bytes(ARCHIVE.read_bytes() + b"tampered")
    with pytest.raises(CorpusError):
        unpack_public_v2_release(
            archive=corrupted, descriptor=DESCRIPTOR, output=tmp_path / "output"
        )
    assert not (tmp_path / "output").exists()


def test_unpack_cli_and_public_runtime_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pack = tmp_path / "pack"
    assert (
        main(
            [
                "unpack-public-practice",
                "--archive",
                str(ARCHIVE),
                "--descriptor",
                str(DESCRIPTOR),
                "--output",
                str(pack),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["weight_eligible"] is False
    images = json.loads((RELEASE / "images.json").read_bytes())
    entries = [
        json.loads(line)
        for line in (pack / "tasks/index.jsonl").read_bytes().splitlines()
    ]
    assert set(images) == {entry["task_id"] for entry in entries}
    assert all("@sha256:" in value for value in images.values())
