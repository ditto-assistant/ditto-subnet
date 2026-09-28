#!/usr/bin/env python3
"""Local, report-only v14 generator-template lead scan of an extracted source tree.

The source tree must already have been safely extracted. This command never
executes its contents and never emits matched text or hashes. A hit is a lead
for an independent source review, not a policy finding or clearance.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ditto_screener.generator_ngrams import hash_text, load_corpus

MAX_FILES = 256
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 16_000_000
MAX_LEAD_LOCATIONS = 16
MIN_DISTINCT_HITS = 3
SOURCE_SUFFIXES = frozenset({".rs", ".py", ".go", ".js", ".ts", ".json"})


def report(root: Path) -> dict[str, object]:
    """Return bounded location-only leads; incomplete scans never imply safety."""
    if not root.is_dir() or root.is_symlink():
        raise ValueError("source root must be an extracted, non-symlink directory")
    corpus = load_corpus()
    if not corpus:
        raise ValueError("generator-template corpus unavailable")
    leads: list[dict[str, object]] = []
    files_seen = 0
    bytes_seen = 0
    incomplete = False
    for path in _source_paths(root):
        if path.is_symlink():
            incomplete = True
            continue
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        size = path.stat().st_size
        if files_seen >= MAX_FILES or bytes_seen + size > MAX_TOTAL_BYTES:
            incomplete = True
            break
        files_seen += 1
        bytes_seen += size
        if size > MAX_FILE_BYTES:
            incomplete = True
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            incomplete = True
            continue
        hits: set[str] = set()
        locations: list[int] = []
        for number, line in enumerate(lines, 1):
            line_hits = hash_text(line[:4096]) & corpus
            if line_hits:
                hits.update(line_hits)
                if len(locations) < MAX_LEAD_LOCATIONS:
                    locations.append(number)
        if len(hits) >= MIN_DISTINCT_HITS:
            leads.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "lines": locations,
                    "matched_grams": len(hits),
                    "kind": "fixture-generator-ngram",
                }
            )
    return {
        "authority": "none",
        "candidate_policy_version": 14,
        "incomplete": incomplete,
        "files_scanned": files_seen,
        "leads": leads,
    }


def _source_paths(root: Path):
    """Walk in stable order without following links or listing the full tree."""
    for directory, dirs, names in root.walk(follow_symlinks=False):
        linked_dirs = sorted(name for name in dirs if (directory / name).is_symlink())
        dirs[:] = sorted(name for name in dirs if name not in linked_dirs)
        for name in linked_dirs:
            yield directory / name
        for name in sorted(names):
            yield directory / name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(report(args.source_dir), sort_keys=True))


if __name__ == "__main__":
    main()
