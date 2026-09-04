from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_private_catalog_v2_compile import (
    compile_private_catalog_v2,
    verify_private_catalog_v2,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _write(path: Path, value: Mapping[str, object]) -> str:
    body = coding_canonical_json_bytes(
        dict(value), maximum_bytes=4 << 20, label="private v2 compiler test"
    )
    path.write_bytes(body)
    path.chmod(0o600)
    return hashlib.sha256(body).hexdigest()


def _fixture(root: Path) -> tuple[Path, Path]:
    groups_root = root / "groups"
    groups_root.mkdir(mode=0o700)
    groups: list[dict[str, object]] = []
    conditions = (
        "v0_none",
        "v1_relevant",
        "v2_irrelevant",
        "v3_stale_conflict",
        "v4_current_override",
    )
    for index in range(50):
        group_id = f"private-group-{index:03d}"
        authority = groups_root / group_id / "authority"
        authority.mkdir(parents=True, mode=0o700)
        manifest = {
            "arms": [
                {
                    "condition": condition,
                    "memory_bundle_sha256": _sha(f"memory-{index}-{condition}"),
                    "memory_volume_tier": "small",
                    "seeded_memory_bytes": 4096,
                }
                for condition in conditions
            ],
            "repository_epoch": f"epoch-{index}",
            "runtime_policy_sha256": _sha(f"runtime-{index}"),
            "resource_profile_sha256": _sha(f"resource-{index}"),
        }
        manifest_sha = _write(authority / "group-manifest.json", manifest)
        groups.append(
            {
                "opaque_group_id": group_id,
                "group_manifest_sha256": manifest_sha,
                "visible_snapshot_tree_sha256": _sha(f"visible-{index}"),
                "hidden_grader_tree_sha256": _sha(f"grader-{index}"),
                "memory_bundle_set_sha256": _sha(f"memory-set-{index}"),
                "calibration_sha256": _sha(f"calibration-{index}"),
                "semantic_review_sha256": _sha(f"semantic-{index}"),
                "runner_profile_sha256": _sha(f"runner-{index}"),
            }
        )
    projection = {
        "schema": "dittobench-coding-private-release-v2",
        "coding_contract_version": 2,
        "weight_eligible": False,
        "corpus_release_id": "coding-private-v2-r1",
        "group_count": 50,
        "groups": groups,
        "repository_stratum_count": 10,
    }
    release = {
        **projection,
        "release_sha256": _sha(json.dumps(projection, sort_keys=True)),
    }
    release["release_sha256"] = hashlib.sha256(
        coding_canonical_json_bytes(
            projection, maximum_bytes=4 << 20, label="private release authority"
        )
    ).hexdigest()
    release_path = root / "release.json"
    _write(release_path, release)
    return release_path, groups_root


def test_private_v2_compiler_emits_all_condition_leaves(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    release, groups = _fixture(protected)
    output = protected / "catalog"
    authority = compile_private_catalog_v2(
        release_authority=release,
        groups_root=groups,
        output=output,
    )
    assert authority["task_version_count"] == 250
    assert authority["weight_eligible"] is False
    assert len(list((output / "records").glob("*.json"))) == 250
    assert len(list((output / "proofs").glob("*.json"))) == 250
    first = json.loads((output / "records" / "000000.json").read_bytes())
    final = json.loads((output / "records" / "000249.json").read_bytes())
    assert first["condition"] == "v0_none"
    assert final["condition"] == "v4_current_override"
    assert verify_private_catalog_v2(output) == authority
