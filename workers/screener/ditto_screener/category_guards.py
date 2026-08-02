"""Guards that compare a scored category against a literal that cannot match.

A submission that routes on the benchmark's category slug is doing something a
reviewer must look at. But a guard is only a trigger if it can actually fire,
and that is decidable: the category vocabulary is public, so a literal compared
against a category variable either is capable of matching a real slug or it is
not.

This exists because of a case the reviewer got structurally right and factually
wrong. A submission contained a genuine pre-model return behind
``if category.contains("proal-reas")``. The real label is ``temporal-reasoning``,
which contains ``poral-reas`` — the literal transposes two characters, so the
branch is dead and the code inside it never runs. Two reviews escalated it at
0.99 confidence and it sat in the manual queue because nothing short of
resolving the label set can tell those two strings apart by eye.

The label set is *ours*, not the miner's, so resolving it is a set-membership
test rather than an analysis problem.

**This module never releases anything on its own.** It annotates: it reports
that a cited guard cannot match, and what the nearest real slug is. The
inference "dead guard, therefore harmless" depends on this vocabulary being
current, and a stale vocabulary would turn a live violation into a silent
release. That trade is not worth making automatically, so the finding is
surfaced to the reviewer and the operator with its reasoning attached, and a
human still decides.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

_DATA = Path(__file__).parent / "data" / "bench-categories-v1.json"
_MAX_GUARDS = 16
# Only comparisons against a variable that plausibly holds the scored category.
# `case_id` is deliberately excluded: it carries a different vocabulary (and the
# reserved `preflight:` prefix), so a literal that fails to match a category
# slug says nothing about it.
_SUBJECT_KEYWORDS = ("categ", "kind", "label", "family", "bucket")
_NORMALIZERS = r"(?:\.\s*(?:to_lowercase|to_ascii_lowercase|trim|as_str)\s*\(\s*\))*"
_CATEGORY_SUBJECT = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_.]*)" + _NORMALIZERS + r"\s*"
    r"\.\s*(contains|starts_with|ends_with|eq|eq_ignore_ascii_case)"
    r"\s*\(\s*&?\s*\"([^\"]{2,64})\"",
    re.IGNORECASE,
)
_CATEGORY_EQUALITY = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_.]*)" + _NORMALIZERS + r"\s*==\s*\"([^\"]{2,64})\"",
    re.IGNORECASE,
)


def _is_category_subject(subject: str) -> bool:
    """Whether the compared variable plausibly holds the scored category.

    Judged on the final path segment, so ``req.case_category`` qualifies and
    ``req.case_id`` does not — ``case_id`` carries a different vocabulary,
    including the reserved ``preflight:`` prefix, and a literal that fails to
    match a category slug says nothing about it.
    """
    segment = subject.rsplit(".", 1)[-1].casefold()
    return any(keyword in segment for keyword in _SUBJECT_KEYWORDS)


@dataclass(frozen=True)
class UnmatchableGuard:
    """A category comparison whose literal cannot match any known slug."""

    path: str
    line: int
    subject: str
    literal: str
    operator: str
    nearest: str
    similarity: float


def _load_categories() -> tuple[str, ...]:
    payload = json.loads(_DATA.read_text())
    return tuple(payload["categories"])


CATEGORIES: tuple[str, ...] = _load_categories()


def _can_match(literal: str, operator: str) -> bool:
    value = literal.casefold()
    if operator in {"contains"}:
        return any(value in slug for slug in CATEGORIES)
    if operator == "starts_with":
        return any(slug.startswith(value) for slug in CATEGORIES)
    if operator == "ends_with":
        return any(slug.endswith(value) for slug in CATEGORIES)
    # Equality forms.
    return value in CATEGORIES


def _nearest(literal: str) -> tuple[str, float]:
    value = literal.casefold()
    best = ""
    score = 0.0
    for slug in CATEGORIES:
        ratio = SequenceMatcher(None, value, slug).ratio()
        if ratio > score:
            best, score = slug, ratio
    return best, round(score, 3)


def find_unmatchable_category_guards(
    files: Iterable[tuple[str, str]],
) -> list[UnmatchableGuard]:
    """Report category guards whose literal cannot match any scored slug.

    ``files`` are ``(path, comment-masked text)`` pairs. Callers pass the masked
    view so a literal quoted in a comment is not mistaken for a live guard.
    """
    guards: list[UnmatchableGuard] = []
    for path, text in files:
        for line_number, line in enumerate(text.splitlines(), 1):
            for pattern, has_operator in (
                (_CATEGORY_SUBJECT, True),
                (_CATEGORY_EQUALITY, False),
            ):
                for match in pattern.finditer(line[:4096]):
                    if has_operator:
                        subject, operator, literal = match.groups()
                    else:
                        subject, literal = match.groups()
                        operator = "=="
                    if not _is_category_subject(subject):
                        continue
                    if _can_match(literal, operator.casefold()):
                        continue
                    nearest, similarity = _nearest(literal)
                    guards.append(
                        UnmatchableGuard(
                            path=path,
                            line=line_number,
                            subject=subject,
                            literal=literal,
                            operator=operator.casefold(),
                            nearest=nearest,
                            similarity=similarity,
                        )
                    )
                    if len(guards) >= _MAX_GUARDS:
                        return guards
    return guards


def guard_report(guards: list[UnmatchableGuard]) -> dict[str, object]:
    """Render guards for the reviewer dossier and the operator record."""
    return {
        "vocabulary_size": len(CATEGORIES),
        "authority": "advisory",
        "meaning": (
            "The cited comparison cannot match any scored category slug, so the "
            "branch it guards is unreachable at runtime. Confirm the vocabulary "
            "is current before treating that as exculpatory."
        ),
        "guards": [
            {
                "path": guard.path,
                "line": guard.line,
                "subject": guard.subject,
                "operator": guard.operator,
                "literal": guard.literal,
                "nearest_category": guard.nearest,
                "similarity": guard.similarity,
            }
            for guard in guards
        ],
    }


__all__ = [
    "CATEGORIES",
    "UnmatchableGuard",
    "find_unmatchable_category_guards",
    "guard_report",
]
