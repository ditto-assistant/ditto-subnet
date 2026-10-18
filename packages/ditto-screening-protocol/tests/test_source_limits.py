"""Per-file source limits shared by the miner CLI, Platform and screener."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta

import pytest

from ditto_screening_protocol import source_limits
from ditto_screening_protocol.source_limits import (
    MAX_SOURCE_FILE_BYTES,
    MAX_SOURCE_FILE_LINES,
    MAX_SOURCE_LINE_CHARS,
    SOURCE_LIMITS_EFFECTIVE_AT,
    check_source_file,
    check_source_stream,
    source_limits_enforced,
    vendored_upstream_digests,
)


def _lines(count: int) -> bytes:
    return b"x = 1\n" * count


def test_at_the_line_limit_passes_and_one_over_fails() -> None:
    assert check_source_file("src/a.py", _lines(MAX_SOURCE_FILE_LINES)) is None
    violation = check_source_file("src/a.py", _lines(MAX_SOURCE_FILE_LINES + 1))
    assert violation is not None
    assert (violation.kind, violation.value) == ("lines", MAX_SOURCE_FILE_LINES + 1)
    assert "src/a.py: 10,001 lines (limit 10,000)" in violation.message


def test_byte_and_line_length_limits() -> None:
    wide = (b"y" * 3_900 + b"\n") * 300  # under 10k lines, over 1 MiB
    violation = check_source_file("lib.rs", wide)
    assert violation is not None and violation.kind == "bytes"
    packed = b"z" * (MAX_SOURCE_LINE_CHARS + 1)
    violation = check_source_file("bundle.js", packed)
    assert violation is not None
    assert (violation.kind, violation.limit) == ("line_length", MAX_SOURCE_LINE_CHARS)


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("fixtures/models/vocab.txt", _lines(MAX_SOURCE_FILE_LINES + 1)),
        ("data/table.json", b"{" + b"0" * (MAX_SOURCE_FILE_BYTES + 1)),
        ("src/blob.c", b"\xff\xfe" + b"\x00" * 10),
    ],
    ids=["non-source", "data-file", "non-utf8"],
)
def test_out_of_scope_files_are_not_limited(path: str, data: bytes) -> None:
    assert check_source_file(path, data) is None


def test_unmodified_upstream_digest_is_exempt_and_any_edit_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendored = _lines(MAX_SOURCE_FILE_LINES + 50)
    monkeypatch.setattr(
        source_limits,
        "vendored_upstream_digests",
        lambda: frozenset({hashlib.sha256(vendored).hexdigest()}),
    )
    assert check_source_file("third_party/quickjs/quickjs.c", vendored) is None
    assert check_source_file("third_party/quickjs/quickjs.c", vendored + b"//\n")


def test_allowlist_records_official_provenance() -> None:
    document = json.loads(source_limits._ALLOWLIST_PATH.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    entries = document["files"]
    assert {entry["sha256"] for entry in entries} == vendored_upstream_digests()
    for entry in entries:
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        assert entry["source_url"].startswith("https://")
        # Only files the limits would otherwise refuse belong here.
        assert (
            entry["lines"] > MAX_SOURCE_FILE_LINES
            or entry["bytes"] > MAX_SOURCE_FILE_BYTES
        )


def test_enforcement_starts_at_the_published_instant() -> None:
    assert not source_limits_enforced(SOURCE_LIMITS_EFFECTIVE_AT - timedelta(seconds=1))
    assert source_limits_enforced(SOURCE_LIMITS_EFFECTIVE_AT)


@pytest.mark.parametrize("chunk", [1, 3, 7, 64])
def test_streamed_check_matches_in_memory_across_chunk_boundaries(chunk: int) -> None:
    # A multi-byte character and line breaks land on chunk boundaries.
    data = ("é" * (MAX_SOURCE_LINE_CHARS + 1) + "\nok\n").encode()
    pieces = [data[i : i + chunk] for i in range(0, len(data), chunk)]
    streamed = check_source_stream("src/a.rs", pieces)
    assert streamed == check_source_file("src/a.rs", data)
    assert streamed is not None
    assert (streamed.kind, streamed.value) == (
        "line_length",
        MAX_SOURCE_LINE_CHARS + 1,
    )


def test_streamed_line_count_handles_a_missing_final_newline() -> None:
    data = _lines(MAX_SOURCE_FILE_LINES) + b"tail"
    violation = check_source_stream("src/a.py", [data[:5], data[5:]])
    assert violation is not None
    assert violation.value == MAX_SOURCE_FILE_LINES + 1
