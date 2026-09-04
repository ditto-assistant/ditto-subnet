from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.cli import main
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_authoring import write_private_authoring_output
from dittobench_coding_datagen.private_group import (
    PrivateGroupArm,
    build_private_group_manifest,
)
from dittobench_coding_datagen.private_progress import audit_private_corpus_progress


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _groups(root: Path, *, count: int = 10) -> Path:
    root.mkdir(mode=0o700)
    conditions = (
        "v0_none",
        "v1_relevant",
        "v2_irrelevant",
        "v3_stale_conflict",
        "v4_current_override",
    )
    for index in range(count):
        group_id = f"private-group-{index:03d}"
        group = root / group_id
        group.mkdir(mode=0o700)
        authority = group / "authority"
        authority.mkdir(mode=0o700)
        manifest = build_private_group_manifest(
            opaque_group_id=group_id,
            opaque_repository_stratum_id=f"private-stratum-{index // 5:02d}",
            repository_epoch=f"private-epoch-{index:03d}",
            snapshot_manifest_sha256=_sha(f"snapshot-{index}"),
            visible_issue_sha256=_sha(f"issue-{index}"),
            runtime_policy_sha256=_sha(f"runtime-{index}"),
            hidden_grader_sha256=_sha(f"grader-{index}"),
            resource_profile_sha256=_sha(f"resource-{index}"),
            arms=tuple(
                PrivateGroupArm(
                    condition=condition,  # type: ignore[arg-type]
                    memory_bundle_sha256=_sha(f"memory-{index}-{condition}"),
                    seeded_memory_bytes=4096,
                    memory_volume_tier="small",
                )
                for condition in conditions
            ),
        )
        write_private_authoring_output(
            authority / "group-manifest.json", manifest.canonical_bytes()
        )
        audit = {
            "group_manifest_sha256": manifest.manifest_sha256(),
            "hidden_grader_tree_sha256": _sha(f"grader-tree-{index}"),
            "memory_bundle_set_sha256": _sha(f"memory-set-{index}"),
            "overlap_review_sha256": _sha(f"overlap-{index}"),
            "passed": True,
            "schema": "dittobench-coding-private-input-audit-v2",
            "visible_snapshot_tree_sha256": _sha(f"visible-tree-{index}"),
        }
        write_private_authoring_output(
            authority / "group-audit.json", canonical_json_bytes(audit)
        )
        calibration = {
            "base_observation_sha256": sorted(
                [_sha(f"base-{index}-0"), _sha(f"base-{index}-1")]
            ),
            "group_manifest_sha256": manifest.manifest_sha256(),
            "passed": True,
            "reference_observation_sha256": sorted(
                [_sha(f"reference-{index}-0"), _sha(f"reference-{index}-1")]
            ),
            "replicate_count_per_candidate": 2,
            "runner_profile_sha256": _sha("runner-profile"),
            "schema": "dittobench-coding-private-calibration-v2",
            "weight_eligible": False,
        }
        write_private_authoring_output(
            authority / "group-calibration.json", canonical_json_bytes(calibration)
        )
    return root


def test_private_progress_reports_redacted_partial_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    groups = _groups(protected / "groups")
    output = protected / "progress.json"
    assert (
        main(
            [
                "audit-private-corpus-progress",
                "--groups-dir",
                str(groups),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    progress = json.loads(capsys.readouterr().out)
    assert progress == json.loads(output.read_bytes())
    assert progress["group_count"] == 10
    assert progress["repository_stratum_count"] == 2
    assert progress["complete_repository_stratum_count"] == 2
    assert progress["remaining_group_count"] == 40
    assert progress["ready_for_release"] is False
    assert progress["weight_eligible"] is False
    assert "groups" not in progress and "opaque_group_id" not in progress


def test_private_progress_rejects_duplicate_group_authority(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    groups = _groups(protected / "groups")
    first = groups / "private-group-000" / "authority" / "group-audit.json"
    second = groups / "private-group-001" / "authority" / "group-audit.json"
    first_audit = json.loads(first.read_bytes())
    second_audit = json.loads(second.read_bytes())
    second_audit["hidden_grader_tree_sha256"] = first_audit["hidden_grader_tree_sha256"]
    second.write_bytes(canonical_json_bytes(second_audit))
    with pytest.raises(CorpusError, match="duplicated"):
        audit_private_corpus_progress(groups)


def test_private_progress_rejects_missing_calibration(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    groups = _groups(protected / "groups", count=1)
    calibration = groups / "private-group-000" / "authority" / "group-calibration.json"
    calibration.rename(protected / "held-calibration.json")
    with pytest.raises(CorpusError, match="incomplete"):
        audit_private_corpus_progress(groups)


def test_private_progress_rejects_exposed_authority(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    groups = _groups(protected / "groups", count=1)
    authority = groups / "private-group-000" / "authority"
    authority.chmod(0o755)
    with pytest.raises(CorpusError, match="authority"):
        audit_private_corpus_progress(groups)
