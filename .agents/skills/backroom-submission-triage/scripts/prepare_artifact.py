#!/usr/bin/env python3
"""Download, digest-verify, and safely extract one Backroom source tarball."""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

MAX_MEMBERS = 10_000
MAX_EXPANDED_BYTES = 128 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Short-lived Backroom artifact URL")
    source.add_argument("--file", type=Path, help="Existing local tarball")
    parser.add_argument("--sha256", required=True, help="Expected lowercase SHA-256")
    parser.add_argument("--output-dir", type=Path, help="New directory to create")
    return parser.parse_args()


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def validate_member(member: tarfile.TarInfo) -> PurePosixPath:
    path = PurePosixPath(member.name)
    if member.isdir() and member.name in {".", "./"}:
        return PurePosixPath()
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise SystemExit(f"unsafe archive path: {member.name!r}")
    if not (member.isdir() or member.isfile()):
        raise SystemExit(f"links and special files are not allowed: {member.name!r}")
    return path


def main() -> None:
    args = parse_args()
    expected = args.sha256.lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise SystemExit("--sha256 must be 64 hexadecimal characters")

    output = args.output_dir or Path(tempfile.mkdtemp(prefix="backroom-submission-"))
    if args.output_dir is not None:
        output.mkdir(parents=True, exist_ok=False)

    def cleanup_incomplete_output() -> None:
        shutil.rmtree(output, ignore_errors=True)

    atexit.register(cleanup_incomplete_output)
    tar_path = output / "agent.tar.gz"
    if args.file is not None:
        shutil.copyfile(args.file, tar_path)
    else:
        request = urllib.request.Request(
            args.url,
            headers={"User-Agent": "sn118-backroom-review"},
        )
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            tar_path.open("wb") as dest,
        ):
            shutil.copyfileobj(response, dest)

    actual = digest(tar_path)
    if actual != expected:
        raise SystemExit(
            f"artifact SHA-256 mismatch: expected {expected}, got {actual}"
        )

    source_dir = output / "source"
    source_dir.mkdir()
    file_count = 0
    expanded_bytes = 0
    paths: list[str] = []
    with tarfile.open(tar_path, mode="r:*") as archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise SystemExit(f"archive has too many members: {len(members)}")
        for member in members:
            relative = validate_member(member)
            expanded_bytes += member.size
            if expanded_bytes > MAX_EXPANDED_BYTES:
                raise SystemExit("archive exceeds the 128 MiB expanded-size limit")
            target = source_dir.joinpath(*relative.parts)
            if member.isdir():
                if relative.parts:
                    target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SystemExit(f"unable to read archive member: {member.name}")
            with extracted, target.open("wb") as destination:
                shutil.copyfileobj(extracted, destination)
            file_count += 1
            paths.append(member.name)

    print(
        json.dumps(
            {
                "artifact_sha256": actual,
                "tar_path": str(tar_path),
                "source_path": str(source_dir),
                "file_count": file_count,
                "expanded_bytes": expanded_bytes,
                "paths": paths,
            },
            indent=2,
        )
    )
    atexit.unregister(cleanup_incomplete_output)


if __name__ == "__main__":
    main()
