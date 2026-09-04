from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_public_staging import _write_task

from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.cli import main
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_controls import validate_public_task_controls


def _task(root: Path, condition: str = "v1_relevant") -> tuple[Path, Path]:
    task_id = "PUBLIC-V2-CONTROLS"
    _write_task(root, task_id, condition)
    task = root / "tasks" / task_id
    return task, task / "snapshot" / "workspace"


def _rewrite(path: Path, mutate: object) -> None:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    mutate(value)
    path.write_bytes(canonical_json_bytes(value))


def test_public_controls_bind_valid_cross_file_authority(tmp_path: Path) -> None:
    task, workspace = _task(tmp_path)
    authority = validate_public_task_controls(
        task_root=task,
        task_id="PUBLIC-V2-CONTROLS",
        condition="v1_relevant",
        workspace=workspace,
    )

    assert len(authority.issue_sha256) == 64
    assert len(authority.memory_sha256) == 64
    assert len(authority.runtime_policy_sha256) == 64
    assert len(authority.visible_grader_sha256) == 64


def test_public_controls_cli_emits_canonical_authority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task, _ = _task(tmp_path)
    assert (
        main(
            [
                "validate-public-controls",
                "--task-root",
                str(task),
                "--task-id",
                "PUBLIC-V2-CONTROLS",
                "--condition",
                "v1_relevant",
            ]
        )
        == 0
    )
    value = json.loads(capsys.readouterr().out)
    assert set(value) == {
        "issue_sha256",
        "memory_sha256",
        "runtime_policy_sha256",
        "visible_grader_sha256",
    }


def test_public_controls_reject_embedding_and_condition_drift(tmp_path: Path) -> None:
    task, workspace = _task(tmp_path)
    memory = task / "memory.json"

    def add_embedding(value: dict[str, object]) -> None:
        memories = value["memories"]
        assert isinstance(memories, list) and isinstance(memories[0], dict)
        memories[0]["embedding"] = [0.0]

    _rewrite(memory, add_embedding)
    with pytest.raises(CorpusError, match="memory record fields"):
        validate_public_task_controls(
            task_root=task,
            task_id="PUBLIC-V2-CONTROLS",
            condition="v1_relevant",
            workspace=workspace,
        )

    task, workspace = _task(tmp_path / "condition", condition="v0_none")
    with pytest.raises(CorpusError, match="memory authority"):
        validate_public_task_controls(
            task_root=task,
            task_id="PUBLIC-V2-CONTROLS",
            condition="v2_irrelevant",
            workspace=workspace,
        )


def test_public_controls_reject_unsafe_runtime_commands(tmp_path: Path) -> None:
    task, workspace = _task(tmp_path)
    policy = task / "runtime-policy.json"

    def use_git(value: dict[str, object]) -> None:
        commands = value["test_commands"]
        assert isinstance(commands, list) and isinstance(commands[0], dict)
        commands[0]["argv"] = ["git", "status"]

    _rewrite(policy, use_git)
    with pytest.raises(CorpusError, match="executable is forbidden"):
        validate_public_task_controls(
            task_root=task,
            task_id="PUBLIC-V2-CONTROLS",
            condition="v1_relevant",
            workspace=workspace,
        )


def test_public_controls_allow_pinned_offline_cargo_lock_refresh(
    tmp_path: Path,
) -> None:
    task, workspace = _task(tmp_path)
    policy = task / "runtime-policy.json"
    grader = task / "grader" / "manifest.json"

    def use_offline_cargo(value: dict[str, object]) -> None:
        commands = value["test_commands"]
        assert isinstance(commands, list) and isinstance(commands[0], dict)
        commands[0]["argv"] = ["cargo", "test", "--offline"]

    def use_offline_cargo_grader(value: dict[str, object]) -> None:
        groups = value["test_groups"]
        assert isinstance(groups, list)
        for group in groups:
            assert isinstance(group, dict) and isinstance(group["command"], dict)
            group["command"]["argv"] = ["cargo", "test", "--offline"]

    _rewrite(policy, use_offline_cargo)
    _rewrite(grader, use_offline_cargo_grader)
    validate_public_task_controls(
        task_root=task,
        task_id="PUBLIC-V2-CONTROLS",
        condition="v1_relevant",
        workspace=workspace,
    )

    def remove_offline(value: dict[str, object]) -> None:
        commands = value["test_commands"]
        assert isinstance(commands, list) and isinstance(commands[0], dict)
        commands[0]["argv"] = ["cargo", "test"]

    _rewrite(policy, remove_offline)
    with pytest.raises(CorpusError, match="Cargo command must be offline"):
        validate_public_task_controls(
            task_root=task,
            task_id="PUBLIC-V2-CONTROLS",
            condition="v1_relevant",
            workspace=workspace,
        )

    task, workspace = _task(tmp_path / "npx")
    policy = task / "runtime-policy.json"

    def use_mutable_npx(value: dict[str, object]) -> None:
        commands = value["test_commands"]
        assert isinstance(commands, list) and isinstance(commands[0], dict)
        commands[0]["argv"] = ["npx", "karma"]

    _rewrite(policy, use_mutable_npx)
    with pytest.raises(CorpusError, match="disable installation"):
        validate_public_task_controls(
            task_root=task,
            task_id="PUBLIC-V2-CONTROLS",
            condition="v1_relevant",
            workspace=workspace,
        )


def test_public_controls_reject_grader_authority_and_solution_paths(
    tmp_path: Path,
) -> None:
    task, workspace = _task(tmp_path)
    manifest = task / "grader" / "manifest.json"

    def change_image(value: dict[str, object]) -> None:
        value["environment_image_digest"] = f"sha256:{'b' * 64}"

    _rewrite(manifest, change_image)
    with pytest.raises(CorpusError, match="grader authority"):
        validate_public_task_controls(
            task_root=task,
            task_id="PUBLIC-V2-CONTROLS",
            condition="v1_relevant",
            workspace=workspace,
        )

    task, workspace = _task(tmp_path / "solution")
    manifest = task / "grader" / "manifest.json"

    def name_solution(value: dict[str, object]) -> None:
        files = value["files"]
        assert isinstance(files, list) and isinstance(files[0], dict)
        files[0]["source_path"] = "files/solution.py"

    _rewrite(manifest, name_solution)
    with pytest.raises(CorpusError, match="source path is forbidden"):
        validate_public_task_controls(
            task_root=task,
            task_id="PUBLIC-V2-CONTROLS",
            condition="v1_relevant",
            workspace=workspace,
        )

    task, workspace = _task(tmp_path / "destination")
    manifest = task / "grader" / "manifest.json"

    def target_source(value: dict[str, object]) -> None:
        files = value["files"]
        assert isinstance(files, list) and isinstance(files[0], dict)
        files[0]["destination_path"] = "src/runtime.py"

    _rewrite(manifest, target_source)
    with pytest.raises(CorpusError, match="destination is not test-scoped"):
        validate_public_task_controls(
            task_root=task,
            task_id="PUBLIC-V2-CONTROLS",
            condition="v1_relevant",
            workspace=workspace,
        )
