#!/usr/local/bin/python3 -I
"""Trusted restricted-AST oracle; never imports candidate code into this process."""

import argparse
import ast
import json
import math
import os
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dittobench_wire import pack, unpack

MAX_MESSAGE = 65536
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


class InvalidSuite(ValueError):
    pass


class CandidateFailure(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    module: str | None
    reference: int | None
    path: tuple[str, ...]


def read_suite(root, relative):
    if (
        not relative
        or Path(relative).is_absolute()
        or any(p in {"", ".", ".."} for p in relative.split("/"))
    ):
        raise InvalidSuite()
    path = root / relative
    if path.resolve() != path or not path.is_relative_to(root):
        raise InvalidSuite()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 512 << 10:
            raise InvalidSuite()
        return source.read((512 << 10) + 1).decode("utf-8")


def compile_suite(source, candidate_modules):
    if not candidate_modules or any(
        not NAME.fullmatch(name)
        or name in sys.stdlib_module_names
        or name in {"pytest", "pluggy", "packaging", "iniconfig", "pygments"}
        for name in candidate_modules
    ):
        raise InvalidSuite()
    tree = ast.parse(source)
    symbols, tests = {}, []
    allowed = (
        ast.Module,
        ast.ImportFrom,
        ast.alias,
        ast.FunctionDef,
        ast.arguments,
        ast.Assign,
        ast.Name,
        ast.Store,
        ast.Load,
        ast.Call,
        ast.Attribute,
        ast.Constant,
        ast.List,
        ast.Dict,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Assert,
        ast.Expr,
        ast.keyword,
        ast.UnaryOp,
        ast.USub,
    )
    if any(not isinstance(node, allowed) for node in ast.walk(tree)):
        raise InvalidSuite()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module not in candidate_modules:
                raise InvalidSuite()
            for item in node.names:
                local = item.asname or item.name
                if (
                    not NAME.fullmatch(item.name)
                    or not NAME.fullmatch(local)
                    or local in symbols
                ):
                    raise InvalidSuite()
                symbols[local] = Target(node.module, None, (item.name,))
        elif isinstance(node, ast.FunctionDef):
            if (
                not node.name.startswith("test_")
                or node.decorator_list
                or (
                    node.returns is not None
                    and not (
                        isinstance(node.returns, ast.Constant)
                        and node.returns.value is None
                    )
                )
                or node.args.args
                or node.args.posonlyargs
                or node.args.kwonlyargs
                or node.args.vararg
                or node.args.kwarg
                or node.args.defaults
                or node.args.kw_defaults
                or getattr(node, "type_params", [])
                or not any(isinstance(n, ast.Assert) for n in node.body)
                or not any(isinstance(n, ast.Call) for n in ast.walk(node))
            ):
                raise InvalidSuite()
            for statement in node.body:
                if not isinstance(statement, (ast.Assign, ast.Expr, ast.Assert)):
                    raise InvalidSuite()
                if isinstance(statement, ast.Assign) and (
                    len(statement.targets) != 1
                    or not isinstance(statement.targets[0], ast.Name)
                ):
                    raise InvalidSuite()
            tests.append(node)
        elif not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is str
        ):
            raise InvalidSuite()
    if (
        not symbols
        or not 1 <= len(tests) <= 1000
        or len({n.name for n in tests}) != len(tests)
    ):
        raise InvalidSuite()
    if any(test.name in symbols for test in tests):
        raise InvalidSuite()
    for test in tests:
        # Python assignment makes a name local for the entire function. Do not
        # accidentally resolve an unbound local to an imported remote symbol.
        assigned = {n.targets[0].id for n in test.body if isinstance(n, ast.Assign)}
        known = set(symbols) - assigned
        for statement in test.body:
            expression = (
                statement.value
                if isinstance(statement, (ast.Assign, ast.Expr))
                else statement.test
            )
            if any(
                isinstance(n, ast.Name)
                and isinstance(n.ctx, ast.Load)
                and n.id not in known
                for n in ast.walk(expression)
            ):
                raise InvalidSuite()
            if isinstance(statement, ast.Assign):
                known.add(statement.targets[0].id)
    # No dangerous attributes, splats, non-JSON literals, or complex comparisons.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and node.msg is not None:
            raise InvalidSuite()
        if isinstance(node, ast.Call) and len({k.arg for k in node.keywords}) != len(
            node.keywords
        ):
            raise InvalidSuite()
        if isinstance(node, ast.Attribute) and not NAME.fullmatch(node.attr):
            raise InvalidSuite()
        if isinstance(node, ast.keyword) and (
            node.arg is None or not NAME.fullmatch(node.arg)
        ):
            raise InvalidSuite()
        if isinstance(node, ast.Constant) and (
            type(node.value) not in (str, bytes, int, float, bool, type(None))
            or type(node.value) is float
            and not math.isfinite(node.value)
        ):
            raise InvalidSuite()
        if isinstance(node, ast.Compare) and len(node.ops) != 1:
            raise InvalidSuite()
        if isinstance(node, ast.Dict):
            if any(
                not isinstance(k, ast.Constant) or type(k.value) is not str
                for k in node.keys
            ):
                raise InvalidSuite()
            if len({k.value for k in node.keys}) != len(node.keys):
                raise InvalidSuite()
    return symbols, tests


def unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise CandidateFailure()
        result[key] = value
    return result


class Child:
    def __init__(self, uid, gid, timeout):
        self.deadline = time.monotonic() + timeout
        self.sequence = 0
        self.process = subprocess.Popen(
            ["/usr/local/bin/python3", "-I", "/usr/local/lib/dittobench-child.py"],
            cwd="/workspace",
            env={"PATH": "/usr/local/bin:/usr/bin"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            user=uid,
            group=gid,
            extra_groups=[],
            close_fds=True,
        )
        os.set_blocking(self.process.stdout.fileno(), False)
        os.set_blocking(self.process.stdin.fileno(), False)
        try:
            marker = b"DITTO-PYTHON-CHILD-READY-V1\n"
            ready = bytearray()
            while len(ready) < len(marker):
                self.wait(self.process.stdout, selectors.EVENT_READ)
                chunk = os.read(self.process.stdout.fileno(), len(marker) - len(ready))
                if not chunk:
                    raise RuntimeError("child initialization failed")
                ready.extend(chunk)
            if ready != marker:
                raise RuntimeError("child initialization failed")
        except Exception:
            self.close()
            # No candidate module has run yet. This is infrastructure failure,
            # not a failing candidate test and not an authoritative report.
            raise RuntimeError("child initialization failed") from None

    def wait(self, stream, event):
        with selectors.DefaultSelector() as selector:
            selector.register(stream, event)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise CandidateFailure()

    def rpc(self, target, operation, args=None, kwargs=None):
        try:
            encoded_args, encoded_kwargs = pack(args or []), pack(kwargs or {})
        except ValueError:
            raise InvalidSuite() from None
        self.sequence += 1
        nonce = secrets.token_hex(16)
        identity = (
            {"module": target.module}
            if target.module
            else {"reference": target.reference}
        )
        identity["path"] = list(target.path)
        raw = (
            json.dumps(
                {
                    "id": nonce,
                    "target": identity,
                    "operation": operation,
                    "args": encoded_args,
                    "kwargs": encoded_kwargs,
                },
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if len(raw) > MAX_MESSAGE:
            raise CandidateFailure()
        view = memoryview(raw)
        while view:
            self.wait(self.process.stdin, selectors.EVENT_WRITE)
            written = os.write(self.process.stdin.fileno(), view)
            view = view[written:]
        output = bytearray()
        while not output.endswith(b"\n"):
            self.wait(self.process.stdout, selectors.EVENT_READ)
            chunk = os.read(self.process.stdout.fileno(), MAX_MESSAGE + 1 - len(output))
            if not chunk:
                raise CandidateFailure()
            output.extend(chunk)
            if len(output) > MAX_MESSAGE or b"\n" in output[:-1]:
                raise CandidateFailure()
        response = json.loads(
            output,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(CandidateFailure()),
        )
        if (
            type(response) is not dict
            or set(response) != {"id", "result"}
            or type(response["id"]) is not str
            or response["id"] != nonce
        ):
            raise CandidateFailure()
        result = response["result"]
        if type(result) is not dict or set(result) != {"kind", "value"}:
            raise CandidateFailure()
        if result["kind"] == "data":
            return unpack(result["value"])
        if (
            result["kind"] == "reference"
            and type(result["value"]) is int
            and 1 <= result["value"] <= 10000
        ):
            return Target(None, result["value"], ())
        raise CandidateFailure()

    def close(self):
        # Child seccomp prevents new processes and process-group escape. Reap it
        # before the next test; outer supervisor/container cleanup is independent.
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()


def evaluate(node, names, child, *, target=False):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        value = names[node.id]
    elif isinstance(node, ast.Attribute):
        base = evaluate(node.value, names, child, target=True)
        if not isinstance(base, Target):
            raise CandidateFailure()
        value = Target(base.module, base.reference, (*base.path, node.attr))
    elif isinstance(node, ast.List):
        return [evaluate(n, names, child) for n in node.elts]
    elif isinstance(node, ast.Dict):
        keys = [evaluate(n, names, child) for n in node.keys]
        if any(type(k) is not str for k in keys) or len(set(keys)) != len(keys):
            raise InvalidSuite()
        return dict(
            zip(keys, (evaluate(n, names, child) for n in node.values), strict=True)
        )
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = evaluate(node.operand, names, child)
        if type(value) not in (int, float):
            raise CandidateFailure()
        return -value
    elif isinstance(node, ast.Call):
        function = evaluate(node.func, names, child, target=True)
        if not isinstance(function, Target):
            raise InvalidSuite()
        args = [evaluate(n, names, child) for n in node.args]
        kwargs = {k.arg: evaluate(k.value, names, child) for k in node.keywords}
        return child.rpc(function, "call", args, kwargs)
    elif isinstance(node, ast.Compare):
        left = evaluate(node.left, names, child)
        right = evaluate(node.comparators[0], names, child)
        if isinstance(left, Target) or isinstance(right, Target):
            raise CandidateFailure()
        return left == right if isinstance(node.ops[0], ast.Eq) else left != right
    else:
        raise InvalidSuite()
    if isinstance(value, Target) and value.path and not target:
        return child.rpc(value, "get")
    return value


def run_suite(symbols, tests, uid, gid, timeout):
    passed = 0
    for test in tests:
        child = Child(uid, gid, timeout)
        names = dict(symbols)
        try:
            for statement in test.body:
                if isinstance(statement, ast.Assign):
                    names[statement.targets[0].id] = evaluate(
                        statement.value, names, child
                    )
                elif isinstance(statement, ast.Expr):
                    evaluate(statement.value, names, child)
                elif isinstance(statement, ast.Assert):
                    result = evaluate(statement.test, names, child)
                    if isinstance(result, Target) or not result:
                        raise CandidateFailure()
            passed += 1
        except InvalidSuite:
            raise
        except (
            CandidateFailure,
            BrokenPipeError,
            ConnectionError,
            ValueError,
            KeyError,
            TypeError,
        ):
            pass
        finally:
            child.close()
    return passed


def main():
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--group", choices=("visible", "hidden"), required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--module", action="append", required=True)
    parser.add_argument("--candidate-timeout-ms", type=int, required=True)
    for name in ("report", "nonce", "expected", "candidate-uid", "candidate-gid"):
        parser.add_argument("--dittobench-" + name, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not sys.flags.isolated:
        raise InvalidSuite()
    uid, gid, total = (
        int(args.dittobench_candidate_uid),
        int(args.dittobench_candidate_gid),
        int(args.dittobench_expected),
    )
    if (
        not 1 <= uid <= 0xFFFFFFFF
        or not 1 <= gid <= 0xFFFFFFFF
        or not 1 <= args.candidate_timeout_ms <= 300000
        or not re.fullmatch(r"[0-9a-f]{48}", args.dittobench_nonce)
    ):
        raise InvalidSuite()
    report = Path(args.dittobench_report)
    if str(report) != "/run/dittobench-control/test-report.json":
        raise InvalidSuite()
    for root in (Path("/run/dittobench-control"), Path("/run/dittobench-grader")):
        info = root.lstat()
        if (
            root.resolve() != root
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise InvalidSuite()
    root = (
        Path("/workspace")
        if args.group == "visible"
        else Path("/run/dittobench-grader")
    )
    symbols, tests = compile_suite(read_suite(root, args.suite), frozenset(args.module))
    if len(tests) != total:
        raise InvalidSuite()
    passed = run_suite(symbols, tests, uid, gid, args.candidate_timeout_ms / 1000)
    with os.fdopen(
        os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
        "w",
    ) as output:
        json.dump(
            {
                "schema": "dittobench-coding-trusted-test-report-v1",
                "nonce": args.dittobench_nonce,
                "passed": passed,
                "total": total,
                "completed": True,
            },
            output,
        )
        output.flush()
        os.fsync(output.fileno())
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(70) from None
