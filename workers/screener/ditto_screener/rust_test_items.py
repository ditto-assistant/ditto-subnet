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


__all__ = ["is_rust_test_only_attribute"]
