#!/usr/bin/env python3
"""Generate a staged starter-kit provenance manifest from the monorepo kit.

Source review exempts a submitted file from parts of its scrutiny only on an
exact path and SHA-256 match against a runtime manifest,
``ditto_screener/data/starter-kit-provenance-v<N>.json``. The static preflight,
the L1 provenance block, and the L2 analyzer image all load that set, so adding
a manifest to it is an admission-relevant trust expansion.

This script therefore never writes into the runtime set. After a
``miners/dittobench-starter-kit`` change is committed, stage the next manifest
in ``workers/screener/staged-starter-provenance/`` (``regenerate_command``
prints the exact line) and commit it with the kit change. No runtime path reads
the staged directory, and neither screener image contains it. A staged manifest
is trusted only after a separate, reviewed activation change moves it, unrenamed,
into ``ditto_screener/data/`` (see the screener README).

Never edit or delete a published manifest: older honest derivatives must keep
matching exactly. ``tests/test_starter_provenance_current.py`` fails until the
newest manifest, staged or active, equals the kit's tracked, submittable
regular files.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

ORIGIN = "ditto-assistant/ditto-subnet/miners/dittobench-starter-kit"
MANIFEST_VERSION = 1
MANIFEST_GLOB = "starter-kit-provenance-v*.json"
STARTER_DIR = "miners/dittobench-starter-kit"
RUNTIME_MANIFEST_DIR = "workers/screener/ditto_screener/data"
STAGED_MANIFEST_DIR = "workers/screener/staged-starter-provenance"
_SCREENER_ROOT = Path(__file__).resolve().parents[1]
# Loaded by every runtime trust consumer; this script never writes here.
RUNTIME_MANIFESTS = _SCREENER_ROOT / "ditto_screener" / "data"
# Outside the ``ditto_screener`` package, so no image or runtime glob sees it.
STAGED_MANIFESTS = _SCREENER_ROOT / "staged-starter-provenance"

_MANIFEST_NAME = re.compile(r"starter-kit-provenance-v([1-9][0-9]*)\.json")
# Git index modes for regular files. Symlinks (120000) and submodules (160000)
# are never trusted: the tracked ``.agents``/``.claude`` skill links point
# outside the kit, and a submission never carries them as regular files.
_REGULAR_FILE_MODES = frozenset({"100644", "100755"})
# Mirror the starter ``submit`` archive excludes (#2395) exactly, so a manifest
# never trusts a file an honest submission does not contain, and never drops a
# file it does contain (``*.tar`` is packaged, so it stays). ``.env.*`` also
# drops the committed ``.env.example`` template: ``submit`` never packages it.
# Like ``tar --exclude``, every path component is checked.
_EXCLUDED_COMPONENTS = frozenset({".agents", ".claude", ".git", "target"})
_EXCLUDED_PATTERNS = (".env", ".env.*", "*.db", "*.db-*", "*.tgz")


def is_submittable(relative: str) -> bool:
    """Return whether a kit-relative path can appear in an honest submission."""
    for part in PurePosixPath(relative).parts:
        if part in _EXCLUDED_COMPONENTS or any(
            fnmatch.fnmatchcase(part, pattern) for pattern in _EXCLUDED_PATTERNS
        ):
            return False
    return True


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True
        ).stdout
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"starter provenance needs a Git checkout of {root}: {detail}"
        ) from error
    except OSError as error:
        raise RuntimeError(f"starter provenance needs Git: {error}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def starter_files(root: Path) -> dict[str, str]:
    """Map each tracked, submittable regular kit file to its working-tree sha256.

    Only Git-tracked paths are considered, so untracked build output such as a
    local ``target/`` directory can never enter or break the manifest. A
    tracked path that is missing or no longer a regular file in the working
    tree is left out, which the drift guard reports as removed.
    """
    files: dict[str, str] = {}
    for record in _git(root, "ls-files", "--stage", "-z", "--", ".").split(b"\0"):
        if not record:
            continue
        metadata, _, raw_path = record.partition(b"\t")
        mode, _object, stage = metadata.decode("ascii").split(" ")
        if stage != "0":
            raise RuntimeError("resolve starter-kit merge conflicts first")
        relative = raw_path.decode("utf-8")
        if mode not in _REGULAR_FILE_MODES or not is_submittable(relative):
            continue
        path = root / relative
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
        except FileNotFoundError:
            continue
        files[relative] = _sha256(path)
    return dict(sorted(files.items()))


def starter_revision(root: Path) -> str:
    """Return the last commit that touched the kit, refusing uncommitted edits.

    Pinning the kit's own last commit instead of repository ``HEAD`` keeps a
    rerun byte-identical until the kit itself changes.
    """
    if _git(root, "status", "--porcelain", "--untracked-files=no", "--", "."):
        raise RuntimeError("commit starter-kit changes before generating provenance")
    revision = _git(root, "log", "-1", "--format=%H", "--", ".").decode().strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise RuntimeError("starter revision is not a full Git SHA")
    return revision


def build_manifest(root: Path) -> dict[str, object]:
    return {
        "files": starter_files(root),
        "origin": ORIGIN,
        "revision": starter_revision(root),
        "version": MANIFEST_VERSION,
    }


def render_manifest(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def manifest_number(path: Path) -> int:
    match = _MANIFEST_NAME.fullmatch(path.name)
    if match is None:
        raise ValueError(f"unexpected starter manifest name: {path.name}")
    return int(match.group(1))


def manifests_in(*directories: Path) -> list[Path]:
    """Every manifest in ``directories``, ordered by version number."""
    return sorted(
        (path for directory in directories for path in directory.glob(MANIFEST_GLOB)),
        key=manifest_number,
    )


def newest_manifest(*directories: Path) -> Path:
    """Return the highest-numbered manifest; numeric, so v10 follows v9.

    Staged and runtime manifests share one version sequence, because
    activation moves a staged file into the runtime set without renaming it.
    """
    manifests = manifests_in(*directories)
    if not manifests:
        raise FileNotFoundError("no starter provenance manifest found")
    return manifests[-1]


def regenerate_command(version: int) -> str:
    return (
        "python workers/screener/scripts/generate_starter_provenance.py "
        f"--starter-dir {STARTER_DIR} "
        f"--output {STAGED_MANIFEST_DIR}/starter-kit-provenance-v{version}.json"
    )


def runtime_write_refusal(output: Path) -> str | None:
    """Why ``output`` must not be written, or None when staging it is safe.

    The generator only stages. Writing into the runtime set, or reusing an
    active version number, would expand trust without the activation review.
    """
    runtime = RUNTIME_MANIFESTS.resolve()
    # Anywhere in the package ships in the screener image; data/ itself is
    # the runtime-loaded trust set.
    if output.resolve().is_relative_to(runtime.parent):
        return (
            f"refusing to write {output}: {RUNTIME_MANIFEST_DIR} is the "
            f"runtime-loaded trust set. Stage the manifest in {STAGED_MANIFEST_DIR} "
            "and activate it in a separate reviewed change."
        )
    if _MANIFEST_NAME.fullmatch(output.name) and (runtime / output.name).exists():
        return (
            f"refusing to write {output}: {output.name} is already an active "
            "manifest; write the next manifest version"
        )
    return None


def manifest_drift(
    expected: dict[str, str], actual: dict[str, str]
) -> dict[str, list[str]]:
    """Exact path+digest delta between a manifest and the kit; empty if equal."""
    drift = {
        "modified": sorted(
            path
            for path in expected.keys() & actual.keys()
            if expected[path] != actual[path]
        ),
        "added": sorted(actual.keys() - expected.keys()),
        "removed": sorted(expected.keys() - actual.keys()),
    }
    return {kind: paths for kind, paths in drift.items() if paths}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a starter-kit provenance manifest."
    )
    parser.add_argument("--starter-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    refusal = runtime_write_refusal(args.output)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1
    rendered = render_manifest(build_manifest(args.starter_dir.resolve()))
    if args.output.exists() and args.output.read_text() != rendered:
        # A published manifest is an append-only trust anchor for older
        # derivatives; a kit change always gets the next version number.
        print(
            f"refusing to rewrite {args.output}; write the next manifest version",
            file=sys.stderr,
        )
        return 1
    directory = args.output.resolve().parent
    if directory == STAGED_MANIFESTS.resolve():
        # Git drops the staging directory once activation moves its last
        # manifest, so the printed regenerate command recreates it.
        directory.mkdir(parents=True, exist_ok=True)
    elif not directory.is_dir():
        print(
            f"refusing to write {args.output}: {directory} does not exist",
            file=sys.stderr,
        )
        return 1
    args.output.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
