#!/usr/bin/env python3
"""Generate the pinned starter-kit file provenance index."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ORIGIN = "ditto-assistant/ditto-subnet/miners/dittobench-starter-kit"


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
    for path in _tracked_files(root):
        relative = path.relative_to(root).as_posix()
        files[relative] = _sha256(path)
    payload = {
        "files": files,
        "origin": ORIGIN,
        "revision": revision,
        "version": 1,
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
