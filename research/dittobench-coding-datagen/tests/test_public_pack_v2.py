from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_public_staging import _intake

from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_pack_v2 import (
    compile_public_v2_pack,
    validate_public_v2_pack,
)


def test_public_v2_pack_is_deterministic_and_verifiable(tmp_path: Path) -> None:
    intake = _intake(tmp_path / "staging")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = compile_public_v2_pack(
        staging_root=intake.parent,
        intake_path=intake,
        output=first,
    )
    second_manifest = compile_public_v2_pack(
        staging_root=intake.parent,
        intake_path=intake,
        output=second,
    )

    assert first_manifest == second_manifest == validate_public_v2_pack(first)
    assert (first / "manifest.json").read_bytes() == (
        second / "manifest.json"
    ).read_bytes()
    assert (
        first / "capsules" / "PUBLIC-V2-00" / "visible" / "workspace" / "app.txt"
    ).stat().st_mode & 0o777 == 0o755
    assert (
        first / "capsules" / "PUBLIC-V2-00" / "grader" / "files" / "test_public.py"
    ).stat().st_mode & 0o777 == 0o755


def test_public_v2_pack_rejects_identity_drift(tmp_path: Path) -> None:
    intake = _intake(tmp_path / "staging")
    output = tmp_path / "pack"
    compile_public_v2_pack(
        staging_root=intake.parent, intake_path=intake, output=output
    )
    manifest = json.loads((output / "manifest.json").read_bytes())
    manifest["task_count"] = 9
    (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CorpusError, match="authority"):
        validate_public_v2_pack(output)


def test_public_v2_pack_rejects_mode_drift(tmp_path: Path) -> None:
    intake = _intake(tmp_path / "staging")
    output = tmp_path / "pack"
    compile_public_v2_pack(
        staging_root=intake.parent, intake_path=intake, output=output
    )
    executable = (
        output / "capsules" / "PUBLIC-V2-00" / "visible" / "workspace" / "app.txt"
    )
    executable.chmod(0o644)
    with pytest.raises(CorpusError, match="identities drifted"):
        validate_public_v2_pack(output)
