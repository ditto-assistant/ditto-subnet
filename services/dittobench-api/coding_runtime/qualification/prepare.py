"""Prepare private base/reference control plans from the staged curator corpus.

This is compatibility tooling, not API approval. Rust and Go inventories are
derived from pristine snapshots, never patched candidate source or test imports.
Node expected counts come from a separately supplied source-hash-bound inventory.
"""

import argparse
import ast
import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path


def need(value):
    if not value:
        raise ValueError("private control plan rejected")


def sha(body):
    return hashlib.sha256(body).hexdigest()


def read(path):
    need(path.resolve() == path and path.is_file() and path.stat().st_size <= 8 << 20)
    return path.read_bytes()


def files(root):
    result = []
    for path in sorted(root.rglob("*")):
        need(not path.is_symlink())
        if path.is_file():
            result.append(path)
        else:
            need(path.is_dir())
    need(len(result) <= 2048)
    return result


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def invoke(arguments):
    result = subprocess.run(arguments, capture_output=True, timeout=15)
    need(result.returncode == 0 and not result.stderr and len(result.stdout) <= 1 << 20)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--rust-inventory", type=Path, required=True)
    parser.add_argument("--go-helper", type=Path, required=True)
    parser.add_argument("--node-counts", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    need(
        args.corpus.is_absolute()
        and args.corpus.resolve() == args.corpus
        and not args.corpus.stat().st_mode & 0o077
    )
    node_inventory = json.loads(read(args.node_counts))
    node_counts = {
        (row["group"], row["phase"], row["suite_sha256"]): row["tests"]
        for row in node_inventory["records"]
        if row["admitted"]
    }
    cases = []

    def entry(source, target):
        return {
            "path": target,
            "source": str(source.relative_to(args.corpus)),
            "sha256": sha(read(source)),
        }

    for group in sorted((args.corpus / "groups").iterdir()):
        need(group.is_dir() and not group.is_symlink())
        snapshot = group / "snapshot/workspace"
        original = files(snapshot)
        if (snapshot / "Cargo.toml").exists():
            language, suffixes = "rust", (".rs",)
        elif (snapshot / "go.mod").exists():
            language, suffixes = "go", (".go",)
        elif any(p.suffix in (".ts", ".js", ".mjs", ".cjs") for p in original):
            language, suffixes = "node", (".ts", ".js", ".mjs", ".cjs")
        else:
            language, suffixes = "python", (".py",)

        def is_test(path, root=snapshot):
            return (
                "tests" in path.relative_to(root).parts
                or path.name.startswith("test_")
                or path.name.endswith("_test.go")
                or ".test." in path.name
                or ".spec." in path.name
            )

        visible = [p for p in original if p.suffix in suffixes and is_test(p)]
        source = [p for p in original if p not in visible]
        modules = [str(p.relative_to(snapshot)) for p in source if p.suffix in suffixes]
        hidden = [p for p in files(group / "grader") if p.suffix in suffixes]
        need(len(visible) == len(hidden) == 1 and modules)
        rust = None
        if language == "rust":
            rust = invoke([str(args.rust_inventory), "--snapshot", str(snapshot)])
            need(rust["production_api_approval"] is False)
        for phase, suite in (("visible", visible[0]), ("hidden", hidden[0])):
            relative = str(
                suite.relative_to(snapshot if phase == "visible" else group / "grader")
            )
            body = read(suite)
            argv = ["dittobench-test-driver", "--group", phase]
            authority = None
            if language == "python":
                expected = sum(
                    isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
                    for node in ast.parse(body).body
                )
                names = []
                for module in modules:
                    parts = Path(module).with_suffix("").parts
                    name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
                    if name:
                        names.append(name)
                argv += ["--suite", relative]
                for name in sorted(names):
                    argv += ["--module", name]
                argv += ["--candidate-timeout-ms", "5000"]
            elif language == "node":
                expected = node_counts[(group.name, phase, sha(body))]
                argv += ["--suite", relative]
                for module in sorted(modules):
                    argv += ["--module", module]
                argv += ["--candidate-timeout-ms", "5000"]
            elif language == "go":
                info = invoke(
                    [str(args.go_helper), "--inspect-go", str(snapshot), str(suite)]
                )
                expected = info["expected_total"]
                argv += ["--suite", relative, "--package-path", info["module_path"]]
                for name in info["functions"]:
                    argv += ["--function", name]
                if phase == "hidden":
                    for file in visible:
                        argv += ["--support", str(file.relative_to(snapshot))]
                argv += [
                    "--candidate-timeout-ms",
                    "5000",
                    "--build-timeout-ms",
                    "120000",
                ]
            else:
                expected = invoke(
                    [str(args.rust_inventory), "--count-suite", str(suite)]
                )
                authority = encode(
                    {
                        "schema": "dittobench-coding-rust-authority-v1",
                        "group": phase,
                        "crate_name": rust["crate_name"],
                        "suite": relative,
                        "suite_sha256": sha(body),
                        "functions": rust["functions"],
                        "files": [f["path"] for f in rust["files"]],
                        "build_timeout_ms": 120000,
                        "candidate_timeout_ms": 5000,
                    }
                )
                argv += [
                    "--authority",
                    "rust/control.json",
                    "--authority-sha256",
                    sha(authority),
                ]
            need(type(expected) is int and 0 < expected <= 1024)
            for role in ("base", "reference"):
                workspace = []
                for file in source:
                    name = str(file.relative_to(snapshot))
                    selected = (
                        file
                        if role == "base"
                        else group / "curator/gold-workspace" / name
                    )
                    workspace.append(entry(selected, name))
                workspace += [entry(p, str(p.relative_to(snapshot))) for p in visible]
                grader = [
                    entry(p, str(p.relative_to(group / "grader"))) for p in hidden
                ]
                case = {
                    "schema": "dittobench-private-compatibility-case-v1",
                    "language": language,
                    "group_id": group.name,
                    "role": role,
                    "phase": phase,
                    "expected_total": expected,
                    "argv": argv,
                    "workspace": workspace,
                    "grader": grader,
                    "image_sha256": "0" * 64,
                }
                if authority is not None:
                    grader.append(
                        {
                            "path": "rust/control.json",
                            "inline": base64.b64encode(authority).decode(),
                            "sha256": sha(authority),
                        }
                    )
                    case["rust_files"] = [f["path"] for f in rust["files"]]
                cases.append(case)
    need({c["language"] for c in cases} == {"python", "node", "go", "rust"})
    plan = {
        "schema": "dittobench-private-compatibility-plan-v1",
        "source_sha": args.source_sha,
        "replicates": 2,
        "cases": cases,
        "production_api_approval": False,
        "node_count_inventory_sha256": sha(read(args.node_counts)),
        "preparer_sha256": sha(Path(__file__).read_bytes()),
    }
    with args.output.open("xb") as output:
        output.write(encode(plan) + b"\n")
        output.flush()
        os.fsync(output.fileno())
    print(f"Prepared {len(cases)} private compatibility cases; no production approval.")


if __name__ == "__main__":
    main()
