"""Keep automated and operator review guidance aligned without activating v14."""

from pathlib import Path

import pytest

from ditto_screener.adjudicator import _system_prompt
from ditto_screener.decision_path_prompt import (
    DECISION_PATH_GUIDANCE,
    V13_BATCH_READS_GUIDANCE,
)
from ditto_screener.l2_review import _l2_review_system_prompt
from ditto_screener.source_review import _source_review_system_prompt


@pytest.mark.parametrize(
    "render", [_source_review_system_prompt, _l2_review_system_prompt, _system_prompt]
)
def test_decision_path_guidance_is_version_scoped(render) -> None:
    assert DECISION_PATH_GUIDANCE in render(13)
    for historical in (10, 11, 12):
        assert DECISION_PATH_GUIDANCE not in render(historical)
    with pytest.raises(ValueError, match="not implemented"):
        render(14)


def test_v13_does_not_inherit_area_count_stop_instruction() -> None:
    old_stop = "construction areas each have a cleared note"
    assert old_stop in _source_review_system_prompt(12)
    current = _source_review_system_prompt(13)
    assert old_stop not in current
    assert V13_BATCH_READS_GUIDANCE in current


def test_operator_checklist_matches_the_automated_safeguards() -> None:
    root = Path(__file__).resolve().parents[3]
    skill = root / ".agents/skills/backroom-review"
    checklist = skill / "references/decision-path-review.md"
    assert DECISION_PATH_GUIDANCE in checklist.read_text()
    for entry in (
        "SKILL.md",
        "references/review-bar.md",
        "references/review-rules.md",
        "scripts/review-loop-prompt.md",
    ):
        assert "decision-path-review.md" in (skill / entry).read_text()
    assert (root / ".claude/skills/backroom-review").resolve() == skill


@pytest.mark.parametrize(
    "render", [_source_review_system_prompt, _l2_review_system_prompt, _system_prompt]
)
def test_case_file_currency_rule_is_not_rejectable_alone(render) -> None:
    prompt = render(13)
    assert "I5 case-file currency safe harbor." in prompt
    assert "stacking them is not a second factor" in prompt
    assert "ditto-subnet#3088" in prompt
    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "policy-v13.md"
    ).read_text()
    for fragment in (
        "#### Clarification: case-file currency rules (2026-10-23)",
        "is not, by itself, a rejection ground under I5",
        "These elements are one behavior.",
        "are withdrawn as\nprecedent",
    ):
        assert fragment in policy


@pytest.mark.parametrize(
    "render", [_source_review_system_prompt, _l2_review_system_prompt, _system_prompt]
)
def test_routed_instruction_guidance_is_not_rejectable_alone(render) -> None:
    prompt = render(13)
    assert "I5 routed-guidance safe harbor." in prompt
    assert "resolve such I5 leads as refuted, not\nunresolved" in prompt
    assert "if the stored setting already matches" in prompt
    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "policy-v13.md"
    ).read_text()
    for fragment in (
        "#### Clarification: routed instruction guidance (2026-10-23)",
        "is not, by itself,\na rejection ground under I5",
        "Telling the model\nabout them is not I5",
        "SN-214 settings predicates included) are withdrawn as precedent",
    ):
        assert fragment in policy


@pytest.mark.parametrize(
    "render", [_source_review_system_prompt, _l2_review_system_prompt, _system_prompt]
)
def test_over_budget_retrieval_is_not_rejectable_alone(render) -> None:
    prompt = render(13)
    assert "W3 over-budget safe harbor." in prompt
    assert "resolve such I2/W3 leads as refuted, not unresolved" in prompt
    assert "deduplication dropping a later distinct record (W3)" in prompt
    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "policy-v13.md"
    ).read_text()
    for fragment in (
        "#### Clarification: over-budget retrieval (2026-10-23)",
        "is not,\nby itself, a rejection ground under W3, I2 or any other condition",
        "it does not drop a record that fits the budget",
        "size-triggered budget skip are\nwithdrawn as precedent",
    ):
        assert fragment in policy
