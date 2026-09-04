"""Semantic validation for external public Coding v2 task controls."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    normalized_tree_identities,
    normalized_tree_sha256,
    safe_opaque_id,
    safe_relative_path,
)
from dittobench_coding_datagen.model import CorpusError

PUBLIC_ISSUE_SCHEMA = "dittobench-coding-public-issue-v2"
PUBLIC_MEMORY_SCHEMA = "dittobench-coding-public-memory-v2"
PUBLIC_RUNTIME_SCHEMA = "dittobench-coding-public-runtime-policy-v2"
PUBLIC_GRADER_SCHEMA = "dittobench-coding-public-grader-v2"

PUBLIC_CONDITIONS = frozenset(
    {
        "v0_none",
        "v1_relevant",
        "v2_irrelevant",
        "v3_stale_conflict",
        "v4_current_override",
    }
)
_ALLOWED_EXECUTABLES = {"cargo", "go", "npx", "pytest", "python", "python3"}
_ALLOWED_NPX_TOOLS = {"jest", "karma", "mocha", "tsc", "vitest"}
_SENSITIVE_ENV = re.compile(
    r"(?:AUTH|CREDENTIAL|KEY|PASSWORD|SECRET|TOKEN)", re.IGNORECASE
)
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_GRADER_PARTS = {"answer", "gold", "reference", "solution"}
_PATCH_TEXT_MARKERS = ("diff --git ", "```diff", "\n--- a/", "\n+++ b/")
_PATCH_HUNK = re.compile(r"(?m)^@@ -[0-9]+(?:,[0-9]+)? \+[0-9]+(?:,[0-9]+)? @@")
_MAX_CONTROL_BYTES = 1 << 20
_ALLOWED_TEXT_CONTROLS = {"\t", "\n", "\r"}


@dataclass(frozen=True)
class PublicTaskControlAuthority:
    issue_sha256: str
    memory_sha256: str
    runtime_policy_sha256: str
    visible_grader_sha256: str

    def as_json(self) -> dict[str, object]:
        return {
            "issue_sha256": self.issue_sha256,
            "memory_sha256": self.memory_sha256,
            "runtime_policy_sha256": self.runtime_policy_sha256,
            "visible_grader_sha256": self.visible_grader_sha256,
        }


def validate_public_task_controls(
    *, task_root: Path, task_id: str, condition: str, workspace: Path
) -> PublicTaskControlAuthority:
    """Validate one task's canonical controls and their cross-file authority."""

    task_id = safe_opaque_id(task_id)
    if condition not in PUBLIC_CONDITIONS:
        raise CorpusError("public task memory condition is invalid")
    issue_body, issue = _load_json(task_root / "issue.json", "issue")
    memory_body, memory = _load_json(task_root / "memory.json", "memory")
    policy_body, policy = _load_json(
        task_root / "runtime-policy.json", "runtime policy"
    )
    _validate_issue(issue, task_id)
    _validate_memory(memory, task_id, condition)
    runtime_image, runtime_command_ids = _validate_runtime_policy(
        policy, task_id, workspace
    )
    grader = task_root / "grader"
    grader_sha256 = _validate_grader(
        grader=grader,
        task_id=task_id,
        runtime_image=runtime_image,
        runtime_command_ids=runtime_command_ids,
    )
    return PublicTaskControlAuthority(
        issue_sha256=_sha256(issue_body),
        memory_sha256=_sha256(memory_body),
        runtime_policy_sha256=_sha256(policy_body),
        visible_grader_sha256=grader_sha256,
    )


def _validate_issue(value: dict[str, object], task_id: str) -> None:
    _require_fields(
        value,
        {"schema", "task_id", "title", "description", "constraints"},
        "issue",
    )
    if value["schema"] != PUBLIC_ISSUE_SCHEMA or value["task_id"] != task_id:
        raise CorpusError("public task issue authority is invalid")
    _bounded_text(value["title"], 1, 1024, "issue title")
    _bounded_text(value["description"], 1, 64 << 10, "issue description")
    _reject_patch_text(value["description"], "issue description")
    constraints = value["constraints"]
    if (
        not isinstance(constraints, list)
        or len(constraints) > 64
        or any(
            not isinstance(item, str)
            or not item
            or len(item.encode()) > 4096
            or _unsafe_control(item)
            for item in constraints
        )
    ):
        raise CorpusError("public task issue constraints are invalid")
    for constraint in constraints:
        _reject_patch_text(constraint, "issue constraint")


def _validate_memory(value: dict[str, object], task_id: str, condition: str) -> None:
    _require_fields(value, {"schema", "task_id", "condition", "memories"}, "memory")
    if (
        value["schema"] != PUBLIC_MEMORY_SCHEMA
        or value["task_id"] != task_id
        or value["condition"] != condition
    ):
        raise CorpusError("public task memory authority is invalid")
    memories = value["memories"]
    if not isinstance(memories, list) or len(memories) > 128:
        raise CorpusError("public task memories are invalid")
    if (condition == "v0_none") != (len(memories) == 0):
        raise CorpusError("public task memories do not match condition")
    memory_ids: list[str] = []
    for raw in memories:
        if not isinstance(raw, dict):
            raise CorpusError("public task memory record is invalid")
        _require_fields(
            raw,
            {
                "confidence_micros",
                "content",
                "fact_group_id",
                "memory_id",
                "repository_capability_id",
                "scope",
                "supersedes",
                "type",
                "valid_from_epoch",
                "valid_until_epoch",
            },
            "memory record",
        )
        memory_id = _opaque(raw["memory_id"], "memory ID")
        memory_ids.append(memory_id)
        for field in (
            "repository_capability_id",
            "fact_group_id",
            "valid_from_epoch",
            "valid_until_epoch",
        ):
            if raw[field] is not None:
                _opaque(raw[field], field)
        _opaque(raw["scope"], "memory scope")
        _opaque(raw["type"], "memory type")
        _bounded_text(raw["content"], 1, 16 << 10, "memory content")
        _reject_patch_text(raw["content"], "memory content")
        confidence = raw["confidence_micros"]
        if type(confidence) is not int or not 0 <= confidence <= 1_000_000:
            raise CorpusError("public task memory confidence is invalid")
        supersedes = raw["supersedes"]
        if not isinstance(supersedes, list) or len(supersedes) > 64:
            raise CorpusError("public task memory supersession is invalid")
        normalized = [_opaque(item, "superseded memory ID") for item in supersedes]
        if normalized != sorted(set(normalized)) or memory_id in normalized:
            raise CorpusError("public task memory supersession is invalid")
    if memory_ids != sorted(set(memory_ids)):
        raise CorpusError("public task memories must be unique and sorted")


def _validate_runtime_policy(
    value: dict[str, object], task_id: str, workspace: Path
) -> tuple[str, set[str]]:
    _require_fields(
        value,
        {
            "build_commands",
            "creatable_paths",
            "deletable_paths",
            "editable_paths",
            "environment_image_digest",
            "environment_platform",
            "limits",
            "network",
            "schema",
            "task_id",
            "test_commands",
        },
        "runtime policy",
    )
    image = value["environment_image_digest"]
    if (
        value["schema"] != PUBLIC_RUNTIME_SCHEMA
        or value["task_id"] != task_id
        or not isinstance(image, str)
        or _OCI_DIGEST.fullmatch(image) is None
        or value["environment_platform"] != "linux/amd64"
        or value["network"] != "none"
    ):
        raise CorpusError("public task runtime authority is invalid")
    path_sets = {
        name: _path_list(value[name], name)
        for name in ("editable_paths", "creatable_paths", "deletable_paths")
    }
    if not path_sets["editable_paths"]:
        raise CorpusError("public task requires at least one editable path")
    combined = [item for values in path_sets.values() for item in values]
    if len(combined) != len(set(combined)):
        raise CorpusError("public task runtime paths overlap")
    for path in path_sets["editable_paths"] + path_sets["deletable_paths"]:
        target = workspace / path
        if target.is_symlink() or not target.is_file():
            raise CorpusError("public task editable path is unavailable")
    for path in path_sets["creatable_paths"]:
        if (workspace / path).exists() or (workspace / path).is_symlink():
            raise CorpusError("public task creatable path already exists")
    test_commands = _commands(value["test_commands"], "test commands")
    build_commands = _commands(value["build_commands"], "build commands")
    if not test_commands:
        raise CorpusError("public task requires at least one test command")
    command_ids = [str(item["id"]) for item in test_commands + build_commands]
    if len(command_ids) != len(set(command_ids)):
        raise CorpusError("public task command IDs overlap")
    _validate_limits(value["limits"], workspace)
    return image, set(command_ids)


def _validate_limits(value: object, workspace: Path) -> None:
    if not isinstance(value, dict):
        raise CorpusError("public task runtime limits are invalid")
    _require_fields(
        value,
        {
            "cpu_quota_millis",
            "max_patch_bytes",
            "max_workspace_bytes",
            "memory_limit_bytes",
            "pids_limit",
            "wall_time_seconds",
        },
        "runtime limits",
    )
    bounds = {
        "cpu_quota_millis": (100, 16_000),
        "max_patch_bytes": (1, 128 << 20),
        "max_workspace_bytes": (1, 4 << 30),
        "memory_limit_bytes": (256 << 20, 64 << 30),
        "pids_limit": (1, 4096),
        "wall_time_seconds": (1, 7200),
    }
    for field, (minimum, maximum) in bounds.items():
        item = value[field]
        if type(item) is not int or not minimum <= item <= maximum:
            raise CorpusError("public task runtime limits are invalid")
    workspace_bytes = sum(
        int(item["size_bytes"]) for item in normalized_tree_identities(workspace)
    )
    max_patch_bytes = int(value["max_patch_bytes"])
    max_workspace_bytes = int(value["max_workspace_bytes"])
    if max_patch_bytes > max_workspace_bytes or workspace_bytes > max_workspace_bytes:
        raise CorpusError("public task runtime limits are incoherent")


def _commands(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > 64:
        raise CorpusError(f"public task {label} are invalid")
    commands: list[dict[str, object]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise CorpusError(f"public task {label} are invalid")
        _require_fields(
            raw, {"argv", "environment", "id", "timeout_milliseconds"}, label
        )
        command_id = _command_id(raw["id"])
        argv = raw["argv"]
        timeout = raw["timeout_milliseconds"]
        environment = raw["environment"]
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 64
            or any(
                not isinstance(argument, str)
                or not argument
                or len(argument.encode()) > 4096
                or "\x00" in argument
                for argument in argv
            )
            or sum(len(argument.encode()) for argument in argv) > 8192
            or type(timeout) is not int
            or not 1 <= timeout <= 600_000
            or not isinstance(environment, dict)
            or len(environment) > 32
        ):
            raise CorpusError(f"public task {label} are invalid")
        executable = argv[0]
        if executable not in _ALLOWED_EXECUTABLES:
            raise CorpusError("public task command executable is forbidden")
        if any(
            "://" in argument
            or argument.startswith("git+")
            or argument == ".git"
            or "/.git/" in argument
            for argument in argv[1:]
        ):
            raise CorpusError("public task command argument is forbidden")
        if executable in {"python", "python3"} and "-c" in argv[1:]:
            raise CorpusError("public task Python command may not execute inline code")
        if executable == "npx":
            if "--no-install" not in argv[1:]:
                raise CorpusError("public task npx command must disable installation")
            tool = next(
                (argument for argument in argv[1:] if not argument.startswith("-")),
                None,
            )
            if tool not in _ALLOWED_NPX_TOOLS:
                raise CorpusError("public task npx tool is forbidden")
        if executable == "cargo" and (
            len(argv) < 2
            or argv[1] not in {"check", "test"}
            or "--offline" not in argv[2:]
        ):
            raise CorpusError("public task Cargo command must be offline")
        if executable == "go" and (len(argv) < 2 or argv[1] not in {"test", "vet"}):
            raise CorpusError("public task Go command is forbidden")
        if (
            len(argv) >= 3
            and argv[0] in {"python", "python3"}
            and argv[1:3] == ["-m", "pip"]
        ):
            required = {"--no-build-isolation", "--no-deps", "--no-index"}
            if len(argv) < 4 or argv[3] != "install" or not required.issubset(argv[4:]):
                raise CorpusError("public task pip command must be fully offline")
        for name, item in environment.items():
            if (
                not isinstance(name, str)
                or _ENV_NAME.fullmatch(name) is None
                or _SENSITIVE_ENV.search(name)
                or not isinstance(item, str)
                or len(item.encode()) > 1024
                or _unsafe_control(item)
            ):
                raise CorpusError("public task command environment is unsafe")
        commands.append({"id": command_id})
    command_ids = [str(item["id"]) for item in commands]
    if command_ids != sorted(set(command_ids)):
        raise CorpusError(f"public task {label} must be unique and sorted")
    return commands


def _validate_grader(
    *,
    grader: Path,
    task_id: str,
    runtime_image: str,
    runtime_command_ids: set[str],
) -> str:
    if grader.is_symlink() or not grader.is_dir():
        raise CorpusError("public task grader is unsafe")
    _, manifest = _load_json(grader / "manifest.json", "grader manifest")
    _require_fields(
        manifest,
        {
            "build_command",
            "environment_image_digest",
            "environment_platform",
            "files",
            "schema",
            "task_id",
            "test_groups",
        },
        "grader manifest",
    )
    if (
        manifest["schema"] != PUBLIC_GRADER_SCHEMA
        or manifest["task_id"] != task_id
        or manifest["environment_image_digest"] != runtime_image
        or manifest["environment_platform"] != "linux/amd64"
    ):
        raise CorpusError("public task grader authority is invalid")
    command_ids: list[str] = []
    build = manifest["build_command"]
    if build is not None:
        command_ids.append(_one_command(build, "grader build command"))
    groups = manifest["test_groups"]
    if not isinstance(groups, list) or len(groups) != 2:
        raise CorpusError("public task grader groups are invalid")
    expected_groups = ("fail_to_pass", "pass_to_pass")
    all_tests: list[str] = []
    for raw, expected_group in zip(groups, expected_groups, strict=True):
        if not isinstance(raw, dict):
            raise CorpusError("public task grader group is invalid")
        _require_fields(raw, {"command", "expected_tests", "group"}, "grader group")
        if raw["group"] != expected_group:
            raise CorpusError("public task grader group order is invalid")
        command_ids.append(_one_command(raw["command"], "grader test command"))
        tests = raw["expected_tests"]
        if (
            not isinstance(tests, list)
            or not tests
            or len(tests) > 10_000
            or any(
                not isinstance(test, str)
                or not test
                or len(test.encode()) > 1024
                or _unsafe_control(test)
                for test in tests
            )
            or tests != sorted(set(tests))
        ):
            raise CorpusError("public task grader expected tests are invalid")
        all_tests.extend(tests)
    if len(all_tests) != len(set(all_tests)) or len(command_ids) != len(
        set(command_ids)
    ):
        raise CorpusError("public task grader identities overlap")
    if runtime_command_ids.intersection(command_ids):
        raise CorpusError("public task grader command IDs overlap visible commands")
    _validate_grader_files(grader, manifest["files"])
    return normalized_tree_sha256(grader)


def _one_command(value: object, label: str) -> str:
    commands = _commands([value], label)
    return str(commands[0]["id"])


def _validate_grader_files(grader: Path, value: object) -> None:
    if not isinstance(value, list) or not value or len(value) > 100_000:
        raise CorpusError("public task grader files are invalid")
    identities = {
        str(item["path"]): item
        for item in normalized_tree_identities(grader)
        if item["path"] != "manifest.json"
    }
    source_paths: list[str] = []
    destinations: list[str] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise CorpusError("public task grader file is invalid")
        _require_fields(
            raw,
            {"destination_path", "mode", "sha256", "size_bytes", "source_path"},
            "grader file",
        )
        source_path = safe_relative_path(_string(raw["source_path"], "source path"))
        destination = safe_relative_path(
            _string(raw["destination_path"], "destination path")
        )
        if not source_path.startswith("files/") or _forbidden_grader_path(source_path):
            raise CorpusError("public task grader source path is forbidden")
        if not _test_destination(destination):
            raise CorpusError("public task grader destination is not test-scoped")
        identity = identities.get(source_path)
        if identity is None or any(
            raw[field] != identity[field] for field in ("mode", "sha256", "size_bytes")
        ):
            raise CorpusError("public task grader file identity drifted")
        source_paths.append(source_path)
        destinations.append(destination)
    if (
        source_paths != sorted(set(source_paths))
        or len(destinations) != len(set(destinations))
        or set(source_paths) != set(identities)
    ):
        raise CorpusError("public task grader files are not canonical")


def _forbidden_grader_path(path: str) -> bool:
    filename = Path(path).name.casefold()
    stem = Path(path).stem.casefold()
    parts = {part.casefold() for part in Path(path).parts}
    return (
        bool(parts.intersection(_FORBIDDEN_GRADER_PARTS))
        or stem in _FORBIDDEN_GRADER_PARTS
        or filename.endswith((".diff", ".patch"))
        or filename == ".env"
        or filename.startswith(".env.")
    )


def _test_destination(path: str) -> bool:
    candidate = Path(path)
    parts = {part.casefold() for part in candidate.parts[:-1]}
    filename = candidate.name.casefold()
    return bool(parts.intersection({"test", "tests", "testing"})) or (
        filename.startswith("test_") or "_test." in filename or ".test." in filename
    )


def _path_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise CorpusError(f"public task {label} are invalid")
    paths = [safe_relative_path(_string(item, label)) for item in value]
    if paths != sorted(set(paths)):
        raise CorpusError(f"public task {label} must be unique and sorted")
    return paths


def _load_json(path: Path, label: str) -> tuple[bytes, dict[str, object]]:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size < 1
        or path.stat().st_size > _MAX_CONTROL_BYTES
    ):
        raise CorpusError(f"public task {label} is unsafe")
    try:
        body = path.read_bytes()
        value = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError(f"public task {label} is invalid") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != body:
        raise CorpusError(f"public task {label} is not canonical JSON")
    return body, value


def _require_fields(value: dict[str, object], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise CorpusError(f"public task {label} fields are invalid")


def _reject_patch_text(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise CorpusError(f"public task {label} is invalid")
    lowered = value.lower()
    if any(marker in lowered for marker in _PATCH_TEXT_MARKERS) or _PATCH_HUNK.search(
        value
    ):
        raise CorpusError(f"public task {label} contains patch-form solution text")


def _bounded_text(value: object, minimum: int, maximum: int, label: str) -> str:
    if (
        not isinstance(value, str)
        or not minimum <= len(value.encode()) <= maximum
        or _unsafe_control(value)
    ):
        raise CorpusError(f"public task {label} is invalid")
    return value


def _unsafe_control(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co"}
        and character not in _ALLOWED_TEXT_CONTROLS
        for character in value
    )


def _opaque(value: object, label: str) -> str:
    try:
        return safe_opaque_id(value)
    except CorpusError as error:
        raise CorpusError(f"public task {label} is invalid") from error


def _command_id(value: object) -> str:
    command_id = _opaque(value, "command ID")
    if len(command_id.encode()) > 80:
        raise CorpusError("public task command ID is invalid")
    return command_id


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise CorpusError(f"public task {label} is invalid")
    return value


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


__all__ = [
    "PUBLIC_GRADER_SCHEMA",
    "PUBLIC_CONDITIONS",
    "PUBLIC_ISSUE_SCHEMA",
    "PUBLIC_MEMORY_SCHEMA",
    "PUBLIC_RUNTIME_SCHEMA",
    "PublicTaskControlAuthority",
    "validate_public_task_controls",
]
