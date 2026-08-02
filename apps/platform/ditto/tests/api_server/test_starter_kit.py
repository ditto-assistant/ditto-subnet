"""Unit coverage for the starter-kit baseline diff.

The point of this feature is subtraction: an operator reviewing a quarantine
should see the miner's own code, not the ~36 kit files every submission carries.
These tests pin the behaviours that make that subtraction trustworthy — stock
code is recognized even when it is not the tip revision, and the custom-surface
total never counts kit code as authored work.
"""

from ditto.api_server.source_diff import build_baseline_diff_manifest
from ditto.api_server.starter_kit import (
    align_candidate_paths,
    is_stock_kit_text,
    starter_kit_head_text,
    starter_kit_provenance,
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
