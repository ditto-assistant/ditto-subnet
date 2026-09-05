from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_private_catalog_v2_compile import (
    compile_private_catalog_v2,
)
from ditto.api_server.coding_private_v2_payload import (
    PrivateV2PayloadError,
    build_private_v2_payload,
    verify_private_v2_payload,
)

_CONDITIONS = (
    "v0_none",
    "v1_relevant",
    "v2_irrelevant",
    "v3_stale_conflict",
    "v4_current_override",
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _write_json(path: Path, value: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = coding_canonical_json_bytes(
        value, maximum_bytes=4 << 20, label="private v2 payload test"
    )
    path.write_bytes(body)
    path.chmod(0o600)
    return hashlib.sha256(body).hexdigest()


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o644)


def _tree_digest(root: Path) -> str:
    identities = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        body = path.read_bytes()
        identities.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(body).hexdigest(),
                "size_bytes": len(body),
            }
        )
    return hashlib.sha256(
        coding_canonical_json_bytes(
            identities, maximum_bytes=4 << 20, label="private v2 tree identity"
        )
    ).hexdigest()


def _bound_fixture(root: Path) -> tuple[Path, Path]:
    groups_root = root / "groups"
    groups_root.mkdir(mode=0o700)
    groups: list[dict[str, object]] = []
    for index in range(50):
        group_id = f"private-group-{index:03d}"
        group = groups_root / group_id
        _write_text(group / "snapshot" / "app.py", f"print({index})\n")
        _write_text(group / "grader" / "test_app.py", f"assert {index} >= 0\n")
        issue_sha = _write_json(group / "issue.json", {"title": f"issue-{index}"})
        runtime_sha = _write_json(
            group / "runtime-policy.json", {"editable_paths": ["app.py"]}
        )
        resource_sha = _write_json(
            group / "resource-profile.json", {"cpu": 1, "index": index}
        )
        arms = []
        for condition in _CONDITIONS:
            memory_sha = _write_json(
                group / "memory" / f"{condition}.json",
                {"memories": [] if condition == "v0_none" else [{"id": condition}]},
            )
            arms.append(
                {
                    "condition": condition,
                    "memory_bundle_sha256": memory_sha,
                    "memory_volume_tier": "small",
                    "seeded_memory_bytes": 4096,
                }
            )
        manifest_sha = _write_json(
            group / "authority" / "group-manifest.json",
            {
                "arms": arms,
                "repository_epoch": f"epoch-{index}",
                "runtime_policy_sha256": runtime_sha,
                "resource_profile_sha256": resource_sha,
                "visible_issue_sha256": issue_sha,
            },
        )
        groups.append(
            {
                "audit_sha256": _sha(f"audit-{index}"),
                "calibration_sha256": _sha(f"calibration-{index}"),
                "group_manifest_sha256": manifest_sha,
                "hidden_grader_tree_sha256": _tree_digest(group / "grader"),
                "memory_bundle_set_sha256": _sha(f"memory-set-{index}"),
                "opaque_group_id": group_id,
                "opaque_repository_stratum_id": f"stratum-{index // 5:02d}",
                "overlap_review_sha256": _sha(f"overlap-{index}"),
                "runner_profile_sha256": _sha(f"runner-{index}"),
                "semantic_family_id": f"family-{index:03d}",
                "semantic_review_sha256": _sha(f"semantic-{index}"),
                "visible_snapshot_tree_sha256": _tree_digest(group / "snapshot"),
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
        "release_sha256": hashlib.sha256(
            coding_canonical_json_bytes(
                projection, maximum_bytes=4 << 20, label="private release authority"
            )
        ).hexdigest(),
    }
    release_path = root / "release.json"
    _write_json(release_path, release)
    return release_path, groups_root


def test_private_v2_payload_binds_leaf_digests(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    release, groups = _bound_fixture(protected)
    catalog = compile_private_catalog_v2(
        release_authority=release,
        groups_root=groups,
        output=protected / "catalog",
    )
    payload = build_private_v2_payload(
        catalog_directory=protected / "catalog",
        groups_root=groups,
        output=protected / "payload",
    )
    assert payload["weight_eligible"] is False
    assert payload["catalog_sha256"] == catalog["catalog_sha256"]
    assert verify_private_v2_payload(protected / "payload") == payload


def test_private_v2_payload_rejects_memory_drift(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    release, groups = _bound_fixture(protected)
    compile_private_catalog_v2(
        release_authority=release,
        groups_root=groups,
        output=protected / "catalog",
    )
    drifted = groups / "private-group-000" / "memory" / "v0_none.json"
    drifted.write_bytes(drifted.read_bytes() + b"\n")
    with pytest.raises(PrivateV2PayloadError, match="drifted"):
        build_private_v2_payload(
            catalog_directory=protected / "catalog",
            groups_root=groups,
            output=protected / "payload",
        )


def test_private_v2_payload_rejects_leftover_objects(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    release, groups = _bound_fixture(protected)
    compile_private_catalog_v2(
        release_authority=release,
        groups_root=groups,
        output=protected / "catalog",
    )
    payload = build_private_v2_payload(
        catalog_directory=protected / "catalog",
        groups_root=groups,
        output=protected / "payload",
    )
    extra = protected / "payload" / "objects" / f"{'0' * 64}.bin"
    extra.write_bytes(b"leftover")
    extra.chmod(0o600)
    with pytest.raises(PrivateV2PayloadError, match="objects drifted"):
        verify_private_v2_payload(protected / "payload")
    extra.unlink()
    assert verify_private_v2_payload(protected / "payload") == payload
    objects_dir = protected / "payload" / "objects"
    actual_dir = protected / "payload" / "actual-objects"
    objects_dir.rename(actual_dir)
    objects_dir.symlink_to(actual_dir, target_is_directory=True)
    with pytest.raises(PrivateV2PayloadError, match="objects are invalid"):
        verify_private_v2_payload(protected / "payload")


def test_private_v2_payload_rejects_issue_drift(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    release, groups = _bound_fixture(protected)
    compile_private_catalog_v2(
        release_authority=release,
        groups_root=groups,
        output=protected / "catalog",
    )
    drifted = groups / "private-group-000" / "issue.json"
    drifted.write_bytes(drifted.read_bytes() + b"\n")
    with pytest.raises(PrivateV2PayloadError, match="drifted"):
        build_private_v2_payload(
            catalog_directory=protected / "catalog",
            groups_root=groups,
            output=protected / "payload",
        )


def test_private_v2_payload_rejects_swapped_issue_object(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    release, groups = _bound_fixture(protected)
    compile_private_catalog_v2(
        release_authority=release,
        groups_root=groups,
        output=protected / "catalog",
    )
    payload_dir = protected / "payload"
    build_private_v2_payload(
        catalog_directory=protected / "catalog",
        groups_root=groups,
        output=payload_dir,
    )
    swapped = b'{"title":"swapped-issue"}'
    digest = hashlib.sha256(swapped).hexdigest()
    object_path = payload_dir / "objects" / f"{digest}.bin"
    object_path.write_bytes(swapped)
    object_path.chmod(0o600)
    authority = json.loads((payload_dir / "payload-authority.json").read_bytes())
    objects = [item for item in authority["objects"] if item["sha256"] != digest]
    objects.append({"sha256": digest, "size_bytes": len(swapped)})
    authority["objects"] = sorted(objects, key=lambda item: str(item["sha256"]))
    authority["task_assets"][0]["artifacts"]["issue"] = digest
    projection = {
        key: value for key, value in authority.items() if key != "payload_sha256"
    }
    authority["payload_sha256"] = hashlib.sha256(
        coding_canonical_json_bytes(
            projection, maximum_bytes=8 << 20, label="private v2 payload authority"
        )
    ).hexdigest()
    (payload_dir / "payload-authority.json").write_bytes(
        coding_canonical_json_bytes(
            authority, maximum_bytes=8 << 20, label="private v2 payload authority"
        )
    )
    with pytest.raises(PrivateV2PayloadError, match="drifted"):
        verify_private_v2_payload(payload_dir)
