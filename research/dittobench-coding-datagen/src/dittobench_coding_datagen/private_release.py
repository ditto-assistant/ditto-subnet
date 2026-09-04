"""Compile fifty audited private Coding groups into one release authority."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_opaque_id,
    sha256_hex,
)
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_authoring import (
    load_private_group_manifest,
    write_private_authoring_output,
)

PRIVATE_RELEASE_SCHEMA = "dittobench-coding-private-release-v2"
_GROUP_COUNT = 50
_STRATUM_COUNT = 10
_GROUPS_PER_STRATUM = 5
_MAX_AUDIT_BYTES = 1 << 20


def compile_private_release(
    *, groups_dir: Path, corpus_release_id: str, output: Path
) -> dict[str, Any]:
    """Compile exactly fifty audited groups across ten balanced strata."""

    release_id = safe_opaque_id(corpus_release_id)
    if (
        groups_dir.is_symlink()
        or not groups_dir.is_dir()
        or stat.S_IMODE(groups_dir.stat().st_mode) & 0o077
    ):
        raise CorpusError("private release groups directory is unsafe")
    directories = sorted(groups_dir.iterdir(), key=lambda path: path.name)
    if len(directories) != _GROUP_COUNT:
        raise CorpusError("private release requires exactly fifty groups")
    groups: list[dict[str, object]] = []
    strata: dict[str, int] = {}
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            raise CorpusError("private release group directory is unsafe")
        if {path.name for path in directory.iterdir()} != {
            "group-audit.json",
            "group-manifest.json",
        }:
            raise CorpusError("private release group directory has unexpected files")
        manifest_path = directory / "group-manifest.json"
        manifest = load_private_group_manifest(manifest_path)
        if directory.name != manifest.opaque_group_id:
            raise CorpusError("private release group directory identity drifted")
        manifest_sha256 = manifest.manifest_sha256()
        audit_path = directory / "group-audit.json"
        audit, audit_body = _load_audit(audit_path)
        if audit["group_manifest_sha256"] != manifest_sha256:
            raise CorpusError("private release group audit does not match manifest")
        stratum = manifest.opaque_repository_stratum_id
        strata[stratum] = strata.get(stratum, 0) + 1
        groups.append(
            {
                "audit_sha256": sha256_hex(audit_body),
                "group_manifest_sha256": manifest_sha256,
                "hidden_grader_tree_sha256": audit["hidden_grader_tree_sha256"],
                "opaque_group_id": manifest.opaque_group_id,
                "opaque_repository_stratum_id": stratum,
                "overlap_review_sha256": audit["overlap_review_sha256"],
                "visible_snapshot_tree_sha256": audit["visible_snapshot_tree_sha256"],
            }
        )
    if (
        len(strata) != _STRATUM_COUNT
        or any(count != _GROUPS_PER_STRATUM for count in strata.values())
        or len({str(group["opaque_group_id"]) for group in groups}) != _GROUP_COUNT
        or len({str(group["group_manifest_sha256"]) for group in groups})
        != _GROUP_COUNT
    ):
        raise CorpusError("private release repository strata are not balanced")
    projection = {
        "coding_contract_version": 2,
        "corpus_release_id": release_id,
        "group_count": _GROUP_COUNT,
        "groups": groups,
        "repository_stratum_count": _STRATUM_COUNT,
        "schema": PRIVATE_RELEASE_SCHEMA,
        "weight_eligible": False,
    }
    release = {
        **projection,
        "release_sha256": sha256_hex(canonical_json_bytes(projection)),
    }
    body = canonical_json_bytes(release)
    write_private_authoring_output(output, body)
    return release


def load_private_release(path: Path) -> dict[str, Any]:
    """Load and re-derive one canonical private release authority."""

    raw, body = _canonical_object(path, label="private release")
    expected = {
        "coding_contract_version",
        "corpus_release_id",
        "group_count",
        "groups",
        "release_sha256",
        "repository_stratum_count",
        "schema",
        "weight_eligible",
    }
    if (
        set(raw) != expected
        or raw["schema"] != PRIVATE_RELEASE_SCHEMA
        or raw["coding_contract_version"] != 2
        or raw["weight_eligible"] is not False
        or raw["group_count"] != _GROUP_COUNT
        or raw["repository_stratum_count"] != _STRATUM_COUNT
        or not isinstance(raw["groups"], list)
        or len(raw["groups"]) != _GROUP_COUNT
    ):
        raise CorpusError("private release authority is invalid")
    projection = dict(raw)
    release_sha256 = projection.pop("release_sha256", None)
    if (
        not _sha256(release_sha256)
        or sha256_hex(canonical_json_bytes(projection)) != release_sha256
        or canonical_json_bytes(raw) != body
    ):
        raise CorpusError("private release authority is invalid")
    return raw


def _load_audit(path: Path) -> tuple[dict[str, Any], bytes]:
    raw, body = _canonical_object(path, label="private group audit")
    expected = {
        "group_manifest_sha256",
        "hidden_grader_tree_sha256",
        "overlap_review_sha256",
        "passed",
        "schema",
        "visible_snapshot_tree_sha256",
    }
    if (
        set(raw) != expected
        or raw["schema"] != "dittobench-coding-private-input-audit-v2"
        or raw["passed"] is not True
        or any(
            not _sha256(raw[field])
            for field in (
                "group_manifest_sha256",
                "hidden_grader_tree_sha256",
                "overlap_review_sha256",
                "visible_snapshot_tree_sha256",
            )
        )
    ):
        raise CorpusError("private group audit authority is invalid")
    return raw, body


def _canonical_object(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size < 1
        or path.stat().st_size > _MAX_AUDIT_BYTES
    ):
        raise CorpusError(f"{label} is invalid")
    try:
        body = path.read_bytes()
        raw: Any = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError(f"{label} is invalid") from error
    if not isinstance(raw, dict) or canonical_json_bytes(raw) != body:
        raise CorpusError(f"{label} is invalid")
    return raw, body


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = ["PRIVATE_RELEASE_SCHEMA", "compile_private_release", "load_private_release"]
