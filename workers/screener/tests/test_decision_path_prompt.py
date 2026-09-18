"""Keep automated and operator review guidance aligned without activating v14."""

from pathlib import Path

import pytest

from ditto_screener.adjudicator import _system_prompt
from ditto_screener.decision_path_prompt import (
    DECISION_PATH_GUIDANCE,
    V13_BATCH_READS_GUIDANCE,
)
from ditto_screener.source_review import _source_review_system_prompt


@pytest.mark.parametrize("render", [_source_review_system_prompt, _system_prompt])
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
