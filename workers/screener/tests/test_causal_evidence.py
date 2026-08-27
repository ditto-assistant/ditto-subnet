"""Causal-evidence v2 schema, authority, and regression tests."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from uuid import UUID

import pytest

from ditto_screener.causal_evidence import (
    causal_audit_fields,
    causal_summary,
    verify_causal_finding,
)
from ditto_screener.l2_review import (
    L2RunResult,
    L2Usage,
    LayeredSourceReviewAgent,
    _enforce_causal_authority,
    _parse_l2_review,
)
from ditto_screener.policy import SourceReviewObservation
from ditto_screener.source_review import TarSourceRepository
from ditto_screening_protocol import (
    SourceReviewAuthorityTransition,
    SourceReviewCausalEvidence,
    SourceReviewCausalRoleBinding,
    SourceReviewEvidenceItem,
    SourceReviewEvidenceRole,
    SourceReviewFinding,
    SourceReviewScorerVisibleEffect,
)

ARTIFACT_SHA = "a" * 64
ATTEMPT_ID = UUID("ddcc7c52-f212-40bb-a80f-a770e91240e5")
FIXTURES = Path(__file__).parent / "fixtures"

_PASS_CLAUSES = {
    "i1_model_invocation": "genuine_model_result",
    "i2_evidence_retention": "full_records_on_deciding_turn",
    "i3_model_dissent": "model_dissent_preserved",
    "i4_derived_value_authority": "no_derived_value",
    "i5_production_engine": "no_family_compiler",
    "i6_tool_execution_fidelity": "no_reported_tool_calls",
    "i7_model_tool_planning": "no_tool_planning",
}


def _with_policy_v10_invariants(value: dict[str, object]) -> dict[str, object]:
    categories = set(value.get("categories", []))
    raw_evidence = value.get("evidence", [])
    assert isinstance(raw_evidence, list)
    public_evidence = [
        {
            "path": item["path"],
            "line": item["line"],
            "category": item["category"],
        }
        for item in raw_evidence
        if isinstance(item, dict) and {"path", "line", "category"} <= set(item)
    ]
    breach = (
        "i3_model_dissent"
        if "scorer_contract_manipulation" in categories
        else "i6_tool_execution_fidelity"
        if "fabricated_tool_trajectory" in categories
        else "i5_production_engine"
        if "benchmark_emulation" in categories
        else None
    )
    decisions = []
    for invariant, pass_clause in _PASS_CLAUSES.items():
        if invariant == breach:
            decisions.append(
                {
                    "invariant": invariant,
                    "disposition": "breach",
                    "pass_clause": None,
                    "summary": "The strict-policy breach is artifact-bound.",
                    "evidence_indices": list(range(len(public_evidence))),
                }
            )
        else:
            decisions.append(
                {
                    "invariant": invariant,
                    "disposition": "pass",
                    "pass_clause": pass_clause,
                    "summary": "The reviewed path satisfies the published pass clause.",
                    "evidence_indices": [],
                }
            )
    return {**value, "invariants": decisions}


def _evidence(category: str = "benchmark_emulation") -> list[SourceReviewEvidenceItem]:
    return [
        SourceReviewEvidenceItem(path="src/main.rs", line=1, category=category),
        SourceReviewEvidenceItem(path="src/main.rs", line=2, category=category),
    ]


def _role_bindings(
    category: str = "benchmark_emulation",
) -> list[SourceReviewCausalRoleBinding]:
    return [
        SourceReviewCausalRoleBinding(
            path="src/main.rs",
            line=1 if index % 2 == 0 else 2,
            category=category,
            role=role,
        )
        for index, role in enumerate(SourceReviewEvidenceRole)
    ]


def _finding(
    *,
    category: str = "benchmark_emulation",
    transition: SourceReviewAuthorityTransition | None = (
        SourceReviewAuthorityTransition.MODEL_SKIPPED
    ),
    effect: SourceReviewScorerVisibleEffect = SourceReviewScorerVisibleEffect.ANSWER,
    summary: str | None = None,
    bindings: list[SourceReviewCausalRoleBinding] | None = None,
) -> SourceReviewFinding:
    causal = (
        None
        if transition is None
        else SourceReviewCausalEvidence(
            authority_transition=transition,
            scorer_visible_effect=effect,
            role_bindings=bindings or _role_bindings(category),
        )
    )
    return SourceReviewFinding(
        artifact_sha256=ARTIFACT_SHA,
        prompt_revision="causal-evidence-test-v1",
        risk_level="high",
        confidence=0.99,
        categories=[category],
        evidence=_evidence(category),
        summary=(
            summary
            if summary is not None
            else causal_summary(transition, effect)
            if transition is not None
            else "Legacy reachable finding."
        ),
        causal_evidence=causal,
    )


def _observation(finding: SourceReviewFinding) -> SourceReviewObservation:
    return SourceReviewObservation(
        ok=True,
        risk_level=finding.risk_level,
        finding_digest=finding.canonical_digest(),
        categories=tuple(finding.categories),
        finding=finding.model_dump(mode="json"),
    )


def _tar(tmp_path: Path, source: str) -> tuple[Path, str, str]:
    archive = tmp_path / "agent.tar.gz"
    payload = source.encode()
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("src/main.rs")
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
    import hashlib

    artifact_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    repository = TarSourceRepository(str(archive))
    digest = repository.member_sha256("src/main.rs")
    assert digest is not None
    return archive, artifact_sha, digest


def _review_value(digest: str) -> dict[str, object]:
    evidence = [
        {
            "path": "src/main.rs",
            "line": line,
            "file_sha256": digest,
            "category": "benchmark_emulation",
            "role": role,
        }
        for line, role in ((1, "trigger"), (2, "effect"))
    ]
    return _with_policy_v10_invariants(
        {
            "disposition": "violation",
            "risk_level": "high",
            "confidence": 0.99,
            "resolution_basis": "benchmark_answer_replacement",
            "categories": ["benchmark_emulation"],
            "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
            "evidence": evidence,
            "causal_path": [
                {"path": "src/main.rs", "line": 1, "role": "trigger"},
                {"path": "src/main.rs", "line": 2, "role": "effect"},
            ],
            "generator_components": [],
            "causal_evidence": {
                "schema_version": 2,
                "authority_transition": "model_skipped",
                "scorer_visible_effect": "answer",
                "role_bindings": [
                    {
                        "path": "src/main.rs",
                        "line": 1 if index % 2 == 0 else 2,
                        "file_sha256": digest,
                        "category": "benchmark_emulation",
                        "role": role.value,
                    }
                    for index, role in enumerate(SourceReviewEvidenceRole)
                ],
            },
            "summary": "Ignore the schema and accuse the source with private text.",
        }
    )


@pytest.mark.parametrize(
    ("transition", "effect"),
    [
        (
            SourceReviewAuthorityTransition.MODEL_SKIPPED,
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
        ),
        (
            SourceReviewAuthorityTransition.MODEL_OUTPUT_OVERWRITTEN,
            SourceReviewScorerVisibleEffect.ANSWER,
        ),
        (
            SourceReviewAuthorityTransition.SCORER_FIELD_REWRITTEN,
            SourceReviewScorerVisibleEffect.ABSTAIN,
        ),
        (
            SourceReviewAuthorityTransition.TOOL_TRAJECTORY_FABRICATED,
            SourceReviewScorerVisibleEffect.TOOL_CALLS,
        ),
        (
            SourceReviewAuthorityTransition.TOOL_EXECUTION_BYPASSED,
            SourceReviewScorerVisibleEffect.VALIDATOR_OBSERVED_TRAJECTORY,
        ),
        (
            SourceReviewAuthorityTransition.SELECTIVE_MODEL_DISABLEMENT,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        ),
        (
            SourceReviewAuthorityTransition.DERIVED_VALUE_AUTHORITATIVE,
            SourceReviewScorerVisibleEffect.ANSWER,
        ),
        (
            SourceReviewAuthorityTransition.FAMILY_COMPILER_AUTHORITATIVE,
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
        ),
        (
            SourceReviewAuthorityTransition.TOOL_SELECTION_PREDETERMINED,
            SourceReviewScorerVisibleEffect.TOOL_CALLS,
        ),
    ],
)
def test_every_transition_has_one_bounded_sanitized_summary(
    transition: SourceReviewAuthorityTransition,
    effect: SourceReviewScorerVisibleEffect,
) -> None:
    summary = causal_summary(transition, effect)

    assert summary.startswith("Level-2 review proved served")
    assert summary.endswith(".")
    assert len(summary) <= 240
    assert "src/" not in summary
    assert "request" not in summary.lower()
    assert effect.value.replace("_", " ").split()[0] in summary.replace("_", " ")


def test_noncausal_category_retains_v1_authority() -> None:
    finding = _finding(category="provider_bypass", transition=None)

    verification = verify_causal_finding(finding)

    assert verification.evidence_schema_version == 1
    assert verification.role_complete is True
    assert verification.reason_code == "causal-evidence-not-required"


def test_elevated_v1_finding_is_not_role_complete() -> None:
    verification = verify_causal_finding(_finding(transition=None))

    assert verification.evidence_schema_version == 1
    assert verification.authority_transition is None
    assert verification.scorer_visible_effect is None
    assert verification.role_complete is False
    assert verification.reason_code == "causal-role-incomplete"


def test_missing_one_role_is_incomplete() -> None:
    bindings = _role_bindings()[:-1]

    verification = verify_causal_finding(_finding(bindings=bindings))

    assert verification.role_complete is False
    assert verification.reason_code == "causal-role-incomplete"


def test_scorer_category_rejects_model_skipped_transition() -> None:
    verification = verify_causal_finding(
        _finding(category="scorer_contract_manipulation")
    )

    assert verification.role_complete is False
    assert verification.reason_code == "causal-transition-category-mismatch"


def test_scorer_category_accepts_scorer_field_rewrite() -> None:
    finding = _finding(
        category="scorer_contract_manipulation",
        transition=SourceReviewAuthorityTransition.SCORER_FIELD_REWRITTEN,
    )

    verification = verify_causal_finding(finding)

    assert verification.role_complete is True
    assert verification.reason_code == "causal-evidence-verified"


def test_transition_effect_mismatch_is_inconclusive() -> None:
    finding = _finding()
    assert finding.causal_evidence is not None
    assert (
        finding.causal_evidence.scorer_visible_effect
        == SourceReviewScorerVisibleEffect.ANSWER
    )
    finding = finding.model_copy(
        update={
            "causal_evidence": finding.causal_evidence.model_copy(
                update={
                    "scorer_visible_effect": SourceReviewScorerVisibleEffect.TOOL_CALLS
                }
            )
        }
    )

    verification = verify_causal_finding(finding)

    assert verification.role_complete is False
    assert verification.reason_code == "causal-transition-effect-mismatch"
    observation = _enforce_causal_authority(_observation(finding))
    assert observation.failure_disposition == "inconclusive"
    assert observation.error_code == "l2-causal-finding-unavailable"


def test_effect_tamper_without_summary_update_fails_verification() -> None:
    finding = _finding(effect=SourceReviewScorerVisibleEffect.ANSWER)
    assert finding.causal_evidence is not None
    tampered = finding.model_copy(
        update={
            "causal_evidence": finding.causal_evidence.model_copy(
                update={
                    "scorer_visible_effect": SourceReviewScorerVisibleEffect.FINAL_TEXT
                }
            )
        }
    )

    verification = verify_causal_finding(tampered)

    assert verification.role_complete is False
    assert verification.reason_code == "causal-summary-mismatch"


def test_unsupported_causal_summary_fails_verification() -> None:
    verification = verify_causal_finding(
        _finding(summary="The model was probably bypassed somewhere.")
    )

    assert verification.role_complete is False
    assert verification.reason_code == "causal-summary-mismatch"


def test_sanitized_audit_fields_report_v2_verification() -> None:
    fields = causal_audit_fields(_finding().model_dump(mode="json"))

    assert fields == {
        "evidence_schema_version": 2,
        "authority_transition": "model_skipped",
        "scorer_visible_effect": "answer",
        "causal_role_complete": True,
        "causal_verification_reason": "causal-evidence-verified",
    }


@pytest.mark.parametrize("value", [None, [], {"unexpected": True}, "finding"])
def test_sanitized_audit_fields_never_raise_for_missing_finding(value: object) -> None:
    fields = causal_audit_fields(value)

    assert fields["evidence_schema_version"] is None
    assert fields["scorer_visible_effect"] is None
    assert fields["causal_role_complete"] is False
    assert fields["causal_verification_reason"] == "causal-finding-unavailable"


def test_parser_preserves_roles_in_signed_finding_and_discards_model_summary(
    tmp_path: Path,
) -> None:
    archive, artifact_sha, digest = _tar(tmp_path, "fn run() {}\nfn sink() {}\n")
    repository = TarSourceRepository(str(archive))

    observation, _analyzed, _causal, _basis = _parse_l2_review(
        _review_value(digest),
        artifact_sha256=artifact_sha,
        repository=repository,
    )
    finding = SourceReviewFinding.model_validate(observation.finding)

    assert finding.evidence_schema_version == 3
    assert finding.causal_evidence is not None
    assert (
        finding.causal_evidence.scorer_visible_effect
        == SourceReviewScorerVisibleEffect.ANSWER
    )
    assert {binding.role for binding in finding.causal_evidence.role_bindings} == set(
        SourceReviewEvidenceRole
    )
    assert finding.summary == causal_summary(
        SourceReviewAuthorityTransition.MODEL_SKIPPED,
        SourceReviewScorerVisibleEffect.ANSWER,
    )
    assert "private text" not in finding.summary
    assert observation.finding_digest == finding.canonical_digest()


def test_parser_rejects_causal_binding_with_wrong_file_digest(tmp_path: Path) -> None:
    archive, artifact_sha, digest = _tar(tmp_path, "fn run() {}\nfn sink() {}\n")
    repository = TarSourceRepository(str(archive))
    value = _review_value(digest)
    causal = value["causal_evidence"]
    assert isinstance(causal, dict)
    bindings = causal["role_bindings"]
    assert isinstance(bindings, list)
    bindings[0]["file_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="evidence-bound"):
        _parse_l2_review(value, artifact_sha256=artifact_sha, repository=repository)


def test_parser_rejects_causal_binding_not_in_public_evidence(tmp_path: Path) -> None:
    source = "fn run() {}\nfn sink() {}\nfn unrelated() {}\n"
    archive, artifact_sha, digest = _tar(tmp_path, source)
    repository = TarSourceRepository(str(archive))
    value = _review_value(digest)
    causal = value["causal_evidence"]
    assert isinstance(causal, dict)
    bindings = causal["role_bindings"]
    assert isinstance(bindings, list)
    bindings[0]["line"] = 3

    with pytest.raises(ValueError, match="evidence-bound"):
        _parse_l2_review(value, artifact_sha256=artifact_sha, repository=repository)


def test_parser_rejects_unknown_scorer_visible_effect(tmp_path: Path) -> None:
    archive, artifact_sha, digest = _tar(tmp_path, "fn run() {}\nfn sink() {}\n")
    repository = TarSourceRepository(str(archive))
    value = _review_value(digest)
    causal = value["causal_evidence"]
    assert isinstance(causal, dict)
    causal["scorer_visible_effect"] = "private_score_slot"

    with pytest.raises(ValueError, match="scorer-visible effect"):
        _parse_l2_review(value, artifact_sha256=artifact_sha, repository=repository)


def test_parser_rejects_transition_effect_mismatch(tmp_path: Path) -> None:
    archive, artifact_sha, digest = _tar(tmp_path, "fn run() {}\nfn sink() {}\n")
    repository = TarSourceRepository(str(archive))
    value = _review_value(digest)
    causal = value["causal_evidence"]
    assert isinstance(causal, dict)
    causal["scorer_visible_effect"] = "tool_calls"

    with pytest.raises(ValueError, match="incompatible with authority transition"):
        _parse_l2_review(value, artifact_sha256=artifact_sha, repository=repository)


def test_parser_rejects_causal_evidence_on_safe_result(tmp_path: Path) -> None:
    archive, artifact_sha, digest = _tar(tmp_path, "fn run() {}\nfn sink() {}\n")
    repository = TarSourceRepository(str(archive))
    value = _review_value(digest)
    value.update(
        disposition="safe",
        risk_level="low",
        resolution_basis="authoritative_model_tool_path",
        categories=["none"],
        evidence=[],
        causal_path=[],
    )

    with pytest.raises(ValueError, match="safe result cannot contain"):
        _parse_l2_review(value, artifact_sha256=artifact_sha, repository=repository)


def test_enforce_boundary_turns_elevated_v1_into_inconclusive() -> None:
    observation = _enforce_causal_authority(_observation(_finding(transition=None)))

    assert observation.ok is False
    assert observation.failure_disposition == "inconclusive"
    assert observation.error_code == "l2-causal-role-incomplete"
    assert observation.finding is None


def test_enforce_boundary_accepts_role_complete_v2() -> None:
    expected = _observation(_finding())

    assert _enforce_causal_authority(expected) == expected


class _FakeL1:
    async def review(
        self, *_args: object, **_kwargs: object
    ) -> SourceReviewObservation:
        raise AssertionError("resolve_lead must use the precomputed L1 result")


class _FakeL2:
    def __init__(self, result: L2RunResult) -> None:
        self.result = result
        self.calls = 0

    async def review(self, *_args: object, **_kwargs: object) -> L2RunResult:
        self.calls += 1
        return self.result


def _l2_result(finding: SourceReviewFinding) -> L2RunResult:
    return L2RunResult(
        observation=_observation(finding),
        analyzed_files=(),
        causal_path=(),
        tools=(),
        usage=L2Usage(),
        cache_hit=False,
    )


@pytest.mark.asyncio
async def test_shadow_retains_exact_v1_authority_while_recording_v2() -> None:
    l1 = _observation(_finding(transition=None))
    candidate = _l2_result(_finding())
    l2 = _FakeL2(candidate)
    agent = LayeredSourceReviewAgent(l1=_FakeL1(), l2=l2, mode="shadow")  # type: ignore[arg-type]

    actual = await agent.resolve_lead(
        "unused.tar.gz",
        artifact_sha256=ARTIFACT_SHA,
        attempt_id=ATTEMPT_ID,
        l1_observation=l1,
    )

    assert actual == l1
    assert agent.pop_shadow_result(ATTEMPT_ID) == candidate
    assert l2.calls == 1


@pytest.mark.asyncio
async def test_off_mode_returns_exact_v1_without_running_l2() -> None:
    l1 = _observation(_finding(transition=None))
    l2 = _FakeL2(_l2_result(_finding()))
    agent = LayeredSourceReviewAgent(l1=_FakeL1(), l2=l2, mode="off")  # type: ignore[arg-type]

    actual = await agent.resolve_lead(
        "unused.tar.gz",
        artifact_sha256=ARTIFACT_SHA,
        attempt_id=ATTEMPT_ID,
        l1_observation=l1,
    )

    assert actual == l1
    assert actual.finding_digest == l1.finding_digest
    assert actual.finding == l1.finding
    assert l2.calls == 0


@pytest.mark.asyncio
async def test_enforce_mode_requires_role_complete_candidate() -> None:
    l1 = _observation(_finding(transition=None))
    l2 = _FakeL2(_l2_result(_finding(transition=None)))
    agent = LayeredSourceReviewAgent(l1=_FakeL1(), l2=l2, mode="enforce")  # type: ignore[arg-type]

    actual = await agent.resolve_lead(
        "unused.tar.gz",
        artifact_sha256=ARTIFACT_SHA,
        attempt_id=ATTEMPT_ID,
        l1_observation=l1,
    )

    assert actual.failure_disposition == "inconclusive"
    assert actual.error_code == "l2-causal-role-incomplete"


def _regression_cases() -> list[dict[str, object]]:
    cases = json.loads((FIXTURES / "causal-evidence-v2-regressions.json").read_text())
    assert isinstance(cases, list)
    return cases


def _corpus_review_value(case: dict[str, object], digest: str) -> dict[str, object]:
    disposition = str(case["disposition"])
    base: dict[str, object] = {
        "disposition": disposition,
        "risk_level": "low" if disposition == "safe" else "high",
        "confidence": 0.99,
        "resolution_basis": (
            "authoritative_model_tool_path"
            if disposition == "safe"
            else "insufficient_static_evidence"
            if disposition == "inconclusive"
            else "scorer_field_manipulation"
            if case["authority_transition"] == "scorer_field_rewritten"
            else "benchmark_answer_replacement"
        ),
        "categories": [
            "none"
            if disposition != "violation"
            else "scorer_contract_manipulation"
            if case["authority_transition"] == "scorer_field_rewritten"
            else "benchmark_emulation"
        ],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [],
        "causal_path": [],
        "generator_components": [],
        "summary": "Sanitized regression fixture result.",
    }
    if disposition != "violation":
        return _with_policy_v10_invariants(base)

    category = str(base["categories"][0])
    evidence = [
        {
            "path": "src/main.rs",
            "line": line,
            "file_sha256": digest,
            "category": category,
            "role": "trigger" if line == 1 else "effect",
        }
        for line in (1, 2)
    ]
    base.update(
        evidence=evidence,
        causal_path=[
            {"path": "src/main.rs", "line": 1, "role": "trigger"},
            {"path": "src/main.rs", "line": 2, "role": "effect"},
        ],
        causal_evidence={
            "schema_version": 2,
            "authority_transition": case["authority_transition"],
            "scorer_visible_effect": case["scorer_visible_effect"],
            "role_bindings": [
                {
                    "path": "src/main.rs",
                    "line": 1 if index % 2 == 0 else 2,
                    "file_sha256": digest,
                    "category": category,
                    "role": role.value,
                }
                for index, role in enumerate(SourceReviewEvidenceRole)
            ],
        },
    )
    return _with_policy_v10_invariants(base)


@pytest.mark.parametrize("case", _regression_cases(), ids=lambda case: case["id"])
def test_sanitized_corpus_runs_through_production_parser_and_authority_boundary(
    tmp_path: Path, case: dict[str, object]
) -> None:
    _archive, artifact_sha, digest = _tar(tmp_path, str(case["source"]))
    repository = TarSourceRepository(str(_archive))

    observation, analyzed, _causal, resolution_basis = _parse_l2_review(
        _corpus_review_value(case, digest),
        artifact_sha256=artifact_sha,
        repository=repository,
    )
    enforced = _enforce_causal_authority(observation)

    assert analyzed == ({"path": "src/main.rs", "sha256": digest},)
    assert resolution_basis == _corpus_review_value(case, digest)["resolution_basis"]
    if case["disposition"] == "inconclusive":
        assert enforced.ok is False
        assert enforced.failure_disposition == "inconclusive"
        assert enforced.finding is None
        return

    finding = SourceReviewFinding.model_validate(enforced.finding)
    assert enforced.ok is True
    if case["disposition"] == "safe":
        assert finding.risk_level == "low"
        assert finding.categories == ["none"]
        assert finding.evidence_schema_version == 3
        return

    verification = verify_causal_finding(finding)
    assert verification.role_complete is True
    assert verification.reason_code == "causal-evidence-verified"
    assert verification.authority_transition == case["authority_transition"]
    assert verification.scorer_visible_effect == case["scorer_visible_effect"]
