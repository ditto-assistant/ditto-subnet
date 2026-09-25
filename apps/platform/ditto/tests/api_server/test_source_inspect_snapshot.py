"""Unit coverage for the bounded whole-file text snapshot behind source diffs.

Issue #480: the snapshot used to spend its combined text budget in ARCHIVE
order. A starter-kit crate stores ~2.4 MB of fixture JSON under ``fixtures/``
before ``src/``, so a large authored ``src/baseline.rs`` was silently dropped
and every diff built on the snapshot then called it a deleted file. These tests
pin the replacement: members are chosen up front, smallest first, and every
readable member left out is named with the bound that excluded it.
"""

import io
import tarfile

import pytest

from ditto.api_server.source_diff import (
    build_source_diff_manifest,
    unified_diff_for_file,
)
from ditto.api_server.source_inspect import (
    OMIT_REASON_BYTE_BUDGET,
    OMIT_REASON_FILE_LIMIT,
    TEXT_SIZE_LIMIT,
    OmittedTextFile,
    SourceInspectError,
    TarSourceInspector,
)


def _tarball(members: list[tuple[str, bytes]]) -> bytes:
    """A gzip tarball storing ``members`` in exactly the given order."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, raw in members:
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return buffer.getvalue()


def test_budget_is_spent_smallest_first_not_in_archive_order() -> None:
    # Two large fixtures stored FIRST, then the authored source. In archive
    # order the fixtures would spend the whole budget before src/ is reached.
    fixture_a = b"a" * 900
    fixture_b = b"b" * 800
    authored = b"fn solve() {}\n" * 30  # 420 bytes
    inspector = TarSourceInspector(
        _tarball(
            [
                ("fixtures/pairs.json", fixture_a),
                ("fixtures/subjects.json", fixture_b),
                ("src/baseline.rs", authored),
            ]
        )
    )

    snapshot = inspector.read_text_snapshot(max_total_bytes=1500)

    assert snapshot.texts == {
        "src/baseline.rs": authored.decode(),
        "fixtures/subjects.json": fixture_b.decode(),
    }
    assert snapshot.omitted == (
        OmittedTextFile("fixtures/pairs.json", 900, OMIT_REASON_BYTE_BUDGET),
    )
    assert snapshot.omitted_paths == ["fixtures/pairs.json"]


def test_file_cap_is_reported_with_its_own_reason() -> None:
    inspector = TarSourceInspector(
        _tarball(
            [
                ("c.rs", b"ccc\n"),
                ("a.rs", b"a\n"),
                ("b.rs", b"bb\n"),
            ]
        )
    )

    snapshot = inspector.read_text_snapshot(max_files=2)

    assert set(snapshot.texts) == {"a.rs", "b.rs"}
    assert snapshot.omitted == (OmittedTextFile("c.rs", 4, OMIT_REASON_FILE_LIMIT),)


def test_equal_sizes_tie_break_on_path_not_archive_order() -> None:
    inspector = TarSourceInspector(
        _tarball([("z.rs", b"same\n"), ("m.rs", b"same\n"), ("a.rs", b"same\n")])
    )

    snapshot = inspector.read_text_snapshot(max_files=2)

    assert set(snapshot.texts) == {"a.rs", "m.rs"}
    assert snapshot.omitted_paths == ["z.rs"]


def test_small_archive_loads_every_text_file_and_omits_nothing() -> None:
    members = [
        ("Cargo.toml", b'[package]\nname = "agent"\n'),
        ("src/main.rs", b"fn main() {}\n"),
        ("README.md", b"# agent\n"),
    ]
    inspector = TarSourceInspector(_tarball(members))

    snapshot = inspector.read_text_snapshot()

    assert snapshot.texts == {name: raw.decode() for name, raw in members}
    assert snapshot.omitted == ()
    # The legacy map view is unchanged for callers that only want text.
    assert inspector.read_all_text() == snapshot.texts


def test_opaque_members_are_neither_loaded_nor_counted_as_omitted() -> None:
    # Binary blobs never were diff candidates; listing() reports them. Counting
    # them here would mark every kit-derived submission (which ships ONNX
    # weights) as incomplete.
    inspector = TarSourceInspector(
        _tarball([("weights.bin", b"\xff\xfe\x00" * 10), ("src/lib.rs", b"x\n")])
    )

    snapshot = inspector.read_text_snapshot()

    assert snapshot.texts == {"src/lib.rs": "x\n"}
    assert snapshot.omitted == ()


def test_unsafe_members_stay_invisible() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        escape = tarfile.TarInfo("../escape.rs")
        escape.size = 2
        archive.addfile(escape, io.BytesIO(b"x\n"))
        link = tarfile.TarInfo("src/link.rs")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
        safe = tarfile.TarInfo("src/lib.rs")
        safe.size = 2
        archive.addfile(safe, io.BytesIO(b"y\n"))

    snapshot = TarSourceInspector(buffer.getvalue()).read_text_snapshot()

    assert snapshot.texts == {"src/lib.rs": "y\n"}
    assert snapshot.omitted == ()


def test_repeated_member_name_loads_the_inventoried_entry_once() -> None:
    inspector = TarSourceInspector(
        _tarball([("src/lib.rs", b"first\n"), ("src/lib.rs", b"second body\n")])
    )

    snapshot = inspector.read_text_snapshot()

    assert snapshot.texts == {"src/lib.rs": "second body\n"}
    assert snapshot.omitted == ()


def test_read_full_text_reads_a_member_the_budget_skipped() -> None:
    big = b"line\n" * 400
    inspector = TarSourceInspector(_tarball([("big.rs", big), ("small.rs", b"s\n")]))
    snapshot = inspector.read_text_snapshot(max_total_bytes=100)
    assert snapshot.omitted_paths == ["big.rs"]

    assert inspector.read_full_text("big.rs") == big.decode()
    assert inspector.read_full_text("./small.rs") == "s\n"
    with pytest.raises(SourceInspectError) as missing:
        inspector.read_full_text("ghost.rs")
    assert missing.value.code == "file-not-found"


def test_default_budget_is_the_text_size_limit() -> None:
    # Two members that together exceed the default combined budget: the
    # smaller always loads, the larger is reported rather than dropped.
    half = TEXT_SIZE_LIMIT // 2
    inspector = TarSourceInspector(
        _tarball([("big.txt", b"b" * (half + 10)), ("small.txt", b"s" * half)])
    )

    snapshot = inspector.read_text_snapshot()

    assert set(snapshot.texts) == {"small.txt"}
    assert snapshot.omitted == (
        OmittedTextFile("big.txt", half + 10, OMIT_REASON_BYTE_BUDGET),
    )


# A tar may repeat a path. Only the LAST entry survives extraction, so every
# reader must bind to that exact entry. The bodies below have EQUAL size but
# different content, which defeats any name + size match.
_SHADOWED = b"fn run() { honest(); }\n"
_EXTRACTED = b"fn run() { cheats(); }\n"
assert len(_SHADOWED) == len(_EXTRACTED)


def _extracted_bytes(tar_bytes: bytes, path: str, tmp_path) -> bytes:
    """What extraction actually leaves on disk for ``path``."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    return (tmp_path / path).read_bytes()


@pytest.mark.parametrize(
    "first_name",
    ["src/code.rs", "./src/code.rs"],
    ids=["same-spelling", "dot-prefixed"],
)
def test_duplicate_path_of_equal_size_binds_every_reader_to_the_extracted_entry(
    first_name: str, tmp_path
) -> None:
    tar_bytes = _tarball([(first_name, _SHADOWED), ("src/code.rs", _EXTRACTED)])
    assert _extracted_bytes(tar_bytes, "src/code.rs", tmp_path) == _EXTRACTED
    inspector = TarSourceInspector(tar_bytes)
    expected = _EXTRACTED.decode()

    assert inspector.read_text_snapshot().texts == {"src/code.rs": expected}
    assert inspector.read_full_text("src/code.rs") == expected
    assert inspector.read("src/code.rs", 1, 1)["lines"] == [
        {"line": 1, "text": expected.rstrip("\n")}
    ]
    assert inspector.search("cheats")["match_count"] == 1
    # The shadowed body is never read, so it cannot contribute search hits.
    assert inspector.search("honest")["match_count"] == 0


def test_duplicate_path_manifest_and_file_diff_agree_with_extraction() -> None:
    # The reference matches the SHADOWED body; the code that actually ships is
    # the later entry. Binding to the wrong entry would make the manifest call
    # the file unchanged and hide the change from a source review.
    reference = {"src/code.rs": _SHADOWED.decode()}
    candidate_inspector = TarSourceInspector(
        _tarball([("src/code.rs", _SHADOWED), ("src/code.rs", _EXTRACTED)])
    )
    candidate = candidate_inspector.read_text_snapshot().texts

    manifest = build_source_diff_manifest(candidate, reference)
    (row,) = manifest["files"]
    assert row["path"] == "src/code.rs"
    assert row["status"] == "modified"

    diff = unified_diff_for_file("src/code.rs", candidate, reference)
    assert diff["identical"] is False
    assert "+fn run() { cheats(); }" in diff["diff_lines"]
    assert candidate["src/code.rs"] == candidate_inspector.read_full_text("src/code.rs")
