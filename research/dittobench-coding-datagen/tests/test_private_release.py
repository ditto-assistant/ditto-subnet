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
from dittobench_coding_datagen.private_release import (
    compile_private_release,
    load_private_release,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _groups(root: Path, *, count: int = 50, unbalanced: bool = False) -> Path:
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
        stratum_index = index // 5
        if unbalanced and index == 49:
            stratum_index = 0
        manifest = build_private_group_manifest(
            opaque_group_id=group_id,
            opaque_repository_stratum_id=f"private-stratum-{stratum_index:02d}",
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
            group / "group-manifest.json", manifest.canonical_bytes()
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
            group / "group-audit.json", canonical_json_bytes(audit)
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
            group / "group-calibration.json", canonical_json_bytes(calibration)
        )
    return root


def test_private_release_compiles_fifty_balanced_groups(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    groups = _groups(protected / "groups")
    output = protected / "release.json"
    assert (
        main(
            [
                "compile-private-release",
                "--groups-dir",
                str(groups),
                "--release-id",
                "coding-private-v2-r1",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    compiled = json.loads(capsys.readouterr().out)
    assert compiled["group_count"] == 50
    assert compiled["repository_stratum_count"] == 10
    assert all("calibration_sha256" in group for group in compiled["groups"])
    assert compiled["weight_eligible"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    assert load_private_release(output) == compiled
    assert main(["verify-private-release", str(output)]) == 0
    assert json.loads(capsys.readouterr().out) == compiled


def test_private_release_rejects_missing_or_unbalanced_groups(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    with pytest.raises(CorpusError, match="fifty"):
        compile_private_release(
            groups_dir=_groups(protected / "short", count=49),
            corpus_release_id="coding-private-v2-short",
            output=protected / "short.json",
        )
    with pytest.raises(CorpusError, match="balanced"):
        compile_private_release(
            groups_dir=_groups(protected / "unbalanced", unbalanced=True),
            corpus_release_id="coding-private-v2-unbalanced",
            output=protected / "unbalanced.json",
        )


def test_private_release_rejects_audit_drift(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    groups = _groups(protected / "groups")
    audit = groups / "private-group-000" / "group-audit.json"
    raw = json.loads(audit.read_bytes())
    raw["group_manifest_sha256"] = "f" * 64
    audit.write_bytes(canonical_json_bytes(raw))
    with pytest.raises(CorpusError, match="does not match"):
        compile_private_release(
            groups_dir=groups,
            corpus_release_id="coding-private-v2-drift",
            output=protected / "drift.json",
        )


def test_private_release_loader_rejects_rehashed_group_projection_drift(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    release_path = protected / "release.json"
    compile_private_release(
        groups_dir=_groups(protected / "groups"),
        corpus_release_id="coding-private-v2-r1",
        output=release_path,
    )
    raw = json.loads(release_path.read_bytes())
    raw["groups"][0]["unexpected"] = "field"
    projection = dict(raw)
    projection.pop("release_sha256")
    raw["release_sha256"] = hashlib.sha256(canonical_json_bytes(projection)).hexdigest()
    release_path.write_bytes(canonical_json_bytes(raw))
    with pytest.raises(CorpusError, match="release authority"):
        load_private_release(release_path)


def test_private_release_allows_task_bound_runner_profile_diversity(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    groups = _groups(protected / "groups")
    calibration_path = groups / "private-group-049" / "group-calibration.json"
    calibration = json.loads(calibration_path.read_bytes())
    calibration["runner_profile_sha256"] = _sha("other-runner-profile")
    calibration_path.write_bytes(canonical_json_bytes(calibration))
    compiled = compile_private_release(
        groups_dir=groups,
        corpus_release_id="coding-private-v2-runner-diversity",
        output=protected / "runner-diversity.json",
    )
    assert len({group["runner_profile_sha256"] for group in compiled["groups"]}) == 2
