"""Conservative recognition of Rust items that require the test build."""

from __future__ import annotations

import re

from ditto_screener.source_masking import mask_rust_literals

# Only erase attributes that affirmatively require ``test``. Unknown cfg
# expressions stay production-visible: in particular, ``cfg(not(test))`` is
# the normal production branch and ``cfg(any(test, feature = ...))`` can be.
_TEST_ONLY_ATTRIBUTE = re.compile(
    r"#\s*\[\s*(?:test|cfg\s*\(\s*(?:test|all\s*\(\s*test(?:\s*,[^]]*)?\))"
    r"\s*\))\s*\]"
)


def is_rust_test_only_attribute(line: str) -> bool:
    """Return whether an attribute positively restricts its item to tests."""
    return _TEST_ONLY_ATTRIBUTE.search(line) is not None


def test_only_item_lines(comment_masked_lines: list[str]) -> frozenset[int]:
    """Find test-gated Rust items without swallowing neighboring served code.

    Input comments must already be blanked, with line positions preserved.
    Literals are blanked here by the Rust lexer (raw strings, char literals
    versus lifetimes) so fake attributes and literal braces cannot shift the
    item boundary. Source it cannot lex, like an uncertain boundary, remains
    visible.
    """
    structural_source = mask_rust_literals("\n".join(comment_masked_lines))
    if structural_source is None:
        return frozenset()
    lines = structural_source.splitlines()
    marked: set[int] = set()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("#[") or not is_rust_test_only_attribute(line):
            continue
        span: set[int] = set()
        depth = 0
        opened = False
        complete = False
        for cursor in range(index, len(lines)):
            span.add(cursor + 1)
            depth += lines[cursor].count("{")
            depth -= lines[cursor].count("}")
            if "{" in lines[cursor]:
                opened = True
            if opened and depth <= 0:
                complete = True
                break
            if not opened and lines[cursor].rstrip().endswith(";"):
                complete = True
                break
            if not opened and cursor > index + 8:
                break
        if complete:
            marked.update(span)
    return frozenset(marked)


__all__ = ["is_rust_test_only_attribute", "test_only_item_lines"]
