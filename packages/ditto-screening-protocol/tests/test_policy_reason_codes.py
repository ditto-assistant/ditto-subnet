from ditto_screening_protocol.policy_reason_codes import (
    ALL_PUBLISHED_VIOLATION_REASON_CODES,
    FIRST_REASON_CODE_POLICY_VERSION,
    POLICY_V13_REASON_CODES,
    PUBLISHED_REASON_CODES,
    published_reason_codes,
    published_violation_reason_codes,
    unpublished_violation_codes,
)


def test_v13_catalog_is_the_published_first_catalog() -> None:
    assert FIRST_REASON_CODE_POLICY_VERSION == 13
    assert PUBLISHED_REASON_CODES[13] is POLICY_V13_REASON_CODES
    assert len(POLICY_V13_REASON_CODES) == 31


def test_pre_v13_decisions_use_the_first_catalog() -> None:
    assert published_reason_codes(12) is POLICY_V13_REASON_CODES
    assert published_reason_codes(1) is POLICY_V13_REASON_CODES


def test_an_unpublished_policy_version_has_no_catalog() -> None:
    # v14 codes ship with the v14 contract; until then nothing is valid.
    assert published_reason_codes(14) is None
    assert published_violation_reason_codes(14) is None
    assert unpublished_violation_codes(["I5.benchmark_semantic_compiler"], 14) is None


def test_violation_codes_exclude_protocol_and_verification_failures() -> None:
    violations = published_violation_reason_codes(13)
    assert violations is not None
    assert violations == ALL_PUBLISHED_VIOLATION_REASON_CODES
    assert all(code[0] in {"I", "S"} for code in violations)
    assert {
        "Q1.protocol_contract_failure",
        "V1.required_submission_evidence_missing",
        "V2.platform_verification_failed",
        "V3.provider_verification_failed",
    }.isdisjoint(violations)
    assert len(violations) == 27


def test_unpublished_codes_are_reported_in_input_order() -> None:
    assert unpublished_violation_codes(
        ["xx", "I5.benchmark_semantic_compiler", "V2.platform_verification_failed"],
        13,
    ) == ["xx", "V2.platform_verification_failed"]
    assert unpublished_violation_codes(["S3.screening_evasion"]) == []
    assert unpublished_violation_codes(["I5.benchmark_semantic_compiler "]) == [
        "I5.benchmark_semantic_compiler "
    ]
