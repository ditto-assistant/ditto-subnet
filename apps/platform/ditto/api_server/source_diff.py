"""Bounded per-file source diffing between a held agent and its match.

Feeds the operator copy-review console: given the candidate (held) tarball and
the reference it was matched against, produce (1) a compact per-file manifest
classifying every path as added / removed / modified / identical / renamed with
change stats, and (2) an on-demand bounded unified diff for a single file. The
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
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Literal

from ditto.api_server.fingerprint import _normalized_source

# A single file's unified diff is capped so one pathological pair can't return
# a multi-megabyte body; the manifest's line counts still report the true size.
MAX_UNIFIED_DIFF_LINES = 4000
# The manifest lists at most this many files (the reader already bounds how many
# members it returns, but the manifest is defensive in its own right).
MAX_MANIFEST_FILES = 512
# Leftover add/remove paths whose normalized line sequences match at or above
# this ratio are the same file under a new name, not an independent add.
RENAME_SIMILARITY_THRESHOLD = 0.9

FileStatus = Literal["added", "removed", "modified", "identical", "renamed"]


def _line_counts(text: str) -> int:
    return len(text.splitlines())


def _normalized_file_source(text: str) -> str:
    return _normalized_source(text.encode("utf-8"))


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


def _file_entry(
    *,
    path: str,
    status: FileStatus,
    candidate_text: str | None,
    reference_text: str | None,
    added_lines: int,
    removed_lines: int,
    similarity: float,
    normalized_identical: bool,
    from_path: str | None = None,
    to_path: str | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "path": path,
        "status": status,
        "candidate_lines": _line_counts(candidate_text or ""),
        "reference_lines": _line_counts(reference_text or ""),
        "added_lines": added_lines,
        "removed_lines": removed_lines,
        "similarity": similarity,
        "normalized_identical": normalized_identical,
    }
    if from_path is not None:
        entry["from_path"] = from_path
    if to_path is not None:
        entry["to_path"] = to_path
    return entry


def _same_path_entry(path: str, candidate: str, reference: str) -> dict[str, object]:
    if candidate == reference:
        return _file_entry(
            path=path,
            status="identical",
            candidate_text=candidate,
            reference_text=reference,
            added_lines=0,
            removed_lines=0,
            similarity=1.0,
            normalized_identical=True,
        )
    add, rem, ratio = _change_stats(candidate, reference)
    return _file_entry(
        path=path,
        status="modified",
        candidate_text=candidate,
        reference_text=reference,
        added_lines=add,
        removed_lines=rem,
        similarity=round(ratio, 4),
        # Identical once comments/whitespace are canonicalized: a
        # reformatted or re-commented copy of the same code.
        normalized_identical=_normalized_file_source(candidate)
        == _normalized_file_source(reference),
    )


def _added_entry(path: str, text: str) -> dict[str, object]:
    return _file_entry(
        path=path,
        status="added",
        candidate_text=text,
        reference_text=None,
        added_lines=_line_counts(text),
        removed_lines=0,
        similarity=0.0,
        normalized_identical=False,
    )


def _removed_entry(path: str, text: str) -> dict[str, object]:
    return _file_entry(
        path=path,
        status="removed",
        candidate_text=None,
        reference_text=text,
        added_lines=0,
        removed_lines=_line_counts(text),
        similarity=0.0,
        normalized_identical=False,
    )


def _rename_entry(
    to_path: str, from_path: str, candidate: str, reference: str
) -> dict[str, object]:
    normalized_identical = _normalized_file_source(
        candidate
    ) == _normalized_file_source(reference)
    if candidate == reference:
        add, rem, ratio = 0, 0, 1.0
    else:
        add, rem, ratio = _change_stats(candidate, reference)
    return _file_entry(
        path=to_path,
        status="renamed",
        candidate_text=candidate,
        reference_text=reference,
        added_lines=add,
        removed_lines=rem,
        similarity=round(ratio, 4),
        normalized_identical=normalized_identical,
        from_path=from_path,
        to_path=to_path,
    )


def _pair_rename_leftovers(
    added_paths: list[str],
    removed_paths: list[str],
    candidate: dict[str, str],
    reference: dict[str, str],
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Pair leftover add/remove paths that are the same source under a new name.

    Exact-path matches are already consumed by the caller. Remaining files are
    first matched on identical ``_normalized_source`` bytes (comment/whitespace
    canonicalization), then by a high SequenceMatcher ratio on that normalized
    line sequence so a rename that also edits a few lines still lands as one
    ``renamed`` row instead of an unrelated add + remove.
    """
    added_norm = {
        path: _normalized_file_source(candidate[path]) for path in added_paths
    }
    removed_norm = {
        path: _normalized_file_source(reference[path]) for path in removed_paths
    }
    remaining_removed: dict[str, list[str]] = defaultdict(list)
    for path in removed_paths:
        norm = removed_norm[path]
        if norm:
            remaining_removed[norm].append(path)

    pairs: list[tuple[str, str]] = []
    paired_added: set[str] = set()
    paired_removed: set[str] = set()
    for to_path in added_paths:
        norm = added_norm[to_path]
        if not norm:
            continue
        bucket = remaining_removed.get(norm)
        if not bucket:
            continue
        from_path = bucket.pop(0)
        pairs.append((to_path, from_path))
        paired_added.add(to_path)
        paired_removed.add(from_path)

    leftover_added = [path for path in added_paths if path not in paired_added]
    leftover_removed = [path for path in removed_paths if path not in paired_removed]
    leftover_added_norm_lines = {
        path: added_norm[path].splitlines()
        for path in leftover_added
        if added_norm[path]
    }
    leftover_removed_norm_lines = {
        path: removed_norm[path].splitlines()
        for path in leftover_removed
        if removed_norm[path]
    }
    while leftover_added_norm_lines and leftover_removed_norm_lines:
        best: tuple[float, str, str] | None = None
        for to_path, cand_lines in leftover_added_norm_lines.items():
            for from_path, ref_lines in leftover_removed_norm_lines.items():
                ratio = difflib.SequenceMatcher(
                    a=ref_lines, b=cand_lines, autojunk=False
                ).ratio()
                if ratio < RENAME_SIMILARITY_THRESHOLD:
                    continue
                if (
                    best is None
                    or ratio > best[0]
                    or (ratio == best[0] and (to_path, from_path) < (best[1], best[2]))
                ):
                    best = (ratio, to_path, from_path)
        if best is None:
            break
        _, to_path, from_path = best
        pairs.append((to_path, from_path))
        del leftover_added_norm_lines[to_path]
        del leftover_removed_norm_lines[from_path]

    leftover_added = [path for path in added_paths if path not in {p[0] for p in pairs}]
    leftover_removed = [
        path for path in removed_paths if path not in {p[1] for p in pairs}
    ]
    return pairs, leftover_added, leftover_removed


def build_source_diff_manifest(
    candidate: dict[str, str],
    reference: dict[str, str],
    *,
    max_files: int = MAX_MANIFEST_FILES,
    pair_renames: bool = True,
) -> dict[str, Any]:
    """Classify every file across the two artifacts with change statistics."""
    paths = sorted(set(candidate) | set(reference))
    files: list[dict[str, object]] = []
    identical = modified = added_files = removed_files = renamed_files = 0
    leftover_added: list[str] = []
    leftover_removed: list[str] = []
    for path in paths:
        cand = candidate.get(path)
        ref = reference.get(path)
        if cand is not None and ref is None:
            leftover_added.append(path)
            continue
        if cand is None and ref is not None:
            leftover_removed.append(path)
            continue
        assert cand is not None and ref is not None
        entry = _same_path_entry(path, cand, ref)
        if entry["status"] == "identical":
            identical += 1
        else:
            modified += 1
        files.append(entry)

    if pair_renames and leftover_added and leftover_removed:
        pairs, leftover_added, leftover_removed = _pair_rename_leftovers(
            leftover_added, leftover_removed, candidate, reference
        )
        for to_path, from_path in pairs:
            renamed_files += 1
            files.append(
                _rename_entry(
                    to_path, from_path, candidate[to_path], reference[from_path]
                )
            )

    for path in leftover_added:
        added_files += 1
        files.append(_added_entry(path, candidate[path]))
    for path in leftover_removed:
        removed_files += 1
        files.append(_removed_entry(path, reference[path]))

    files.sort(key=lambda row: str(row["path"]))
    truncated = len(files) > max_files
    return {
        "files": files[:max_files],
        "file_count": len(paths),
        "identical_count": identical,
        "modified_count": modified,
        "added_count": added_files,
        "removed_count": removed_files,
        "renamed_count": renamed_files,
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

    Rename pairing stays off here: starter-kit review is path-oriented (which
    kit files the miner kept) and its wire status enum does not include
    ``renamed``. Copy-review pairing is the operator-facing copy-diff only.
    """
    manifest = build_source_diff_manifest(
        candidate, baseline, max_files=max_files, pair_renames=False
    )
    manifest.pop("renamed_count", None)
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


def _rename_counterpart(
    path: str, candidate: dict[str, str], reference: dict[str, str]
) -> tuple[str | None, str | None]:
    """Return ``(from_path, to_path)`` when ``path`` is one side of a rename."""
    leftover_added = sorted(p for p in candidate if p not in reference)
    leftover_removed = sorted(p for p in reference if p not in candidate)
    if path not in leftover_added and path not in leftover_removed:
        return None, None
    pairs, _, _ = _pair_rename_leftovers(
        leftover_added, leftover_removed, candidate, reference
    )
    for to_path, from_path in pairs:
        if path in (to_path, from_path):
            return from_path, to_path
    return None, None


def unified_diff_for_file(
    path: str,
    candidate: dict[str, str],
    reference: dict[str, str],
    *,
    max_lines: int = MAX_UNIFIED_DIFF_LINES,
    pair_renames: bool = True,
) -> dict[str, Any]:
    """Bounded unified diff (reference -> candidate) for a single file.

    Returns ``present`` flags for each side so the UI can render an add/remove
    of a whole file, and ``truncated`` when the body hit ``max_lines``. When
    ``pair_renames`` is set, a leftover add/remove that the manifest would
    classify as ``renamed`` is diffed against its counterpart rather than as a
    whole-file add or delete.
    """
    cand = candidate.get(path)
    ref = reference.get(path)
    from_path: str | None = None
    to_path: str | None = None
    if pair_renames and (cand is None) != (ref is None):
        from_path, to_path = _rename_counterpart(path, candidate, reference)
        if from_path is not None and to_path is not None:
            cand = candidate[to_path]
            ref = reference[from_path]
    if cand is None and ref is None:
        raise KeyError(path)
    from_label = from_path or path
    to_label = to_path or path
    diff = difflib.unified_diff(
        (ref or "").splitlines(),
        (cand or "").splitlines(),
        fromfile=f"reference/{from_label}",
        tofile=f"candidate/{to_label}",
        lineterm="",
    )
    lines: list[str] = []
    truncated = False
    for line in diff:
        if len(lines) >= max_lines:
            truncated = True
            break
        lines.append(line[:1000])
    detail: dict[str, Any] = {
        "path": path,
        "candidate_present": cand is not None,
        "reference_present": ref is not None,
        "identical": cand is not None and ref is not None and cand == ref,
        "diff_lines": lines,
        "truncated": truncated,
    }
    if from_path is not None:
        detail["from_path"] = from_path
    if to_path is not None:
        detail["to_path"] = to_path
    return detail


__all__ = [
    "MAX_MANIFEST_FILES",
    "MAX_UNIFIED_DIFF_LINES",
    "RENAME_SIMILARITY_THRESHOLD",
    "build_source_diff_manifest",
    "unified_diff_for_file",
]
