from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_public_staging import _intake

from dittobench_coding_datagen.cli import main


def test_public_v2_cli_compiles_and_verifies_pack_and_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    intake = _intake(tmp_path / "staging")
    pack = tmp_path / "pack"
    assert (
        main(
            [
                "compile-public-v2-pack",
                "--staging-root",
                str(intake.parent),
                "--intake",
                str(intake),
                "--output",
                str(pack),
            ]
        )
        == 0
    )
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["task_count"] == 10
    assert manifest["weight_eligible"] is False

    assert main(["validate-public-v2-pack", str(pack)]) == 0
    assert json.loads(capsys.readouterr().out) == manifest

    release = tmp_path / "release"
    assert (
        main(
            [
                "build-public-v2-release",
                "--pack",
                str(pack),
                "--output",
                str(release),
            ]
        )
        == 0
    )
    descriptor = json.loads(capsys.readouterr().out)
    assert descriptor["weight_eligible"] is False

    assert (
        main(
            [
                "verify-public-v2-release",
                "--archive",
                str(release / "coding-public-v2.tar.gz"),
                "--descriptor",
                str(release / "coding-public-v2.release.json"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == descriptor
