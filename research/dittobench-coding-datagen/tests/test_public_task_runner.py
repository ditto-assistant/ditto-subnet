from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_public_staging import _intake, _write_task

from dittobench_coding_datagen import public_task_runner as runner
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_pack_v2 import compile_public_v2_pack


def _pack_and_workspace(tmp_path: Path) -> tuple[Path, Path]:
    intake = _intake(tmp_path / "staging")
    pack = tmp_path / "pack"
    compile_public_v2_pack(
        staging_root=intake.parent,
        intake_path=intake,
        output=pack,
    )
    workspace = tmp_path / "workspace"
    shutil.copytree(
        pack / "capsules" / "PUBLIC-V2-00" / "visible" / "workspace",
        workspace,
    )
    return pack, workspace


def test_public_task_runner_reuses_shared_command_and_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, workspace = _pack_and_workspace(tmp_path)
    (workspace / "app.txt").write_text("candidate repair", encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(
        runner, "_verified_local_image", lambda **_kwargs: "sha256:image"
    )
    monkeypatch.setattr(runner, "_start_container", lambda **_kwargs: "container")
    monkeypatch.setattr(
        runner, "_stop_container", lambda container: calls.append(container)
    )

    def run_command(*, container: str, command: dict[str, object]) -> tuple[int, str]:
        assert container == "container"
        calls.append(str(command["id"]))
        return 0, "test-fail-to-pass PASSED\ntest-pass-to-pass PASSED\n"

    monkeypatch.setattr(runner, "_run_command", run_command)
    result = runner.run_public_v2_task(
        pack=pack,
        task_id="PUBLIC-V2-00",
        workspace=workspace,
        image="example/image@sha256:authority",
    )

    assert result.resolved is True
    assert result.protocol_valid is True
    assert result.patch_valid is True
    assert result.terminal_domain == "resolved"
    assert calls == ["grader-fail-to-pass", "container"]


def test_public_controls_runner_uses_same_execution_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = "PUBLIC-V2-CONTROL"
    _write_task(tmp_path, task_id, "v1_relevant")
    task_root = tmp_path / "tasks" / task_id
    workspace = tmp_path / "workspace"
    shutil.copytree(task_root / "snapshot" / "workspace", workspace)
    monkeypatch.setattr(
        runner, "_verified_local_image", lambda **_kwargs: "sha256:image"
    )
    monkeypatch.setattr(runner, "_start_container", lambda **_kwargs: "container")
    monkeypatch.setattr(runner, "_stop_container", lambda _container: None)
    monkeypatch.setattr(
        runner,
        "_run_command",
        lambda **_kwargs: (
            0,
            "test-fail-to-pass PASSED\ntest-pass-to-pass PASSED\n",
        ),
    )

    result = runner.run_public_v2_controls(
        task_root=task_root,
        task_id=task_id,
        condition="v1_relevant",
        workspace=workspace,
        image="example/image@sha256:authority",
    )

    assert result.task_id == task_id
    assert result.resolved is True


def test_public_task_runner_rejects_unapproved_workspace_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, workspace = _pack_and_workspace(tmp_path)
    (workspace / "unapproved.txt").write_text("not allowed", encoding="utf-8")
    monkeypatch.setattr(
        runner, "_verified_local_image", lambda **_kwargs: "sha256:image"
    )

    result = runner.run_public_v2_task(
        pack=pack,
        task_id="PUBLIC-V2-00",
        workspace=workspace,
        image="example/image@sha256:authority",
    )

    assert result.resolved is False
    assert result.protocol_valid is True
    assert result.patch_valid is False
    assert result.terminal_domain == "harness_failure"


def test_public_task_runner_requires_declared_test_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, workspace = _pack_and_workspace(tmp_path)
    monkeypatch.setattr(
        runner, "_verified_local_image", lambda **_kwargs: "sha256:image"
    )
    monkeypatch.setattr(runner, "_start_container", lambda **_kwargs: "container")
    monkeypatch.setattr(runner, "_stop_container", lambda _container: None)
    monkeypatch.setattr(
        runner,
        "_run_command",
        lambda **_kwargs: (0, "a different test passed\n"),
    )

    result = runner.run_public_v2_task(
        pack=pack,
        task_id="PUBLIC-V2-00",
        workspace=workspace,
        image="example/image@sha256:authority",
    )

    assert result.resolved is False
    assert result.patch_valid is True
    assert result.terminal_domain == "repair_failure"


def test_public_task_runner_accepts_hierarchical_test_output() -> None:
    output = "  useState\n    respects updates initiated from the parent PASSED\n"
    assert runner._expected_test_observed(
        "useState > respects updates initiated from the parent", output
    )
    assert not runner._expected_test_observed("useState > missing behavior", output)


def test_public_task_runner_binds_loaded_image_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = f"sha256:{'a' * 64}"
    body = (
        f'[{{"Id":"sha256:local-image","RepoDigests":["example/image@{expected}"]}}]'
    ).encode()
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, body, b""),
    )

    assert (
        runner._verified_local_image(image="example/image:local", expected=expected)
        == "sha256:local-image"
    )

    body = f"[{json.dumps({'Id': expected, 'RepoDigests': None})}]".encode()
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, body, b""),
    )
    assert runner._verified_local_image(image=expected, expected=expected) == expected

    with pytest.raises(CorpusError, match="does not match"):
        runner._verified_local_image(
            image="example/image:local", expected=f"sha256:{'b' * 64}"
        )
