"""Conservative recognition of Rust items that require the test build."""

from __future__ import annotations

import re

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
    Literal contents are blanked here so fake attributes and literal braces
    cannot shift the item boundary. An uncertain boundary remains visible.
    """
    structural_source = _mask_literal_contents("\n".join(comment_masked_lines))
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


def _mask_literal_contents(text: str) -> str | None:
    """Blank Rust string and char contents while preserving braces and lines.

    Returns None for an unterminated literal, leaving the whole file visible
    to the static detector instead of guessing its test-item boundary.
    """
    chars = list(text)
    length = len(text)
    index = 0
    while index < length:
        char = text[index]
        if char in {"r", "b"}:
            cursor = index + (2 if text.startswith("br", index) else 1)
            hashes = 0
            while cursor < length and text[cursor] == "#":
                hashes += 1
                cursor += 1
            if cursor < length and text[cursor] == '"':
                terminator = '"' + "#" * hashes
                end = text.find(terminator, cursor + 1)
                if end < 0:
                    return None
                for offset in range(cursor + 1, end):
                    if chars[offset] not in "\r\n":
                        chars[offset] = " "
                index = end + len(terminator)
                continue
        if char == '"':
            cursor = index + 1
            while cursor < length:
                if text[cursor] == "\\":
                    cursor += 2
                    continue
                if text[cursor] == '"':
                    break
                if chars[cursor] not in "\r\n":
                    chars[cursor] = " "
                cursor += 1
            if cursor >= length:
                return None
            index = cursor + 1
            continue
        if char == "'":
            for span in (3, 4):
                if text[index + span - 1 : index + span] == "'":
                    for offset in range(index + 1, index + span - 1):
                        if chars[offset] not in "\r\n":
                            chars[offset] = " "
                    index += span
                    break
            else:
                index += 1
            continue
        index += 1
    return "".join(chars)


__all__ = ["is_rust_test_only_attribute", "test_only_item_lines"]
