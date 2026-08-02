"""Build the starter-kit baseline bundle from an official starter-kit clone.

Usage::

    uv run python scripts/build_starter_kit_baseline.py /path/to/starter-kit

The anti-copy reference bundles (``build_reference_fingerprints.py``) reduce the
starter kit to one-way shingle hashes: they answer "was this window ever in the
kit?" and nothing else. Operator review needs the opposite — the *text*, so a
quarantined submission can be diffed against the harness every miner starts
from and the reviewer sees only what the miner actually wrote.

This bundle therefore carries two things:

* ``head`` + ``blobs`` — the tip tree's path -> text, which the baseline diff
  renders as a readable unified diff.
* ``historical_sha256`` / ``historical_normalized_sha256`` — content digests of
  every text blob reachable from mainline history. A candidate file whose text
  matches any of them is stock kit code even when it differs from the tip,
  which is what a miner who forked at an older commit will produce. Without
  this the diff calls honest kit files "modified" and buries the real delta.

Output is deterministic (sorted keys, sorted digest lists) and committed beside
the shingle bundles so review never needs network access at request time.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
from pathlib import Path

from ditto.api_server.fingerprint import _normalized_source

OUTPUT = Path(__file__).parents[1] / "ditto" / "anticopy"
BUNDLE_NAME = "starter_kit_baseline_v1.json.gz"
SOURCE = "https://github.com/ditto-assistant/dittobench-starter-kit"

# Mirrors TEXT_SIZE_LIMIT in source_inspect: a blob the reader would refuse to
# treat as text is useless as a diff baseline, so it never enters the bundle.
MAX_BLOB_BYTES = 2 * 1024 * 1024


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def _text(raw: bytes) -> str | None:
    """Decoded blob, or ``None`` when it is binary or too large to diff."""
    if len(raw) > MAX_BLOB_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("starter_repo", type=Path)
    parser.add_argument(
        "--revision",
        default="origin/main",
        help="authoritative default-branch lineage to include (default: origin/main)",
    )
    args = parser.parse_args()

    resolved_revision = (
        _git(args.starter_repo, "rev-parse", args.revision).decode().strip()
    )
    commits = sorted(
        _git(args.starter_repo, "rev-list", args.revision).decode().splitlines()
    )

    # Every unique blob across the whole lineage, not just the tip: miners fork
    # at different commits and a file that is verbatim kit code at an older
    # revision must still read as stock.
    historical: set[str] = set()
    historical_normalized: set[str] = set()
    seen_oids: set[str] = set()
    for commit in commits:
        for line in (
            _git(args.starter_repo, "ls-tree", "-r", commit).decode().splitlines()
        ):
            metadata = line.split("\t", 1)[0].split()
            if len(metadata) < 3 or metadata[1] != "blob":
                continue
            oid = metadata[2]
            if oid in seen_oids:
                continue
            seen_oids.add(oid)
            raw = _git(args.starter_repo, "cat-file", "blob", oid)
            text = _text(raw)
            if text is None:
                continue
            historical.add(hashlib.sha256(text.encode("utf-8")).hexdigest())
            historical_normalized.add(
                hashlib.sha256(_normalized_source(raw).encode("utf-8")).hexdigest()
            )

    # The tip tree is the diff baseline proper; only these blobs need bodies.
    head: dict[str, str] = {}
    blobs: dict[str, str] = {}
    for line in (
        _git(args.starter_repo, "ls-tree", "-r", resolved_revision)
        .decode()
        .splitlines()
    ):
        metadata, path = line.split("\t", 1)
        fields = metadata.split()
        if len(fields) < 3 or fields[1] != "blob":
            continue
        text = _text(_git(args.starter_repo, "cat-file", "blob", fields[2]))
        if text is None:
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        head[path] = digest
        blobs[digest] = text

    bundle = {
        "format": "starter-kit-baseline-v1",
        "source": SOURCE,
        "revision": resolved_revision,
        "requested_revision": args.revision,
        "commits": commits,
        "commit_set_sha256": hashlib.sha256("\n".join(commits).encode()).hexdigest(),
        "head": head,
        "blobs": blobs,
        "historical_sha256": sorted(historical),
        "historical_normalized_sha256": sorted(historical_normalized),
    }
    payload = json.dumps(bundle, indent=2, sort_keys=True).encode() + b"\n"
    # mtime=0 so rebuilding identical input produces a byte-identical bundle.
    (OUTPUT / BUNDLE_NAME).write_bytes(gzip.compress(payload, 9, mtime=0))
    print(
        f"{BUNDLE_NAME}: {len(head)} head files, {len(historical)} historical blobs, "
        f"{len(commits)} commits @ {resolved_revision[:12]}"
    )


if __name__ == "__main__":
    main()
