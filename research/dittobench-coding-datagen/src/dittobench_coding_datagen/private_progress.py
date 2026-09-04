"""Redacted progress verification for an externally authored private corpus."""

from __future__ import annotations

import json
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import canonical_json_bytes, sha256_hex
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_authoring import load_private_group_manifest
from dittobench_coding_datagen.private_calibration import load_private_calibration

PRIVATE_CORPUS_PROGRESS_SCHEMA = "dittobench-coding-private-corpus-progress-v2"
_MAX_GROUPS = 50
_MAX_STRATA = 10
_GROUPS_PER_STRATUM = 5
_MAX_AUTHORITY_BYTES = 1 << 20


@dataclass(frozen=True)
class PrivateCorpusProgress:
    schema: str
    group_count: int
    repository_stratum_count: int
    complete_repository_stratum_count: int
    remaining_group_count: int
    ready_for_release: bool
    corpus_progress_sha256: str
    weight_eligible: bool = False

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "complete_repository_stratum_count": (
                    self.complete_repository_stratum_count
                ),
                "corpus_progress_sha256": self.corpus_progress_sha256,
                "group_count": self.group_count,
                "ready_for_release": self.ready_for_release,
                "remaining_group_count": self.remaining_group_count,
                "repository_stratum_count": self.repository_stratum_count,
                "schema": self.schema,
                "weight_eligible": self.weight_eligible,
            }
        )


def audit_private_corpus_progress(groups_dir: Path) -> PrivateCorpusProgress:
    """Verify all current group authorities while returning only redacted counts."""

    if (
        groups_dir.is_symlink()
        or not groups_dir.is_dir()
        or stat.S_IMODE(groups_dir.stat().st_mode) & 0o077
    ):
        raise CorpusError("private corpus groups directory is unsafe")
    directories = sorted(groups_dir.iterdir(), key=lambda path: path.name)
    if not directories or len(directories) > _MAX_GROUPS:
        raise CorpusError("private corpus group count is invalid")

    strata: Counter[str] = Counter()
    identities: list[dict[str, str]] = []
    for directory in directories:
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or stat.S_IMODE(directory.stat().st_mode) & 0o077
        ):
            raise CorpusError("private corpus group directory is unsafe")
        authority = directory / "authority"
        if (
            authority.is_symlink()
            or not authority.is_dir()
            or stat.S_IMODE(authority.stat().st_mode) & 0o077
        ):
            raise CorpusError("private corpus group authority is unavailable")
        manifest_path = authority / "group-manifest.json"
        audit_path = authority / "group-audit.json"
        calibration_path = authority / "group-calibration.json"
        if not all(
            path.is_file()
            and not path.is_symlink()
            and not (stat.S_IMODE(path.stat().st_mode) & 0o077)
            for path in (
                manifest_path,
                audit_path,
                calibration_path,
            )
        ):
            raise CorpusError("private corpus group authority is incomplete")

        manifest = load_private_group_manifest(manifest_path)
        manifest_sha256 = manifest.manifest_sha256()
        if directory.name != manifest.opaque_group_id:
            raise CorpusError("private corpus group identity drifted")
        audit, audit_body = _load_audit(audit_path, manifest_sha256=manifest_sha256)
        calibration, calibration_body = load_private_calibration(
            calibration_path,
            group_manifest_sha256=manifest_sha256,
        )
        stratum = manifest.opaque_repository_stratum_id
        strata[stratum] += 1
        identities.append(
            {
                "audit_sha256": sha256_hex(audit_body),
                "calibration_sha256": sha256_hex(calibration_body),
                "group_manifest_sha256": manifest_sha256,
                "hidden_grader_tree_sha256": str(audit["hidden_grader_tree_sha256"]),
                "memory_bundle_set_sha256": str(audit["memory_bundle_set_sha256"]),
                "opaque_group_id": manifest.opaque_group_id,
                "opaque_repository_stratum_id": stratum,
                "overlap_review_sha256": str(audit["overlap_review_sha256"]),
                "visible_snapshot_tree_sha256": str(
                    audit["visible_snapshot_tree_sha256"]
                ),
            }
        )

    if len(strata) > _MAX_STRATA or any(
        count > _GROUPS_PER_STRATUM for count in strata.values()
    ):
        raise CorpusError("private corpus repository strata are invalid")
    _require_unique_authorities(identities)
    complete_strata = sum(count == _GROUPS_PER_STRATUM for count in strata.values())
    ready = (
        len(identities) == _MAX_GROUPS
        and len(strata) == _MAX_STRATA
        and complete_strata == _MAX_STRATA
    )
    return PrivateCorpusProgress(
        schema=PRIVATE_CORPUS_PROGRESS_SCHEMA,
        group_count=len(identities),
        repository_stratum_count=len(strata),
        complete_repository_stratum_count=complete_strata,
        remaining_group_count=_MAX_GROUPS - len(identities),
        ready_for_release=ready,
        corpus_progress_sha256=sha256_hex(canonical_json_bytes(identities)),
    )


def _load_audit(path: Path, *, manifest_sha256: str) -> tuple[dict[str, Any], bytes]:
    if path.stat().st_size < 1 or path.stat().st_size > _MAX_AUTHORITY_BYTES:
        raise CorpusError("private corpus group audit is invalid")
    try:
        body = path.read_bytes()
        raw: Any = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("private corpus group audit is invalid") from error
    expected = {
        "group_manifest_sha256",
        "hidden_grader_tree_sha256",
        "memory_bundle_set_sha256",
        "overlap_review_sha256",
        "passed",
        "schema",
        "visible_snapshot_tree_sha256",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != expected
        or raw["schema"] != "dittobench-coding-private-input-audit-v2"
        or raw["passed"] is not True
        or raw["group_manifest_sha256"] != manifest_sha256
        or canonical_json_bytes(raw) != body
        or any(
            not _sha256(raw[field]) for field in expected if field.endswith("sha256")
        )
    ):
        raise CorpusError("private corpus group audit is invalid")
    return raw, body


def _require_unique_authorities(identities: list[dict[str, str]]) -> None:
    fields = (
        "audit_sha256",
        "calibration_sha256",
        "group_manifest_sha256",
        "hidden_grader_tree_sha256",
        "memory_bundle_set_sha256",
        "opaque_group_id",
        "overlap_review_sha256",
        "visible_snapshot_tree_sha256",
    )
    if any(
        len({item[field] for item in identities}) != len(identities) for field in fields
    ):
        raise CorpusError("private corpus contains duplicated group authority")


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "PRIVATE_CORPUS_PROGRESS_SCHEMA",
    "PrivateCorpusProgress",
    "audit_private_corpus_progress",
]
