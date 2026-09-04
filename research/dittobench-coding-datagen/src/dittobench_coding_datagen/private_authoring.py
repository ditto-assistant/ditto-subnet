"""Strict external source parsing and create-only private authoring outputs."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_group import (
    PrivateGroupArm,
    PrivateGroupManifest,
    build_private_group_manifest,
)

PRIVATE_GROUP_SOURCE_SCHEMA = "dittobench-coding-private-group-source-v2"
_MAX_SOURCE_BYTES = 1 << 20


def build_private_group_from_source(path: Path) -> PrivateGroupManifest:
    """Parse one canonical curator source and build its shadow-only manifest."""

    raw = _canonical_object(path, label="private group source")
    expected = {
        "arms",
        "hidden_grader_sha256",
        "opaque_group_id",
        "opaque_repository_stratum_id",
        "repository_epoch",
        "resource_profile_sha256",
        "runtime_policy_sha256",
        "schema",
        "snapshot_manifest_sha256",
        "visible_issue_sha256",
    }
    if set(raw) != expected or raw["schema"] != PRIVATE_GROUP_SOURCE_SCHEMA:
        raise CorpusError("private group source authority is invalid")
    arms = _parse_arms(raw["arms"])
    try:
        return build_private_group_manifest(
            opaque_group_id=_string(raw["opaque_group_id"]),
            opaque_repository_stratum_id=_string(raw["opaque_repository_stratum_id"]),
            repository_epoch=_string(raw["repository_epoch"]),
            snapshot_manifest_sha256=_string(raw["snapshot_manifest_sha256"]),
            visible_issue_sha256=_string(raw["visible_issue_sha256"]),
            runtime_policy_sha256=_string(raw["runtime_policy_sha256"]),
            hidden_grader_sha256=_string(raw["hidden_grader_sha256"]),
            resource_profile_sha256=_string(raw["resource_profile_sha256"]),
            arms=arms,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CorpusError("private group source authority is invalid") from error


def load_private_group_manifest(path: Path) -> PrivateGroupManifest:
    """Load and re-derive one canonical private group manifest."""

    raw = _canonical_object(path, label="private group manifest")
    expected = {
        "arms",
        "hidden_grader_sha256",
        "opaque_group_id",
        "opaque_repository_stratum_id",
        "repository_epoch",
        "resource_profile_sha256",
        "runtime_policy_sha256",
        "schema",
        "snapshot_manifest_sha256",
        "visible_issue_sha256",
        "weight_eligible",
    }
    if (
        set(raw) != expected
        or raw["schema"] != "dittobench-coding-private-group-v2"
        or raw["weight_eligible"] is not False
    ):
        raise CorpusError("private group manifest authority is invalid")
    manifest = build_private_group_manifest(
        opaque_group_id=_string(raw["opaque_group_id"]),
        opaque_repository_stratum_id=_string(raw["opaque_repository_stratum_id"]),
        repository_epoch=_string(raw["repository_epoch"]),
        snapshot_manifest_sha256=_string(raw["snapshot_manifest_sha256"]),
        visible_issue_sha256=_string(raw["visible_issue_sha256"]),
        runtime_policy_sha256=_string(raw["runtime_policy_sha256"]),
        hidden_grader_sha256=_string(raw["hidden_grader_sha256"]),
        resource_profile_sha256=_string(raw["resource_profile_sha256"]),
        arms=_parse_arms(raw["arms"]),
    )
    if manifest.canonical_bytes() != canonical_json_bytes(raw):
        raise CorpusError("private group manifest authority is invalid")
    return manifest


def write_private_authoring_output(path: Path, body: bytes) -> None:
    """Create one mode-0600 output under an existing protected directory."""

    parent = path.parent
    if (
        path.exists()
        or path.is_symlink()
        or parent.is_symlink()
        or not parent.is_dir()
        or stat.S_IMODE(parent.stat().st_mode) & 0o077
    ):
        raise CorpusError("private authoring output path is unsafe")
    try:
        with path.open("xb") as output:
            output.write(body)
        path.chmod(0o600)
    except OSError as error:
        raise CorpusError("private authoring output could not be created") from error


def _canonical_object(path: Path, *, label: str) -> dict[str, Any]:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size < 1
        or path.stat().st_size > _MAX_SOURCE_BYTES
    ):
        raise CorpusError(f"{label} is invalid")
    try:
        body = path.read_bytes()
        raw: Any = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError(f"{label} is invalid") from error
    if not isinstance(raw, dict) or canonical_json_bytes(raw) != body:
        raise CorpusError(f"{label} is invalid")
    return raw


def _parse_arms(value: Any) -> tuple[PrivateGroupArm, ...]:
    if not isinstance(value, list) or len(value) != 5:
        raise CorpusError("private group arms are invalid")
    arms: list[PrivateGroupArm] = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {
            "condition",
            "memory_bundle_sha256",
            "memory_volume_tier",
            "seeded_memory_bytes",
        }:
            raise CorpusError("private group arms are invalid")
        condition = raw["condition"]
        volume_tier = raw["memory_volume_tier"]
        seeded_bytes = raw["seeded_memory_bytes"]
        if (
            condition
            not in {
                "v0_none",
                "v1_relevant",
                "v2_irrelevant",
                "v3_stale_conflict",
                "v4_current_override",
            }
            or volume_tier not in {"small", "medium", "large"}
            or type(seeded_bytes) is not int
        ):
            raise CorpusError("private group arms are invalid")
        arms.append(
            PrivateGroupArm(
                condition=condition,
                memory_bundle_sha256=_string(raw["memory_bundle_sha256"]),
                seeded_memory_bytes=seeded_bytes,
                memory_volume_tier=volume_tier,
            )
        )
    return tuple(arms)


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise CorpusError("private group string authority is invalid")
    return value


__all__ = [
    "PRIVATE_GROUP_SOURCE_SCHEMA",
    "build_private_group_from_source",
    "load_private_group_manifest",
    "write_private_authoring_output",
]
