#!/usr/bin/env python3
"""Run the Coding starter harness against the ten-task public-v2 pack."""

from __future__ import annotations

import argparse
import json
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_relative_path,
    sha256_hex,
)
from dittobench_coding_datagen.local_result import (
    LocalPracticeTaskResult,
    build_local_practice_result,
)
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.practice_runtime import (
    INITIAL_EVENT_ROOT,
    MAX_TOOL_BODY_BYTES,
    PracticeWorkspaceSession,
    ToolRequest,
    ToolResponse,
    _bounded_string,
    _changed_paths,
    _exact_arguments,
    _FileState,
    _tree_sha256,
)
from dittobench_coding_datagen.practice_server import (
    PracticeCapabilityServer,
    _loopback_base_url,
    _request_json,
)
from dittobench_coding_datagen.public_pack_v2 import validate_public_v2_pack
from dittobench_coding_datagen.public_task_runner import (
    _run_command,
    _start_container,
    _stop_container,
    _verified_local_image,
    run_public_v2_task,
)
from dittobench_coding_datagen.public_workspace import prepare_public_workspace

HOSTED_CODING_CONTRACT_VERSION = 2
MAX_PUBLIC_TOOL_CALLS = 150
MAX_VISIBLE_OUTPUT_BYTES = 28 * 1024
HARNESS_RESPONSE_GRACE_SECONDS = 60


@dataclass(frozen=True)
class PublicRuntimePolicy:
    editable_paths: tuple[str, ...]
    test_command_ids: tuple[str, ...]
    build_command_ids: tuple[str, ...]


@dataclass(frozen=True)
class PublicAgentCase:
    task_id: str
    active_user_id: str
    runtime_policy: PublicRuntimePolicy


def _load_json(path: Path) -> dict[str, Any]:
    body = path.read_bytes()
    value: Any = json.loads(body)
    if not isinstance(value, dict) or canonical_json_bytes(value) != body:
        raise CorpusError(f"public control is not canonical: {path}")
    return value


def _entries(pack: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in (pack / "tasks/index.jsonl").read_bytes().splitlines(keepends=True):
        value: Any = json.loads(line)
        if not isinstance(value, dict) or canonical_json_bytes(value) != line:
            raise CorpusError("public task index is invalid")
        entries.append(value)
    return entries


def _snapshot_public(root: Path) -> dict[str, _FileState]:
    """Snapshot public repositories without the tiny v1 fixture file cap."""

    if not root.is_dir() or root.is_symlink():
        raise CorpusError("public workspace is not a real directory")
    snapshot: dict[str, _FileState] = {}
    for path in sorted(root.rglob("*")):
        relative = safe_relative_path(path.relative_to(root).as_posix())
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise CorpusError(f"workspace contains a symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise CorpusError(f"workspace contains a special file: {relative}")
        snapshot[relative] = _FileState(path.read_bytes(), stat.S_IMODE(info.st_mode))
    return snapshot


class PublicV2WorkspaceSession(PracticeWorkspaceSession):
    """Typed authoring capability over one persistent public-v2 workspace."""

    def __init__(
        self,
        *,
        pack: Path,
        entry: dict[str, Any],
        output: Path,
        image: str,
    ) -> None:
        task_id = str(entry["task_id"])
        prepare_public_workspace(pack=pack, task_id=task_id, output=output)
        policy = _load_json(output / "runtime-policy.json")
        self._policy = policy
        self._image_id = _verified_local_image(
            image=image,
            expected=str(policy["environment_image_digest"]),
        )
        self._test_commands = {
            str(command["id"]): command for command in policy["test_commands"]
        }
        self._build_commands = {
            str(command["id"]): command for command in policy["build_commands"]
        }
        self._case_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"dittobench-public-v2:{task_id}:attempt")
        )
        self._ticket_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"dittobench-public-v2:{task_id}:evaluation")
        )
        profile = f"public-v2-{task_id.lower()}"
        runtime = PublicRuntimePolicy(
            editable_paths=tuple(policy["editable_paths"]),
            test_command_ids=tuple(self._test_commands),
            build_command_ids=tuple(self._build_commands),
        )
        # The base session reads only task_id, active_user_id and the three
        # runtime_policy tuples, which PublicAgentCase provides; public-v2 has no
        # pack-v1 PracticeAgentCase fields to populate.
        self.case = PublicAgentCase(  # type: ignore[assignment]
            task_id=task_id,
            active_user_id=profile,
            runtime_policy=runtime,
        )
        self._pack = pack.resolve()
        self._workspace = output / "workspace"
        self._base = _snapshot_public(self._workspace)
        self._base_tree_sha256 = _tree_sha256(self._base)
        self._event_root = INITIAL_EVENT_ROOT
        self._sequence = 0
        self._call_results: dict[str, tuple[str, ToolResponse]] = {}
        self._frozen = False
        self._closed = False
        self._lock = threading.RLock()

    @property
    def ticket_id(self) -> str:
        return self._ticket_id

    @property
    def case_id(self) -> str:
        return self._case_id

    def close(self) -> None:
        self._closed = True

    def _invoke_locked(self, request: ToolRequest) -> ToolResponse:
        if self._closed:
            raise CorpusError("public workspace is closed")
        if self._frozen:
            raise CorpusError("public workspace capability has been revoked")
        if request.coding_contract_version != HOSTED_CODING_CONTRACT_VERSION:
            raise CorpusError("unsupported coding contract version")
        if request.case_id != self._case_id:
            raise CorpusError("tool request case capability mismatch")
        if request.profile_capability_id != self.case.active_user_id:
            raise CorpusError("tool request profile capability mismatch")
        request_sha256 = sha256_hex(
            canonical_json_bytes(
                {
                    "arguments": request.arguments,
                    "call_id": request.call_id,
                    "case_id": request.case_id,
                    "coding_contract_version": request.coding_contract_version,
                    "name": request.name,
                    "profile_capability_id": request.profile_capability_id,
                }
            )
        )
        replay = self._call_results.get(request.call_id)
        if replay is not None:
            previous_sha256, previous_response = replay
            if request_sha256 != previous_sha256:
                raise CorpusError(
                    "tool request call_id was reused with different bytes"
                )
            return previous_response
        if len(self._call_results) >= MAX_PUBLIC_TOOL_CALLS:
            raise CorpusError("public workspace tool-call budget exhausted")
        try:
            result = self._dispatch(request.name, request.arguments)
            if len(canonical_json_bytes(result)) > MAX_TOOL_BODY_BYTES:
                raise CorpusError("tool result exceeds the public output limit")
            response = self._record(request, result=result, error=None)
        except CorpusError as error:
            response = self._record(
                request,
                result=None,
                error={"code": "invalid_tool_request", "message": str(error)},
            )
        self._call_results[request.call_id] = (request_sha256, response)
        return response

    def _assert_workspace_policy(
        self,
    ) -> tuple[dict[str, _FileState], tuple[str, ...]]:
        current = _snapshot_public(self._workspace)
        paths = _changed_paths(self._base, current)
        editable = set(self.case.runtime_policy.editable_paths)
        unauthorized = sorted(set(paths) - editable)
        if unauthorized:
            raise CorpusError(
                f"workspace changed protected or undeclared paths: {unauthorized}"
            )
        for path in paths:
            if path not in self._base or path not in current:
                raise CorpusError(f"public files may not be added or deleted: {path}")
            if self._base[path].mode != current[path].mode:
                raise CorpusError(f"public file mode changed: {path}")
        return current, paths

    def _run_declared(self, command: dict[str, Any]) -> dict[str, Any]:
        self._assert_workspace_policy()
        container = _start_container(
            image=self._image_id,
            workspace=self._workspace,
            policy=self._policy,
        )
        started = time.monotonic()
        try:
            prefix: list[str] = []
            for build in self._policy["build_commands"]:
                returncode, output = _run_command(container=container, command=build)
                prefix.append(output)
                if returncode != 0:
                    combined = "".join(prefix)
                    return {
                        "command_id": command["id"],
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "output_truncated": len(combined.encode())
                        > MAX_VISIBLE_OUTPUT_BYTES,
                        "returncode": returncode,
                        "stderr": "",
                        "stdout": _bounded_output(combined),
                        "timed_out": returncode == 124,
                    }
            returncode, output = _run_command(container=container, command=command)
            prefix.append(output)
            return {
                "command_id": command["id"],
                "duration_ms": int((time.monotonic() - started) * 1000),
                "output_truncated": sum(len(item.encode()) for item in prefix)
                > MAX_VISIBLE_OUTPUT_BYTES,
                "returncode": returncode,
                "stderr": "",
                "stdout": _bounded_output("".join(prefix)),
                "timed_out": returncode == 124,
            }
        finally:
            _stop_container(container)
            self._assert_workspace_policy()

    def _tests_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(arguments, frozenset({"command_id"}), "tests.run")
        command_id = _bounded_string(arguments["command_id"], "command_id", 80)
        command = self._test_commands.get(command_id)
        if command is None:
            raise CorpusError(f"test command is not allowed: {command_id!r}")
        return self._run_declared(command)

    def _build_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(arguments, frozenset({"command_id"}), "build.run")
        command_id = _bounded_string(arguments["command_id"], "command_id", 80)
        command = self._build_commands.get(command_id)
        if command is None:
            raise CorpusError(f"build command is not allowed: {command_id!r}")
        self._assert_workspace_policy()
        container = _start_container(
            image=self._image_id,
            workspace=self._workspace,
            policy=self._policy,
        )
        started = time.monotonic()
        try:
            returncode, output = _run_command(container=container, command=command)
            return {
                "command_id": command_id,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "output_truncated": len(output.encode()) > MAX_VISIBLE_OUTPUT_BYTES,
                "returncode": returncode,
                "stderr": "",
                "stdout": _bounded_output(output),
                "timed_out": returncode == 124,
            }
        finally:
            _stop_container(container)
            self._assert_workspace_policy()


def _bounded_output(value: str) -> str:
    body = value.encode("utf-8")
    if len(body) <= MAX_VISIBLE_OUTPUT_BYTES:
        return value
    return body[-MAX_VISIBLE_OUTPUT_BYTES:].decode("utf-8", errors="replace")


def _validate_health(value: Any) -> None:
    if not isinstance(value, dict) or value.get("status") != "ok":
        raise CorpusError("coding harness health response is not ready")
    versions = value.get("supported_coding_contract_versions")
    required = {
        "case_scoped_inference_v2",
        "coding_runner_tools_v2",
        "scoped_memory_seed_v2",
    }
    if not isinstance(versions, list) or HOSTED_CODING_CONTRACT_VERSION not in versions:
        raise CorpusError("coding harness does not support contract v2")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, list) or not required.issubset(capabilities):
        raise CorpusError("coding harness lacks required v2 capabilities")


def _validate_seed_response(value: Any, seed: dict[str, Any]) -> None:
    if (
        not isinstance(value, dict)
        or value.get("case_id") != seed["case_id"]
        or value.get("profile_capability_id") != seed["profile_capability_id"]
        or value.get("memory_bundle_sha256") != seed["memory_bundle_sha256"]
        or value.get("memory_count") != len(seed["memories"])
        or type(value.get("idempotent_replay")) is not bool
    ):
        raise CorpusError("coding harness seed response identity mismatch")


def _validate_run_response(value: Any, case_id: str) -> None:
    if not isinstance(value, dict) or value.get("case_id") != case_id:
        raise CorpusError("coding harness run response identity mismatch")
    final_report = value.get("final_report")
    if not isinstance(final_report, dict):
        raise CorpusError("coding harness run response lacks a final report")
    summary = final_report.get("summary")
    risks = final_report.get("remaining_risks")
    if not isinstance(summary, str) or not summary or len(summary) > 2_000:
        raise CorpusError("coding harness final summary is invalid")
    if (
        not isinstance(risks, list)
        or len(risks) > 32
        or any(
            not isinstance(risk, str) or not risk or len(risk) > 2_000 for risk in risks
        )
    ):
        raise CorpusError("coding harness remaining risks are invalid")


def _request_bodies(
    session: PublicV2WorkspaceSession,
    *,
    entry: dict[str, Any],
    issue: dict[str, Any],
    memory: dict[str, Any],
    policy: dict[str, Any],
    capability_url: str,
    inference_base_url: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    memories = sorted(memory["memories"], key=lambda item: item["memory_id"])
    seed = {
        "coding_contract_version": HOSTED_CODING_CONTRACT_VERSION,
        "ticket_id": session.ticket_id,
        "case_id": session.case_id,
        "profile_capability_id": session.case.active_user_id,
        "memory_bundle_sha256": sha256_hex(
            canonical_json_bytes({"memories": memories})
        ),
        "memories": memories,
    }
    visible = {
        "issue": issue,
        "memory": memory,
        "runtime_policy": policy,
    }
    run = {
        "coding_contract_version": HOSTED_CODING_CONTRACT_VERSION,
        "ticket_id": session.ticket_id,
        "case_id": session.case_id,
        "profile_capability_id": session.case.active_user_id,
        "visible_bundle_sha256": sha256_hex(canonical_json_bytes(visible)),
        "issue": {
            "title": issue["title"],
            "description": issue["description"],
            "constraints": issue["constraints"],
        },
        "repository_epoch": entry["workspace_tree_sha256"],
        "runtime_policy": {
            "editable_paths": policy["editable_paths"],
            "test_command_ids": [command["id"] for command in policy["test_commands"]],
            "build_command_ids": [
                command["id"] for command in policy["build_commands"]
            ],
        },
        "workspace_capability_url": capability_url,
        "inference_base_url": inference_base_url,
        "budgets": {
            "model_input_tokens": 200_000,
            "model_output_tokens": 30_000,
            "workspace_tool_calls": MAX_PUBLIC_TOOL_CALLS,
            "wall_time_seconds": min(
                int(policy["limits"]["wall_time_seconds"]),
                1_800,
                timeout_seconds - HARNESS_RESPONSE_GRACE_SECONDS,
            ),
        },
    }
    return seed, run


def _run_task(
    *,
    pack: Path,
    entry: dict[str, Any],
    image: str,
    harness_url: str,
    output: Path,
    timeout_seconds: int,
) -> LocalPracticeTaskResult:
    task_id = str(entry["task_id"])
    print(f"[{task_id}] starting", flush=True)
    session = PublicV2WorkspaceSession(
        pack=pack,
        entry=entry,
        output=output,
        image=image,
    )
    issue = _load_json(output / "issue.json")
    memory = _load_json(output / "memory.json")
    policy = _load_json(output / "runtime-policy.json")
    harness_completed = False
    run_response: Any = None
    error_text: str | None = None
    freeze: Any = None
    try:
        with PracticeCapabilityServer(session) as capability:
            health = _request_json(
                "GET",
                f"{harness_url}/coding/health",
                None,
                timeout_seconds=30,
            )
            _validate_health(health)
            seed, run = _request_bodies(
                session,
                entry=entry,
                issue=issue,
                memory=memory,
                policy=policy,
                capability_url=capability.capability_url,
                inference_base_url="http://127.0.0.1:9/direct-openrouter-mode",
                timeout_seconds=timeout_seconds,
            )
            seed_response = _request_json(
                "POST",
                f"{harness_url}/coding/seed",
                seed,
                timeout_seconds=30,
            )
            _validate_seed_response(seed_response, seed)
            run_response = _request_json(
                "POST",
                f"{harness_url}/coding/run",
                run,
                timeout_seconds=timeout_seconds,
            )
            _validate_run_response(run_response, session.case_id)
            harness_completed = True
    except (CorpusError, OSError, TimeoutError, ValueError) as error:
        error_text = str(error)[:2_000]
    finally:
        if not session.frozen:
            try:
                freeze = session.freeze().as_json()
            except (CorpusError, OSError, UnicodeError) as error:
                error_text = error_text or str(error)[:2_000]
        session.close()

    try:
        grade = run_public_v2_task(
            pack=pack,
            task_id=task_id,
            workspace=output / "workspace",
            image=image,
        )
    except (CorpusError, OSError, ValueError) as error:
        grade = LocalPracticeTaskResult(
            task_id=task_id,
            condition=str(entry["condition"]),  # type: ignore[arg-type]
            resolved=False,
            protocol_valid=False,
            patch_valid=False,
            terminal_domain="harness_failure",
        )
        error_text = error_text or str(error)[:2_000]
    if not harness_completed:
        grade = LocalPracticeTaskResult(
            task_id=task_id,
            condition=grade.condition,
            resolved=False,
            protocol_valid=False,
            patch_valid=grade.patch_valid,
            terminal_domain="harness_failure",
        )
    detail = {
        "authoritative": False,
        "error": error_text,
        "freeze": freeze,
        "grade": grade.as_json(),
        "harness_completed": harness_completed,
        "run_response": run_response,
        "task_id": task_id,
        "weight_eligible": False,
    }
    (output / "harness-result.json").write_bytes(canonical_json_bytes(detail))
    print(
        f"[{task_id}] {grade.terminal_domain} resolved={str(grade.resolved).lower()}",
        flush=True,
    )
    return grade


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the contract-v2 Coding starter harness against the public-v2 "
            "practice pack. Start the harness in direct-OpenRouter local-practice "
            "mode: run requests carry only a placeholder inference URL. Results are "
            "local-only and never weight eligible."
        )
    )
    parser.add_argument(
        "--pack", type=Path, required=True, help="unpacked public-v2 pack"
    )
    parser.add_argument(
        "--images", type=Path, required=True, help="task-to-runtime-image JSON map"
    )
    parser.add_argument(
        "--harness-url",
        required=True,
        help="loopback HTTP base URL of the running Coding starter harness",
    )
    parser.add_argument(
        "--harness-artifact-sha256",
        required=True,
        help="lowercase SHA-256 label for the locally tested harness artifact",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="new directory for task reports"
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="run one task ID; repeat to select multiple tasks (default: all ten)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=960,
        help=(
            "per-task harness timeout from 61 to 3600 seconds; the harness wall-time "
            "budget is kept 60 seconds shorter (default: 960)"
        ),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    manifest = validate_public_v2_pack(args.pack)
    harness_url = _loopback_base_url(args.harness_url)
    if len(args.harness_artifact_sha256) != 64 or any(
        character not in "0123456789abcdef"
        for character in args.harness_artifact_sha256
    ):
        raise CorpusError("harness artifact must be lowercase SHA-256")
    if (
        args.timeout_seconds <= HARNESS_RESPONSE_GRACE_SECONDS
        or args.timeout_seconds > 3_600
    ):
        raise CorpusError("timeout must be between 61 and 3600 seconds")
    images: Any = json.loads(args.images.read_bytes())
    if not isinstance(images, dict) or not all(
        isinstance(task_id, str) and isinstance(image, str)
        for task_id, image in images.items()
    ):
        raise CorpusError("image map is invalid")
    entries = _entries(args.pack)
    selected = set(args.task)
    if selected:
        entries = [entry for entry in entries if entry["task_id"] in selected]
        if {entry["task_id"] for entry in entries} != selected:
            raise CorpusError("unknown selected task")
    if any(entry["task_id"] not in images for entry in entries):
        raise CorpusError("image map is missing a selected task")
    if args.output.exists() or args.output.is_symlink():
        raise CorpusError("output must be a new path")
    _validate_health(
        _request_json(
            "GET",
            f"{harness_url}/coding/health",
            None,
            timeout_seconds=30,
        )
    )
    args.output.mkdir(parents=True)
    tasks = tuple(
        _run_task(
            pack=args.pack,
            entry=entry,
            image=str(images[entry["task_id"]]),
            harness_url=harness_url,
            output=args.output / entry["task_id"],
            timeout_seconds=args.timeout_seconds,
        )
        for entry in entries
    )
    if len(tasks) == 10:
        result = build_local_practice_result(
            public_release_id=str(manifest["public_release_id"]),
            public_release_manifest_sha256=sha256_hex(
                (args.pack / "manifest.json").read_bytes()
            ),
            harness_artifact_sha256=args.harness_artifact_sha256,
            tasks=tasks,
        )
        (args.output / "result.json").write_bytes(result.canonical_bytes())
        print(
            f"complete resolved={result.resolved_count}/10 "
            f"score_micros={result.local_practice_score_micros}",
            flush=True,
        )
        return 0 if result.resolved_count == 10 else 1
    summary = {
        "authoritative": False,
        "harness_artifact_sha256": args.harness_artifact_sha256,
        "tasks": [task.as_json() for task in tasks],
        "weight_eligible": False,
    }
    (args.output / "result.json").write_bytes(canonical_json_bytes(summary))
    return 0 if all(task.resolved for task in tasks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
