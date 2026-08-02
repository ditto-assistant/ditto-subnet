"""Category guards that cannot match any scored slug."""

from __future__ import annotations

from ditto_screener.category_guards import (
    CATEGORIES,
    find_unmatchable_category_guards,
    guard_report,
)
from ditto_screener.source_signals import mask_comments


def _scan(source: str, path: str = "src/memcase.rs"):
    return find_unmatchable_category_guards([(path, mask_comments(source))])


def test_the_transposed_literal_is_reported_with_its_nearest_slug() -> None:
    """The cliM@X shape, verbatim.

    `temporal-reasoning` contains `poral-reas`. The submission wrote
    `proal-reas`, transposing two characters, so the genuine pre-model return
    inside the branch is dead code. Two reviews escalated it at 0.99.
    """
    guards = _scan('if category.contains("proal-reas") {\n    return answer;\n}\n')

    assert len(guards) == 1
    assert guards[0].literal == "proal-reas"
    assert guards[0].operator == "contains"
    assert guards[0].line == 1
    assert guards[0].nearest == "temporal-reasoning"


def test_the_intended_literal_is_not_reported() -> None:
    """`poral-reas` really does match, so the branch is live and stays silent."""
    assert _scan('if category.contains("poral-reas") { return answer; }\n') == []


def test_real_slugs_and_prefixes_are_not_reported() -> None:
    for literal in ("temporal-reasoning", "aggreg", "assistant-recall", "canary"):
        assert _scan(f'if kind.contains("{literal}") {{ go(); }}\n') == [], literal


def test_equality_against_a_real_slug_is_live() -> None:
    assert _scan('if category == "temporal-depth" { go(); }\n') == []


def test_equality_against_a_substring_cannot_match() -> None:
    """`contains("temporal")` matches; `== "temporal"` never does."""
    guards = _scan('if category == "temporal" { go(); }\n')
    assert len(guards) == 1 and guards[0].operator == "=="


def test_normalizing_calls_between_subject_and_comparison_are_followed() -> None:
    guards = _scan('if case_category.to_lowercase().contains("proal-reas") { go(); }\n')
    assert len(guards) == 1


def test_case_id_is_not_treated_as_a_category() -> None:
    """`case_id` carries a different vocabulary, including `preflight:`."""
    assert _scan('if case_id.starts_with("preflight:") { return ack(); }\n') == []
    assert _scan('if req.case_id.contains("zzz-not-a-slug") { go(); }\n') == []


def test_a_literal_quoted_in_a_comment_is_not_a_guard() -> None:
    source = '// we used to check category.contains("proal-reas") here\ncall_model();\n'
    assert _scan(source) == []


def test_report_is_advisory_and_names_its_reasoning() -> None:
    report = guard_report(_scan('if category.contains("proal-reas") { go(); }\n'))
    assert report["authority"] == "advisory"
    assert report["vocabulary_size"] == len(CATEGORIES) == 32
    assert report["guards"][0]["nearest_category"] == "temporal-reasoning"


def test_every_shipped_slug_is_self_consistent() -> None:
    """No slug may be reported unmatchable against its own vocabulary."""
    for slug in CATEGORIES:
        assert _scan(f'if category == "{slug}" {{ go(); }}\n') == [], slug
