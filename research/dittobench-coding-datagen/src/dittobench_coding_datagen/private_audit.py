"""Fail-closed local checks for externally staged private Coding task material."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    sha256_hex,
    tree_identities,
)
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_group import PrivateGroupManifest

_FORBIDDEN_BYTES = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
)
_MAX_AUDIT_FILE_BYTES = 64 << 20


@dataclass(frozen=True)
class PrivateInputAudit:
    schema: str
    group_manifest_sha256: str
    visible_snapshot_tree_sha256: str
    hidden_grader_tree_sha256: str
    memory_bundle_set_sha256: str
    overlap_review_sha256: str
    passed: bool

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "group_manifest_sha256": self.group_manifest_sha256,
                "hidden_grader_tree_sha256": self.hidden_grader_tree_sha256,
                "memory_bundle_set_sha256": self.memory_bundle_set_sha256,
                "overlap_review_sha256": self.overlap_review_sha256,
                "passed": self.passed,
                "schema": self.schema,
                "visible_snapshot_tree_sha256": self.visible_snapshot_tree_sha256,
            }
        )


def audit_private_group_inputs(
    *,
    manifest: PrivateGroupManifest,
    visible_snapshot: Path,
    hidden_grader: Path,
    memory_bundles: Path,
    overlap_review_sha256: str,
) -> PrivateInputAudit:
    """Scan staged files and bind independent overlap review without disclosing it."""

    if not _sha256(overlap_review_sha256):
        raise CorpusError("private overlap review identity is invalid")
    visible_tree = _scan_tree(visible_snapshot, label="visible snapshot")
    hidden_tree = _scan_tree(hidden_grader, label="hidden grader")
    memory_set = _scan_memory_bundles(manifest=manifest, root=memory_bundles)
    return PrivateInputAudit(
        schema="dittobench-coding-private-input-audit-v2",
        group_manifest_sha256=manifest.manifest_sha256(),
        visible_snapshot_tree_sha256=visible_tree,
        hidden_grader_tree_sha256=hidden_tree,
        memory_bundle_set_sha256=memory_set,
        overlap_review_sha256=overlap_review_sha256,
        passed=True,
    )


def _scan_memory_bundles(*, manifest: PrivateGroupManifest, root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise CorpusError("private memory bundles are unsafe")
    paths = sorted(root.iterdir(), key=lambda path: path.name)
    expected_names = {f"{arm.condition}.json" for arm in manifest.arms}
    if {path.name for path in paths} != expected_names:
        raise CorpusError("private memory bundle set is incomplete")
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise CorpusError("private memory bundle is unsafe")
        condition = path.stem
        arm = next(
            (item for item in manifest.arms if item.condition == condition), None
        )
        if arm is None:
            raise CorpusError("private memory bundle condition is invalid")
        body = path.read_bytes()
        if (
            not body
            or len(body) > min(_MAX_AUDIT_FILE_BYTES, arm.seeded_memory_bytes)
            or sha256_hex(body) != arm.memory_bundle_sha256
            or any(pattern.search(body) for pattern in _FORBIDDEN_BYTES)
        ):
            raise CorpusError("private memory bundle identity is invalid")
        try:
            raw = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CorpusError("private memory bundle is invalid") from error
        if (
            not isinstance(raw, dict)
            or set(raw) != {"memories"}
            or canonical_json_bytes(raw) != body
            or not isinstance(raw["memories"], list)
            or ((condition == "v0_none") != (len(raw["memories"]) == 0))
        ):
            raise CorpusError("private memory bundle is invalid")
    identities = tree_identities(root)
    return sha256_hex(canonical_json_bytes([item.as_json() for item in identities]))


def _scan_tree(root: Path, *, label: str) -> str:
    if root.is_symlink() or not root.is_dir():
        raise CorpusError(f"private {label} is unsafe")
    identities = tree_identities(root)
    for identity in identities:
        path = root / identity.path
        if path.stat().st_size > _MAX_AUDIT_FILE_BYTES:
            raise CorpusError(f"private {label} file is too large for audit")
        body = path.read_bytes()
        if any(pattern.search(body) for pattern in _FORBIDDEN_BYTES):
            raise CorpusError(f"private {label} contains a credential-like secret")
    return sha256_hex(canonical_json_bytes([item.as_json() for item in identities]))


def _sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


__all__ = ["PrivateInputAudit", "audit_private_group_inputs"]
