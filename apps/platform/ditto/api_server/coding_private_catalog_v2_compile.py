"""Offline compiler from an audited v2 group release to sealed catalog leaves."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_private_catalog_v2 import (
    CodingMemoryConditionV2,
    CodingPrivateCatalogV2Task,
    coding_private_catalog_v2_task_digest,
)
from ditto.coding_selection import (
    coding_catalog_empty_leaf_hash,
    coding_catalog_leaf_hash,
    coding_catalog_node_hash,
)

_CONDITIONS = (
    CodingMemoryConditionV2.NONE,
    CodingMemoryConditionV2.RELEVANT,
    CodingMemoryConditionV2.IRRELEVANT,
    CodingMemoryConditionV2.STALE_CONFLICT,
    CodingMemoryConditionV2.CURRENT_OVERRIDE,
)
_GROUP_COUNT = 50
_TASK_COUNT = _GROUP_COUNT * len(_CONDITIONS)


class PrivateCatalogV2CompileError(ValueError):
    """Protected group material cannot become a v2 private catalog."""


def compile_private_catalog_v2(
    *, release_authority: Path, groups_root: Path, output: Path
) -> dict[str, Any]:
    """Create 250 condition-specific leaves and Merkle proofs, offline only."""

    release = _canonical_object(release_authority, "private release authority")
    if (
        release.get("schema") != "dittobench-coding-private-release-v2"
        or release.get("coding_contract_version") != 2
        or release.get("weight_eligible") is not False
        or release.get("group_count") != _GROUP_COUNT
        or not isinstance(release.get("groups"), list)
        or len(release["groups"]) != _GROUP_COUNT
    ):
        raise PrivateCatalogV2CompileError("private release authority is invalid")
    release_sha256 = release.get("release_sha256")
    release_projection = dict(release)
    release_projection.pop("release_sha256", None)
    if (
        not _sha256(release_sha256)
        or _digest(release_projection, "private release authority") != release_sha256
        or groups_root.is_symlink()
        or not groups_root.is_dir()
    ):
        raise PrivateCatalogV2CompileError("private catalog inputs are invalid")
    _new_directory(output)
    records_dir = output / "records"
    proofs_dir = output / "proofs"
    records_dir.mkdir(mode=0o700)
    proofs_dir.mkdir(mode=0o700)
    tasks: list[CodingPrivateCatalogV2Task] = []
    task_metadata: list[dict[str, Any]] = []
    for group in release["groups"]:
        group_tasks = _compile_group(
            group=group,
            release_sha256=release_sha256,
            corpus_release_id=release["corpus_release_id"],
            groups_root=groups_root,
            catalog_start=len(tasks),
        )
        task_metadata.extend(group_tasks)
        tasks.extend(item["task"] for item in group_tasks)
    if len(tasks) != _TASK_COUNT:
        raise PrivateCatalogV2CompileError("private catalog task count is invalid")
    leaves = [
        coding_catalog_leaf_hash(
            catalog_index=task.catalog_index,
            task_commitment_sha256=task.task_commitment_sha256,
        )
        for task in tasks
    ]
    leaf_count = 1 << (len(leaves) - 1).bit_length()
    leaves.extend(
        coding_catalog_empty_leaf_hash(catalog_index=index)
        for index in range(len(leaves), leaf_count)
    )
    levels = [leaves]
    level = 0
    while len(levels[-1]) > 1:
        previous = levels[-1]
        levels.append(
            [
                coding_catalog_node_hash(
                    level=level,
                    left_sha256=previous[index],
                    right_sha256=previous[index + 1],
                )
                for index in range(0, len(previous), 2)
            ]
        )
        level += 1
    root = levels[-1][0]
    records: list[dict[str, Any]] = []
    for metadata in task_metadata:
        task = metadata["task"]
        body = coding_canonical_json_bytes(
            task.model_dump(mode="json", by_alias=True),
            maximum_bytes=64 << 10,
            label="private v2 catalog task",
        )
        _write_new(records_dir / f"{task.catalog_index:06d}.json", body)
        siblings = []
        index = task.catalog_index
        for current in levels[:-1]:
            siblings.append(current[index ^ 1])
            index >>= 1
        proof = {
            "schema": "dittobench-coding-private-catalog-membership-v2",
            "coding_contract_version": 2,
            "weight_eligible": False,
            "catalog_index": task.catalog_index,
            "task_commitment_sha256": task.task_commitment_sha256,
            "catalog_merkle_root": root,
            "sibling_sha256": siblings,
        }
        proof_body = coding_canonical_json_bytes(
            proof, maximum_bytes=64 << 10, label="private v2 membership proof"
        )
        _write_new(proofs_dir / f"{task.catalog_index:06d}.json", proof_body)
        records.append(
            {
                "catalog_index": task.catalog_index,
                "task_version_id": task.task_version_id,
                "task_commitment_sha256": task.task_commitment_sha256,
                "record_sha256": hashlib.sha256(body).hexdigest(),
                "record_size_bytes": len(body),
                "proof_sha256": hashlib.sha256(proof_body).hexdigest(),
            }
        )
    projection = {
        "schema": "dittobench-coding-private-catalog-v2",
        "coding_contract_version": 2,
        "weight_eligible": False,
        "corpus_release_id": release["corpus_release_id"],
        "private_release_sha256": release_sha256,
        "task_version_count": len(tasks),
        "catalog_merkle_root": root,
        "records": records,
    }
    authority = {
        **projection,
        "catalog_sha256": _digest(projection, "private v2 catalog authority"),
    }
    _write_new(
        output / "catalog-authority.json",
        coding_canonical_json_bytes(
            authority, maximum_bytes=4 << 20, label="private v2 catalog authority"
        ),
    )
    return authority


def verify_private_catalog_v2(directory: Path) -> dict[str, Any]:
    """Recheck every v2 leaf and proof without touching any storage provider."""

    authority = _canonical_object(
        directory / "catalog-authority.json", "private v2 catalog authority"
    )
    expected = {
        "schema",
        "coding_contract_version",
        "weight_eligible",
        "corpus_release_id",
        "private_release_sha256",
        "task_version_count",
        "catalog_merkle_root",
        "records",
        "catalog_sha256",
    }
    projection = dict(authority)
    catalog_sha = projection.pop("catalog_sha256", None)
    if (
        set(authority) != expected
        or authority["schema"] != "dittobench-coding-private-catalog-v2"
        or authority["coding_contract_version"] != 2
        or authority["weight_eligible"] is not False
        or authority["task_version_count"] != _TASK_COUNT
        or not _sha256(catalog_sha)
        or _digest(projection, "private v2 catalog authority") != catalog_sha
        or not isinstance(authority["records"], list)
        or len(authority["records"]) != _TASK_COUNT
    ):
        raise PrivateCatalogV2CompileError("private v2 catalog authority is invalid")
    for index, item in enumerate(authority["records"]):
        if not isinstance(item, dict) or item.get("catalog_index") != index:
            raise PrivateCatalogV2CompileError(
                "private v2 catalog record order is invalid"
            )
        record = _canonical_object(
            directory / "records" / f"{index:06d}.json", "private v2 catalog record"
        )
        task = CodingPrivateCatalogV2Task.model_validate(record)
        if (
            task.catalog_index != index
            or task.task_version_id != item.get("task_version_id")
            or task.task_commitment_sha256 != item.get("task_commitment_sha256")
            or _file_sha256(directory / "records" / f"{index:06d}.json")
            != item.get("record_sha256")
        ):
            raise PrivateCatalogV2CompileError("private v2 catalog record drifted")
        proof = _canonical_object(
            directory / "proofs" / f"{index:06d}.json", "private v2 catalog proof"
        )
        siblings = proof.get("sibling_sha256")
        if (
            proof.get("catalog_index") != index
            or proof.get("task_commitment_sha256") != task.task_commitment_sha256
            or proof.get("catalog_merkle_root") != authority["catalog_merkle_root"]
            or not isinstance(siblings, list)
            or any(not _sha256(sibling) for sibling in siblings)
            or _file_sha256(directory / "proofs" / f"{index:06d}.json")
            != item.get("proof_sha256")
        ):
            raise PrivateCatalogV2CompileError("private v2 catalog proof drifted")
        node = coding_catalog_leaf_hash(
            catalog_index=index, task_commitment_sha256=task.task_commitment_sha256
        )
        for level, sibling in enumerate(siblings):
            node = (
                coding_catalog_node_hash(
                    level=level, left_sha256=sibling, right_sha256=node
                )
                if (index >> level) & 1
                else coding_catalog_node_hash(
                    level=level, left_sha256=node, right_sha256=sibling
                )
            )
        if node != authority["catalog_merkle_root"]:
            raise PrivateCatalogV2CompileError("private v2 catalog proof is invalid")
    return authority


def _compile_group(
    *,
    group: Any,
    release_sha256: str,
    corpus_release_id: str,
    groups_root: Path,
    catalog_start: int,
) -> list[dict[str, Any]]:
    if not isinstance(group, dict):
        raise PrivateCatalogV2CompileError("private release group is invalid")
    required = {
        "opaque_group_id",
        "group_manifest_sha256",
        "visible_snapshot_tree_sha256",
        "hidden_grader_tree_sha256",
        "memory_bundle_set_sha256",
        "calibration_sha256",
        "semantic_review_sha256",
        "runner_profile_sha256",
    }
    if not required <= set(group) or any(
        not _sha256(group[key]) for key in required - {"opaque_group_id"}
    ):
        raise PrivateCatalogV2CompileError("private release group is invalid")
    root = groups_root / group["opaque_group_id"]
    manifest = _canonical_object(
        root / "authority/group-manifest.json", "private group manifest"
    )
    if (
        _file_sha256(root / "authority/group-manifest.json")
        != group["group_manifest_sha256"]
    ):
        raise PrivateCatalogV2CompileError("private group manifest drifted")
    arms = manifest.get("arms")
    if not isinstance(arms, list) or [arm.get("condition") for arm in arms] != [
        condition.value for condition in _CONDITIONS
    ]:
        raise PrivateCatalogV2CompileError("private group conditions are invalid")
    task_metadata = []
    for offset, arm in enumerate(arms):
        index = catalog_start + offset
        task = CodingPrivateCatalogV2Task.model_construct(
            schema_name="dittobench-coding-private-catalog-task-v2",
            coding_contract_version=2,
            weight_eligible=False,
            corpus_release_id=corpus_release_id,
            catalog_index=index,
            task_version_id=f"{group['opaque_group_id']}-{arm['condition']}",
            base_task_group_id=group["opaque_group_id"],
            condition=CodingMemoryConditionV2(arm["condition"]),
            repository_epoch=manifest["repository_epoch"],
            private_release_sha256=release_sha256,
            group_manifest_sha256=group["group_manifest_sha256"],
            visible_snapshot_tree_sha256=group["visible_snapshot_tree_sha256"],
            hidden_grader_tree_sha256=group["hidden_grader_tree_sha256"],
            memory_bundle_sha256=arm["memory_bundle_sha256"],
            runtime_policy_sha256=manifest["runtime_policy_sha256"],
            resource_profile_sha256=manifest["resource_profile_sha256"],
            calibration_sha256=group["calibration_sha256"],
            semantic_review_sha256=group["semantic_review_sha256"],
            runner_profile_sha256=group["runner_profile_sha256"],
            task_commitment_sha256="0" * 64,
        )
        task = CodingPrivateCatalogV2Task.model_validate(
            {
                **task.model_dump(mode="json", by_alias=True),
                "task_commitment_sha256": coding_private_catalog_v2_task_digest(task),
            }
        )
        task_metadata.append({"task": task})
    return task_metadata


def _canonical_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PrivateCatalogV2CompileError(f"{label} is unavailable")
    try:
        body = path.read_bytes()
        value: Any = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PrivateCatalogV2CompileError(f"{label} is invalid") from error
    if (
        not isinstance(value, dict)
        or coding_canonical_json_bytes(value, maximum_bytes=4 << 20, label=label)
        != body
    ):
        raise PrivateCatalogV2CompileError(f"{label} is not canonical")
    return value


def _new_directory(path: Path) -> None:
    if (
        path.exists()
        or path.is_symlink()
        or path.parent.is_symlink()
        or not path.parent.is_dir()
        or stat.S_IMODE(path.parent.stat().st_mode) & 0o077
    ):
        raise PrivateCatalogV2CompileError("private catalog output exists")
    path.mkdir(mode=0o700)


def _write_new(path: Path, body: bytes) -> None:
    with path.open("xb") as output:
        output.write(body)
    path.chmod(0o600)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _digest(value: dict[str, Any], label: str) -> str:
    return hashlib.sha256(
        coding_canonical_json_bytes(value, maximum_bytes=4 << 20, label=label)
    ).hexdigest()
