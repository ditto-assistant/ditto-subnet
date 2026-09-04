"""Run one public-v2 practice task in a disposable local container."""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_opaque_id,
    safe_relative_path,
    sha256_hex,
)
from dittobench_coding_datagen.local_result import LocalPracticeTaskResult
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_controls import (
    PUBLIC_CONDITIONS,
    validate_public_task_controls,
)
from dittobench_coding_datagen.public_pack_v2 import validate_public_v2_pack

_MAX_COMMAND_OUTPUT_BYTES = 4 << 20


def run_public_v2_task(
    *, pack: Path, task_id: str, workspace: Path, image: str
) -> LocalPracticeTaskResult:
    """Grade one local workspace; the result is permanently non-authoritative."""

    validate_public_v2_pack(pack)
    task_id = safe_opaque_id(task_id)
    entry = _task_entry(pack, task_id)
    policy = _load_json(pack / "tasks" / task_id / "runtime-policy.json")
    grader_root = pack / "capsules" / task_id / "grader"
    grader = _load_json(grader_root / "manifest.json")
    return _run_task_authority(
        entry=entry,
        visible=pack / "capsules" / task_id / "visible" / "workspace",
        grader_root=grader_root,
        candidate=workspace,
        image=image,
        policy=policy,
        grader=grader,
    )


def run_public_v2_controls(
    *,
    task_root: Path,
    task_id: str,
    condition: str,
    workspace: Path,
    image: str,
) -> LocalPracticeTaskResult:
    """Grade one external curator control set through the public runner core."""

    task_id = safe_opaque_id(task_id)
    if condition not in PUBLIC_CONDITIONS:
        raise CorpusError("public v2 task condition is invalid")
    visible = task_root / "snapshot" / "workspace"
    validate_public_task_controls(
        task_root=task_root,
        task_id=task_id,
        condition=condition,
        workspace=visible,
    )
    policy = _load_json(task_root / "runtime-policy.json")
    grader_root = task_root / "grader"
    grader = _load_json(grader_root / "manifest.json")
    return _run_task_authority(
        entry={"condition": condition, "task_id": task_id},
        visible=visible,
        grader_root=grader_root,
        candidate=workspace,
        image=image,
        policy=policy,
        grader=grader,
    )


def _run_task_authority(
    *,
    entry: dict[str, Any],
    visible: Path,
    grader_root: Path,
    candidate: Path,
    image: str,
    policy: dict[str, Any],
    grader: dict[str, Any],
) -> LocalPracticeTaskResult:
    image_id = _verified_local_image(
        image=image, expected=str(policy["environment_image_digest"])
    )
    patch_valid, grading = _prepare_grading_workspace(
        visible=visible,
        grader_root=grader_root,
        candidate=candidate,
        policy=policy,
        grader=grader,
    )
    if not patch_valid or grading is None:
        return _task_result(entry, resolved=False, patch_valid=False)
    with grading:
        container = _start_container(
            image=image_id,
            workspace=Path(grading.name),
            policy=policy,
        )
        try:
            build = grader.get("build_command")
            if build is not None:
                if not isinstance(build, dict):
                    raise CorpusError("public grader build command is invalid")
                exit_code, _ = _run_command(container=container, command=build)
                if exit_code != 0:
                    return _task_result(entry, resolved=False, patch_valid=True)
            groups = grader.get("test_groups")
            if not isinstance(groups, list) or len(groups) != 2:
                raise CorpusError("public grader test groups are invalid")
            observed: dict[str, tuple[int, str]] = {}
            outcomes: list[bool] = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(
                    group.get("command"), dict
                ):
                    raise CorpusError("public grader test group is invalid")
                command = group["command"]
                key = _command_key(command)
                result = observed.get(key)
                if result is None:
                    result = _run_command(container=container, command=command)
                    observed[key] = result
                exit_code, output = result
                if exit_code == 124:
                    return _task_result(entry, resolved=False, patch_valid=True)
                expected = group.get("expected_tests")
                if not isinstance(expected, list) or not all(
                    isinstance(item, str) and item for item in expected
                ):
                    raise CorpusError("public grader expected tests are invalid")
                outcomes.append(
                    exit_code == 0
                    and all(_expected_test_observed(item, output) for item in expected)
                )
            return _task_result(
                entry,
                resolved=all(outcomes),
                patch_valid=True,
            )
        finally:
            _stop_container(container)


def _task_result(
    entry: dict[str, Any], *, resolved: bool, patch_valid: bool
) -> LocalPracticeTaskResult:
    return LocalPracticeTaskResult(
        task_id=str(entry["task_id"]),
        condition=str(entry["condition"]),  # type: ignore[arg-type]
        resolved=resolved,
        protocol_valid=True,
        patch_valid=patch_valid,
        terminal_domain=(
            "resolved"
            if resolved
            else "repair_failure"
            if patch_valid
            else "harness_failure"
        ),
    )


def _task_entry(pack: Path, task_id: str) -> dict[str, Any]:
    try:
        for line in (
            (pack / "tasks" / "index.jsonl").read_bytes().splitlines(keepends=True)
        ):
            raw: Any = json.loads(line)
            if canonical_json_bytes(raw) != line:
                raise CorpusError("public v2 task index is invalid")
            if isinstance(raw, dict) and raw.get("task_id") == task_id:
                return raw
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("public v2 task index is invalid") from error
    raise CorpusError(f"unknown public v2 task: {task_id}")


def _load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1 << 20:
        raise CorpusError("public v2 task control is unsafe")
    try:
        body = path.read_bytes()
        raw: Any = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("public v2 task control is invalid") from error
    if not isinstance(raw, dict) or canonical_json_bytes(raw) != body:
        raise CorpusError("public v2 task control is invalid")
    return raw


def _verified_local_image(*, image: str, expected: str) -> str:
    digest = expected.removeprefix("sha256:")
    if (
        not image
        or image.startswith("-")
        or any(character.isspace() for character in image)
        or "\x00" in image
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise CorpusError("public v2 runtime image authority is invalid")
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CorpusError("public v2 runtime image inspection failed") from error
    try:
        inspected: Any = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("public v2 runtime image inspection failed") from error
    if (
        completed.returncode != 0
        or not isinstance(inspected, list)
        or len(inspected) != 1
        or not isinstance(inspected[0], dict)
    ):
        raise CorpusError("public v2 runtime image is unavailable")
    item = inspected[0]
    image_id = item.get("Id")
    repo_digests = item.get("RepoDigests") or []
    if (
        not isinstance(image_id, str)
        or not isinstance(repo_digests, list)
        or not all(isinstance(value, str) for value in repo_digests)
        or (
            image_id != expected
            and not any(value.endswith(f"@{expected}") for value in repo_digests)
        )
    ):
        raise CorpusError("local image does not match public task runtime digest")
    return image_id


def _workspace_files(root: Path) -> dict[str, Path]:
    if root.is_symlink() or not root.is_dir():
        raise CorpusError("public practice workspace is unsafe")
    files: dict[str, Path] = {}
    for path in root.rglob("*"):
        relative = safe_relative_path(path.relative_to(root).as_posix())
        if path.is_symlink():
            raise CorpusError(
                f"public practice workspace contains a symlink: {relative}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise CorpusError(
                f"public practice workspace contains a special file: {relative}"
            )
        files[relative] = path
    return files


def _prepare_grading_workspace(
    *,
    visible: Path,
    grader_root: Path,
    candidate: Path,
    policy: dict[str, Any],
    grader: dict[str, Any],
) -> tuple[bool, tempfile.TemporaryDirectory[str] | None]:
    base = _workspace_files(visible)
    supplied = _workspace_files(candidate)
    editable = _path_set(policy.get("editable_paths"))
    creatable = _path_set(policy.get("creatable_paths"))
    deletable = _path_set(policy.get("deletable_paths"))
    allowed = set(base) | creatable
    if (
        not set(supplied).issubset(allowed)
        or set(base) - set(supplied) - deletable
        or any(
            relative not in editable
            and sha256_hex(path.read_bytes()) != sha256_hex(base[relative].read_bytes())
            for relative, path in supplied.items()
            if relative in base
        )
    ):
        return False, None
    changed_bytes = sum(
        path.stat().st_size
        for relative, path in supplied.items()
        if relative not in base
        or sha256_hex(path.read_bytes()) != sha256_hex(base[relative].read_bytes())
    ) + sum(base[relative].stat().st_size for relative in set(base) - set(supplied))
    limits = policy.get("limits")
    workspace_bytes = sum(path.stat().st_size for path in supplied.values())
    if (
        not isinstance(limits, dict)
        or changed_bytes > int(limits["max_patch_bytes"])
        or workspace_bytes > int(limits["max_workspace_bytes"])
    ):
        return False, None
    temporary = tempfile.TemporaryDirectory(prefix="dittobench-public-v2-grade-")
    try:
        grading = Path(temporary.name)
        shutil.copytree(visible, grading, dirs_exist_ok=True)
        for relative in deletable - set(supplied):
            target = grading / relative
            if target.is_file():
                target.unlink()
        for relative in sorted(editable | creatable):
            source = supplied.get(relative)
            if source is None:
                continue
            target = grading / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o755 if source.stat().st_mode & 0o100 else 0o644)
        _inject_grader(root=grader_root, workspace=grading, manifest=grader)
    except Exception:
        temporary.cleanup()
        raise
    return True, temporary


def _path_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        raise CorpusError("public v2 runtime path policy is invalid")
    paths = {safe_relative_path(item) for item in value}
    if len(paths) != len(value):
        raise CorpusError("public v2 runtime path policy is invalid")
    return paths


def _inject_grader(*, root: Path, workspace: Path, manifest: dict[str, Any]) -> None:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise CorpusError("public grader files are invalid")
    for item in files:
        if not isinstance(item, dict):
            raise CorpusError("public grader file is invalid")
        source_path = item.get("source_path")
        destination_path = item.get("destination_path")
        if not isinstance(source_path, str) or not isinstance(destination_path, str):
            raise CorpusError("public grader file path is invalid")
        source = root / safe_relative_path(source_path)
        destination = workspace / safe_relative_path(destination_path)
        body = source.read_bytes()
        if len(body) != item.get("size_bytes") or sha256_hex(body) != item.get(
            "sha256"
        ):
            raise CorpusError("public grader file identity drifted")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        destination.chmod(int(item["mode"]))


def _start_container(*, image: str, workspace: Path, policy: dict[str, Any]) -> str:
    limits = policy.get("limits")
    if not isinstance(limits, dict):
        raise CorpusError("public v2 runtime limits are invalid")
    name = f"dittobench-public-v2-{uuid.uuid4().hex}"
    command = [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        str(limits["pids_limit"]),
        "--memory",
        str(limits["memory_limit_bytes"]),
        "--cpus",
        str(int(limits["cpu_quota_millis"]) / 1000),
        "--mount",
        f"type=bind,src={workspace},dst=/input,readonly",
        image,
        "/bin/sh",
        "-c",
        (
            "mkdir -m 0755 /tmp/testbed && cp -R /input/. /tmp/testbed/ && "
            "if [ -d /testbed/node_modules ] && "
            "[ ! -e /tmp/testbed/node_modules ]; then "
            "ln -s /testbed/node_modules /tmp/testbed/node_modules; fi && "
            "touch /tmp/dittobench-ready && exec tail -f /dev/null"
        ),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _stop_container(name)
        raise CorpusError("public v2 practice container failed to start") from error
    if completed.returncode != 0:
        _stop_container(name)
        raise CorpusError("public v2 practice container failed to start")
    for _ in range(100):
        try:
            ready = subprocess.run(
                ["docker", "exec", name, "test", "-f", "/tmp/dittobench-ready"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            _stop_container(name)
            raise CorpusError(
                "public v2 practice container readiness failed"
            ) from error
        if ready.returncode == 0:
            return name
        time.sleep(0.05)
    _stop_container(name)
    raise CorpusError("public v2 practice container did not become ready")


def _run_command(*, container: str, command: dict[str, Any]) -> tuple[int, str]:
    argv = command.get("argv")
    environment = command.get("environment")
    timeout_milliseconds = command.get("timeout_milliseconds")
    if (
        not isinstance(argv, list)
        or not all(isinstance(item, str) and item for item in argv)
        or not isinstance(environment, dict)
        or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in environment.items()
        )
        or type(timeout_milliseconds) is not int
    ):
        raise CorpusError("public v2 practice command is invalid")
    invocation = ["docker", "exec", "--workdir", "/tmp/testbed"]
    for name, value in sorted(environment.items()):
        invocation.extend(["--env", f"{name}={value}"])
    invocation.append(container)
    invocation.extend(argv)
    try:
        completed = subprocess.run(
            invocation,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=(timeout_milliseconds / 1000) + 30,
        )
        output = completed.stdout
        returncode = completed.returncode
    except subprocess.TimeoutExpired as error:
        output = error.stdout or b""
        returncode = 124
    except OSError as error:
        raise CorpusError("public v2 practice command could not execute") from error
    if len(output) > _MAX_COMMAND_OUTPUT_BYTES:
        raise CorpusError("public v2 practice command output exceeded its bound")
    try:
        return returncode, output.decode("utf-8", errors="replace")
    except AttributeError as error:
        raise CorpusError("public v2 practice command output is invalid") from error


def _stop_container(container: str) -> None:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["docker", "stop", "--time", "1", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )


def _command_key(command: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {key: value for key, value in command.items() if key != "id"}
        )
    ).hexdigest()


def _expected_test_observed(identifier: str, output: str) -> bool:
    parts = [part.strip() for part in identifier.replace("::", " > ").split(" > ")]
    return all(part and part in output for part in parts)


__all__ = ["run_public_v2_controls", "run_public_v2_task"]
