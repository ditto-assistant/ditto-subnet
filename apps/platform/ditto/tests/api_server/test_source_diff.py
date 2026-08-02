"""Unit coverage for the copy-review per-file source diff."""

from ditto.api_server.source_diff import (
    build_source_diff_manifest,
    unified_diff_for_file,
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
    assert by_path["same.py"]["status"] == "identical"
    assert by_path["same.py"]["similarity"] == 1.0
    assert by_path["changed.py"]["status"] == "modified"
    assert by_path["changed.py"]["added_lines"] == 1
    assert by_path["changed.py"]["removed_lines"] == 1
    assert by_path["added.py"]["status"] == "added"
    assert by_path["removed.py"]["status"] == "removed"


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
