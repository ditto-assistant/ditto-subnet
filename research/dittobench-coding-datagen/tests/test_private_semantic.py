from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_semantic import load_private_semantic_review


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _review(path: Path) -> Path:
    path.write_bytes(
        canonical_json_bytes(
            {
                "group_manifest_sha256": _sha("group"),
                "nearest_group_manifest_sha256": _sha("nearest"),
                "nearest_similarity_micros": 420_000,
                "passed": True,
                "reviewer_authority_sha256": _sha("reviewer"),
                "schema": "dittobench-coding-private-semantic-review-v2",
                "semantic_family_id": "semantic-family-one",
            }
        )
    )
    return path


def test_semantic_review_loads_canonical_authority(tmp_path: Path) -> None:
    review, body = load_private_semantic_review(
        _review(tmp_path / "review.json"),
        group_manifest_sha256=_sha("group"),
    )
    assert review["passed"] is True
    assert (
        hashlib.sha256(body).hexdigest()
        == hashlib.sha256(canonical_json_bytes(review)).hexdigest()
    )


def test_semantic_review_rejects_self_neighbor_or_noncanonical(
    tmp_path: Path,
) -> None:
    path = _review(tmp_path / "review.json")
    raw = json.loads(path.read_bytes())
    raw["nearest_group_manifest_sha256"] = _sha("group")
    path.write_bytes(canonical_json_bytes(raw))
    with pytest.raises(CorpusError, match="semantic review"):
        load_private_semantic_review(path, group_manifest_sha256=_sha("group"))

    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    with pytest.raises(CorpusError, match="semantic review"):
        load_private_semantic_review(path, group_manifest_sha256=_sha("group"))
