"""Reproduce the public v0.330.3 starter source-control archive.

Only the released tree is read. The exclusions match the starter's ``submit``
command; no checkout state, generated files, credentials, or symlinks enter the
archive. File bytes and executable bits remain those of the release tree.
"""

from __future__ import annotations

import argparse
import gzip
import io
import subprocess
import tarfile
from pathlib import Path

RELEASE = "v0.330.3"
COMMIT = "b20b065b133ecbe1a8e0e9c350169bfcb19e4be2"
TREE = "733904ee190b9a11e7d5023f83559cb48bac0d01"
SUBDIR = "miners/dittobench-starter-kit"
EXCLUDED_ROOTS = {".agents", ".claude", ".git", "target"}


def _included(name: str) -> bool:
    path = Path(name.removeprefix("./"))
    if not path.parts or path.parts[0] in EXCLUDED_ROOTS:
        return False
    return not any(
        part == ".env"
        or part.startswith(".env.")
        or part.endswith((".tgz", ".db"))
        or ".db-" in part
        for part in path.parts
    )


def package() -> bytes:
    actual = (
        subprocess.check_output(["git", "rev-parse", f"{COMMIT}:{SUBDIR}"])
        .decode()
        .strip()
    )
    if actual != TREE:
        raise ValueError(f"release source tree changed: {actual}")
    source = subprocess.check_output(["git", "archive", "--format=tar", TREE])
    output = io.BytesIO()
    with (
        gzip.GzipFile(
            fileobj=output, mode="wb", compresslevel=9, mtime=0, filename=""
        ) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.GNU_FORMAT) as archive,
        tarfile.open(fileobj=io.BytesIO(source), mode="r:") as original,
    ):
        for member in original:
            if not _included(member.name):
                continue
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"unexpected link or special file: {member.name}")
            member.uid = member.gid = member.mtime = 0
            member.uname = member.gname = ""
            member.mode = (
                0o755
                if member.isfile() and member.mode & 0o111
                else (0o755 if member.isdir() else 0o644)
            )
            archive.addfile(
                member,
                original.extractfile(member) if member.isfile() else None,
            )
    return output.getvalue()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_bytes(package())
