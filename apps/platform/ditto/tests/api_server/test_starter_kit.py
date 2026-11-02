"""Unit coverage for the starter-kit baseline diff.

The point of this feature is subtraction: an operator reviewing a quarantine
should see the miner's own code, not the ~36 kit files every submission carries.
These tests pin the behaviours that make that subtraction trustworthy — stock
code is recognized even when it is not the tip revision, and the custom-surface
total never counts kit code as authored work.
"""

import io
import tarfile

from ditto.api_server.source_diff import build_baseline_diff_manifest
from ditto.api_server.source_inspect import TarSourceInspector
from ditto.api_server.starter_kit import (
    align_candidate_paths,
    is_stock_kit_text,
    starter_kit_head_text,
    starter_kit_provenance,
    strip_wrapping_root,
    wrapping_root,
)


def test_packaged_baseline_loads_with_pinned_provenance() -> None:
    provenance = starter_kit_provenance()
    assert provenance["source"].endswith("dittobench-starter-kit")
    assert len(provenance["revision"]) == 40
    assert len(provenance["commit_set_sha256"]) == 64
    assert int(provenance["commit_count"]) > 0

    head = starter_kit_head_text()
    # The kit is a Rust harness crate; these anchor that we shipped real text
    # rather than an empty or hash-only bundle.
    assert "Cargo.toml" in head
    assert "src/baseline.rs" in head
    assert head["src/baseline.rs"].strip()


def test_head_files_are_recognized_as_stock() -> None:
    head = starter_kit_head_text()
    assert is_stock_kit_text(head["src/baseline.rs"]) is True


def test_reformatted_kit_file_is_still_stock() -> None:
    # A miner who only re-indents or re-comments kit code has authored nothing;
    # the normalized channel must catch that or the delta fills with noise.
    original = starter_kit_head_text()["src/baseline.rs"]
    reformatted = "\n".join(f"    {line}" for line in original.splitlines())
    assert is_stock_kit_text(reformatted) is True


def test_miner_written_code_is_not_stock() -> None:
    assert is_stock_kit_text("fn solve_as_of() -> u64 { 42 }\n") is False


def test_custom_surface_excludes_stock_kit_files() -> None:
    baseline = {"kit.rs": "a\nb\nc\n"}
    candidate = {
        # Kit file from an OLDER revision: differs from the tip, still not the
        # miner's work, so it must not inflate the custom surface.
        "kit.rs": "a\nb\nc\nd\n",
        "solver.rs": "one\ntwo\n",
    }

    def is_stock(text: str) -> bool:
        return text == "a\nb\nc\nd\n"

    manifest = build_baseline_diff_manifest(candidate, baseline, is_stock)
    by_path = {entry["path"]: entry for entry in manifest["files"]}

    assert by_path["kit.rs"]["stock_kit"] is True
    assert by_path["solver.rs"]["stock_kit"] is False
    assert manifest["stock_kit_count"] == 1
    assert manifest["custom_file_count"] == 1
    # Only solver.rs's two lines count as authored.
    assert manifest["custom_added_lines"] == 2


def test_identical_files_are_stock_without_consulting_the_lineage() -> None:
    manifest = build_baseline_diff_manifest(
        {"kit.rs": "same\n"}, {"kit.rs": "same\n"}, lambda _text: False
    )
    entry = manifest["files"][0]
    assert entry["status"] == "identical"
    assert entry["stock_kit"] is True
    assert manifest["custom_added_lines"] == 0


def test_removed_files_never_count_as_custom_surface() -> None:
    # A kit file the miner deleted is not code they wrote.
    manifest = build_baseline_diff_manifest(
        {}, {"kit.rs": "a\nb\n"}, lambda _text: False
    )
    assert manifest["files"][0]["status"] == "removed"
    assert manifest["custom_file_count"] == 0
    assert manifest["custom_added_lines"] == 0


def test_path_alignment_strips_one_wrapping_directory() -> None:
    head = starter_kit_head_text()
    nested = {f"agent/{path}": text for path, text in head.items()}
    aligned = align_candidate_paths(nested)
    assert "Cargo.toml" in aligned
    assert aligned["Cargo.toml"] == head["Cargo.toml"]


def test_path_alignment_leaves_already_aligned_archives_alone() -> None:
    head = starter_kit_head_text()
    candidate = dict(head)
    assert align_candidate_paths(candidate) is candidate


def test_path_alignment_leaves_genuinely_custom_layouts_alone() -> None:
    # One shared root, but stripping it produces no kit overlap: this archive
    # really is laid out its own way, so inventing a match would be wrong.
    candidate = {"weird/one.rs": "x\n", "weird/two.rs": "y\n"}
    assert align_candidate_paths(candidate) is candidate


# Issue #480. A kit-derived submission whose authored src/baseline.rs is large:
# the kit's own fixture JSON (~2.4 MB) is stored before src/ in tar order.
AUTHORED_LINES = 9952


def _authored_baseline() -> str:
    return "".join(
        f"pub fn authored_step_{i:05d}(x: u64) -> u64 {{ x ^ {i} }}\n"
        for i in range(AUTHORED_LINES)
    )


def _kit_submission_tarball(root: str = "") -> bytes:
    files = dict(starter_kit_head_text())
    files["src/baseline.rs"] = _authored_baseline()
    ordered = sorted(files, key=lambda path: (not path.startswith("fixtures/"), path))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ordered:
            raw = files[name].encode()
            member = tarfile.TarInfo(f"{root}{name}")
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return buffer.getvalue()


def test_large_authored_baseline_behind_kit_fixtures_is_counted() -> None:
    # Failed before the fix: the fixtures spent the budget in archive order, so
    # src/baseline.rs came back {status: removed, candidate_lines: 0,
    # reference_lines: 739} and custom_added_lines was 0.
    inspector = TarSourceInspector(_kit_submission_tarball())
    candidate = align_candidate_paths(inspector.read_all_text())
    manifest = build_baseline_diff_manifest(
        candidate, starter_kit_head_text(), is_stock_kit_text
    )
    entry = {row["path"]: row for row in manifest["files"]}["src/baseline.rs"]
    assert entry["status"] == "modified"
    assert entry["candidate_lines"] == AUTHORED_LINES
    assert entry["stock_kit"] is False
    assert manifest["custom_added_lines"] >= AUTHORED_LINES


def test_budget_skipped_kit_files_are_omitted_never_removed() -> None:
    inspector = TarSourceInspector(_kit_submission_tarball())
    snapshot = inspector.read_text_snapshot()
    # The kit alone exceeds the combined text budget, so its largest fixture is
    # skipped; the authored source, being smaller, is not.
    assert snapshot.omitted_paths == ["fixtures/seed-user/pairs.json"]
    assert "src/baseline.rs" in snapshot.texts

    manifest = build_baseline_diff_manifest(
        snapshot.texts,
        starter_kit_head_text(),
        is_stock_kit_text,
        omitted=snapshot.omitted_paths,
    )
    by_path = {row["path"]: row for row in manifest["files"]}
    assert "fixtures/seed-user/pairs.json" not in by_path
    assert all(row["status"] != "removed" for row in manifest["files"])
    assert manifest["removed_count"] == 0
    assert manifest["omitted_paths"] == ["fixtures/seed-user/pairs.json"]
    assert manifest["omitted_file_count"] == 1
    assert manifest["custom_added_lines_complete"] is False
    assert manifest["custom_added_lines"] >= AUTHORED_LINES
    assert manifest["file_count"] + manifest["omitted_file_count"] == len(
        starter_kit_head_text()
    )


def test_wrapping_root_aligns_loaded_and_skipped_paths_together() -> None:
    inspector = TarSourceInspector(_kit_submission_tarball(root="agent/"))
    snapshot = inspector.read_text_snapshot()
    root = wrapping_root([*snapshot.texts, *snapshot.omitted_paths])
    assert root == "agent"
    assert [strip_wrapping_root(path, root) for path in snapshot.omitted_paths] == [
        "fixtures/seed-user/pairs.json"
    ]
    assert strip_wrapping_root("src/lib.rs", None) == "src/lib.rs"


def test_wrapping_root_is_none_for_mixed_or_empty_layouts() -> None:
    assert wrapping_root([]) is None
    assert wrapping_root(["agent/Cargo.toml", "README.md"]) is None
    assert wrapping_root(["weird/one.rs", "weird/two.rs"]) is None
