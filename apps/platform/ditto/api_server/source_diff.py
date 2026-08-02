"""Bounded per-file source diffing between a held agent and its match.

Feeds the operator copy-review console: given the candidate (held) tarball and
the reference it was matched against, produce (1) a compact per-file manifest
classifying every path as added / removed / modified / identical with change
stats, and (2) an on-demand bounded unified diff for a single file. The
manifest is small enough to render inline; unified-diff bodies are fetched one
file at a time so a large submission never returns an unbounded payload.

All inputs are ``path -> full text`` maps produced by
:meth:`ditto.api_server.source_inspect.TarSourceInspector.read_all_text`, i.e.
already size-bounded, UTF-8, and free of unsafe paths. Normalized identity
reuses the anti-copy fingerprint canonicalization (comments and whitespace
stripped) so an operator can tell a genuine copy from an identical-after-
reformat repack. Pure CPU work — callers run it via ``asyncio.to_thread``.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from typing import Any, Literal

from ditto.api_server.fingerprint import _normalized_source

# A single file's unified diff is capped so one pathological pair can't return
# a multi-megabyte body; the manifest's line counts still report the true size.
MAX_UNIFIED_DIFF_LINES = 4000
# The manifest lists at most this many files (the reader already bounds how many
# members it returns, but the manifest is defensive in its own right).
MAX_MANIFEST_FILES = 512

FileStatus = Literal["added", "removed", "modified", "identical"]


def _line_counts(text: str) -> int:
    return len(text.splitlines())


def _change_stats(candidate: str, reference: str) -> tuple[int, int, float]:
    """(added_lines, removed_lines, similarity) for one candidate/reference pair.

    ``similarity`` is difflib's ratio over the raw lines in [0, 1]; 1.0 means
    byte-identical text. Added/removed counts come from the same opcodes so the
    manifest and the unified diff never disagree.
    """
    cand_lines = candidate.splitlines()
    ref_lines = reference.splitlines()
    matcher = difflib.SequenceMatcher(a=ref_lines, b=cand_lines, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            removed += i2 - i1
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
    return added, removed, matcher.ratio()


def build_source_diff_manifest(
    candidate: dict[str, str],
    reference: dict[str, str],
    *,
    max_files: int = MAX_MANIFEST_FILES,
) -> dict[str, Any]:
    """Classify every file across the two artifacts with change statistics."""
    paths = sorted(set(candidate) | set(reference))
    files: list[dict[str, object]] = []
    identical = modified = added_files = removed_files = 0
    for path in paths:
        cand = candidate.get(path)
        ref = reference.get(path)
        if cand is not None and ref is None:
            added_files += 1
            files.append(
                {
                    "path": path,
                    "status": "added",
                    "candidate_lines": _line_counts(cand),
                    "reference_lines": 0,
                    "added_lines": _line_counts(cand),
                    "removed_lines": 0,
                    "similarity": 0.0,
                    "normalized_identical": False,
                }
            )
            continue
        if cand is None and ref is not None:
            removed_files += 1
            files.append(
                {
                    "path": path,
                    "status": "removed",
                    "candidate_lines": 0,
                    "reference_lines": _line_counts(ref),
                    "added_lines": 0,
                    "removed_lines": _line_counts(ref),
                    "similarity": 0.0,
                    "normalized_identical": False,
                }
            )
            continue
        assert cand is not None and ref is not None
        if cand == ref:
            identical += 1
            files.append(
                {
                    "path": path,
                    "status": "identical",
                    "candidate_lines": _line_counts(cand),
                    "reference_lines": _line_counts(ref),
                    "added_lines": 0,
                    "removed_lines": 0,
                    "similarity": 1.0,
                    "normalized_identical": True,
                }
            )
            continue
        add, rem, ratio = _change_stats(cand, ref)
        modified += 1
        files.append(
            {
                "path": path,
                "status": "modified",
                "candidate_lines": _line_counts(cand),
                "reference_lines": _line_counts(ref),
                "added_lines": add,
                "removed_lines": rem,
                "similarity": round(ratio, 4),
                # Identical once comments/whitespace are canonicalized: a
                # reformatted or re-commented copy of the same code.
                "normalized_identical": _normalized_source(cand.encode("utf-8"))
                == _normalized_source(ref.encode("utf-8")),
            }
        )
    truncated = len(files) > max_files
    return {
        "files": files[:max_files],
        "file_count": len(paths),
        "identical_count": identical,
        "modified_count": modified,
        "added_count": added_files,
        "removed_count": removed_files,
        "truncated": truncated,
    }


def build_baseline_diff_manifest(
    candidate: dict[str, str],
    baseline: dict[str, str],
    is_stock: Callable[[str], bool],
    *,
    max_files: int = MAX_MANIFEST_FILES,
) -> dict[str, Any]:
    """Manifest against the starter kit, with stock-kit files marked.

    Same classification as :func:`build_source_diff_manifest`, plus a
    ``stock_kit`` flag per file and the aggregate an operator actually wants:
    how many lines of this submission the miner wrote themselves.

    ``stock_kit`` is broader than ``status == "identical"``. Identical means
    "matches the baseline tip"; a miner who forked an older commit has kit files
    that differ from the tip but are still not their work. ``is_stock`` answers
    that against the whole lineage, so those files stay out of the custom-surface
    total instead of masquerading as authored code.
    """
    manifest = build_source_diff_manifest(candidate, baseline, max_files=max_files)
    stock_count = 0
    custom_files = 0
    custom_added = 0
    for entry in manifest["files"]:
        text = candidate.get(str(entry["path"]))
        stock = entry["status"] == "identical" or (text is not None and is_stock(text))
        entry["stock_kit"] = stock
        if stock:
            stock_count += 1
        elif entry["status"] != "removed":
            custom_files += 1
            custom_added += int(entry["added_lines"])
    manifest["stock_kit_count"] = stock_count
    manifest["custom_file_count"] = custom_files
    # The headline number: lines present in the submission that are neither
    # baseline code nor kit code at any revision.
    manifest["custom_added_lines"] = custom_added
    return manifest


def unified_diff_for_file(
    path: str,
    candidate: dict[str, str],
    reference: dict[str, str],
    *,
    max_lines: int = MAX_UNIFIED_DIFF_LINES,
) -> dict[str, Any]:
    """Bounded unified diff (reference -> candidate) for a single file.

    Returns ``present`` flags for each side so the UI can render an add/remove
    of a whole file, and ``truncated`` when the body hit ``max_lines``.
    """
    cand = candidate.get(path)
    ref = reference.get(path)
    if cand is None and ref is None:
        raise KeyError(path)
    diff = difflib.unified_diff(
        (ref or "").splitlines(),
        (cand or "").splitlines(),
        fromfile=f"reference/{path}",
        tofile=f"candidate/{path}",
        lineterm="",
    )
    lines: list[str] = []
    truncated = False
    for line in diff:
        if len(lines) >= max_lines:
            truncated = True
            break
        lines.append(line[:1000])
    return {
        "path": path,
        "candidate_present": cand is not None,
        "reference_present": ref is not None,
        "identical": cand is not None and ref is not None and cand == ref,
        "diff_lines": lines,
        "truncated": truncated,
    }


__all__ = [
    "MAX_MANIFEST_FILES",
    "MAX_UNIFIED_DIFF_LINES",
    "build_source_diff_manifest",
    "unified_diff_for_file",
]
