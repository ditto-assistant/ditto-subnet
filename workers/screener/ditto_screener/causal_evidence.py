"""Deterministic authority checks for elevated source-review findings."""

from __future__ import annotations

from dataclasses import dataclass

from ditto_screening_protocol import (
    SourceReviewAuthorityTransition,
    SourceReviewFinding,
    SourceReviewScorerVisibleEffect,
)

_CAUSAL_CATEGORIES = frozenset({"benchmark_emulation", "scorer_contract_manipulation"})
_TRANSITIONS_BY_CATEGORY = {
    "benchmark_emulation": frozenset(
        {
            SourceReviewAuthorityTransition.MODEL_SKIPPED,
            SourceReviewAuthorityTransition.MODEL_OUTPUT_OVERWRITTEN,
            SourceReviewAuthorityTransition.TOOL_EXECUTION_BYPASSED,
            SourceReviewAuthorityTransition.TOOL_TRAJECTORY_FABRICATED,
            SourceReviewAuthorityTransition.SELECTIVE_MODEL_DISABLEMENT,
        }
    ),
    "scorer_contract_manipulation": frozenset(
        {
            SourceReviewAuthorityTransition.MODEL_OUTPUT_OVERWRITTEN,
            SourceReviewAuthorityTransition.SCORER_FIELD_REWRITTEN,
        }
    ),
}
_EFFECTS_BY_TRANSITION = {
    SourceReviewAuthorityTransition.MODEL_SKIPPED: frozenset(
        {
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
            SourceReviewScorerVisibleEffect.ANSWER,
            SourceReviewScorerVisibleEffect.ABSTAIN,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.MODEL_OUTPUT_OVERWRITTEN: frozenset(
        {
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
            SourceReviewScorerVisibleEffect.ANSWER,
            SourceReviewScorerVisibleEffect.ABSTAIN,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.TOOL_EXECUTION_BYPASSED: frozenset(
        {
            SourceReviewScorerVisibleEffect.VALIDATOR_OBSERVED_TRAJECTORY,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.TOOL_TRAJECTORY_FABRICATED: frozenset(
        {
            SourceReviewScorerVisibleEffect.TOOL_CALLS,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.SELECTIVE_MODEL_DISABLEMENT: frozenset(
        {
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
            SourceReviewScorerVisibleEffect.ANSWER,
            SourceReviewScorerVisibleEffect.ABSTAIN,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.SCORER_FIELD_REWRITTEN: frozenset(
        {
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
            SourceReviewScorerVisibleEffect.ANSWER,
            SourceReviewScorerVisibleEffect.ABSTAIN,
            SourceReviewScorerVisibleEffect.TOOL_CALLS,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
}
_EFFECT_LABELS = {
    SourceReviewScorerVisibleEffect.FINAL_TEXT: "the `final_text` field",
    SourceReviewScorerVisibleEffect.ANSWER: "the `answer` field",
    SourceReviewScorerVisibleEffect.ABSTAIN: "the `abstain` field",
    SourceReviewScorerVisibleEffect.TOOL_CALLS: "the `tool_calls` field",
    SourceReviewScorerVisibleEffect.VALIDATOR_OBSERVED_TRAJECTORY: (
        "the validator-observed tool trajectory"
    ),
    SourceReviewScorerVisibleEffect.GRADED_OUTCOME: "the graded outcome",
}
_TRANSITION_PHRASES = {
    SourceReviewAuthorityTransition.MODEL_SKIPPED: (
        "skipped genuine model authority and changed"
    ),
    SourceReviewAuthorityTransition.MODEL_OUTPUT_OVERWRITTEN: (
        "overwrote genuine model output in"
    ),
    SourceReviewAuthorityTransition.TOOL_EXECUTION_BYPASSED: (
        "bypassed genuine tool execution and changed"
    ),
    SourceReviewAuthorityTransition.TOOL_TRAJECTORY_FABRICATED: "fabricated",
    SourceReviewAuthorityTransition.SELECTIVE_MODEL_DISABLEMENT: (
        "selectively disabled genuine model authority and changed"
    ),
    SourceReviewAuthorityTransition.SCORER_FIELD_REWRITTEN: (
        "used score-driven logic to rewrite"
    ),
}


@dataclass(frozen=True)
class CausalEvidenceVerification:
    """Public-safe result of checking one signed finding."""

    evidence_schema_version: int
    authority_transition: str | None
    scorer_visible_effect: str | None
    role_complete: bool
    reason_code: str


def causal_summary(
    transition: SourceReviewAuthorityTransition,
    effect: SourceReviewScorerVisibleEffect,
) -> str:
    """Return the only supported public sentence for a transition/effect pair."""
    return (
        "Level-2 review proved served code "
        f"{_TRANSITION_PHRASES[transition]} {_EFFECT_LABELS[effect]}."
    )


def verify_causal_finding(finding: SourceReviewFinding) -> CausalEvidenceVerification:
    """Verify role completeness, category compatibility, and causal wording."""
    transition = (
        finding.causal_evidence.authority_transition
        if finding.causal_evidence is not None
        else None
    )
    effect = (
        finding.causal_evidence.scorer_visible_effect
        if finding.causal_evidence is not None
        else None
    )
    evidence_schema_version = finding.evidence_schema_version
    authority_transition = transition.value if transition is not None else None
    scorer_visible_effect = effect.value if effect is not None else None
    required_categories = set(finding.categories) & _CAUSAL_CATEGORIES
    if not required_categories:
        return CausalEvidenceVerification(
            evidence_schema_version=evidence_schema_version,
            authority_transition=authority_transition,
            scorer_visible_effect=scorer_visible_effect,
            role_complete=True,
            reason_code="causal-evidence-not-required",
        )
    try:
        finding.require_role_complete_causal_evidence()
    except ValueError:
        return CausalEvidenceVerification(
            evidence_schema_version=evidence_schema_version,
            authority_transition=authority_transition,
            scorer_visible_effect=scorer_visible_effect,
            role_complete=False,
            reason_code="causal-role-incomplete",
        )
    if transition is None or any(
        transition not in _TRANSITIONS_BY_CATEGORY[category]
        for category in required_categories
    ):
        return CausalEvidenceVerification(
            evidence_schema_version=evidence_schema_version,
            authority_transition=authority_transition,
            scorer_visible_effect=scorer_visible_effect,
            role_complete=False,
            reason_code="causal-transition-category-mismatch",
        )
    if effect is None or effect not in _EFFECTS_BY_TRANSITION[transition]:
        return CausalEvidenceVerification(
            evidence_schema_version=evidence_schema_version,
            authority_transition=authority_transition,
            scorer_visible_effect=scorer_visible_effect,
            role_complete=False,
            reason_code="causal-transition-effect-mismatch",
        )
    if finding.summary != causal_summary(transition, effect):
        return CausalEvidenceVerification(
            evidence_schema_version=evidence_schema_version,
            authority_transition=authority_transition,
            scorer_visible_effect=scorer_visible_effect,
            role_complete=False,
            reason_code="causal-summary-mismatch",
        )
    return CausalEvidenceVerification(
        evidence_schema_version=evidence_schema_version,
        authority_transition=authority_transition,
        scorer_visible_effect=scorer_visible_effect,
        role_complete=True,
        reason_code="causal-evidence-verified",
    )


def causal_audit_fields(
    finding_value: object,
) -> dict[str, object]:
    """Return sanitized audit fields without raising on historical findings."""
    try:
        finding = SourceReviewFinding.model_validate(finding_value)
    except (TypeError, ValueError):
        return {
            "evidence_schema_version": None,
            "authority_transition": None,
            "scorer_visible_effect": None,
            "causal_role_complete": False,
            "causal_verification_reason": "causal-finding-unavailable",
        }
    result = verify_causal_finding(finding)
    return {
        "evidence_schema_version": result.evidence_schema_version,
        "authority_transition": result.authority_transition,
        "scorer_visible_effect": result.scorer_visible_effect,
        "causal_role_complete": result.role_complete,
        "causal_verification_reason": result.reason_code,
    }
