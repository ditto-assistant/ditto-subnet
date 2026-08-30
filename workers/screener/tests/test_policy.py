"""Contract tests for typed private outcomes and daily module rotations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

import pytest

from ditto_screener.policy import (
    _ORACLE_SYSTEM_PROMPT,
    CORE_ONLY_MANIFEST,
    AgenticSourceReviewModule,
    BehavioralChallengePackModule,
    BehavioralOracleModule,
    ChallengeObservation,
    PolicyContext,
    PolicyEngine,
    PolicyManifest,
    ReviewJournal,
    ScreeningOutcome,
    SourceFingerprintTriageModule,
    SourceReviewObservation,
    TimingRelayRiskModule,
    core_decision,
    load_policy_engine,
)
from ditto_screening_protocol import SCREENING_POLICY_VERSION, SourceReviewFinding

_AGENT = UUID("4f2a1309-f763-4d40-9326-9eb7d13339e8")
_ATTEMPT = UUID("7c5df3f9-3ea7-47ba-92d1-1bbcf4c5f300")
_DIGEST = "de" * 32


def _context(  # type: ignore[no-untyped-def]
    challenge,
    review_source=None,
) -> PolicyContext:
    return PolicyContext(
        agent_id=_AGENT,
        attempt_id=_ATTEMPT,
        miner_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
        artifact_sha256=_DIGEST,
        source_digest=_DIGEST,
        source_paths=("Cargo.toml", "Dockerfile", "src/main.rs"),
        build_elapsed_ms=100,
        health_elapsed_ms=20,
        run_challenge=challenge,
        review_source=review_source,
    )


def _model_binding_engine(tmp_path: Path) -> PolicyEngine:
    pack = tmp_path / "rotating-pack.json"
    pack.write_text(
        json.dumps(
            {
                "challenges": [
                    {
                        "id": "rotating-private-control",
                        "request": {"case_id": "private-control"},
                        "timeout_seconds": 10,
                        "required_response_keys": ["final_text", "tool_calls"],
                        "require_model_call": True,
                        "require_gateway_token": True,
                    }
                ]
            }
        )
    )
    selector = SourceFingerprintTriageModule(
        module_id="private-selector",
        known_source_digests=frozenset({_DIGEST}),
    )
    challenge = BehavioralChallengePackModule(module_id="model-binding", pack_path=pack)
    manifest = PolicyManifest(
        rotation_id="private-control",
        module_specs=(
            {"kind": "source_fingerprint"},
            {"kind": "behavioral_challenge_pack"},
        ),
    )
    return PolicyEngine(manifest, (selector, challenge))


async def test_core_only_pass_never_calls_run() -> None:
    calls = 0

    async def challenge(*_):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise AssertionError("core-only policy must not call /run")

    decision = await PolicyEngine(CORE_ONLY_MANIFEST).evaluate(_context(challenge))
    assert decision.outcome == ScreeningOutcome.PASS
    assert decision.submits_verdict and decision.passed
    assert calls == 0


async def test_default_v7_runs_luna_review_and_behavioral_oracle_and_passes() -> None:
    reviews = 0
    challenges = 0

    async def challenge(challenge_id, _request, _timeout):  # type: ignore[no-untyped-def]
        nonlocal challenges
        challenges += 1
        assert challenge_id == "v8-behavioral-oracle"
        return ChallengeObservation(
            challenge_id=challenge_id,
            ok=True,
            response_digest="ab" * 32,
            elapsed_ms=800,
            gateway_calls=2,
            gateway_token_observed=True,
            oracle_answer_correct=True,
        )

    async def review() -> SourceReviewObservation:
        nonlocal reviews
        reviews += 1
        return SourceReviewObservation(
            ok=True,
            risk_level="low",
            finding_digest=None,
            categories=("none",),
        )

    engine = load_policy_engine(None)
    decision = await engine.evaluate(_context(challenge, review))

    assert engine.manifest.rotation_id == "v8-luna-source-review-behavioral-oracle"
    assert decision.outcome == ScreeningOutcome.PASS
    assert reviews == 1
    # The always-on oracle runs even though source review cleared (no tripwire).
    assert challenges == 1


async def test_timing_is_only_a_tripwire_and_routes_to_quarantine(
    tmp_path: Path,
) -> None:
    feed = tmp_path / "risk.json"
    feed.write_text(json.dumps({str(_AGENT): {"composite": 0.999, "median_ms": 3}}))
    module = TimingRelayRiskModule(module_id="timing", feed_path=feed)
    manifest = PolicyManifest(
        rotation_id="daily-1",
        module_specs=({"kind": "timing_relay_risk"},),
    )
    engine = PolicyEngine(manifest, (module,))

    async def no_challenge(*_):  # type: ignore[no-untyped-def]
        raise AssertionError

    decision = await engine.evaluate(_context(no_challenge))
    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert not decision.submits_verdict
    assert all(item.code != "deterministic_reject" for item in decision.evidence)


async def test_agentic_source_review_can_only_select_quarantine() -> None:
    async def challenge(*_):  # type: ignore[no-untyped-def]
        raise AssertionError("no behavioral pack is configured")

    async def review() -> SourceReviewObservation:
        return SourceReviewObservation(
            ok=True,
            risk_level="high",
            finding_digest="ab" * 32,
            categories=("provider_bypass",),
        )

    module = AgenticSourceReviewModule(module_id="private-source-review")
    engine = PolicyEngine(
        PolicyManifest(
            rotation_id="source-review",
            module_specs=({"kind": "agentic_source_review"},),
        ),
        (module,),
    )
    decision = await engine.evaluate(_context(challenge, review))
    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert not decision.submits_verdict
    assert decision.evidence[0].code == "source-safety-private-challenge-risk"


@pytest.mark.parametrize(
    ("failure_disposition", "expected"),
    [
        ("retryable_infra", ScreeningOutcome.RETRYABLE_INFRA),
        ("inconclusive", ScreeningOutcome.INCONCLUSIVE),
    ],
)
async def test_l2_failure_disposition_fails_closed_without_rejection(
    failure_disposition: str, expected: ScreeningOutcome
) -> None:
    async def challenge(*_):  # type: ignore[no-untyped-def]
        raise AssertionError

    async def review() -> SourceReviewObservation:
        return SourceReviewObservation(
            ok=False,
            risk_level=None,
            finding_digest=None,
            categories=(),
            error_code="l2-review-unavailable",
            failure_disposition=failure_disposition,
        )

    engine = PolicyEngine(
        PolicyManifest(
            rotation_id="l2-failure-safety",
            module_specs=({"kind": "agentic_source_review"},),
        ),
        (AgenticSourceReviewModule(module_id="private-source-review"),),
    )
    decision = await engine.evaluate(_context(challenge, review))

    assert decision.outcome == expected
    assert decision.outcome != ScreeningOutcome.DETERMINISTIC_REJECT


def test_l2_enforcement_is_manifest_bound_but_shadow_is_not() -> None:
    off = load_policy_engine(None, l2_mode="off").manifest
    shadow = load_policy_engine(None, l2_mode="shadow").manifest
    enforce = load_policy_engine(None, l2_mode="enforce").manifest

    assert off.digest == shadow.digest
    assert enforce.digest != off.digest
    assert enforce.policy_version == off.policy_version
    assert "sol-l2" in enforce.rotation_id


@pytest.mark.parametrize(
    ("category", "expected_code"),
    [
        ("benchmark_emulation", "source-safety-private-challenge-risk"),
        ("scorer_contract_manipulation", "source-safety-private-challenge-risk"),
        ("hidden_value_leakage", "source-safety-private-challenge-risk"),
        ("embedded_secret", "source-safety-malicious-risk"),
        ("data_exfiltration", "source-safety-malicious-risk"),
        ("malicious_build", "source-safety-malicious-risk"),
        ("cross_user_access", "source-safety-malicious-risk"),
        ("duplicate_submission", "originality-duplicate-risk"),
    ],
)
async def test_adversarial_and_originality_risks_remain_quarantined(
    category: str, expected_code: str
) -> None:
    async def challenge(*_):  # type: ignore[no-untyped-def]
        raise AssertionError("no private challenge is required to retain the hold")

    async def review() -> SourceReviewObservation:
        return SourceReviewObservation(
            ok=True,
            risk_level="high",
            finding_digest="ab" * 32,
            categories=(category,),
        )

    engine = PolicyEngine(
        PolicyManifest(
            rotation_id="risk-domain-regression",
            module_specs=({"kind": "agentic_source_review"},),
        ),
        (AgenticSourceReviewModule(module_id="private-source-review"),),
    )
    decision = await engine.evaluate(_context(challenge, review))

    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert decision.evidence[-1].code == expected_code
    assert all(
        item.code != "audit-awaiting-private-challenge" for item in decision.evidence
    )


@pytest.mark.parametrize(
    "category", ["external_build_dependency", "user_isolation_correctness"]
)
async def test_low_advisory_source_categories_clear_without_anti_cheat_hold(
    category: str,
) -> None:
    finding = {"categories": [category], "summary": "advisory only"}

    async def challenge(*_):  # type: ignore[no-untyped-def]
        raise AssertionError("advisory-only review must not select a challenge")

    async def review() -> SourceReviewObservation:
        return SourceReviewObservation(
            ok=True,
            risk_level="low",
            finding_digest="ab" * 32,
            categories=(category,),
            finding=finding,
        )

    engine = PolicyEngine(
        PolicyManifest(
            rotation_id="advisory-source-review",
            module_specs=({"kind": "agentic_source_review"},),
        ),
        (AgenticSourceReviewModule(module_id="private-source-review"),),
    )
    decision = await engine.evaluate(_context(challenge, review))

    assert decision.outcome == ScreeningOutcome.PASS
    assert decision.finding == finding


async def test_medium_isolation_correctness_uses_non_anti_cheat_reason() -> None:
    async def challenge(*_):  # type: ignore[no-untyped-def]
        raise AssertionError("no behavioral pack is configured")

    async def review() -> SourceReviewObservation:
        return SourceReviewObservation(
            ok=True,
            risk_level="medium",
            finding_digest="ab" * 32,
            categories=("user_isolation_correctness",),
        )

    engine = PolicyEngine(
        PolicyManifest(
            rotation_id="correctness-review",
            module_specs=({"kind": "agentic_source_review"},),
        ),
        (AgenticSourceReviewModule(module_id="private-source-review"),),
    )
    decision = await engine.evaluate(_context(challenge, review))

    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert decision.evidence[0].code == "source-correctness-review"
    assert "not terminal anti-cheat proof" in decision.evidence[0].summary


async def test_low_risk_label_cannot_clear_a_malicious_category() -> None:
    async def challenge(*_):  # type: ignore[no-untyped-def]
        raise AssertionError

    async def review() -> SourceReviewObservation:
        return SourceReviewObservation(
            ok=True,
            risk_level="low",
            finding_digest="ab" * 32,
            categories=("embedded_secret",),
        )

    engine = PolicyEngine(
        PolicyManifest(
            rotation_id="inconsistent-review-regression",
            module_specs=({"kind": "agentic_source_review"},),
        ),
        (AgenticSourceReviewModule(module_id="private-source-review"),),
    )
    decision = await engine.evaluate(_context(challenge, review))

    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert decision.evidence[-1].code == "source-safety-malicious-risk"


async def test_tripwire_plus_behavioral_shape_anomaly_stays_quarantine(
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack.json"
    pack.write_text(
        json.dumps(
            {
                "challenges": [
                    {
                        "id": "rotating-private-case",
                        "request": {"case_id": "private"},
                        "timeout_seconds": 10,
                        "required_response_keys": ["final_text", "tool_calls"],
                    }
                ]
            }
        )
    )
    selector = SourceFingerprintTriageModule(
        module_id="fingerprint", known_source_digests=frozenset({_DIGEST})
    )
    challenge_module = BehavioralChallengePackModule(
        module_id="behavior", pack_path=pack
    )
    manifest = PolicyManifest(
        rotation_id="daily-2",
        module_specs=(
            {"kind": "source_fingerprint"},
            {"kind": "behavioral_challenge_pack"},
        ),
    )
    engine = PolicyEngine(manifest, (selector, challenge_module))

    async def observe(challenge_id, request, timeout):  # type: ignore[no-untyped-def]
        assert challenge_id == "rotating-private-case"
        assert request == {"case_id": "private"} and timeout == 10
        return ChallengeObservation(
            challenge_id=challenge_id,
            ok=True,
            response_digest="ab" * 32,
            elapsed_ms=2,
            json_keys=("final_text",),
        )

    decision = await engine.evaluate(_context(observe))
    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert not decision.submits_verdict
    assert any(item.code == "challenge-shape-anomaly" for item in decision.evidence)


async def test_inconclusive_challenge_is_not_a_rejection(tmp_path: Path) -> None:
    pack = tmp_path / "pack.json"
    pack.write_text(
        json.dumps(
            {
                "challenges": [
                    {
                        "id": "case",
                        "request": {},
                        "timeout_seconds": 5,
                        "required_response_keys": [],
                    }
                ]
            }
        )
    )
    selector = SourceFingerprintTriageModule(
        module_id="fingerprint", known_source_digests=frozenset({_DIGEST})
    )
    challenge_module = BehavioralChallengePackModule(
        module_id="behavior", pack_path=pack
    )
    manifest = PolicyManifest(
        rotation_id="daily-3", module_specs=({"kind": "x"}, {"kind": "y"})
    )

    async def observe(*_):  # type: ignore[no-untyped-def]
        return ChallengeObservation("case", False, None, 100, error_code="timeout")

    decision = await PolicyEngine(manifest, (selector, challenge_module)).evaluate(
        _context(observe)
    )
    assert decision.outcome == ScreeningOutcome.INCONCLUSIVE
    assert not decision.submits_verdict


async def test_canonical_starter_control_clears_ephemeral_model_binding(
    tmp_path: Path,
) -> None:
    """Model a canonical kit response that propagates the fake gateway output."""

    async def observe(*_):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            "rotating-private-control",
            True,
            "ab" * 32,
            75,
            json_keys=("final_text", "tool_calls"),
            gateway_calls=1,
            gateway_token_observed=True,
        )

    decision = await _model_binding_engine(tmp_path).evaluate(_context(observe))
    assert decision.outcome == ScreeningOutcome.PASS
    assert decision.submits_verdict and decision.passed


async def test_benchmark_shortcut_fixture_is_quarantined_not_rejected(
    tmp_path: Path,
) -> None:
    """A valid-looking hardcoded response without a model call is review evidence."""

    async def observe(*_):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            "rotating-private-control",
            True,
            "cd" * 32,
            1,
            json_keys=("final_text", "tool_calls"),
            gateway_calls=0,
            gateway_token_observed=False,
        )

    decision = await _model_binding_engine(tmp_path).evaluate(_context(observe))
    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert not decision.submits_verdict
    assert any(
        item.code == "challenge-model-call-missing" for item in decision.evidence
    )


async def test_dummy_model_call_without_dataflow_is_quarantined(
    tmp_path: Path,
) -> None:
    async def observe(*_):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            "rotating-private-control",
            True,
            "ef" * 32,
            2,
            json_keys=("final_text", "tool_calls"),
            gateway_calls=1,
            gateway_token_observed=False,
        )

    decision = await _model_binding_engine(tmp_path).evaluate(_context(observe))
    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert any(
        item.code == "challenge-gateway-token-missing" for item in decision.evidence
    )


async def test_shape_only_pack_preserves_non_model_harness_compatibility(
    tmp_path: Path,
) -> None:
    """Legacy/private audits opt into model binding explicitly."""
    pack = tmp_path / "shape-only.json"
    pack.write_text(
        json.dumps(
            {
                "challenges": [
                    {
                        "id": "shape",
                        "request": {},
                        "timeout_seconds": 5,
                        "required_response_keys": ["final_text"],
                    }
                ]
            }
        )
    )
    selector = SourceFingerprintTriageModule(
        module_id="selector", known_source_digests=frozenset({_DIGEST})
    )
    challenge = BehavioralChallengePackModule(module_id="shape", pack_path=pack)
    engine = PolicyEngine(
        PolicyManifest(rotation_id="shape", module_specs=({"a": 1}, {"b": 2})),
        (selector, challenge),
    )

    async def observe(*_):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            "shape",
            True,
            "12" * 32,
            1,
            json_keys=("final_text",),
        )

    decision = await engine.evaluate(_context(observe))
    assert decision.outcome == ScreeningOutcome.PASS


def _oracle_engine() -> PolicyEngine:
    """Default-v8-style engine: no selector tripwire, always-on oracle only."""
    manifest = PolicyManifest(
        rotation_id="oracle-only",
        module_specs=({"kind": "behavioral_oracle"},),
    )
    return PolicyEngine(manifest, (BehavioralOracleModule(module_id="oracle"),))


async def test_behavioral_oracle_runs_without_any_tripwire() -> None:
    """The oracle challenge runs on every submission, not just on an audit."""
    seen: list[str] = []

    async def observe(challenge_id, _request, _timeout):  # type: ignore[no-untyped-def]
        seen.append(challenge_id)
        return ChallengeObservation(
            challenge_id=challenge_id,
            ok=True,
            response_digest="ab" * 32,
            elapsed_ms=900,
            gateway_calls=2,
            oracle_answer_correct=True,
            gateway_token_observed=True,
        )

    decision = await _oracle_engine().evaluate(_context(observe))
    assert seen == ["v8-behavioral-oracle"]
    assert decision.outcome == ScreeningOutcome.PASS
    assert decision.submits_verdict and decision.passed


async def test_build_only_skips_source_review_selector_and_passes() -> None:
    """A build-only pass never runs the anti-cheat selector (source review)."""
    reviewed = False

    async def challenge(*_):  # type: ignore[no-untyped-def]
        raise AssertionError("build-only pass runs no behavioral pack")

    async def review() -> SourceReviewObservation:
        nonlocal reviewed
        reviewed = True
        return SourceReviewObservation(
            ok=True,
            risk_level="high",
            finding_digest="ab" * 32,
            categories=("provider_bypass",),
        )

    engine = PolicyEngine(
        PolicyManifest(
            rotation_id="source-review",
            module_specs=({"kind": "agentic_source_review"},),
        ),
        (AgenticSourceReviewModule(module_id="private-source-review"),),
    )
    # The same context QUARANTINES in the full pipeline
    # (test_agentic_source_review_can_only_select_quarantine); build-only skips
    # the selector entirely, never consults review, and passes.
    decision = await engine.evaluate(_context(challenge, review), build_only=True)
    assert decision.outcome == ScreeningOutcome.PASS
    assert decision.submits_verdict and decision.passed
    assert reviewed is False


async def test_build_only_skips_behavioral_oracle_and_passes() -> None:
    """Mechanical admission does not spend private-policy/model budget."""

    async def observe(*_):  # type: ignore[no-untyped-def]
        raise AssertionError("build-only admission must not run the oracle")

    decision = await _oracle_engine().evaluate(_context(observe), build_only=True)
    assert decision.outcome == ScreeningOutcome.PASS
    assert decision.submits_verdict and decision.passed


async def test_deferred_source_review_mechanical_lane_skips_oracle() -> None:
    async def observe(*_):  # type: ignore[no-untyped-def]
        raise AssertionError("deferred mechanical admission must not run the oracle")

    decision = await _oracle_engine().evaluate(
        _context(observe),
        build_only=True,
        deferred_source_review=True,
    )
    assert decision.outcome == ScreeningOutcome.PASS


async def test_deferred_source_review_requires_mechanical_lane() -> None:
    async def observe(*_):  # type: ignore[no-untyped-def]
        raise AssertionError("invalid mode must fail before module evaluation")

    with pytest.raises(ValueError, match="requires the mechanical lane"):
        await _oracle_engine().evaluate(_context(observe), deferred_source_review=True)


async def test_full_review_still_reports_private_policy_infra_failure() -> None:
    """Deferral does not weaken the later full source and behavioral review."""

    async def challenge(*_):  # type: ignore[no-untyped-def]
        raise AssertionError("unavailable pack never reaches the runner")

    engine = PolicyEngine(
        PolicyManifest(
            rotation_id="pack",
            module_specs=({"kind": "behavioral_challenge_pack"},),
        ),
        (
            BehavioralChallengePackModule(
                module_id="pack", pack_path=Path("/nonexistent")
            ),
        ),
    )
    decision = await engine.evaluate(_context(challenge))
    assert decision.outcome == ScreeningOutcome.RETRYABLE_INFRA


async def test_reasoning_harness_passes_the_oracle() -> None:
    async def observe(*_):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            "v8-behavioral-oracle",
            True,
            "ab" * 32,
            elapsed_ms=1200,
            gateway_calls=3,
            oracle_answer_correct=True,
            gateway_token_observed=True,
        )

    decision = await _oracle_engine().evaluate(_context(observe))
    assert decision.outcome == ScreeningOutcome.PASS
    assert any(item.code == "behavioral-oracle-passed" for item in decision.evidence)


async def test_single_call_oracle_is_inconclusive_without_causal_evidence() -> None:
    async def observe(*_):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            "v8-behavioral-oracle",
            True,
            "cd" * 32,
            elapsed_ms=900,
            gateway_calls=0,
            oracle_answer_correct=False,
        )

    decision = await _oracle_engine().evaluate(_context(observe))
    assert decision.outcome == ScreeningOutcome.INCONCLUSIVE
    assert not decision.submits_verdict
    assert any(
        item.code == "behavioral-oracle-insufficient-round-trips"
        for item in decision.evidence
    )


async def test_wrong_nonce_table_harness_fails_the_oracle() -> None:
    async def observe(*_):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            "v8-behavioral-oracle",
            True,
            "ef" * 32,
            elapsed_ms=900,
            gateway_calls=2,
            oracle_answer_correct=False,
        )

    decision = await _oracle_engine().evaluate(_context(observe))
    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert any(
        item.code == "behavioral-oracle-wrong-answer" for item in decision.evidence
    )


async def test_too_fast_round_trip_trips_the_timing_floor() -> None:
    async def observe(*_):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            "v8-behavioral-oracle",
            True,
            "01" * 32,
            elapsed_ms=3,
            gateway_calls=2,
            oracle_answer_correct=True,
            gateway_token_observed=True,
        )

    decision = await _oracle_engine().evaluate(_context(observe))
    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert any(
        item.code == "behavioral-oracle-implausibly-fast" for item in decision.evidence
    )


@pytest.mark.parametrize(
    ("gateway_calls", "answer_correct", "elapsed_ms"),
    [(1, False, 900), (2, False, 900), (2, True, 3)],
)
async def test_behavioral_oracle_anomalies_are_escalation_only(
    gateway_calls: int, answer_correct: bool, elapsed_ms: int
) -> None:
    async def observe(*_):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            "v8-behavioral-oracle",
            True,
            "01" * 32,
            elapsed_ms=elapsed_ms,
            gateway_calls=gateway_calls,
            oracle_answer_correct=answer_correct,
            gateway_token_observed=answer_correct,
        )

    decision = await _oracle_engine().evaluate(_context(observe))

    expected = (
        ScreeningOutcome.INCONCLUSIVE
        if gateway_calls < 2
        else ScreeningOutcome.QUARANTINE
    )
    assert decision.outcome == expected
    assert decision.outcome != ScreeningOutcome.DETERMINISTIC_REJECT
    assert not decision.submits_verdict


async def test_inconclusive_oracle_is_not_a_rejection() -> None:
    async def observe(*_):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            "v8-behavioral-oracle",
            False,
            None,
            elapsed_ms=10,
            error_code="challenge-http-failure",
        )

    decision = await _oracle_engine().evaluate(_context(observe))
    assert decision.outcome == ScreeningOutcome.INCONCLUSIVE
    assert not decision.submits_verdict


def test_manifest_rotation_changes_digest_not_policy_or_signature_contract(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "policy_version": 11,
                "rotation_id": "2026-07-14-private",
                "modules": [
                    {
                        "kind": "random_audit",
                        "id": "controls",
                        "rate_basis_points": 500,
                        "seed_env": "SCREENER_AUDIT_SEED",
                    }
                ],
            }
        )
    )
    engine = load_policy_engine(str(manifest))
    assert engine.manifest.policy_version == SCREENING_POLICY_VERSION == 11
    assert engine.manifest.digest != CORE_ONLY_MANIFEST.digest


def test_review_journal_is_bounded_private_and_mode_0600(tmp_path: Path) -> None:
    journal_path = tmp_path / "private" / "review.jsonl"
    journal = ReviewJournal(str(journal_path))

    async def challenge(*_):  # type: ignore[no-untyped-def]
        raise AssertionError

    context = _context(challenge)
    decision = core_decision(
        ScreeningOutcome.QUARANTINE,
        code="private-review",
        summary="operator review required",
        detail="private policy quarantine pending operator review",
    )
    journal.record(context=context, decision=decision)
    row = json.loads(journal_path.read_text())
    assert row["agent_id"] == str(_AGENT)
    assert row["attempt_id"] == str(_ATTEMPT)
    assert row["outcome"] == "quarantine"
    assert not os.stat(journal_path).st_mode & 0o077


def test_live_v6_snapshot_is_an_acceptance_fixture() -> None:
    fixture = Path(__file__).parent / "fixtures" / "production-v6-snapshot.json"
    snapshot = json.loads(fixture.read_text())
    assert snapshot["screening_policy_version"] == 6
    assert SCREENING_POLICY_VERSION == 11
    assert snapshot["queue"]["waiting_validator"] == 9
    assert snapshot["queue"]["evaluating"] == 1
    assert len(snapshot["rust_contract_rejections"]) == 6
    assert "not evidence" in snapshot["interpretation"].lower()
    decision = core_decision(
        ScreeningOutcome.DETERMINISTIC_REJECT,
        code="rust-harness-contract",
        summary="artifact does not satisfy the Rust harness contract",
        detail="contract failed: no Cargo.toml at tarball root",
    )
    assert decision.submits_verdict and not decision.passed


def _finding_payload(risk: str) -> dict[str, object]:
    return SourceReviewFinding(
        artifact_sha256=_DIGEST,
        prompt_revision="source-review-v2",
        risk_level=risk,
        confidence=0.9,
        categories=["provider_bypass"] if risk != "low" else ["none"],
        evidence=(
            [{"path": "src/main.rs", "line": 7, "category": "provider_bypass"}]
            if risk != "low"
            else []
        ),
        summary="bounded operator summary",
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    ("failure_disposition", "outcome"),
    [
        ("retryable_infra", ScreeningOutcome.RETRYABLE_INFRA),
        ("inconclusive", ScreeningOutcome.INCONCLUSIVE),
    ],
)
def test_preexecution_review_failure_never_releases_or_rejects(
    failure_disposition: str, outcome: ScreeningOutcome
) -> None:
    observation = SourceReviewObservation(
        ok=False,
        risk_level=None,
        finding_digest=None,
        categories=(),
        error_code="l2-bounded-failure",
        failure_disposition=failure_disposition,
    )
    decision = PolicyEngine(CORE_ONLY_MANIFEST).preexecution_source_decision(
        observation
    )
    assert decision.outcome == outcome
    assert decision.outcome != ScreeningOutcome.DETERMINISTIC_REJECT
    assert not decision.submits_verdict or not decision.passed


@pytest.mark.parametrize(
    ("court_decision", "outcome"),
    [
        ("clear", ScreeningOutcome.PASS),
        ("reject", ScreeningOutcome.QUARANTINE),
        ("escalate", ScreeningOutcome.QUARANTINE),
    ],
)
def test_final_adjudication_settles_preexecution_source_lead(
    court_decision: str, outcome: ScreeningOutcome
) -> None:
    adjudication = {
        "decision": court_decision,
        "reason": "final court decision",
        "model": "z-ai/glm-5.3-flash",
        "prompt_revision": "adjudicator-v1-policy-v10",
        "notes_considered": 2,
    }
    observation = SourceReviewObservation(
        ok=False,
        risk_level=None,
        finding_digest=None,
        categories=(),
        error_code="l2-filenotfounderror",
        failure_disposition="retryable_infra",
        adjudication=adjudication,
    )

    decision = PolicyEngine(CORE_ONLY_MANIFEST).preexecution_source_decision(
        observation
    )

    assert decision.outcome == outcome
    assert decision.adjudication == adjudication
    assert decision.evidence[0].code == "source-review-adjudicated"


async def test_source_review_finding_travels_to_quarantine_decision() -> None:
    finding = _finding_payload("high")

    async def challenge(*_):  # type: ignore[no-untyped-def]
        raise AssertionError("no behavioral pack is configured")

    async def review() -> SourceReviewObservation:
        parsed = SourceReviewFinding.model_validate(finding)
        return SourceReviewObservation(
            ok=True,
            risk_level="high",
            finding_digest=parsed.canonical_digest(),
            categories=("provider_bypass",),
            finding=finding,
        )

    engine = PolicyEngine(
        PolicyManifest(
            rotation_id="source-review",
            module_specs=({"kind": "agentic_source_review"},),
        ),
        (AgenticSourceReviewModule(module_id="private-source-review"),),
    )
    decision = await engine.evaluate(_context(challenge, review))
    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert decision.finding == finding


@pytest.mark.parametrize(
    ("court_decision", "outcome"),
    [
        ("clear", ScreeningOutcome.PASS),
        ("reject", ScreeningOutcome.QUARANTINE),
        ("escalate", ScreeningOutcome.QUARANTINE),
    ],
)
async def test_final_adjudication_crosses_the_local_policy_boundary(
    court_decision: str, outcome: ScreeningOutcome
) -> None:
    async def challenge(challenge_id, _request, _timeout):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            challenge_id=challenge_id,
            ok=True,
            response_digest="ab" * 32,
            elapsed_ms=800,
            gateway_calls=2,
            gateway_token_observed=True,
            oracle_answer_correct=True,
        )

    adjudication = {
        "decision": court_decision,
        "reason": "final court decision",
        "model": "z-ai/glm-5.3-flash",
        "prompt_revision": "adjudicator-v1-policy-v10",
        "notes_considered": 1,
    }

    async def review() -> SourceReviewObservation:
        return SourceReviewObservation(
            ok=False,
            risk_level=None,
            finding_digest=None,
            categories=(),
            error_code="l3-adjudicator-incomplete",
            failure_disposition="retryable_infra",
            adjudication=adjudication,
        )

    decision = await load_policy_engine(None).evaluate(_context(challenge, review))

    assert decision.outcome == outcome
    assert decision.adjudication == adjudication


async def test_clean_review_finding_is_kept_when_oracle_is_inconclusive() -> None:
    """A low-risk source review remains exculpatory on an inconclusive oracle."""
    finding = _finding_payload("low")

    async def challenge(challenge_id, _request, _timeout):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            challenge_id=challenge_id,
            ok=True,
            response_digest="ab" * 32,
            elapsed_ms=800,
            gateway_calls=1,
        )

    async def review() -> SourceReviewObservation:
        parsed = SourceReviewFinding.model_validate(finding)
        return SourceReviewObservation(
            ok=True,
            risk_level="low",
            finding_digest=parsed.canonical_digest(),
            categories=("none",),
            finding=finding,
        )

    decision = await load_policy_engine(None).evaluate(_context(challenge, review))
    assert decision.outcome == ScreeningOutcome.INCONCLUSIVE
    assert decision.finding == finding


async def test_oracle_pass_does_not_clear_a_source_review_tripwire() -> None:
    """A generic always-on challenge must never self-clear a flagged audit."""
    finding = _finding_payload("high")

    async def challenge(challenge_id, _request, _timeout):  # type: ignore[no-untyped-def]
        return ChallengeObservation(
            challenge_id=challenge_id,
            ok=True,
            response_digest="ab" * 32,
            elapsed_ms=800,
            gateway_calls=2,
            oracle_answer_correct=True,
        )

    async def review() -> SourceReviewObservation:
        parsed = SourceReviewFinding.model_validate(finding)
        return SourceReviewObservation(
            ok=True,
            risk_level="high",
            finding_digest=parsed.canonical_digest(),
            categories=("provider_bypass",),
            finding=finding,
        )

    decision = await load_policy_engine(None).evaluate(_context(challenge, review))
    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert decision.finding == finding
    assert any(item.digest is not None for item in decision.evidence)


async def test_oracle_request_satisfies_harness_run_contract() -> None:
    """The oracle payload must deserialize as the starter kit's RunRequest.

    dittobench-starter-kit `src/protocol.rs` declares `case_id`,
    `system_prompt` and `user_input` as REQUIRED (no serde default): an honest
    axum harness 422s on a request missing any of them before its handler
    runs, which turned every prod screening inconclusive when the oracle
    omitted `system_prompt`. Keep this list in lockstep with the contract.
    """
    seen: list[dict] = []

    async def challenge(challenge_id, request, _timeout):  # type: ignore[no-untyped-def]
        seen.append(dict(request))
        return ChallengeObservation(
            challenge_id=challenge_id,
            ok=True,
            response_digest="ab" * 32,
            elapsed_ms=800,
            gateway_calls=2,
            oracle_answer_correct=True,
        )

    module = BehavioralOracleModule(module_id="v8-behavioral-oracle")
    await module.evaluate(_context(challenge))
    (request,) = seen
    required = {"case_id", "system_prompt", "user_input"}
    optional = {"tools", "tool_endpoint", "user_id"}
    for fieldname in required:
        value = request.get(fieldname)
        assert isinstance(value, str) and value, f"missing/empty {fieldname}"
    assert set(request) <= required | optional


async def test_oracle_request_is_randomized_and_unfingerprintable() -> None:
    seen: list[dict] = []

    async def challenge(challenge_id, request, _timeout):  # type: ignore[no-untyped-def]
        seen.append(dict(request))
        return ChallengeObservation(
            challenge_id=challenge_id,
            ok=True,
            response_digest="ab" * 32,
            elapsed_ms=800,
            gateway_calls=2,
            oracle_answer_correct=True,
        )

    module = BehavioralOracleModule(module_id="v8-behavioral-oracle")
    await module.evaluate(_context(challenge))
    await module.evaluate(_context(challenge))
    first, second = seen
    # No fixed marker a harness could branch on.
    assert "protocol" not in first
    assert first["case_id"] != second["case_id"]
    assert first["user_input"] != second["user_input"]
    assert module.challenge_id not in str(first.values())


async def test_oracle_request_envelope_matches_scored_tool_traffic() -> None:
    """The oracle request must be envelope-identical to a scored tool case, so a
    harness cannot classify oracle-vs-scored from any request field.

    - case_id uses the production "c" + 16 hex format (not a bare 32-hex token);
    - the full production tool catalog is offered (not a two-tool subset);
    - the single scored tool-case system prompt is used (not an oracle pool);
    - no user_id is set (scored tool cases carry none).
    """
    import re

    seen: list[dict] = []

    async def challenge(challenge_id, request, _timeout):  # type: ignore[no-untyped-def]
        seen.append(dict(request))
        return ChallengeObservation(
            challenge_id=challenge_id,
            ok=True,
            response_digest="ab" * 32,
            elapsed_ms=800,
            gateway_calls=2,
            oracle_answer_correct=True,
            gateway_token_observed=True,
        )

    module = BehavioralOracleModule(module_id="v8-behavioral-oracle")
    await module.evaluate(_context(challenge))
    (request,) = seen
    # Production case-ID format: "c" + 16 hex (OpaqueCaseID), not [0-9a-f]{32}.
    assert re.fullmatch(r"c[0-9a-f]{16}", str(request["case_id"])), request["case_id"]
    # Full catalog, not a tiny subset.
    tools = request.get("tools")
    assert isinstance(tools, list) and len(tools) >= 10, (
        "oracle must offer full catalog"
    )
    for tool in tools:
        assert isinstance(tool, dict)
        assert isinstance(tool.get("name"), str) and tool["name"]
    # The single scored tool-case system prompt, and NO user_id.
    assert request["system_prompt"] == _ORACLE_SYSTEM_PROMPT
    # Scored tool cases carry no user_id; the oracle must not either.
    assert "user_id" not in request
