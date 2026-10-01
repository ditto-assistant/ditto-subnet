"""Admissibility of a cited source location as anti-cheat evidence.

The source reviewer's output is untrusted model text. ``source_review`` already
drops citations that point at a member the archive does not contain, or past the
end of a readable file, before the finding is digest-bound — a hallucinated
location must never become signed evidence.

This module extends that same idea from "does the location exist" to "can the
location carry a behaviour". The review policy requires a finding to name the
trigger and the effect of a causal path. A blank line, a comment, a `use`
statement, a `#[derive]`, or a lone closing brace is none of those things: it
compiles to nothing, so it cannot be a trigger and cannot be an effect.

This is deliberately *not* a claim that the surrounding code is safe, and it is
not a hiding place. Nothing here removes a line from the reviewer's view — every
file is still read in full and every rule still runs over it. The filter changes
only what may be *cited as proof*, which is why it cannot be used to smuggle a
violation: a violation that executes is, by construction, on a line that is not
inert.

The declaration and attribute rules are Rust syntax and apply only to Rust
sources; in another language the same text (``mod = os;`` in Python,
``'#[test]'`` in a JavaScript string) is ordinary code.

Two exemptions keep the filter from eating load-bearing evidence:

- ``#[cfg(...)]`` / ``#[cfg_attr(...)]`` attributes stay admissible. They are the
  reachability gates a submission can flip between versions to bring dormant
  code to life, so an attribute-only line can genuinely be the trigger.
- Only lines that are *wholly* inert are dropped. A single line carrying both a
  signature and a body (``fn a(x: &str) -> String { table(x) }``) is code, and a
  trailing comment after a statement does not make the statement prose.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ditto_screener.source_masking import language_for_path
from ditto_screener.source_signals import mask_comments

# ``use``/``extern crate`` bring a name into scope; ``mod`` declares one. None of
# them is a trigger or an effect, though ``mod`` is retained as reachability
# context elsewhere.
_DECLARATION_ONLY = re.compile(
    r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?(?:use|extern\s+crate|mod)\b[^{]*;\s*$"
)
_CLOSING_ONLY = re.compile(r"^[\s)}\];,]*$")
_ATTRIBUTE_ONLY = re.compile(r"^\s*#!?\[.*\]\s*$")
_REACHABILITY_ATTRIBUTE = re.compile(r"\bcfg(?:_attr)?\s*\(")
_TEST_PATH = re.compile(r"(?:^|/)(?:tests?|benches)/")


@dataclass(frozen=True)
class Admissibility:
    """Whether one cited location may be used as evidence, and why not."""

    admissible: bool
    reason: str = ""


ADMISSIBLE = Admissibility(True)


def _inert_reason(code_line: str, raw_line: str, *, rust: bool) -> str:
    """Name why a line cannot carry a behaviour, or return ``""``."""
    # ``code_line`` is the comment-masked view. When it is blank but the raw
    # line is not, everything on the line was prose.
    if not code_line.strip():
        return "comment-or-blank" if raw_line.strip() else "blank"
    if (
        rust
        and _ATTRIBUTE_ONLY.match(code_line)
        and not _REACHABILITY_ATTRIBUTE.search(code_line)
    ):
        return "attribute-only"
    if rust and _DECLARATION_ONLY.match(code_line):
        return "declaration-only"
    if _CLOSING_ONLY.match(code_line):
        return "delimiter-only"
    return ""


def citation_admissibility(
    path: str,
    text: str | None,
    line: int,
    *,
    runtime_paths: frozenset[str] | Callable[[], frozenset[str]] = frozenset(),
) -> Admissibility:
    """Classify one ``path:line`` citation against the submitted source.

    ``text`` is the readable UTF-8 content of ``path``, or ``None`` when the
    member is opaque. An opaque member keeps its citation: the path is proven
    and the line is unverifiable by design, so refusing it would invent a
    false negative.

    A Rust ``#[cfg(test)]`` body remains citable because the served binary can
    enable that cfg with ``rustc --cfg test`` without using a test harness.
    The reviewer must still establish that the build serves the cited path.

    ``runtime_paths`` are members the build or served code explicitly makes
    executable (a Cargo target, ``include!``/``#[path]``, a script the
    Dockerfile runs). A ``tests/`` or ``benches/`` directory name does not make
    such a file inert, so it is exempt from the test-path rule; the comment
    and blank checks still apply. It may be passed as a callable, resolved
    only when a citation's path looks test-only.
    """
    normalized = path.removeprefix("./")
    if _TEST_PATH.search(normalized):
        if callable(runtime_paths):
            # Resolved only here: it walks every Rust member of the archive.
            runtime_paths = runtime_paths()
        if normalized not in runtime_paths:
            return Admissibility(False, "test-only-path")
    if text is None:
        return ADMISSIBLE
    raw_lines = text.splitlines()
    if not 1 <= line <= len(raw_lines):
        return ADMISSIBLE
    code_lines = mask_comments(text, normalized).splitlines()
    code_lines.extend([""] * (len(raw_lines) - len(code_lines)))
    rust = language_for_path(normalized) == "rust"
    reason = _inert_reason(code_lines[line - 1], raw_lines[line - 1], rust=rust)
    if reason:
        return Admissibility(False, reason)
    return ADMISSIBLE


__all__ = ["ADMISSIBLE", "Admissibility", "citation_admissibility"]
