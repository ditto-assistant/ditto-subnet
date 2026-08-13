"""Unit coverage for the operator source search over a submission tarball.

The case this exists for: a real submission's ``baseline.rs`` runs 10,000+
lines and the graded ``RunResponse`` construction sits somewhere in the middle
of it. With only a 400-line excerpt reader, finding that construction costs six
to eight blind reads. These tests pin the properties that make one call enough
-- and, just as importantly, the bounds that keep the answer small.
"""

import io
import tarfile
from typing import Any, cast

import pytest

from ditto.api_server.source_inspect import (
    MAX_SEARCH_CONTEXT,
    MAX_SEARCH_MATCHES,
    MAX_SEARCH_SCAN,
    SEARCH_LINE_CHARS,
    SourceInspectError,
    TarSourceInspector,
)


def _tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return buffer.getvalue()


def _big_baseline(construction_line: int, total_lines: int) -> bytes:
    lines = ["    let _ = 0;"] * total_lines
    lines[construction_line - 1] = "    Ok(protocol::RunResponse { answer, abstain })"
    return ("\n".join(lines) + "\n").encode()


def _inspector(files: dict[str, bytes]) -> TarSourceInspector:
    return TarSourceInspector(_tarball(files))


def _rows(result: dict[str, object], key: str) -> list[dict[str, Any]]:
    """The wire shape is ``dict[str, object]``; name the row lists once."""
    return cast(list[dict[str, Any]], result[key])


def test_search_finds_the_response_construction_in_one_call() -> None:
    """The whole point: one call replaces the bisect."""
    inspector = _inspector(
        {
            "Cargo.toml": b'[package]\nname="agent"\n',
            "src/baseline.rs": _big_baseline(8919, 10795),
        }
    )

    found = inspector.search("RunResponse")

    assert found["match_count"] == 1
    assert _rows(found, "matches") == [
        {
            "path": "src/baseline.rs",
            "line": 8919,
            "text": "    Ok(protocol::RunResponse { answer, abstain })",
            "context_before": [],
            "context_after": [],
        }
    ]
    assert found["files_searched"] == 2
    assert found["files_matched"] == 1
    assert found["has_more"] is False
    assert found["truncated"] is False


def test_search_never_opens_opaque_blobs_and_counts_them() -> None:
    """``cross-encoder.onnx`` is not searchable and must not be pretended over.

    A binary weight file can contain the ASCII bytes of any pattern; searching
    it would produce a match with no meaning, and decoding it would fail. The
    listing already reports these as opaque, so the search skips them and
    returns the count, making the gap explicit in the same response.
    """
    inspector = _inspector(
        {
            "src/lib.rs": b"fn answer() {}\n",
            "assets/cross-encoder.onnx": b"\xff\xfe answer \x00" * 8,
            "assets/mlp-weights.bin": b"\x00\x01 answer \xfe" * 8,
        }
    )

    found = inspector.search("answer")

    assert [match["path"] for match in _rows(found, "matches")] == ["src/lib.rs"]
    assert found["files_searched"] == 1
    assert found["opaque_skipped"] == 2


def test_search_restricts_to_a_path_glob() -> None:
    inspector = _inspector(
        {
            "src/agent.rs": b"let answer = 1;\n",
            "src/deep/nested.rs": b"let answer = 2;\n",
            "tests/agent_test.rs": b"let answer = 3;\n",
        }
    )

    scoped = inspector.search("answer", path_glob="src/*")
    assert [match["path"] for match in _rows(scoped, "matches")] == [
        "src/agent.rs",
        "src/deep/nested.rs",
    ]

    # A separator-free glob also matches on the basename, so the obvious
    # `*.rs` and `agent.rs` spellings both behave the way an operator means.
    by_basename = inspector.search("answer", path_glob="nested.rs")
    assert [match["path"] for match in _rows(by_basename, "matches")] == [
        "src/deep/nested.rs"
    ]


def test_search_returns_bounded_context_lines() -> None:
    inspector = _inspector({"src/lib.rs": b"a\nb\nTARGET\nd\ne\n"})

    found = inspector.search("TARGET", context=1)

    match = _rows(found, "matches")[0]
    assert match["context_before"] == [{"line": 2, "text": "b"}]
    assert match["context_after"] == [{"line": 4, "text": "d"}]

    # Context is clamped, never trusted from the caller.
    clamped = inspector.search("TARGET", context=MAX_SEARCH_CONTEXT + 99)
    clamped_match = _rows(clamped, "matches")[0]
    assert len(clamped_match["context_before"]) == 2
    assert len(clamped_match["context_after"]) == 2


def test_search_pages_deterministically_and_reports_has_more() -> None:
    inspector = _inspector(
        {
            "src/b.rs": b"answer\nanswer\n",
            "src/a.rs": b"answer\nanswer\n",
        }
    )

    first = inspector.search("answer", limit=3)
    assert first["match_count"] == 4
    assert first["returned"] == 3
    assert first["has_more"] is True
    assert [(m["path"], m["line"]) for m in _rows(first, "matches")] == [
        ("src/a.rs", 1),
        ("src/a.rs", 2),
        ("src/b.rs", 1),
    ]

    second = inspector.search("answer", limit=3, offset=3)
    assert second["returned"] == 1
    assert second["has_more"] is False
    assert _rows(second, "matches")[0]["path"] == "src/b.rs"


def test_search_clips_long_lines_and_caps_the_page() -> None:
    """The response must never be able to blow an MCP client's token ceiling.

    A minified single-line bundle and a pattern that matches every line are
    both ordinary in miner artifacts; neither may turn one call into megabytes.
    """
    inspector = _inspector(
        {
            "src/min.js": b"var answer=" + b"x" * 4000 + b";\n",
            "src/many.rs": b"answer\n" * 400,
        }
    )

    found = inspector.search("answer", limit=MAX_SEARCH_MATCHES + 500)

    assert found["limit"] == MAX_SEARCH_MATCHES
    assert found["returned"] == MAX_SEARCH_MATCHES
    assert found["has_more"] is True

    minified = inspector.search("answer", path_glob="*.js")
    assert len(_rows(minified, "matches")[0]["text"]) == SEARCH_LINE_CHARS


def test_search_stops_scanning_at_the_match_cap_and_says_so() -> None:
    inspector = _inspector({"src/many.rs": b"answer\n" * (MAX_SEARCH_SCAN + 50)})

    found = inspector.search("answer")

    assert found["truncated"] is True
    assert found["match_count"] == MAX_SEARCH_SCAN


def test_literal_mode_escapes_regex_metacharacters() -> None:
    """``answer:`` and ``RunResponse {`` are what operators actually type."""
    inspector = _inspector({"src/lib.rs": b"let x = a.b(c);\nlet y = a?b(c);\n"})

    literal = inspector.search("a.b(c)", mode="literal")
    assert [match["line"] for match in _rows(literal, "matches")] == [1]

    as_regex = inspector.search("a.b\\(c\\)", mode="regex")
    assert [match["line"] for match in _rows(as_regex, "matches")] == [1, 2]


def test_search_is_case_sensitive_unless_asked() -> None:
    inspector = _inspector({"src/lib.rs": b"RunResponse\nrunresponse\n"})

    assert inspector.search("RunResponse")["match_count"] == 1
    assert inspector.search("RunResponse", ignore_case=True)["match_count"] == 2


def test_bad_pattern_is_a_typed_inspect_error_not_a_crash() -> None:
    inspector = _inspector({"src/lib.rs": b"x\n"})

    with pytest.raises(SourceInspectError) as invalid:
        inspector.search("(unclosed")
    assert invalid.value.code == "search-pattern-invalid"

    with pytest.raises(SourceInspectError) as too_long:
        inspector.search("x" * 5000)
    assert too_long.value.code == "search-pattern-invalid"

    with pytest.raises(SourceInspectError) as bad_mode:
        inspector.search("x", mode="fuzzy")
    assert bad_mode.value.code == "search-mode-invalid"


def test_search_answers_from_the_same_members_the_listing_shows() -> None:
    """Search and manifest must not disagree about what the artifact contains."""
    files = {
        "Cargo.toml": b"[package]\n",
        "src/lib.rs": b"mod baseline;\n",
        "src/baseline.rs": b"pub fn run() {}\n",
        "assets/weights.bin": b"\xff\xfe\x00" * 16,
    }
    inspector = _inspector(files)

    listing = inspector.listing()
    searchable = {entry["path"] for entry in _rows(listing, "files")} - {
        entry["path"] for entry in _rows(listing, "opaque_blobs")
    }

    everything = inspector.search(".", limit=MAX_SEARCH_MATCHES)
    assert everything["files_searched"] == len(searchable)
    assert {match["path"] for match in _rows(everything, "matches")} == searchable
