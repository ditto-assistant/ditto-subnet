#!/usr/bin/env python3
"""Generate the pinned starter-kit file and Rust-function provenance index."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import cast

import tree_sitter_rust
from tree_sitter import Language, Parser

RUST_LANGUAGE = Language(tree_sitter_rust.language())
ORIGIN = "ditto-assistant/dittobench-starter-kit"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--starter-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tracked_files(root: Path) -> list[Path]:
    """Return only Git-tracked regular files from the canonical starter tree."""
    raw = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    files = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        path = root / item.decode("utf-8")
        if path.is_file() and not path.is_symlink():
            files.append(path)
    return files


def _rust_functions(path: Path, relative: str) -> list[dict[str, object]]:
    raw = path.read_bytes()
    tree = Parser(RUST_LANGUAGE).parse(raw)
    if tree.root_node.has_error:
        raise ValueError(f"canonical starter Rust parse failed: {relative}")
    functions = []
    stack = [tree.root_node]
    ordinals: defaultdict[str, int] = defaultdict(int)
    while stack:
        node = stack.pop()
        if node.type == "function_item":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                raise ValueError(f"unnamed canonical Rust function: {relative}")
            name = raw[name_node.start_byte : name_node.end_byte].decode("utf-8")
            ordinal = ordinals[name]
            ordinals[name] += 1
            functions.append(
                {
                    "path": relative,
                    "name": name,
                    "ordinal": ordinal,
                    "sha256": hashlib.sha256(
                        raw[node.start_byte : node.end_byte]
                    ).hexdigest(),
                }
            )
        stack.extend(reversed(node.named_children))
    return functions


def main() -> int:
    args = _arguments()
    root = args.starter_dir.resolve()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if len(revision) != 40:
        raise ValueError("starter revision is not a full Git SHA")
    files: dict[str, str] = {}
    functions: list[dict[str, object]] = []
    for path in _tracked_files(root):
        relative = path.relative_to(root).as_posix()
        files[relative] = _sha256(path)
        if path.suffix == ".rs":
            functions.extend(_rust_functions(path, relative))
    payload = {
        "files": files,
        "origin": ORIGIN,
        "revision": revision,
        "rust_functions": sorted(
            functions,
            key=lambda item: (
                str(item["path"]),
                str(item["name"]),
                cast(int, item["ordinal"]),
            ),
        ),
        "version": 2,
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
