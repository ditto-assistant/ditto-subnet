"""Unit coverage for citation admissibility."""

from __future__ import annotations

import pytest

from ditto_screener.evidence_quality import citation_admissibility

_SOURCE = """\
// a leading note
/// a doc comment
#[derive(Debug, Clone)]
#[cfg(feature = "grounding")]
use std::collections::HashMap;
pub mod helpers;

fn answer(case: &str) -> String {
    let v = table(case);
    format!("The answer is {v}.")
}
    // trailing prose
}
let base = "https://api.example/v1"; // a real statement with a trailing note
"""


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (1, "comment-or-blank"),
        (2, "comment-or-blank"),
        (3, "attribute-only"),
        (5, "declaration-only"),
        (6, "declaration-only"),
        (7, "blank"),
        (12, "comment-or-blank"),
        (13, "delimiter-only"),
    ],
)
def test_inert_lines_are_inadmissible(line: int, reason: str) -> None:
    verdict = citation_admissibility("src/main.rs", _SOURCE, line)
    assert not verdict.admissible
    assert verdict.reason == reason


@pytest.mark.parametrize("line", [4, 8, 9, 10, 14])
def test_executable_and_gate_lines_are_admissible(line: int) -> None:
    # 4 is a `cfg` reachability gate; 14 is a statement that merely ends in a
    # comment. Neither is inert.
    assert citation_admissibility("src/main.rs", _SOURCE, line).admissible


def test_a_one_line_signature_and_body_stays_admissible() -> None:
    """The cheapest place to hide from a signature filter.

    `fn a(x: &str) -> String { table(x) }` is a signature *and* the violating
    body on one line, which is why signature lines are not filtered at all.
    """
    source = "#[inline]\nfn a(x: &str) -> String { answer_table(x) }\n"
    assert citation_admissibility("src/main.rs", source, 2).admissible


def test_a_deref_assignment_is_not_a_comment() -> None:
    """`*slot = ...` starts with `*` but writes the graded slot."""
    source = 'fn set(slot: &mut String, v: &str) {\n    *slot = format!("{v}.");\n}\n'
    assert citation_admissibility("src/main.rs", source, 2).admissible


def test_a_brace_inside_a_comment_cannot_open_a_test_region() -> None:
    source = (
        "#[cfg(test)]\n"
        "mod t {\n"
        "    fn helper() -> u8 { 1 }\n"
        "}\n"
        "// a stray closing brace in prose }\n"
        "fn served(case: &str) -> String { answer_table(case) }\n"
    )
    assert not citation_admissibility("src/main.rs", source, 3).admissible
    # The served function sits after the region and stays citable.
    assert citation_admissibility("src/main.rs", source, 6).admissible


def test_cfg_not_test_body_remains_production_evidence() -> None:
    source = (
        "#[cfg(not(test))]\n"
        "fn served() {\n"
        '    let path = "/root/private";\n'
        "    read(path);\n"
        "}\n"
    )

    assert citation_admissibility("src/main.rs", source, 3).admissible
    assert citation_admissibility("src/main.rs", source, 4).admissible


def test_test_paths_are_inadmissible() -> None:
    verdict = citation_admissibility("tests/harness.rs", "fn t() { go(); }\n", 1)
    assert not verdict.admissible
    assert verdict.reason == "test-only-path"


def test_an_opaque_member_keeps_its_citation() -> None:
    """The path is proven; the line is unverifiable by design."""
    assert citation_admissibility("fixtures/model.onnx", None, 1).admissible


def test_an_out_of_range_line_is_left_to_the_existing_bounds_check() -> None:
    assert citation_admissibility("src/main.rs", "fn a() {}\n", 99).admissible
