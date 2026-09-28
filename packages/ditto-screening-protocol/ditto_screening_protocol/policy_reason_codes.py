"""Published screening-policy reason codes, versioned by policy.

``workers/screener/docs/policy-v13.md`` ("Required reason codes") publishes the
exact codes a decision record may cite. They are pinned here so every writer of
a decision record validates against one list instead of accepting free text: a
``REJECT`` recorded under an unpublished code would still ban the submission
and read as a proven violation with precedent weight.

A policy version with no published catalog has no valid codes. Callers must
fail closed for it rather than fall back to an older catalog, so a v14 decision
cannot be recorded until the v14 codes ship here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# The first policy whose decision record requires published reason codes.
FIRST_REASON_CODE_POLICY_VERSION = 13

# policy-v13.md "Required reason codes", verbatim.
POLICY_V13_REASON_CODES: frozenset[str] = frozenset(
    {
        "I1.model_authority_bypass",
        "I2.trusted_instruction_removed",
        "I2.final_evidence_removed",
        "I2.receipt_or_history_removed",
        "I3.semantic_retry_or_selection",
        "I3.valid_no_call_suppressed",
        "I4.answer_field_rewritten",
        "I4.final_text_rewritten",
        "I4.abstention_synthesized",
        "I4.tool_argument_rewritten",
        "I4.miner_defined_overflow",
        "I5.benchmark_semantic_compiler",
        "I5.host_selected_financial_operands",
        "I6.execution_fabricated",
        "I6.execution_hidden",
        "I6.execution_misreported",
        "I7.applicable_capability_removed",
        "I7.outer_router_suppression",
        "I7.planning_authority_taken_over",
        "I7.execution_after_revocation",
        "I7.distinct_call_suppressed",
        "I8.evaluation_identity_branch",
        "I8.active_evaluation_training",
        "I8.hidden_variant_collapse",
        "S1.cross_user_data_flow",
        "S2.credential_or_data_exfiltration",
        "S3.screening_evasion",
        "Q1.protocol_contract_failure",
        "V1.required_submission_evidence_missing",
        "V2.platform_verification_failed",
        "V3.provider_verification_failed",
    }
)

PUBLISHED_REASON_CODES: Mapping[int, frozenset[str]] = {
    13: POLICY_V13_REASON_CODES,
}


def _is_violation_code(code: str) -> bool:
    # Invariant (I*) and safety (S*) codes name a proven violation. Q1 and V*
    # name protocol and verification failures, which the policy keeps apart
    # from proven violations.
    return code[:1] in {"I", "S"}


def published_reason_codes(policy_version: int) -> frozenset[str] | None:
    """The catalog governing a decision at ``policy_version``, or ``None``.

    Versions before the first catalog are decided under it: v13 introduced the
    requirement that a decision cite published codes, and a held pre-v13
    submission decided today records a v13-shaped decision. A version newer
    than every published catalog returns ``None``; the caller must refuse.
    """
    if policy_version < FIRST_REASON_CODE_POLICY_VERSION:
        return PUBLISHED_REASON_CODES[FIRST_REASON_CODE_POLICY_VERSION]
    return PUBLISHED_REASON_CODES.get(policy_version)


def published_violation_reason_codes(policy_version: int) -> frozenset[str] | None:
    """The proven-violation (I*/S*) subset of :func:`published_reason_codes`."""
    catalog = published_reason_codes(policy_version)
    if catalog is None:
        return None
    return frozenset(code for code in catalog if _is_violation_code(code))


ALL_PUBLISHED_VIOLATION_REASON_CODES: frozenset[str] = frozenset(
    code
    for catalog in PUBLISHED_REASON_CODES.values()
    for code in catalog
    if _is_violation_code(code)
)


def unpublished_violation_codes(
    codes: Iterable[str], policy_version: int | None = None
) -> list[str] | None:
    """Codes that are not published proven-violation codes, in input order.

    With ``policy_version`` the check is against that version's catalog;
    without it, against every published catalog (a version-free pre-check).
    Returns ``None`` when ``policy_version`` has no published catalog at all.
    """
    if policy_version is None:
        allowed: frozenset[str] | None = ALL_PUBLISHED_VIOLATION_REASON_CODES
    else:
        allowed = published_violation_reason_codes(policy_version)
    if allowed is None:
        return None
    return [code for code in codes if code not in allowed]
