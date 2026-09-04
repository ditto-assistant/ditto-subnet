from __future__ import annotations

import json
from pathlib import Path

import pytest

from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.cli import main
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_authoring import (
    build_private_group_from_source,
    load_private_group_manifest,
    write_private_authoring_output,
)


def _source(path: Path, *, seeded_bytes: object = 4096) -> Path:
    arms = [
        {
            "condition": condition,
            "memory_bundle_sha256": fill * 64,
            "memory_volume_tier": "medium",
            "seeded_memory_bytes": seeded_bytes,
        }
        for condition, fill in (
            ("v0_none", "1"),
            ("v1_relevant", "2"),
            ("v2_irrelevant", "3"),
            ("v3_stale_conflict", "4"),
            ("v4_current_override", "5"),
        )
    ]
    path.write_bytes(
        canonical_json_bytes(
            {
                "arms": arms,
                "hidden_grader_sha256": "6" * 64,
                "opaque_group_id": "private-group-01",
                "opaque_repository_stratum_id": "private-stratum-01",
                "repository_epoch": "private-epoch-01",
                "resource_profile_sha256": "7" * 64,
                "runtime_policy_sha256": "8" * 64,
                "schema": "dittobench-coding-private-group-source-v2",
                "snapshot_manifest_sha256": "9" * 64,
                "visible_issue_sha256": "a" * 64,
            }
        )
    )
    return path


def test_private_authoring_cli_builds_and_audits_create_only_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    source = _source(protected / "source.json")
    manifest = protected / "manifest.json"
    assert (
        main(
            [
                "build-private-group",
                "--source",
                str(source),
                "--output",
                str(manifest),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["weight_eligible"] is False
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert load_private_group_manifest(manifest).opaque_group_id == "private-group-01"

    visible = protected / "visible"
    grader = protected / "grader"
    visible.mkdir(mode=0o700)
    grader.mkdir(mode=0o700)
    (visible / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (grader / "test_hidden.py").write_text("assert True\n", encoding="utf-8")
    audit = protected / "audit.json"
    assert (
        main(
            [
                "audit-private-group",
                "--manifest",
                str(manifest),
                "--visible-snapshot",
                str(visible),
                "--hidden-grader",
                str(grader),
                "--overlap-review-sha256",
                "b" * 64,
                "--output",
                str(audit),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["passed"] is True
    assert audit.stat().st_mode & 0o777 == 0o600


def test_private_authoring_rejects_boolean_memory_size(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="arms"):
        build_private_group_from_source(
            _source(tmp_path / "source.json", seeded_bytes=True)
        )


def test_private_authoring_output_requires_protected_create_only_path(
    tmp_path: Path,
) -> None:
    unprotected = tmp_path / "unprotected"
    unprotected.mkdir(mode=0o755)
    with pytest.raises(CorpusError, match="unsafe"):
        write_private_authoring_output(unprotected / "manifest.json", b"safe\n")

    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    output = protected / "manifest.json"
    write_private_authoring_output(output, b"safe\n")
    with pytest.raises(CorpusError, match="unsafe"):
        write_private_authoring_output(output, b"replacement\n")
