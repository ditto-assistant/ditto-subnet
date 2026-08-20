"""Validator-owned typed workspace runtime for the public practice pack.

This module is a local protocol proof, not a production sandbox. The agent only
receives typed operations; the workspace path and frozen patch remain owned by
the runner.
"""

from __future__ import annotations

import difflib
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_relative_path,
    sha256_hex,
)
from dittobench_coding_datagen.compiler import materialize
from dittobench_coding_datagen.model import CODING_CONTRACT_VERSION, CorpusError
from dittobench_coding_datagen.practice_case import (
    PracticeAgentCase,
    load_practice_agent_case,
)

MAX_CALLS = 64
MAX_READ_BYTES = 32_768
MAX_FILE_BYTES = 65_536
MAX_DIFF_BYTES = 65_536
MAX_RESULTS = 100
MAX_REPLACEMENTS = 16
MAX_TOOL_BODY_BYTES = 65_536
INITIAL_EVENT_ROOT = "0" * 64
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ToolRequest:
    coding_contract_version: int
    case_id: str
    profile_capability_id: str
    call_id: str
    name: str
    arguments: dict[str, Any]

    @classmethod
    def from_json(cls, value: Any) -> ToolRequest:
        if not isinstance(value, dict):
            raise CorpusError("tool request must be an object")
        arguments = value.get("arguments")
        if not isinstance(arguments, dict):
            raise CorpusError("tool request arguments must be an object")
        version = value.get("coding_contract_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise CorpusError("coding_contract_version must be an integer")
        return cls(
            coding_contract_version=version,
            case_id=_bounded_string(value.get("case_id"), "case_id", 128),
            profile_capability_id=_bounded_string(
                value.get("profile_capability_id"), "profile_capability_id", 128
            ),
            call_id=_bounded_string(value.get("call_id"), "call_id", 128),
            name=_bounded_string(value.get("name"), "name", 80),
            arguments=arguments,
        )


@dataclass(frozen=True)
class ToolResponse:
    call_id: str
    sequence: int
    ok: bool
    result: dict[str, Any] | None
    error: dict[str, str] | None
    event_sha256: str

    def as_json(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "error": self.error,
            "event_sha256": self.event_sha256,
            "ok": self.ok,
            "result": self.result,
            "sequence": self.sequence,
        }


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    returncode: int
    stdout: str
    stderr: str
    output_truncated: bool
    timed_out: bool
    duration_ms: int

    def as_json(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "duration_ms": self.duration_ms,
            "output_truncated": self.output_truncated,
            "returncode": self.returncode,
            "stderr": self.stderr,
            "stdout": self.stdout,
            "timed_out": self.timed_out,
        }


@dataclass(frozen=True)
class FrozenFileChange:
    path: str
    before_sha256: str
    after_sha256: str
    after_content: str

    def as_json(self) -> dict[str, Any]:
        return {
            "after_content": self.after_content,
            "after_sha256": self.after_sha256,
            "before_sha256": self.before_sha256,
            "path": self.path,
        }


@dataclass(frozen=True)
class FrozenPracticeSubmission:
    task_id: str
    base_tree_sha256: str
    final_tree_sha256: str
    patch_sha256: str
    changed_path_root: str
    authoring_event_root: str
    changed_paths: tuple[str, ...]
    changes: tuple[FrozenFileChange, ...]
    patch: str

    def as_json(self) -> dict[str, Any]:
        return {
            "authoring_event_root": self.authoring_event_root,
            "base_tree_sha256": self.base_tree_sha256,
            "changed_path_root": self.changed_path_root,
            "changed_paths": list(self.changed_paths),
            "changes": [change.as_json() for change in self.changes],
            "final_tree_sha256": self.final_tree_sha256,
            "patch": self.patch,
            "patch_sha256": self.patch_sha256,
            "task_id": self.task_id,
        }


@dataclass(frozen=True)
class FailedWorkspaceIdentity:
    task_id: str
    base_tree_sha256: str
    final_tree_sha256: str
    changed_path_root: str
    authoring_event_root: str


@dataclass(frozen=True)
class _FileState:
    body: bytes
    mode: int


def _bounded_string(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CorpusError(f"{field} must be a non-empty string of at most {maximum}")
    return value


def _exact_arguments(
    arguments: dict[str, Any], expected: frozenset[str], tool: str
) -> None:
    observed = frozenset(arguments)
    if observed != expected:
        raise CorpusError(
            f"{tool} arguments do not match; "
            f"missing={sorted(expected - observed)}, "
            f"unknown={sorted(observed - expected)}"
        )


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise CorpusError(f"{field} must be between {minimum} and {maximum}")
    return value


def _snapshot(root: Path) -> dict[str, _FileState]:
    if not root.is_dir() or root.is_symlink():
        raise CorpusError("practice workspace is not a real directory")
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
        body = path.read_bytes()
        if len(body) > MAX_FILE_BYTES:
            raise CorpusError(f"workspace file exceeds practice limit: {relative}")
        snapshot[relative] = _FileState(body, stat.S_IMODE(info.st_mode))
    return snapshot


def _tree_sha256(snapshot: dict[str, _FileState]) -> str:
    identities = [
        {
            "mode": state.mode,
            "path": path,
            "sha256": sha256_hex(state.body),
            "size_bytes": len(state.body),
        }
        for path, state in sorted(snapshot.items())
    ]
    return sha256_hex(canonical_json_bytes(identities))


def _changed_paths(
    before: dict[str, _FileState], after: dict[str, _FileState]
) -> tuple[str, ...]:
    return tuple(
        path
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    )


def _unified_diff(
    before: dict[str, _FileState], after: dict[str, _FileState], paths: tuple[str, ...]
) -> str:
    chunks: list[str] = []
    for path in paths:
        old = before.get(path)
        new = after.get(path)
        old_text = "" if old is None else old.body.decode("utf-8")
        new_text = "" if new is None else new.body.decode("utf-8")
        chunks.extend(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{path}" if old is not None else "/dev/null",
                tofile=f"b/{path}" if new is not None else "/dev/null",
            )
        )
    body = "".join(chunks)
    if len(body.encode("utf-8")) > MAX_DIFF_BYTES:
        raise CorpusError("frozen practice patch exceeds the diff limit")
    return body


_TEST_SCRIPT = """
import sys
import unittest

sys.path.insert(0, ".")
suite = unittest.TestLoader().discover("tests", pattern="test_visible.py")
result = unittest.TextTestRunner(verbosity=1).run(suite)
completed = result.wasSuccessful() and result.testsRun > 0
print(f"DITTOBENCH_TEST_COMPLETION:{result.testsRun}:{int(completed)}", flush=True)
raise SystemExit(0 if completed else 1)
"""

_GRADER_SCRIPT = """
import sys
import unittest

sys.path.insert(0, ".")
suite = unittest.TestLoader().discover("tests", pattern="test_regression.py")
result = unittest.TextTestRunner(verbosity=1).run(suite)
completed = result.wasSuccessful() and result.testsRun > 0
print(f"DITTOBENCH_TEST_COMPLETION:{result.testsRun}:{int(completed)}", flush=True)
raise SystemExit(0 if completed else 1)
"""

_BUILD_SCRIPT = """
import ast
from pathlib import Path

body = Path("app.py").read_bytes()
tree = ast.parse(body, "app.py")

# The public 3x3 pack intentionally consists only of pure, single-file
# functions. Enforce that declared capsule contract before importing candidate
# code in a test process. This is a practice-pack integrity gate, not a general
# Python sandbox.
definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
docstrings = [
    node
    for index, node in enumerate(tree.body)
    if index == 0
    and isinstance(node, ast.Expr)
    and isinstance(node.value, ast.Constant)
    and isinstance(node.value.value, str)
]
if len(definitions) != 1 or len(tree.body) != len(definitions) + len(docstrings):
    raise ValueError("practice app.py must contain exactly one function")
if definitions[0].decorator_list:
    raise ValueError("practice app.py function decorators are forbidden")

allowed_nodes = (
    ast.Add,
    ast.And,
    ast.Assign,
    ast.Attribute,
    ast.AugAssign,
    ast.BinOp,
    ast.BoolOp,
    ast.Break,
    ast.Call,
    ast.Compare,
    ast.Constant,
    ast.Continue,
    ast.Dict,
    ast.Div,
    ast.Eq,
    ast.Expr,
    ast.FloorDiv,
    ast.For,
    ast.FormattedValue,
    ast.FunctionDef,
    ast.Gt,
    ast.GtE,
    ast.If,
    ast.IfExp,
    ast.In,
    ast.Is,
    ast.IsNot,
    ast.JoinedStr,
    ast.Lambda,
    ast.List,
    ast.ListComp,
    ast.Load,
    ast.Lt,
    ast.LtE,
    ast.Mod,
    ast.Module,
    ast.Mult,
    ast.Name,
    ast.Not,
    ast.NotEq,
    ast.NotIn,
    ast.Or,
    ast.Pass,
    ast.Raise,
    ast.Return,
    ast.Set,
    ast.Slice,
    ast.Store,
    ast.Sub,
    ast.Subscript,
    ast.Tuple,
    ast.UAdd,
    ast.USub,
    ast.UnaryOp,
    ast.arg,
    ast.arguments,
    ast.comprehension,
    ast.keyword,
)
forbidden_names = {
    "BaseException", "KeyboardInterrupt", "SystemExit", "__import__",
    "breakpoint", "compile", "delattr", "eval", "exec", "exit", "getattr",
    "globals", "help", "input", "locals", "memoryview", "object", "open",
    "print", "property", "quit", "setattr", "staticmethod", "super", "type",
    "vars",
}
allowed_named_calls = {
    "ValueError", "abs", "all", "any", "bool", "dict", "divmod",
    "enumerate", "int", "len", "list", "max", "min", "range", "set",
    "sorted", "str", "sum", "tuple", "zip",
}
allowed_method_calls = {"lower", "rstrip", "strip", "update"}
parents = {
    child: parent
    for parent in ast.walk(tree)
    for child in ast.iter_child_nodes(parent)
}

for node in ast.walk(tree):
    if not isinstance(node, allowed_nodes):
        kind = type(node).__name__
        raise ValueError(f"practice app.py contains unsupported syntax: {kind}")
    if isinstance(node, ast.Name) and (
        node.id.startswith("__") or node.id in forbidden_names
    ):
        raise ValueError(f"practice app.py contains a forbidden name: {node.id}")
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        if node.id in allowed_named_calls:
            raise ValueError(f"practice app.py shadows a safe builtin: {node.id}")
    if isinstance(node, ast.arg) and node.arg in allowed_named_calls:
        raise ValueError(f"practice app.py shadows a safe builtin: {node.arg}")
    if isinstance(node, ast.Attribute):
        parent = parents.get(node)
        if not isinstance(parent, ast.Call) or parent.func is not node:
            raise ValueError("practice app.py contains non-call attribute access")
        if node.attr not in allowed_method_calls:
            raise ValueError(f"practice app.py calls a forbidden method: {node.attr}")
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            if node.func.id not in allowed_named_calls:
                name = node.func.id
                raise ValueError(f"practice app.py calls a forbidden function: {name}")
        elif isinstance(node.func, ast.Attribute):
            pass
        else:
            raise ValueError("practice app.py contains an indirect function call")

compile(body, "app.py", "exec")
"""


def run_practice_command(
    workspace: Path,
    command_id: str,
    *,
    timeout_seconds: int = 10,
) -> CommandResult:
    """Run one fixed public-practice command with bounded returned output."""

    scripts = {
        "grader-unit": _GRADER_SCRIPT,
        "python-compile": _BUILD_SCRIPT,
        "visible-unit": _TEST_SCRIPT,
    }
    try:
        script = scripts[command_id]
    except KeyError as error:
        raise CorpusError(f"unknown practice command id: {command_id!r}") from error
    started = time.monotonic()
    timed_out = False
    environment = {
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", script],
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            returncode = process.wait()
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout_body = stdout_file.read(MAX_READ_BYTES + 1)
        stderr_body = stderr_file.read(MAX_READ_BYTES + 1)
    truncated = len(stdout_body) > MAX_READ_BYTES or len(stderr_body) > MAX_READ_BYTES
    prefixes = {str(workspace), str(workspace.resolve())}

    def scrub(body: bytes) -> str:
        text = body[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        for prefix in sorted(prefixes, key=len, reverse=True):
            text = text.replace(prefix, "<workspace>")
        return text

    stdout = scrub(stdout_body)
    stderr = scrub(stderr_body)
    if command_id in {"grader-unit", "visible-unit"} and returncode == 0:
        completions = re.findall(
            r"(?m)^DITTOBENCH_TEST_COMPLETION:([1-9][0-9]*):1$", stdout
        )
        if len(completions) != 1:
            returncode = 125
            stderr += (
                "\nDittoBench runner rejected a successful exit without exactly one "
                "positive test-completion marker.\n"
            )
    return CommandResult(
        command_id=command_id,
        returncode=124 if timed_out else returncode,
        stdout=stdout,
        stderr=stderr,
        output_truncated=truncated,
        timed_out=timed_out,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
    )


class PracticeWorkspaceSession:
    """One task-scoped validator-owned public practice workspace."""

    def __init__(self, pack: Path, task_id: str) -> None:
        self._pack = pack.resolve()
        self.case: PracticeAgentCase = load_practice_agent_case(self._pack, task_id)
        self._temporary = tempfile.TemporaryDirectory(
            prefix=f"dittobench-practice-{task_id.lower()}-"
        )
        self._workspace = Path(self._temporary.name) / "workspace"
        materialize(self._pack, task_id, self._workspace)
        self._base = _snapshot(self._workspace)
        self._base_tree_sha256 = _tree_sha256(self._base)
        self._event_root = INITIAL_EVENT_ROOT
        self._sequence = 0
        self._call_ids: set[str] = set()
        self._frozen = False
        self._closed = False
        self._lock = threading.RLock()

    @property
    def event_root(self) -> str:
        return self._event_root

    @property
    def frozen(self) -> bool:
        return self._frozen

    def __enter__(self) -> PracticeWorkspaceSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._temporary.cleanup()
            self._closed = True

    def _target(self, raw: Any, *, allow_directory: bool = False) -> tuple[str, Path]:
        path = _bounded_string(raw, "path", 256)
        if path == "." and allow_directory:
            return path, self._workspace
        relative = safe_relative_path(path)
        target = self._workspace / relative
        current = self._workspace
        for part in Path(relative).parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise CorpusError(f"workspace path traverses a symlink: {relative}")
        try:
            target.resolve(strict=False).relative_to(self._workspace.resolve())
        except ValueError as error:
            raise CorpusError(f"workspace path escapes the task: {relative}") from error
        return relative, target

    def _record(
        self,
        request: ToolRequest,
        *,
        result: dict[str, Any] | None,
        error: dict[str, str] | None,
    ) -> ToolResponse:
        self._sequence += 1
        event = {
            "arguments": request.arguments,
            "call_id": request.call_id,
            "case_id": request.case_id,
            "error": error,
            "name": request.name,
            "previous_event_sha256": self._event_root,
            "profile_capability_id": request.profile_capability_id,
            "result": result,
            "sequence": self._sequence,
        }
        self._event_root = sha256_hex(canonical_json_bytes(event))
        return ToolResponse(
            call_id=request.call_id,
            sequence=self._sequence,
            ok=error is None,
            result=result,
            error=error,
            event_sha256=self._event_root,
        )

    def invoke(self, request: ToolRequest) -> ToolResponse:
        with self._lock:
            return self._invoke_locked(request)

    def _invoke_locked(self, request: ToolRequest) -> ToolResponse:
        if self._closed:
            raise CorpusError("practice workspace is closed")
        if self._frozen:
            raise CorpusError("practice workspace capability has been revoked")
        if request.coding_contract_version != CODING_CONTRACT_VERSION:
            raise CorpusError("unsupported coding contract version")
        if request.case_id != self.case.task_id:
            raise CorpusError("tool request case capability mismatch")
        if request.profile_capability_id != self.case.active_user_id:
            raise CorpusError("tool request profile capability mismatch")
        if request.call_id in self._call_ids:
            raise CorpusError("tool request call_id was replayed")
        if len(self._call_ids) >= MAX_CALLS:
            raise CorpusError("practice workspace tool-call budget exhausted")
        self._call_ids.add(request.call_id)
        try:
            result = self._dispatch(request.name, request.arguments)
            if len(canonical_json_bytes(result)) > MAX_TOOL_BODY_BYTES:
                raise CorpusError("tool result exceeds the practice output limit")
            return self._record(request, result=result, error=None)
        except CorpusError as error:
            return self._record(
                request,
                result=None,
                error={"code": "invalid_tool_request", "message": str(error)},
            )

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "build.run": self._build_run,
            "git.diff": self._git_diff,
            "git.status": self._git_status,
            "repo.apply_patch": self._repo_apply_patch,
            "repo.create_file": self._repo_create_file,
            "repo.delete_file": self._repo_delete_file,
            "repo.list_tree": self._repo_list_tree,
            "repo.read_file": self._repo_read_file,
            "repo.read_range": self._repo_read_range,
            "repo.search": self._repo_search,
            "tests.run": self._tests_run,
        }
        try:
            handler = handlers[name]
        except KeyError as error:
            raise CorpusError(f"unknown practice workspace tool: {name!r}") from error
        return handler(arguments)

    def _repo_list_tree(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(arguments, frozenset({"depth", "path"}), "repo.list_tree")
        depth = _integer(arguments["depth"], "depth", 0, 8)
        relative, target = self._target(arguments["path"], allow_directory=True)
        if not target.is_dir():
            raise CorpusError(f"tree path is not a directory: {relative}")
        entries: list[dict[str, Any]] = []
        base_parts = 0 if relative == "." else len(Path(relative).parts)
        for path in sorted(target.rglob("*")):
            item_relative = safe_relative_path(
                path.relative_to(self._workspace).as_posix()
            )
            item_depth = len(Path(item_relative).parts) - base_parts
            if item_depth > depth:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise CorpusError(f"workspace contains a symlink: {item_relative}")
            if stat.S_ISDIR(info.st_mode):
                entries.append({"path": item_relative, "type": "directory"})
            elif stat.S_ISREG(info.st_mode):
                body = path.read_bytes()
                entries.append(
                    {
                        "path": item_relative,
                        "sha256": sha256_hex(body),
                        "size_bytes": len(body),
                        "type": "file",
                    }
                )
            else:
                raise CorpusError(f"workspace contains a special file: {item_relative}")
            if len(entries) > 256:
                raise CorpusError("tree result exceeds the practice entry limit")
        return {"entries": entries, "path": relative}

    def _repo_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(
            arguments,
            frozenset({"max_results", "path", "query"}),
            "repo.search",
        )
        query = _bounded_string(arguments["query"], "query", 256)
        maximum = _integer(arguments["max_results"], "max_results", 1, MAX_RESULTS)
        relative, target = self._target(arguments["path"], allow_directory=True)
        if not target.exists():
            raise CorpusError(f"search path does not exist: {relative}")
        if not target.is_file() and not target.is_dir():
            raise CorpusError(f"search path is not a regular entry: {relative}")
        paths = [target] if target.is_file() else sorted(target.rglob("*"))
        matches: list[dict[str, Any]] = []
        for path in paths:
            if len(matches) >= maximum or not path.is_file():
                continue
            item_relative = safe_relative_path(
                path.relative_to(self._workspace).as_posix()
            )
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(lines, 1):
                start = 0
                while len(matches) < maximum:
                    column = line.find(query, start)
                    if column < 0:
                        break
                    matches.append(
                        {
                            "column": column + 1,
                            "line": number,
                            "path": item_relative,
                            "text": line[:500],
                        }
                    )
                    start = column + len(query)
        return {"matches": matches, "path": relative, "query": query}

    def _read_text(self, raw: Any) -> tuple[str, str, bytes]:
        relative, target = self._target(raw)
        if not target.is_file():
            raise CorpusError(f"workspace path is not a file: {relative}")
        body = target.read_bytes()
        if len(body) > MAX_FILE_BYTES:
            raise CorpusError(f"workspace file exceeds practice limit: {relative}")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CorpusError(f"workspace file is not UTF-8: {relative}") from error
        return relative, text, body

    def _repo_read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(arguments, frozenset({"path"}), "repo.read_file")
        relative, text, body = self._read_text(arguments["path"])
        if len(body) > MAX_READ_BYTES:
            raise CorpusError(f"file exceeds read_file output limit: {relative}")
        return {
            "content": text,
            "path": relative,
            "sha256": sha256_hex(body),
            "total_lines": len(text.splitlines()),
        }

    def _repo_read_range(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(
            arguments,
            frozenset({"end_line", "path", "start_line"}),
            "repo.read_range",
        )
        start = _integer(arguments["start_line"], "start_line", 1, 100_000)
        end = _integer(arguments["end_line"], "end_line", start, 100_000)
        if end - start + 1 > 400:
            raise CorpusError("read_range may return at most 400 lines")
        relative, text, body = self._read_text(arguments["path"])
        lines = text.splitlines(keepends=True)
        content = "".join(lines[start - 1 : end])
        if len(content.encode("utf-8")) > MAX_READ_BYTES:
            raise CorpusError("read_range output exceeds the practice limit")
        return {
            "content": content,
            "end_line": min(end, len(lines)),
            "path": relative,
            "sha256": sha256_hex(body),
            "start_line": start,
            "total_lines": len(lines),
        }

    def _repo_apply_patch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(
            arguments,
            frozenset({"expected_sha256", "path", "replacements"}),
            "repo.apply_patch",
        )
        relative, text, body = self._read_text(arguments["path"])
        if relative not in self.case.runtime_policy.editable_paths:
            raise CorpusError(f"path is protected by the practice policy: {relative}")
        expected = _bounded_string(arguments["expected_sha256"], "expected_sha256", 64)
        if not _SHA256.fullmatch(expected):
            raise CorpusError("expected_sha256 must be lowercase SHA-256")
        if sha256_hex(body) != expected:
            raise CorpusError("expected_sha256 does not match the current file")
        raw_replacements = arguments["replacements"]
        if (
            not isinstance(raw_replacements, list)
            or not raw_replacements
            or len(raw_replacements) > MAX_REPLACEMENTS
        ):
            raise CorpusError(
                f"replacements must contain 1 to {MAX_REPLACEMENTS} edits"
            )
        updated = text
        for index, raw in enumerate(raw_replacements):
            if not isinstance(raw, dict) or frozenset(raw) != frozenset(
                {"new_text", "old_text"}
            ):
                raise CorpusError(f"replacements[{index}] fields do not match")
            old = _bounded_string(
                raw.get("old_text"), f"replacements[{index}].old_text", MAX_FILE_BYTES
            )
            new = raw.get("new_text")
            if not isinstance(new, str) or len(new.encode("utf-8")) > MAX_FILE_BYTES:
                raise CorpusError(f"replacements[{index}].new_text is invalid")
            if updated.count(old) != 1:
                raise CorpusError(
                    f"replacements[{index}].old_text must occur exactly once"
                )
            updated = updated.replace(old, new, 1)
        updated_body = updated.encode("utf-8")
        if len(updated_body) > MAX_FILE_BYTES:
            raise CorpusError("patched file exceeds the practice file limit")
        mode = stat.S_IMODE((self._workspace / relative).stat().st_mode)
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{Path(relative).name}.", dir=(self._workspace / relative).parent
        )
        temporary = Path(raw_temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(updated_body)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(mode)
            os.replace(temporary, self._workspace / relative)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "path": relative,
            "replacement_count": len(raw_replacements),
            "sha256": sha256_hex(updated_body),
            "size_bytes": len(updated_body),
        }

    def _repo_create_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(arguments, frozenset({"content", "path"}), "repo.create_file")
        _bounded_string(arguments["path"], "path", 256)
        if not isinstance(arguments["content"], str):
            raise CorpusError("content must be a string")
        raise CorpusError(
            "repo.create_file is reserved but not enabled by practice policy"
        )

    def _repo_delete_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(
            arguments,
            frozenset({"expected_sha256", "path"}),
            "repo.delete_file",
        )
        _bounded_string(arguments["path"], "path", 256)
        _bounded_string(arguments["expected_sha256"], "expected_sha256", 64)
        raise CorpusError(
            "repo.delete_file is reserved but not enabled by practice policy"
        )

    def _assert_workspace_policy(self) -> tuple[dict[str, _FileState], tuple[str, ...]]:
        current = _snapshot(self._workspace)
        paths = _changed_paths(self._base, current)
        unauthorized = sorted(set(paths) - set(self.case.runtime_policy.editable_paths))
        if unauthorized:
            raise CorpusError(
                f"workspace changed protected or undeclared paths: {unauthorized}"
            )
        for path in paths:
            if path not in self._base or path not in current:
                raise CorpusError(f"practice files may not be added or deleted: {path}")
            if self._base[path].mode != current[path].mode:
                raise CorpusError(f"practice file mode changed: {path}")
        return current, paths

    def _tests_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(arguments, frozenset({"command_id"}), "tests.run")
        command_id = _bounded_string(arguments["command_id"], "command_id", 80)
        if command_id not in self.case.runtime_policy.test_command_ids:
            raise CorpusError(f"test command is not allowed: {command_id!r}")
        self._assert_workspace_policy()
        result = run_practice_command(self._workspace, command_id)
        self._assert_workspace_policy()
        return result.as_json()

    def _build_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(arguments, frozenset({"command_id"}), "build.run")
        command_id = _bounded_string(arguments["command_id"], "command_id", 80)
        if command_id not in self.case.runtime_policy.build_command_ids:
            raise CorpusError(f"build command is not allowed: {command_id!r}")
        self._assert_workspace_policy()
        result = run_practice_command(self._workspace, command_id)
        self._assert_workspace_policy()
        return result.as_json()

    def _git_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(arguments, frozenset(), "git.status")
        _, paths = self._assert_workspace_policy()
        return {"changed_paths": list(paths), "clean": not paths}

    def _git_diff(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _exact_arguments(arguments, frozenset(), "git.diff")
        current, paths = self._assert_workspace_policy()
        patch = _unified_diff(self._base, current, paths)
        return {
            "changed_paths": list(paths),
            "patch": patch,
            "patch_sha256": sha256_hex(patch.encode("utf-8")),
        }

    def freeze(self) -> FrozenPracticeSubmission:
        """Revoke mutation and bind the authoritative practice submission."""

        with self._lock:
            return self._freeze_locked()

    def freeze_failure_identity(self) -> FailedWorkspaceIdentity:
        """Revoke a failed workspace and retain only bounded identity evidence."""

        with self._lock:
            if self._closed:
                raise CorpusError("practice workspace is closed")
            self._frozen = True
            try:
                current = _snapshot(self._workspace)
                paths = _changed_paths(self._base, current)
                final_tree = _tree_sha256(current)
                changed_root = sha256_hex(canonical_json_bytes(list(paths)))
            except CorpusError:
                final_tree = "0" * 64
                changed_root = "0" * 64
            return FailedWorkspaceIdentity(
                task_id=self.case.task_id,
                base_tree_sha256=self._base_tree_sha256,
                final_tree_sha256=final_tree,
                changed_path_root=changed_root,
                authoring_event_root=self._event_root,
            )

    def _freeze_locked(self) -> FrozenPracticeSubmission:
        if self._closed:
            raise CorpusError("practice workspace is closed")
        if self._frozen:
            raise CorpusError("practice workspace is already frozen")
        current, paths = self._assert_workspace_policy()
        patch = _unified_diff(self._base, current, paths)
        changes: list[FrozenFileChange] = []
        for path in paths:
            before = self._base[path]
            after = current[path]
            try:
                after_content = after.body.decode("utf-8")
            except UnicodeDecodeError as error:
                raise CorpusError(f"changed file is not UTF-8: {path}") from error
            changes.append(
                FrozenFileChange(
                    path=path,
                    before_sha256=sha256_hex(before.body),
                    after_sha256=sha256_hex(after.body),
                    after_content=after_content,
                )
            )
        self._frozen = True
        return FrozenPracticeSubmission(
            task_id=self.case.task_id,
            base_tree_sha256=self._base_tree_sha256,
            final_tree_sha256=_tree_sha256(current),
            patch_sha256=sha256_hex(patch.encode("utf-8")),
            changed_path_root=sha256_hex(canonical_json_bytes(list(paths))),
            authoring_event_root=self._event_root,
            changed_paths=paths,
            changes=tuple(changes),
            patch=patch,
        )
