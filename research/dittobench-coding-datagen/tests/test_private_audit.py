from __future__ import annotations

from pathlib import Path

import pytest

from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_audit import audit_private_group_inputs
from dittobench_coding_datagen.private_group import (
    PrivateGroupArm,
    build_private_group_manifest,
)


def _manifest():
    return build_private_group_manifest(
        opaque_group_id="group-1",
        opaque_repository_stratum_id="stratum-1",
        repository_epoch="epoch-1",
        snapshot_manifest_sha256="a" * 64,
        visible_issue_sha256="b" * 64,
        runtime_policy_sha256="c" * 64,
        hidden_grader_sha256="d" * 64,
        resource_profile_sha256="e" * 64,
        arms=tuple(
            PrivateGroupArm(condition, fill * 64, 4096, "medium")
            for condition, fill in (
                ("v0_none", "a"),
                ("v1_relevant", "b"),
                ("v2_irrelevant", "c"),
                ("v3_stale_conflict", "d"),
                ("v4_current_override", "e"),
            )
        ),  # type: ignore[arg-type]
    )


def test_private_audit_binds_tree_and_overlap_identities(tmp_path: Path) -> None:
    visible = tmp_path / "visible"
    grader = tmp_path / "grader"
    visible.mkdir()
    grader.mkdir()
    (visible / "app.txt").write_text("safe", encoding="utf-8")
    (grader / "test.txt").write_text("safe", encoding="utf-8")
    result = audit_private_group_inputs(
        manifest=_manifest(),
        visible_snapshot=visible,
        hidden_grader=grader,
        overlap_review_sha256="f" * 64,
    )

    assert result.passed is True
    assert len(result.canonical_bytes()) > 0


def test_private_audit_rejects_credential_like_material(tmp_path: Path) -> None:
    visible = tmp_path / "visible"
    grader = tmp_path / "grader"
    visible.mkdir()
    grader.mkdir()
    (visible / "app.txt").write_text("safe", encoding="utf-8")
    (grader / "secret.txt").write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    with pytest.raises(CorpusError, match="credential-like"):
        audit_private_group_inputs(
            manifest=_manifest(),
            visible_snapshot=visible,
            hidden_grader=grader,
            overlap_review_sha256="f" * 64,
        )
