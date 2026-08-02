"""Build reference-shingle bundles from an official starter-kit clone.

Usage::

    uv run python scripts/build_reference_fingerprints.py /path/to/starter-kit

The clone must contain the complete official history.  Output is deterministic:
three sorted big-endian uint64 streams plus a manifest that pins every included
commit.  Generated bundles are committed with the fingerprint algorithm so upload
processing never needs network access.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

OUTPUT = Path(__file__).parents[1] / "ditto" / "anticopy"
FINGERPRINT_MODULE = (
    Path(__file__).parents[1] / "ditto" / "api_server" / "fingerprint.py"
)


def _load_fingerprint_module() -> ModuleType:
    """Load the pure fingerprint helpers without importing the API package.

    ``ditto.api_server.__init__`` constructs the FastAPI application and therefore
    needs the platform runtime dependencies.  Reference refresh is intentionally a
    lightweight stdlib-only workflow, so importing the helper file through the
    package made every scheduled run fail before it could regenerate the bundles.
    """
    spec = importlib.util.spec_from_file_location(
        "ditto_reference_fingerprint", FINGERPRINT_MODULE
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load fingerprint helpers from {FINGERPRINT_MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_fingerprint = _load_fingerprint_module()
_file_shingles = _fingerprint._file_shingles
_normalized_source_shingles = _fingerprint._normalized_source_shingles
_prompt_shingles = _fingerprint._prompt_shingles


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def _write_bundle(output: Path, name: str, shingles: set[str]) -> None:
    (output / name).write_bytes(
        b"".join(int(shingle, 16).to_bytes(8, "big") for shingle in sorted(shingles))
    )


def build_reference(
    starter_repo: Path, *, revision: str = "origin/main", output: Path = OUTPUT
) -> dict[str, object]:
    """Build deterministic bundles and return their public provenance manifest."""
    output.mkdir(parents=True, exist_ok=True)
    resolved_revision = _git(starter_repo, "rev-parse", revision).decode().strip()
    commits = sorted(_git(starter_repo, "rev-list", revision).decode().splitlines())
    blobs: set[str] = set()
    for commit in commits:
        for line in _git(starter_repo, "ls-tree", "-r", commit).decode().splitlines():
            metadata = line.split("\t", 1)[0].split()
            if len(metadata) >= 3 and metadata[1] == "blob":
                blobs.add(metadata[2])

    lexical: set[str] = set()
    normalized: set[str] = set()
    prompt: set[str] = set()
    for oid in sorted(blobs):
        raw = _git(starter_repo, "cat-file", "blob", oid)
        lexical.update(_file_shingles(raw))
        normalized.update(_normalized_source_shingles(raw))
        prompt.update(_prompt_shingles(raw))

    bundles = {
        "reference_lexical_v2.bin": lexical,
        "reference_normalized_v2.bin": normalized,
        "reference_prompt_v2.bin": prompt,
    }
    for name, shingles in bundles.items():
        _write_bundle(output, name, shingles)

    manifest: dict[str, object] = {
        "format": "sorted-big-endian-uint64-v1",
        "source": "https://github.com/ditto-assistant/dittobench-starter-kit",
        "revision": resolved_revision,
        "requested_revision": revision,
        "commits": commits,
        "commit_set_sha256": hashlib.sha256("\n".join(commits).encode()).hexdigest(),
        "unique_blobs": len(blobs),
        "bundles": {name: len(shingles) for name, shingles in bundles.items()},
    }
    (output / "reference_manifest_v2.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("starter_repo", type=Path)
    parser.add_argument(
        "--revision",
        default="origin/main",
        help="authoritative default-branch lineage to include (default: origin/main)",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    build_reference(args.starter_repo, revision=args.revision, output=args.output)


if __name__ == "__main__":
    main()
