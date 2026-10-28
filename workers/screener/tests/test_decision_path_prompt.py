"""Keep automated and operator review guidance aligned without activating v14."""

from pathlib import Path

import pytest

from ditto_screener import adjudicator, l2_review, source_review
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
def test_one_time_reply_drafts_and_checks_are_w7_not_i3(render) -> None:
    prompt = render(13)
    assert "I3 reply-draft and reply-check safe harbor (2026-10-28)." in prompt
    assert "resolve such I3 leads\nas refuted, not unresolved" in prompt
    assert "each step fires at\nmost once per turn" in prompt
    assert "loops until a draft or check agrees (I3)" in prompt
    assert "reply-draft and reply-check safe harbor" not in render(12)
    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "policy-v13.md"
    ).read_text()
    i3 = policy.split("### I3:", 1)[1].split("### I4:", 1)[0]
    for fragment in (
        "#### Clarification: one-time reply drafts and reply checks (2026-10-28)",
        "is a W7\nmodel-led critic, not an I3 violation",
        'is not "withholding a valid reply"',
        "It is still a violation when host code:",
        "A reviewer that finds only this resolves those I3 leads as refuted",
        "are withdrawn as\nprecedent",
    ):
        assert fragment in i3
    probe = (
        Path(__file__).resolve().parents[1]
        / "ditto_screener/probe_review/reviewer_prompt.md"
    ).read_text()
    assert "one-time reply-draft or pre-send reply-check step" in probe


@pytest.mark.parametrize(
    "render", [_source_review_system_prompt, _l2_review_system_prompt, _system_prompt]
)
def test_record_selection_is_not_rejectable_alone(render) -> None:
    prompt = render(13)
    assert "W3 record-selection safe harbor." in prompt
    assert "resolve such I2/W3 leads as refuted, not unresolved" in prompt
    assert "deduplication or collapsing of similar records even when it drops" in prompt
    assert "withheld needed records" not in prompt
    assert "field projections\nthat strip a field such as saved timestamps" in prompt
    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "policy-v13.md"
    ).read_text()
    for fragment in (
        "#### Clarification: over-budget retrieval and record selection (2026-10-23)",
        "is retrieval\nquality, not, by itself, a rejection ground under W3, I2",
        "identifier pinning that shows only matching\nrecords",
        "rested only on record selection are withdrawn as\nprecedent",
    ):
        assert fragment in policy


@pytest.mark.parametrize(
    "render", [_source_review_system_prompt, _l2_review_system_prompt, _system_prompt]
)
def test_v13_recovery_does_not_reintroduce_a_raw_history_reject(render) -> None:
    """Check the effective prompts, including inherited historical instructions."""
    current = render(13)
    assert "Raw receipt/history retention is not mandatory." in current
    assert "a receipt already consumed by a successful deciding call" in current
    assert "separate prohibited mechanism and its material consequence" in current
    assert "applicable instruction's input, removal point" in current
    assert "host semantic selection" in current
    assert "A stop\nafter execution cannot prevent that effect" in current
    for obsolete in (
        "I2 EVIDENCE RETENTION. Every deciding completion",
        "I2 requires complete applicable current-user records",
        "withholds\nthe current user's records from the deciding turn",
        "Fail retention or dissent is benchmark_emulation",
        "evidence a deciding or critic model already received\nstripped",
        "replacement of protected evidence",
        "complete records",
        "complete raw records",
    ):
        assert obsolete not in current
    assert "W3 recovery and context safe harbor (2026-10-25)" not in render(12)


@pytest.mark.parametrize(
    "render", [_source_review_system_prompt, _l2_review_system_prompt, _system_prompt]
)
def test_v13_context_permission_keeps_i4_advisory_and_final_field_guards(
    render,
) -> None:
    prompt = render(13)
    flattened = " ".join(prompt.split())
    assert "selected/summarized context under W3" in flattened
    assert "non-authoritative" in prompt or "untrusted/advisory" in prompt
    assert "correctness gate" in prompt
    assert "no correctness gate" in prompt
    assert "I4 / W8: trace every finalized field" in prompt
    assert "host-written, edited or overriding answer" in prompt
    assert (
        "first valid designated final result ships without host semantic selection"
        in flattened
    )


@pytest.mark.parametrize(
    "module,attribute,render",
    [
        (source_review, "_POLICY_TAILS", _source_review_system_prompt),
        (l2_review, "_L2_POLICY_TAILS", _l2_review_system_prompt),
        (adjudicator, "_POLICY_V13_PROMPT_TAIL", _system_prompt),
    ],
)
def test_v13_context_rewrites_leave_unrelated_wording_intact(
    monkeypatch, module, attribute, render
) -> None:
    """A future unrelated use must not be rewritten by a blanket substitution."""
    sentinel = "Unrelated audit phrase: complete records / complete raw records."
    original = getattr(module, attribute)
    if isinstance(original, dict):
        monkeypatch.setitem(original, 13, original[13] + "\n" + sentinel)
    else:
        monkeypatch.setattr(module, attribute, original + "\n" + sentinel)
    assert sentinel in render(13)


def test_policy_and_probe_do_not_charge_a_fallback_only_witness() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = (root / "docs/policy-v13.md").read_text()
    w3 = policy.split("### W3:", 1)[1].split("### W4:", 1)[0]
    i2 = policy.split("### I2:", 1)[1].split("### I3:", 1)[0]
    assert "Once protected:" not in w3
    assert "before and after a deciding or critic model" in w3
    assert "separate mechanism under its published rule" in w3
    assert "trusted instruction, authorization or safety constraint" in i2
    assert "receipt omission" in i2
    reject_examples = policy.split("### REJECT with violation proven", 1)[1]
    assert (
        "Removing a record after it entered the final evidence set"
        not in reject_examples
    )
    active_codes = policy.split("## Required reason codes", 1)[1].split("```", 2)[1]
    assert "I2.trusted_instruction_removed" in active_codes
    assert "I2.final_evidence_removed" not in active_codes
    assert "I2.receipt_or_history_removed" not in active_codes
    # Historical codes remain in the document for readback, not new enforcement.
    assert "Historical `I2.final_evidence_removed`" in policy
    probe = (root / "ditto_screener/probe_review/reviewer_prompt.md").read_text()
    assert "Raw receipt/history retention is not mandatory" in probe
    assert "incidental\n   evidence loss" in probe
    assert "Still charge evidence a model\n   already received" not in probe


@pytest.mark.parametrize(
    "render,obsolete",
    [
        (
            _source_review_system_prompt,
            "I2 EVIDENCE RETENTION. Every deciding completion",
        ),
        (
            _l2_review_system_prompt,
            "I2 requires complete applicable current-user records",
        ),
        (
            _system_prompt,
            "withholds\nthe current user's records from the deciding turn",
        ),
    ],
)
def test_historical_prompts_keep_their_original_i2_scope(render, obsolete) -> None:
    assert obsolete in render(12)
    assert obsolete not in render(13)
