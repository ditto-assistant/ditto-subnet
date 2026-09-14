"""Bounded shadow-only semantic exploration hints, never policy findings.

No submitted source is imported or executed. A hit does not establish reachability,
benchmark specificity, or wrongdoing; legitimate caches/interpreters also match.
"""

from __future__ import annotations

import io
import re
import tarfile
import tokenize
from collections import defaultdict
from collections.abc import Iterator
from contextlib import suppress
from pathlib import PurePosixPath

from ditto_screener.source_review import TarSourceRepository
from ditto_screener.source_signals import _mask_string_literals, mask_comments

REVISION = "shadow-semantic-discovery-v1"
_GUIDANCE = (
    "Untrusted location hint, not a finding or a clearance requirement. Read the "
    "definition, provenance and served caller through its answer/routing effect. "
    "Establish any benchmark-specific assumption and substantive restriction; "
    "ordinary retrieval, caching, product routing, syntax/type/resource validation "
    "and faithfully executed "
    "model-authored programs can be legitimate (W5/W6)."
)
_SOURCE_SUFFIXES = {
    ".rs",
    ".py",
    ".go",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".cc",
    ".cpp",
    ".c",
    ".h",
}
_NON_RUNTIME = {
    "docs",
    "doc",
    "tests",
    "test",
    "examples",
    "fixtures",
    "node_modules",
    ".git",
}
_KEY = r"[A-Za-z_]*(?:question|query|request|user_input|prompt|case_id)[A-Za-z_]*"
_LOOKUP = re.compile(rf"(?:\.get\s*\(\s*&?\s*{_KEY}\b|\[\s*&?\s*{_KEY}\s*\])", re.I)
_DEST = re.compile(
    r"(?:thread|workstream|recipe|program|answer|procedure|template)", re.I
)
_COMPILER = re.compile(
    r"\b(?:def|fn|func)\s+\w*(?:compil\w*|program|expression)\w*\s*\(", re.I
)
_SCHEMA = re.compile(
    r"\b(?:class|enum|struct|interface|type)\s+\w*(?:Program|Expression|Operation|Plan)\w*\b"
)
_RETURN = re.compile(
    r"\breturn\s+([A-Za-z_]\w*(?:program|expression|compiled|plan)\w*|(?:program|expression|compiled|plan)\w*)\b",
    re.I,
)
_KEY_WORD = re.compile(_KEY, re.I)

_VALIDATOR = re.compile(r"\b\w*(?:validat|verif|check)\w*\s*\(", re.I)
_SEMANTIC = re.compile(r"(?:expression|expr|program|operand|field|draft|answer)", re.I)
_FEEDBACK = re.compile(r"(?:repair|retry|feedback|objection|continue)", re.I)


def _code(path: str, text: str) -> str | None:
    if not path.casefold().endswith(".py"):
        return _mask_string_literals(mask_comments(text))
    # Tokenization avoids apostrophes in Python comments desynchronizing the
    # string masker. It reads syntax only; no submitted imports or execution.
    lines = text.splitlines(keepends=True)
    chars = [list(line) for line in lines]
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type not in {tokenize.STRING, tokenize.COMMENT}:
                continue
            (start, col), (end, endcol) = token.start, token.end
            for row in range(start - 1, min(end, len(chars))):
                first = col if row == start - 1 else 0
                last = endcol if row == end - 1 else len(chars[row])
                for index in range(first, min(last, len(chars[row]))):
                    if chars[row][index] not in "\r\n":
                        chars[row][index] = " "
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return "".join("".join(line) for line in chars)


def _source(path: str) -> bool:
    p = PurePosixPath(path.casefold())
    return p.suffix in _SOURCE_SUFFIXES and not (_NON_RUNTIME & set(p.parts))


def _selected_texts(
    repo: TarSourceRepository, selected: dict[str, tuple[str, int]]
) -> Iterator[tuple[str, str | None]]:
    """One forward gzip pass; selected declared bytes are not archive I/O bytes."""
    pending = dict(selected)
    if not pending:
        return
    with tarfile.open(repo._archive_path, mode="r|gz") as archive:
        for member in archive:
            expected = pending.pop(member.name, None)
            if expected is None:
                continue
            path, size = expected
            if not member.isfile() or member.size != size:
                raise ValueError("source archive changed after member validation")
            extracted = archive.extractfile(member)
            text = None
            if extracted is not None:
                raw = extracted.read(size + 1)
                if len(raw) != size:
                    raise ValueError("source archive member length changed")
                with suppress(UnicodeDecodeError):
                    text = raw.decode("utf-8")
            yield path, text
            if not pending:
                break
    for path, _size in pending.values():
        yield path, None


def semantic_discovery(
    source: str | TarSourceRepository,
    *,
    max_hints: int = 8,
    max_files: int = 512,
    max_bytes: int = 16 * 1024 * 1024,
    max_file_bytes: int = 2 * 1024 * 1024,
) -> dict:
    """Return ``{revision, guidance, leads, coverage}`` with no source excerpts.

    Scan all budgeted files before selecting hints. Overlapping windows collapse;
    selection rotates rule families and files, preferring specific map/compile
    structures. Limits never imply complete source coverage or a policy verdict.
    """
    if not (
        1 <= max_hints <= 32
        and 1 <= max_files <= 2048
        and 1 <= max_file_bytes <= 2 * 1024 * 1024
        and 1 <= max_bytes <= 64 * 1024 * 1024
    ):
        raise ValueError("semantic discovery limits out of bounds")
    repo = TarSourceRepository(source) if isinstance(source, str) else source
    paths = sorted(path for path in repo._members if _source(path))
    buckets: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    scanned = scanned_bytes = omitted = unreadable = considered = 0
    windows_omitted = lines_clipped = 0
    selected: dict[str, tuple[str, int]] = {}
    for path in paths:
        size = repo._members[path].size
        if (
            considered >= max_files
            or size > max_file_bytes
            or scanned_bytes + size > max_bytes
        ):
            omitted += 1
            continue
        considered += 1
        scanned_bytes += size
        selected[repo._members[path].archive_name] = (path, size)
    for path, text in _selected_texts(repo, selected):
        if text is None:
            unreadable += 1
            continue
        code = _code(path, text)
        if code is None:
            unreadable += 1
            continue
        scanned += 1
        lines = code.splitlines()
        for number, raw in enumerate(lines, 1):
            line = raw[:4096]
            lines_clipped += int(len(raw) > 4096)
            kinds: list[tuple[str, int]] = []
            if _LOOKUP.search(line):
                kinds.append(("request-key-lookup", 2 if _DEST.search(line) else 1))
            if _SCHEMA.search(line) or (
                _COMPILER.search(line) and _KEY_WORD.search(line)
            ):
                kinds.append(
                    (
                        "program-or-compiler-interface",
                        3
                        if _COMPILER.search(line) and "prompt" in line.casefold()
                        else 2
                        if _COMPILER.search(line)
                        else 1,
                    )
                )
            if _RETURN.search(line):
                kinds.append(("delegated-result-return", 1))
            if _VALIDATOR.search(line) and _SEMANTIC.search(line):
                context = "\n".join(lines[max(0, number - 25) : number + 2])
                if _KEY_WORD.search(line) or _FEEDBACK.search(context):
                    kinds.append(
                        (
                            "semantic-validation-feedback",
                            3 if _KEY_WORD.search(line) else 2,
                        )
                    )
            for kind, score in kinds:
                bucket = buckets[(kind, path)]
                # One location per nearby window, not combinatorial role matches.
                if bucket and number - bucket[-1][0] <= 24:
                    if score > bucket[-1][1]:
                        bucket[-1] = (number, score)
                elif len(bucket) < 4:
                    bucket.append((number, score))
                else:
                    windows_omitted += 1
    # Rotate across families and distinct files before revisiting any file/rule.
    families: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for (kind, path), anchors in buckets.items():
        for rank, (anchor_line, score) in enumerate(
            sorted(anchors, key=lambda x: (-x[1], x[0]))
        ):
            families[kind].append((path, anchor_line, score - rank * 10))
    for rows in families.values():
        rows.sort(key=lambda row: (-row[2], row[0], row[1]))
    leads: list[dict[str, object]] = []
    scheduled_kinds = [
        "request-key-lookup",
        "program-or-compiler-interface",
        "delegated-result-return",
        "semantic-validation-feedback",
    ]
    while len(leads) < max_hints and any(families.values()):
        for kind in scheduled_kinds:
            if families[kind] and len(leads) < max_hints:
                path, anchor_line, _score = families[kind].pop(0)
                leads.append(
                    {"kind": kind, "locations": [{"path": path, "line": anchor_line}]}
                )
    remaining = sum(len(rows) for rows in families.values())
    return {
        "revision": REVISION,
        "guidance": _GUIDANCE,
        "leads": leads,
        "coverage": {
            "eligible_files": len(paths),
            "files_considered": considered,
            "files_scanned": scanned,
            "selected_source_bytes_bound": scanned_bytes,
            "files_omitted": omitted,
            "unreadable_files": unreadable,
            "hint_buckets": len(buckets),
            "hints_omitted": remaining,
            "windows_omitted": windows_omitted,
            "lines_clipped": lines_clipped,
            "truncated": bool(
                omitted or unreadable or remaining or windows_omitted or lines_clipped
            ),
            "exhaustive": False,
        },
    }
