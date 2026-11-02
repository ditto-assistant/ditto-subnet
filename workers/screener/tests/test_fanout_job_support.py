"""Protect the live fan-out worker's source and key boundaries."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ditto_screener.fanout_job_support import _stage_source_review_secret


def test_stage_source_review_secret_copies_group_readable_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mounted-key"
    source.write_text("provider-key-value-that-is-long-enough\n")
    source.chmod(0o440)
    job_dir = tmp_path / "job"
    monkeypatch.setenv("SCREENER_NODE_CREDENTIAL_FILE", str(job_dir / "node.json"))

    staged = Path(_stage_source_review_secret(str(source)))

    assert staged == job_dir / "source-review-api-key.staged"
    assert staged.read_text() == source.read_text()
    assert staged.stat().st_mode & 0o777 == 0o600
    assert source.stat().st_mode & 0o777 == 0o440
    assert os.environ["SCREENER_SOURCE_REVIEW_API_KEY_FILE"] == str(staged)


def test_stage_source_review_secret_keeps_private_file(tmp_path: Path) -> None:
    source = tmp_path / "private-key"
    source.write_text("provider-key-value-that-is-long-enough\n")
    source.chmod(0o600)

    assert _stage_source_review_secret(str(source)) == str(source)
