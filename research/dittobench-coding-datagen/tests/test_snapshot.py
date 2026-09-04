from __future__ import annotations

import json
from pathlib import Path

import pytest

from dittobench_coding_datagen.canonical import normalized_tree_sha256
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.snapshot import (
    export_sanitized_snapshot,
    validate_sanitized_snapshot,
)


def _source(root: Path) -> Path:
    source = root / "source"
    (source / ".git").mkdir(parents=True)
    (source / ".git" / "HEAD").write_text("ref: hidden\n", encoding="utf-8")
    (source / ".github").mkdir()
    (source / ".github" / "workflow.yml").write_text("hidden\n", encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (source / "run.sh").chmod(0o755)
    return source


def test_snapshot_is_deterministic_git_free_and_normalized(tmp_path: Path) -> None:
    source = _source(tmp_path)
    first = export_sanitized_snapshot(source, tmp_path / "first")
    second = export_sanitized_snapshot(source, tmp_path / "second")

    assert first == second
    assert first.excluded_root_entries == (".git", ".github")
    assert not (tmp_path / "first" / "workspace" / ".git").exists()
    assert not (tmp_path / "first" / "workspace" / ".github").exists()
    assert (tmp_path / "first" / "workspace" / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (
        tmp_path / "first" / "workspace" / "src" / "main.py"
    ).stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "first" / "manifest.json").read_bytes() == (
        tmp_path / "second" / "manifest.json"
    ).read_bytes()
    manifest = json.loads((tmp_path / "first" / "manifest.json").read_bytes())
    assert manifest["schema"] == "dittobench-coding-sanitized-snapshot-v2"
    modes = {item["path"]: item["mode"] for item in manifest["files"]}
    assert modes["run.sh"] == 0o755
    assert modes["src/main.py"] == 0o644

    (tmp_path / "first" / "workspace" / "run.sh").chmod(0o644)
    assert (
        normalized_tree_sha256(tmp_path / "first" / "workspace")
        != first.snapshot_tree_sha256
    )
    with pytest.raises(CorpusError, match="does not match workspace"):
        validate_sanitized_snapshot(tmp_path / "first")
    assert export_sanitized_snapshot(source, tmp_path / "third") == first
    assert validate_sanitized_snapshot(tmp_path / "third") == first


def test_snapshot_rejects_nested_git_and_symlinks(tmp_path: Path) -> None:
    source = _source(tmp_path)
    (source / "src" / ".git").mkdir()
    with pytest.raises(CorpusError, match="forbidden control path"):
        export_sanitized_snapshot(source, tmp_path / "nested")

    nested = source / "src" / ".git"
    nested.rmdir()
    (source / "link").symlink_to(source / "src" / "main.py")
    with pytest.raises(CorpusError, match="symlink"):
        export_sanitized_snapshot(source, tmp_path / "symlink")


def test_snapshot_omits_nested_caches_and_rejects_env_files(tmp_path: Path) -> None:
    source = _source(tmp_path)
    (source / "src" / "__pycache__").mkdir()
    (source / "src" / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"\0")
    (source / "web" / "node_modules" / "left-pad").mkdir(parents=True)
    (source / "web" / "node_modules" / "left-pad" / "index.js").write_text(
        "module.exports=1\n", encoding="utf-8"
    )
    snapshot = export_sanitized_snapshot(source, tmp_path / "caches")
    workspace = tmp_path / "caches" / "workspace"
    assert snapshot.excluded_root_entries == (".git", ".github")
    assert not (workspace / "src" / "__pycache__").exists()
    assert not (workspace / "web" / "node_modules").exists()
    assert (workspace / "src" / "main.py").is_file()

    (source / ".env").write_text("OPENROUTER_API_KEY=secret\n", encoding="utf-8")
    with pytest.raises(CorpusError, match="credential file"):
        export_sanitized_snapshot(source, tmp_path / "env")
