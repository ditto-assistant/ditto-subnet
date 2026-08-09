from __future__ import annotations

import hashlib
import json
from importlib import metadata

import pytest
from pydantic import ValidationError

from ditto_screening_protocol import (
    SourceReviewAuthorityTransition,
    SourceReviewCausalEvidence,
    SourceReviewCausalRoleBinding,
    SourceReviewEvidenceItem,
    SourceReviewEvidenceRole,
    SourceReviewFinding,
    SourceReviewScorerVisibleEffect,
)

_SHA256 = "ab" * 32
_LEGACY_CANONICAL = (
    '{"artifact_sha256":"abababababababababababababababababababababababababababababababab",'
    '"categories":["benchmark_emulation","scorer_contract_manipulation"],'
    '"confidence":0.95,"evidence":[{"category":"benchmark_emulation",'
    '"line":7,"path":"src/main.rs"},{"category":"benchmark_emulation",'
    '"line":19,"path":"src/main.rs"}],"prompt_revision":"source-review-v16",'
    '"risk_level":"high","summary":"Reachable deterministic path bypasses '
    'the model result."}'
)
_LEGACY_DIGEST = "1f9d7843b2f259e101bd3064a650d7d7434afd61ea3fb4566d033ec3d18998fb"
_V2_DIGEST = "f70a63e97fe18ea8443a103628cade8a978788c2c2371bdbfbafe7161fb29a80"


def test_distribution_requires_pydantic_with_exclude_if_support() -> None:
    requirements = metadata.requires("ditto-screening-protocol") or []
    normalized = {
        requirement.partition(";")[0].replace(" ", "").lower()
        for requirement in requirements
    }

    assert "pydantic>=2.12" in normalized


def _legacy_finding(**overrides: object) -> SourceReviewFinding:
    values: dict[str, object] = {
        "artifact_sha256": _SHA256,
        "prompt_revision": "source-review-v16",
        "risk_level": "high",
        "confidence": 0.95,
        "categories": [
            "scorer_contract_manipulation",
            "benchmark_emulation",
            "benchmark_emulation",
        ],
        "evidence": [
            SourceReviewEvidenceItem(
                path="src/main.rs", line=7, category="benchmark_emulation"
            ),
            SourceReviewEvidenceItem(
                path="src/main.rs", line=19, category="benchmark_emulation"
            ),
        ],
        "summary": "Reachable deterministic path bypasses the model result.",
    }
    values.update(overrides)
    return SourceReviewFinding.model_validate(values)


def _role_bindings(
    *, category: str = "benchmark_emulation"
) -> list[SourceReviewCausalRoleBinding]:
    return [
        SourceReviewCausalRoleBinding(
            path="src/main.rs",
            line=7,
            category=category,
            role=SourceReviewEvidenceRole.SERVED_TRIGGER,
        ),
        SourceReviewCausalRoleBinding(
            path="src/main.rs",
            line=19,
            category=category,
            role=SourceReviewEvidenceRole.AUTHORITY_BYPASS,
        ),
        SourceReviewCausalRoleBinding(
            path="src/main.rs",
            line=19,
            category=category,
            role=SourceReviewEvidenceRole.SCORER_VISIBLE_EFFECT,
        ),
        SourceReviewCausalRoleBinding(
            path="src/main.rs",
            line=7,
            category=category,
            role=SourceReviewEvidenceRole.REACHABILITY_LINK,
        ),
    ]


def _v2_finding(
    *,
    bindings: list[SourceReviewCausalRoleBinding] | None = None,
    transition: SourceReviewAuthorityTransition = (
        SourceReviewAuthorityTransition.MODEL_OUTPUT_OVERWRITTEN
    ),
    effect: SourceReviewScorerVisibleEffect = SourceReviewScorerVisibleEffect.ANSWER,
    categories: list[str] | None = None,
    evidence: list[SourceReviewEvidenceItem] | None = None,
) -> SourceReviewFinding:
    selected_categories = categories or ["benchmark_emulation"]
    selected_evidence = evidence or [
        SourceReviewEvidenceItem(
            path="src/main.rs", line=7, category="benchmark_emulation"
        ),
        SourceReviewEvidenceItem(
            path="src/main.rs", line=19, category="benchmark_emulation"
        ),
    ]
    return SourceReviewFinding(
        artifact_sha256=_SHA256,
        prompt_revision="source-review-causal-v2",
        risk_level="high",
        confidence=0.98,
        categories=selected_categories,
        evidence=selected_evidence,
        summary="Served code overwrites the model-authored answer.",
        causal_evidence=SourceReviewCausalEvidence(
            authority_transition=transition,
            scorer_visible_effect=effect,
            role_bindings=bindings or _role_bindings(),
        ),
    )


def test_historical_v1_canonical_bytes_and_digest_are_pinned() -> None:
    finding = _legacy_finding()

    assert finding.evidence_schema_version == 1
    assert finding.canonical_bytes() == _LEGACY_CANONICAL.encode()
    assert finding.canonical_digest() == _LEGACY_DIGEST
    assert hashlib.sha256(finding.canonical_bytes()).hexdigest() == _LEGACY_DIGEST


def test_historical_v1_wire_payload_has_no_new_default_fields() -> None:
    finding = _legacy_finding()

    assert finding.model_dump(mode="json") == {
        "artifact_sha256": _SHA256,
        "prompt_revision": "source-review-v16",
        "risk_level": "high",
        "confidence": 0.95,
        "categories": [
            "scorer_contract_manipulation",
            "benchmark_emulation",
            "benchmark_emulation",
        ],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 7,
                "category": "benchmark_emulation",
            },
            {
                "path": "src/main.rs",
                "line": 19,
                "category": "benchmark_emulation",
            },
        ],
        "summary": "Reachable deterministic path bypasses the model result.",
    }


def test_serialization_schema_retains_typed_v1_and_v2_fields() -> None:
    schema = SourceReviewFinding.model_json_schema(mode="serialization")

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "artifact_sha256",
        "prompt_revision",
        "risk_level",
        "confidence",
        "categories",
        "evidence",
        "summary",
        "causal_evidence",
    }
    causal_schema = schema["properties"]["causal_evidence"]
    assert causal_schema["anyOf"] == [
        {"$ref": "#/$defs/SourceReviewCausalEvidence"},
        {"type": "null"},
    ]
    assert schema["$defs"]["SourceReviewCausalEvidence"]["type"] == "object"


def test_historical_v1_json_round_trip_keeps_canonical_identity() -> None:
    original = _legacy_finding()
    restored = SourceReviewFinding.model_validate_json(original.model_dump_json())

    assert restored.model_dump(mode="json") == original.model_dump(mode="json")
    assert restored.canonical_bytes() == original.canonical_bytes()
    assert restored.canonical_digest() == original.canonical_digest()


def test_v1_is_not_retroactively_role_complete() -> None:
    finding = _legacy_finding()

    with pytest.raises(ValueError, match="requires causal evidence schema v2"):
        finding.require_role_complete_causal_evidence()
    assert finding.evidence_schema_version == 1
    assert finding.canonical_digest() == _LEGACY_DIGEST


def test_v1_noncausal_category_needs_no_v2_evidence() -> None:
    finding = _legacy_finding(
        categories=["credential_access"],
        evidence=[
            SourceReviewEvidenceItem(
                path="src/main.rs", line=7, category="credential_access"
            )
        ],
    )

    assert finding.require_role_complete_causal_evidence() is finding


def test_v2_role_complete_finding_passes_explicit_policy_check() -> None:
    finding = _v2_finding()

    assert finding.evidence_schema_version == 2
    assert finding.require_role_complete_causal_evidence() is finding
    assert finding.model_dump(mode="json")["causal_evidence"]["schema_version"] == 2
    assert finding.canonical_digest() == _V2_DIGEST


@pytest.mark.parametrize("missing", list(SourceReviewEvidenceRole))
def test_v2_rejects_each_missing_required_role(
    missing: SourceReviewEvidenceRole,
) -> None:
    bindings = [item for item in _role_bindings() if item.role != missing]
    finding = _v2_finding(bindings=bindings)

    with pytest.raises(ValueError, match=missing.value):
        finding.require_role_complete_causal_evidence()


def test_v2_requires_two_distinct_causal_locations() -> None:
    bindings = [
        item.model_copy(update={"path": "src/main.rs", "line": 7})
        for item in _role_bindings()
    ]
    finding = _v2_finding(bindings=bindings)

    with pytest.raises(ValueError, match="requires two causal locations"):
        finding.require_role_complete_causal_evidence()


def test_duplicate_bindings_cannot_fake_role_completeness() -> None:
    binding = _role_bindings()[0]

    with pytest.raises(ValidationError, match="causal role bindings must be unique"):
        SourceReviewCausalEvidence(
            authority_transition=(
                SourceReviewAuthorityTransition.MODEL_OUTPUT_OVERWRITTEN
            ),
            scorer_visible_effect=SourceReviewScorerVisibleEffect.ANSWER,
            role_bindings=[binding, binding],
        )


def test_distinct_duplicate_role_bindings_still_fail_completeness() -> None:
    bindings = [
        SourceReviewCausalRoleBinding(
            path="src/main.rs",
            line=line,
            category="benchmark_emulation",
            role=SourceReviewEvidenceRole.SERVED_TRIGGER,
        )
        for line in (7, 19)
    ]
    finding = _v2_finding(bindings=bindings)

    with pytest.raises(ValueError, match="authority_bypass"):
        finding.require_role_complete_causal_evidence()


def test_v2_binding_must_reference_existing_finding_evidence() -> None:
    bindings = _role_bindings()
    bindings[0] = bindings[0].model_copy(update={"line": 8})

    with pytest.raises(ValidationError, match="does not reference finding evidence"):
        _v2_finding(bindings=bindings)


def test_v2_binding_category_must_belong_to_finding() -> None:
    evidence = [
        SourceReviewEvidenceItem(
            path="src/main.rs", line=7, category="benchmark_emulation"
        ),
        SourceReviewEvidenceItem(
            path="src/main.rs", line=19, category="benchmark_emulation"
        ),
        SourceReviewEvidenceItem(
            path="src/main.rs", line=21, category="credential_access"
        ),
    ]
    bindings = _role_bindings()
    bindings[0] = SourceReviewCausalRoleBinding(
        path="src/main.rs",
        line=21,
        category="credential_access",
        role=SourceReviewEvidenceRole.SERVED_TRIGGER,
    )

    with pytest.raises(ValidationError, match="category is not in finding"):
        _v2_finding(bindings=bindings, evidence=evidence)


def test_v2_requires_role_completeness_for_each_causal_category() -> None:
    categories = ["benchmark_emulation", "scorer_contract_manipulation"]
    evidence = [
        SourceReviewEvidenceItem(path="src/main.rs", line=line, category=category)
        for category in categories
        for line in (7, 19)
    ]
    finding = _v2_finding(
        categories=categories,
        evidence=evidence,
        bindings=_role_bindings(category="benchmark_emulation"),
    )

    with pytest.raises(
        ValueError, match="scorer_contract_manipulation.*missing causal roles"
    ):
        finding.require_role_complete_causal_evidence()


def test_v2_accepts_role_completeness_for_both_causal_categories() -> None:
    categories = ["benchmark_emulation", "scorer_contract_manipulation"]
    evidence = [
        SourceReviewEvidenceItem(path="src/main.rs", line=line, category=category)
        for category in categories
        for line in (7, 19)
    ]
    finding = _v2_finding(
        categories=categories,
        evidence=evidence,
        bindings=[
            *_role_bindings(category="benchmark_emulation"),
            *_role_bindings(category="scorer_contract_manipulation"),
        ],
    )

    assert finding.require_role_complete_causal_evidence() is finding


def test_v2_canonicalization_normalizes_binding_order() -> None:
    forward = _v2_finding(bindings=_role_bindings())
    reverse = _v2_finding(bindings=list(reversed(_role_bindings())))

    assert forward.canonical_bytes() == reverse.canonical_bytes()
    assert forward.canonical_digest() == reverse.canonical_digest()


@pytest.mark.parametrize("role", list(SourceReviewEvidenceRole))
def test_v2_canonicalization_binds_every_role_change(
    role: SourceReviewEvidenceRole,
) -> None:
    original = _v2_finding(bindings=_role_bindings())
    changed = [item.model_copy() for item in _role_bindings()]
    index = next(index for index, item in enumerate(changed) if item.role == role)
    selected = changed[index]
    roles_at_location = {
        item.role
        for other_index, item in enumerate(changed)
        if other_index != index
        and (item.path, item.line, item.category)
        == (selected.path, selected.line, selected.category)
    }
    replacement = next(
        candidate
        for candidate in SourceReviewEvidenceRole
        if candidate != role and candidate not in roles_at_location
    )
    changed[index] = changed[index].model_copy(update={"role": replacement})
    modified = _v2_finding(bindings=changed)

    assert original.canonical_bytes() != modified.canonical_bytes()
    assert original.canonical_digest() != modified.canonical_digest()


def test_v2_canonicalization_binds_every_authority_transition() -> None:
    digests = {
        _v2_finding(
            transition=transition,
            effect=(
                SourceReviewScorerVisibleEffect.VALIDATOR_OBSERVED_TRAJECTORY
                if transition == SourceReviewAuthorityTransition.TOOL_EXECUTION_BYPASSED
                else SourceReviewScorerVisibleEffect.TOOL_CALLS
                if transition
                == SourceReviewAuthorityTransition.TOOL_TRAJECTORY_FABRICATED
                else SourceReviewScorerVisibleEffect.ANSWER
            ),
        ).canonical_digest()
        for transition in SourceReviewAuthorityTransition
    }

    assert len(digests) == len(SourceReviewAuthorityTransition)


def test_v2_canonicalization_binds_every_scorer_visible_effect() -> None:
    digests = {
        _v2_finding(
            transition=(
                SourceReviewAuthorityTransition.TOOL_EXECUTION_BYPASSED
                if effect
                == SourceReviewScorerVisibleEffect.VALIDATOR_OBSERVED_TRAJECTORY
                else SourceReviewAuthorityTransition.SCORER_FIELD_REWRITTEN
            ),
            effect=effect,
        ).canonical_digest()
        for effect in SourceReviewScorerVisibleEffect
    }

    assert len(digests) == len(SourceReviewScorerVisibleEffect)


def test_v2_effect_tamper_changes_canonical_bytes_and_digest() -> None:
    original = _v2_finding(effect=SourceReviewScorerVisibleEffect.ANSWER)
    modified = _v2_finding(effect=SourceReviewScorerVisibleEffect.FINAL_TEXT)

    assert original.canonical_bytes() != modified.canonical_bytes()
    assert original.canonical_digest() != modified.canonical_digest()


def test_incompatible_transition_and_effect_are_rejected() -> None:
    with pytest.raises(ValidationError, match="incompatible with authority transition"):
        _v2_finding(
            transition=SourceReviewAuthorityTransition.MODEL_SKIPPED,
            effect=SourceReviewScorerVisibleEffect.TOOL_CALLS,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "src/other.rs"),
        ("line", 20),
        ("category", "scorer_contract_manipulation"),
    ],
)
def test_v2_canonicalization_binds_every_binding_location_field(
    field: str, value: str | int
) -> None:
    original = _v2_finding(bindings=_role_bindings())
    changed = [item.model_copy() for item in _role_bindings()]
    changed[0] = changed[0].model_copy(update={field: value})
    causal = SourceReviewCausalEvidence(
        authority_transition=SourceReviewAuthorityTransition.MODEL_OUTPUT_OVERWRITTEN,
        scorer_visible_effect=SourceReviewScorerVisibleEffect.ANSWER,
        role_bindings=changed,
    )
    modified = original.model_copy(update={"causal_evidence": causal})

    assert original.canonical_digest() != modified.canonical_digest()


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValidationError, match="role"):
        SourceReviewCausalRoleBinding.model_validate(
            {
                "path": "src/main.rs",
                "line": 7,
                "category": "benchmark_emulation",
                "role": "model_call_nearby",
            }
        )


def test_unknown_authority_transition_is_rejected() -> None:
    with pytest.raises(ValidationError, match="authority_transition"):
        SourceReviewCausalEvidence.model_validate(
            {
                "schema_version": 2,
                "authority_transition": "probably_overwritten",
                "scorer_visible_effect": "answer",
                "role_bindings": [
                    {
                        "path": "src/main.rs",
                        "line": 7,
                        "category": "benchmark_emulation",
                        "role": "served_trigger",
                    }
                ],
            }
        )


def test_unknown_scorer_visible_effect_is_rejected() -> None:
    with pytest.raises(ValidationError, match="scorer_visible_effect"):
        SourceReviewCausalEvidence.model_validate(
            {
                "schema_version": 2,
                "authority_transition": "model_output_overwritten",
                "scorer_visible_effect": "private_scorer_slot",
                "role_bindings": [
                    {
                        "path": "src/main.rs",
                        "line": 7,
                        "category": "benchmark_emulation",
                        "role": "served_trigger",
                    }
                ],
            }
        )


@pytest.mark.parametrize("schema_version", [0, 1, 3, "2"])
def test_non_v2_causal_schema_version_is_rejected(schema_version: object) -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        SourceReviewCausalEvidence.model_validate(
            {
                "schema_version": schema_version,
                "authority_transition": "model_skipped",
                "scorer_visible_effect": "final_text",
                "role_bindings": [
                    {
                        "path": "src/main.rs",
                        "line": 7,
                        "category": "benchmark_emulation",
                        "role": "served_trigger",
                    }
                ],
            }
        )


def test_empty_role_bindings_are_rejected() -> None:
    with pytest.raises(ValidationError, match="role_bindings"):
        SourceReviewCausalEvidence(
            authority_transition=SourceReviewAuthorityTransition.MODEL_SKIPPED,
            scorer_visible_effect=SourceReviewScorerVisibleEffect.FINAL_TEXT,
            role_bindings=[],
        )


def test_more_than_32_role_bindings_are_rejected() -> None:
    bindings = [
        {
            "path": f"src/file-{index}.rs",
            "line": index + 1,
            "category": "benchmark_emulation",
            "role": "served_trigger",
        }
        for index in range(33)
    ]

    with pytest.raises(ValidationError, match="role_bindings"):
        SourceReviewCausalEvidence.model_validate(
            {
                "authority_transition": "model_skipped",
                "scorer_visible_effect": "final_text",
                "role_bindings": bindings,
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "p" * 241),
        ("line", 0),
        ("category", "c" * 65),
    ],
)
def test_role_binding_bounds_are_enforced(field: str, value: str | int) -> None:
    raw: dict[str, object] = {
        "path": "src/main.rs",
        "line": 7,
        "category": "benchmark_emulation",
        "role": "served_trigger",
    }
    raw[field] = value

    with pytest.raises(ValidationError, match=field):
        SourceReviewCausalRoleBinding.model_validate(raw)


def test_unknown_v2_fields_are_rejected() -> None:
    raw = _v2_finding().model_dump(mode="json")
    causal = raw["causal_evidence"]
    assert isinstance(causal, dict)
    causal["private_reasoning"] = "not allowed"

    with pytest.raises(ValidationError, match="private_reasoning"):
        SourceReviewFinding.model_validate(raw)


def test_v2_json_round_trip_keeps_canonical_identity() -> None:
    original = _v2_finding()
    restored = SourceReviewFinding.model_validate_json(original.model_dump_json())

    assert restored.evidence_schema_version == 2
    assert restored.canonical_bytes() == original.canonical_bytes()
    assert restored.canonical_digest() == original.canonical_digest()
    assert restored.require_role_complete_causal_evidence() is restored


def test_v2_canonical_payload_contains_no_unbounded_or_private_fields() -> None:
    payload = json.loads(_v2_finding().canonical_bytes())

    assert set(payload["causal_evidence"]) == {
        "schema_version",
        "authority_transition",
        "scorer_visible_effect",
        "role_bindings",
    }
    assert all(
        set(binding) == {"path", "line", "category", "role"}
        for binding in payload["causal_evidence"]["role_bindings"]
    )
    assert "source" not in payload
    assert "prompt" not in payload["causal_evidence"]
