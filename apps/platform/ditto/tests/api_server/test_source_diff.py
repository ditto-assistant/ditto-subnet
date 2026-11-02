"""Unit coverage for the copy-review per-file source diff."""

from ditto.api_server.source_diff import (
    MAX_OMITTED_PATHS,
    build_baseline_diff_manifest,
    build_source_diff_manifest,
    unified_diff_for_file,
    without_omitted,
)


def _manifest_by_path(candidate: dict[str, str], reference: dict[str, str]) -> dict:
    manifest = build_source_diff_manifest(candidate, reference)
    return {entry["path"]: entry for entry in manifest["files"]} | {"_": manifest}


def test_manifest_classifies_every_file_status() -> None:
    candidate = {
        "same.py": "a\nb\nc\n",
        "changed.py": "a\nB\nc\n",
        "added.py": "only in candidate\n",
    }
    reference = {
        "same.py": "a\nb\nc\n",
        "changed.py": "a\nb\nc\n",
        "removed.py": "only in reference\n",
    }
    manifest = build_source_diff_manifest(candidate, reference)
    by_path = {entry["path"]: entry for entry in manifest["files"]}

    assert manifest["file_count"] == 4
    assert manifest["identical_count"] == 1
    assert manifest["modified_count"] == 1
    assert manifest["added_count"] == 1
    assert manifest["removed_count"] == 1
    assert manifest["renamed_count"] == 0
    assert by_path["same.py"]["status"] == "identical"
    assert by_path["same.py"]["similarity"] == 1.0
    assert by_path["changed.py"]["status"] == "modified"
    assert by_path["changed.py"]["added_lines"] == 1
    assert by_path["changed.py"]["removed_lines"] == 1
    assert by_path["added.py"]["status"] == "added"
    assert by_path["removed.py"]["status"] == "removed"


def test_exact_path_identical_is_not_reclassified_as_rename() -> None:
    # Exact-path pairing still wins: a file present on both sides stays
    # identical even when leftover add/remove files exist alongside it.
    body = "fn stolen() {\n    1 + 1\n}\n"
    candidate = {"src/whitycat.rs": body, "src/extra.rs": "fn extra() {}\n"}
    reference = {"src/whitycat.rs": body, "src/gone.rs": "fn gone() {}\n"}
    manifest = build_source_diff_manifest(candidate, reference)
    by_path = {entry["path"]: entry for entry in manifest["files"]}

    assert by_path["src/whitycat.rs"]["status"] == "identical"
    assert by_path["src/whitycat.rs"]["normalized_identical"] is True
    assert by_path["src/extra.rs"]["status"] == "added"
    assert by_path["src/gone.rs"]["status"] == "removed"
    assert manifest["renamed_count"] == 0


def test_renamed_identical_normalized_content_is_not_added_and_removed() -> None:
    # Copiers rename the stolen residual (whitycat.rs -> operating_rules.rs)
    # so exact-path pairing would otherwise report added + removed and hide
    # that the miner-authored file is still in the tarball.
    body = "fn stolen() {\n    1 + 1\n}\n"
    candidate = {"src/operating_rules.rs": body}
    reference = {"src/whitycat.rs": body}
    manifest = build_source_diff_manifest(candidate, reference)
    assert manifest["added_count"] == 0
    assert manifest["removed_count"] == 0
    assert manifest["renamed_count"] == 1
    assert len(manifest["files"]) == 1
    entry = manifest["files"][0]
    assert entry["status"] == "renamed"
    assert entry["path"] == "src/operating_rules.rs"
    assert entry["from_path"] == "src/whitycat.rs"
    assert entry["to_path"] == "src/operating_rules.rs"
    assert entry["normalized_identical"] is True
    assert entry["similarity"] == 1.0
    assert "src/whitycat.rs" not in {row["path"] for row in manifest["files"]}


def test_reformatted_rename_is_normalized_identical() -> None:
    reference = {"src/whitycat.rs": "fn f(x: i32) -> i32 {\n    x + 1  // add one\n}\n"}
    candidate = {
        "src/agent_policy.rs": (
            "fn f(x: i32) -> i32 {\n        x+1   /* incremented */\n}\n"
        )
    }
    manifest = build_source_diff_manifest(candidate, reference)
    entry = manifest["files"][0]
    assert entry["status"] == "renamed"
    assert entry["from_path"] == "src/whitycat.rs"
    assert entry["to_path"] == "src/agent_policy.rs"
    assert entry["normalized_identical"] is True
    assert manifest["added_count"] == 0
    assert manifest["removed_count"] == 0


def test_genuinely_different_added_file_stays_added() -> None:
    candidate = {
        "same.py": "a\nb\nc\n",
        "added.py": "fn brand_new_solver() -> u64 { 42 }\n",
    }
    reference = {
        "same.py": "a\nb\nc\n",
        "removed.py": "mod leftover_kit_shim { fn unused() {} }\n",
    }
    manifest = build_source_diff_manifest(candidate, reference)
    by_path = {entry["path"]: entry for entry in manifest["files"]}
    assert by_path["added.py"]["status"] == "added"
    assert by_path["removed.py"]["status"] == "removed"
    assert manifest["added_count"] == 1
    assert manifest["removed_count"] == 1
    assert manifest["renamed_count"] == 0


def test_reformatted_copy_is_flagged_normalized_identical() -> None:
    # Same Rust code, different comments and indentation: raw text differs, but
    # the normalized-source canonicalization (C-style comment + whitespace
    # stripping) collapses them, so an operator sees the copy even though the
    # byte diff is noisy.
    reference = {"m.rs": "fn f(x: i32) -> i32 {\n    x + 1  // add one\n}\n"}
    candidate = {"m.rs": "fn f(x: i32) -> i32 {\n        x+1   /* incremented */\n}\n"}
    manifest = build_source_diff_manifest(candidate, reference)
    entry = manifest["files"][0]
    assert entry["status"] == "modified"
    assert entry["normalized_identical"] is True


def test_unified_diff_reports_presence_and_body() -> None:
    candidate = {"m.py": "line1\nCHANGED\nline3\n"}
    reference = {"m.py": "line1\nline2\nline3\n"}
    detail = unified_diff_for_file("m.py", candidate, reference)
    assert detail["candidate_present"] is True
    assert detail["reference_present"] is True
    assert detail["identical"] is False
    body = "\n".join(detail["diff_lines"])
    assert "-line2" in body
    assert "+CHANGED" in body
    assert detail["truncated"] is False


def test_unified_diff_of_added_file_marks_reference_absent() -> None:
    detail = unified_diff_for_file("new.py", {"new.py": "x\n"}, {})
    assert detail["candidate_present"] is True
    assert detail["reference_present"] is False


def test_unified_diff_of_renamed_file_pairs_the_counterpart() -> None:
    body = "fn stolen() {\n    1 + 1\n}\n"
    candidate = {"src/operating_rules.rs": body}
    reference = {"src/whitycat.rs": body}
    detail = unified_diff_for_file("src/operating_rules.rs", candidate, reference)
    assert detail["candidate_present"] is True
    assert detail["reference_present"] is True
    assert detail["identical"] is True
    assert detail["from_path"] == "src/whitycat.rs"
    assert detail["to_path"] == "src/operating_rules.rs"
    assert detail["diff_lines"] == []


def test_unified_diff_missing_path_raises_keyerror() -> None:
    try:
        unified_diff_for_file("ghost.py", {"a.py": "x"}, {"b.py": "y"})
    except KeyError:
        return
    raise AssertionError("expected KeyError for a path in neither artifact")


def test_unified_diff_body_is_bounded() -> None:
    reference = {"big.py": "\n".join(f"ref{i}" for i in range(5000)) + "\n"}
    candidate = {"big.py": "\n".join(f"cand{i}" for i in range(5000)) + "\n"}
    detail = unified_diff_for_file("big.py", candidate, reference, max_lines=100)
    assert detail["truncated"] is True
    assert len(detail["diff_lines"]) == 100


def test_manifest_file_list_is_bounded() -> None:
    candidate = {f"f{i}.py": "x\n" for i in range(10)}
    reference = {f"f{i}.py": "y\n" for i in range(10)}
    manifest = build_source_diff_manifest(candidate, reference, max_files=3)
    assert manifest["file_count"] == 10
    assert len(manifest["files"]) == 3
    assert manifest["truncated"] is True


def test_manifest_without_omissions_reports_an_empty_omitted_set() -> None:
    manifest = build_source_diff_manifest({"a.py": "x\n"}, {"a.py": "x\n"})
    assert manifest["omitted_file_count"] == 0
    assert manifest["omitted_paths"] == []


def test_file_skipped_in_the_candidate_is_omitted_not_removed() -> None:
    # Issue #480: the bounded reader skipped the candidate's big file, so only
    # the reference side had text for it. It was never compared; calling it
    # "removed" told an operator the miner deleted code they actually wrote.
    candidate = {"src/lib.rs": "fn a() {}\n"}
    reference = {"src/lib.rs": "fn a() {}\n", "src/baseline.rs": "kit\n" * 739}
    manifest = build_source_diff_manifest(
        candidate, reference, omitted=["src/baseline.rs"]
    )
    assert [row["path"] for row in manifest["files"]] == ["src/lib.rs"]
    assert manifest["removed_count"] == 0
    assert manifest["file_count"] == 1
    assert manifest["omitted_file_count"] == 1
    assert manifest["omitted_paths"] == ["src/baseline.rs"]


def test_file_skipped_in_the_reference_is_omitted_not_added() -> None:
    candidate = {"src/lib.rs": "fn a() {}\n", "data.json": "{}\n"}
    reference = {"src/lib.rs": "fn a() {}\n"}
    manifest = build_source_diff_manifest(candidate, reference, omitted=["data.json"])
    assert manifest["added_count"] == 0
    assert "data.json" not in {row["path"] for row in manifest["files"]}
    assert manifest["omitted_paths"] == ["data.json"]


def test_omitted_file_is_not_rename_paired() -> None:
    body = "fn stolen() {\n    1 + 1\n}\n"
    manifest = build_source_diff_manifest(
        {"src/new_name.rs": body},
        {"src/old_name.rs": body},
        omitted=["src/old_name.rs"],
    )
    assert manifest["renamed_count"] == 0
    assert manifest["added_count"] == 1
    assert manifest["omitted_paths"] == ["src/old_name.rs"]


def test_omitted_paths_are_bounded_but_counted_in_full() -> None:
    omitted = [f"fixtures/f{i:04d}.json" for i in range(MAX_OMITTED_PATHS + 7)]
    manifest = build_source_diff_manifest({}, {}, omitted=[*omitted, omitted[0]])
    assert manifest["omitted_file_count"] == MAX_OMITTED_PATHS + 7
    assert manifest["omitted_paths"] == sorted(omitted)[:MAX_OMITTED_PATHS]


def test_without_omitted_returns_the_inputs_when_nothing_was_skipped() -> None:
    candidate, reference = {"a": "1"}, {"b": "2"}
    kept_candidate, kept_reference, skipped = without_omitted(candidate, reference, [])
    assert kept_candidate is candidate
    assert kept_reference is reference
    assert skipped == []


def test_baseline_custom_lines_are_summed_before_the_file_list_cut() -> None:
    candidate = {
        "a_solver.rs": "1\n2\n",
        "b_solver.rs": "1\n2\n3\n",
        "c_solver.rs": "1\n2\n3\n4\n",
    }
    manifest = build_baseline_diff_manifest(
        candidate, {}, lambda _text: False, max_files=1
    )
    assert len(manifest["files"]) == 1
    assert manifest["truncated"] is True
    assert manifest["custom_file_count"] == 3
    assert manifest["custom_added_lines"] == 9
    # The list cut does not make the total a lower bound; only omission does.
    assert manifest["custom_added_lines_complete"] is True


def test_baseline_stock_count_covers_files_past_the_cut() -> None:
    manifest = build_baseline_diff_manifest(
        {f"kit{i}.rs": "same\n" for i in range(5)},
        {f"kit{i}.rs": "same\n" for i in range(5)},
        lambda _text: False,
        max_files=2,
    )
    assert manifest["stock_kit_count"] == 5
    assert manifest["identical_count"] == 5


def test_baseline_omission_makes_the_total_a_lower_bound() -> None:
    baseline = {"src/baseline.rs": "kit\n" * 739, "Cargo.toml": "[package]\n"}
    candidate = {"Cargo.toml": "[package]\n", "src/solver.rs": "a\nb\n"}
    manifest = build_baseline_diff_manifest(
        candidate, baseline, lambda _text: False, omitted=["src/baseline.rs"]
    )
    by_path = {row["path"]: row for row in manifest["files"]}
    assert "src/baseline.rs" not in by_path
    assert manifest["removed_count"] == 0
    assert manifest["custom_added_lines"] == 2
    assert manifest["custom_added_lines_complete"] is False
    assert manifest["omitted_file_count"] == 1
    assert manifest["omitted_paths"] == ["src/baseline.rs"]
    assert "renamed_count" not in manifest


def test_baseline_without_omission_is_complete() -> None:
    manifest = build_baseline_diff_manifest(
        {"src/solver.rs": "a\n"}, {}, lambda _text: False
    )
    assert manifest["custom_added_lines"] == 1
    assert manifest["custom_added_lines_complete"] is True
    assert manifest["omitted_file_count"] == 0
    assert manifest["omitted_paths"] == []
