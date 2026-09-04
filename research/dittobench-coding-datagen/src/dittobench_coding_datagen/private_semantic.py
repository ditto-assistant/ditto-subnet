"""Canonical independent semantic review authorities for private groups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import canonical_json_bytes, safe_opaque_id
from dittobench_coding_datagen.model import CorpusError

PRIVATE_SEMANTIC_REVIEW_SCHEMA = "dittobench-coding-private-semantic-review-v2"
_MAX_REVIEW_BYTES = 1 << 20


def load_private_semantic_review(
    path: Path, *, group_manifest_sha256: str
) -> tuple[dict[str, Any], bytes]:
    """Load a passing external semantic-family and nearest-neighbor review."""

    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size < 1
        or path.stat().st_size > _MAX_REVIEW_BYTES
    ):
        raise CorpusError("private semantic review authority is invalid")
    try:
        body = path.read_bytes()
        raw: Any = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("private semantic review authority is invalid") from error
    expected = {
        "group_manifest_sha256",
        "nearest_group_manifest_sha256",
        "nearest_similarity_micros",
        "passed",
        "reviewer_authority_sha256",
        "schema",
        "semantic_family_id",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != expected
        or canonical_json_bytes(raw) != body
        or raw["schema"] != PRIVATE_SEMANTIC_REVIEW_SCHEMA
        or raw["passed"] is not True
        or raw["group_manifest_sha256"] != group_manifest_sha256
        or not _sha256(raw["reviewer_authority_sha256"])
        or type(raw["nearest_similarity_micros"]) is not int
        or not 0 <= raw["nearest_similarity_micros"] <= 1_000_000
    ):
        raise CorpusError("private semantic review authority is invalid")
    try:
        raw["semantic_family_id"] = safe_opaque_id(raw["semantic_family_id"])
    except (TypeError, ValueError) as error:
        raise CorpusError("private semantic review authority is invalid") from error
    nearest = raw["nearest_group_manifest_sha256"]
    if nearest is None:
        if raw["nearest_similarity_micros"] != 0:
            raise CorpusError("private semantic review authority is invalid")
    elif not _sha256(nearest) or nearest == group_manifest_sha256:
        raise CorpusError("private semantic review authority is invalid")
    return raw, body


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = ["PRIVATE_SEMANTIC_REVIEW_SCHEMA", "load_private_semantic_review"]
