import pytest

from ditto_screening_protocol.router_source_screen import (
    GENERALIZATION_GAP_DENY_BPS,
    GENERALIZATION_GAP_QUARANTINE_BPS,
    RouterGeneralizationSample,
    RouterSourceScreenEvidence,
    RouterSourceScreenFinding,
    RouterSourceScreenOutcome,
    RouterSourceScreenSeverity,
    build_router_source_screen_evidence,
    evaluate_router_generalization,
    router_source_screen_digest,
    router_source_screen_signing_message,
    screen_router_submission,
)

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _generalising() -> RouterGeneralizationSample:
    """A healthy router: similar public/held-out scores, varied routes, clean."""
    return RouterGeneralizationSample(
        public_score_bps=7000,
        heldout_score_bps=6800,
        heldout_cases=20,
        distinct_heldout_routes=5,
        canary_tasks_total=3,
        canary_tasks_failed=0,
        emitted_novel_token=False,
        deterministic_replay=True,
    )


def _evidence(sample: RouterGeneralizationSample) -> RouterSourceScreenEvidence:
    outcome, findings = evaluate_router_generalization(sample)
    return build_router_source_screen_evidence(
        agent_artifact_sha256="a" * 64,
        screened_image_sha256="b" * 64,
        analyzer_version="router-source-v1",
        policy_version=1,
        outcome=outcome,
        findings=findings,
    )


def test_healthy_router_passes_with_no_findings() -> None:
    outcome, findings = evaluate_router_generalization(_generalising())
    assert outcome is RouterSourceScreenOutcome.PASS
    assert findings == ()
    evidence = _evidence(_generalising())
    assert evidence.outcome is RouterSourceScreenOutcome.PASS
    assert evidence.weight_eligible is False
    assert evidence.evidence_sha256 == router_source_screen_digest(evidence)


def test_public_only_router_is_denied_on_generalization_gap() -> None:
    sample = _generalising().model_copy(
        update={
            "public_score_bps": 9000,
            "heldout_score_bps": 9000 - GENERALIZATION_GAP_DENY_BPS,
        }
    )
    outcome, findings = evaluate_router_generalization(sample)
    assert outcome is RouterSourceScreenOutcome.DENY
    assert any(f.rule_id == "generalization-gap" for f in findings)


def test_moderate_gap_quarantines_for_review() -> None:
    sample = _generalising().model_copy(
        update={
            "public_score_bps": 8000,
            "heldout_score_bps": 8000 - GENERALIZATION_GAP_QUARANTINE_BPS,
        }
    )
    outcome, findings = evaluate_router_generalization(sample)
    assert outcome is RouterSourceScreenOutcome.QUARANTINE
    assert {f.severity for f in findings} == {RouterSourceScreenSeverity.QUARANTINE}


def test_held_out_canary_failure_denies() -> None:
    sample = _generalising().model_copy(update={"canary_tasks_failed": 1})
    outcome, findings = evaluate_router_generalization(sample)
    assert outcome is RouterSourceScreenOutcome.DENY
    assert any(f.rule_id == "heldout-canary-failed" for f in findings)


def test_degenerate_constant_route_denies() -> None:
    sample = _generalising().model_copy(
        update={"heldout_cases": 20, "distinct_heldout_routes": 1}
    )
    outcome, findings = evaluate_router_generalization(sample)
    assert outcome is RouterSourceScreenOutcome.DENY
    assert any(f.rule_id == "degenerate-constant-route" for f in findings)


def test_novel_token_denies() -> None:
    sample = _generalising().model_copy(update={"emitted_novel_token": True})
    outcome, _ = evaluate_router_generalization(sample)
    assert outcome is RouterSourceScreenOutcome.DENY


def test_nondeterministic_replay_quarantines() -> None:
    sample = _generalising().model_copy(update={"deterministic_replay": False})
    outcome, findings = evaluate_router_generalization(sample)
    assert outcome is RouterSourceScreenOutcome.QUARANTINE
    assert any(f.rule_id == "nondeterministic-replay" for f in findings)


def test_heldout_below_floor_while_public_healthy_quarantines() -> None:
    sample = _generalising().model_copy(
        update={"public_score_bps": 7000, "heldout_score_bps": 500}
    )
    outcome, findings = evaluate_router_generalization(sample)
    # gap 6500 >= deny threshold, so this denies; the below-floor quarantine
    # finding still rides along as supporting evidence but never downgrades a deny.
    assert outcome is RouterSourceScreenOutcome.DENY
    assert {f.rule_id for f in findings} >= {
        "generalization-gap",
        "heldout-below-floor",
    }


def test_below_floor_alone_quarantines_without_a_deny_gap() -> None:
    # Public healthy, held-out at floor, but the gap stays under the deny line.
    sample = _generalising().model_copy(
        update={"public_score_bps": 6000, "heldout_score_bps": 1900}
    )
    outcome, findings = evaluate_router_generalization(sample)
    assert outcome is RouterSourceScreenOutcome.QUARANTINE
    assert any(f.rule_id == "heldout-below-floor" for f in findings)


def test_findings_are_canonical_and_deduplicated() -> None:
    sample = _generalising().model_copy(
        update={
            "public_score_bps": 9000,
            "heldout_score_bps": 500,
            "distinct_heldout_routes": 1,
            "emitted_novel_token": True,
        }
    )
    _, findings = evaluate_router_generalization(sample)
    keys = [(f.rule_id, f.evidence_sha256) for f in findings]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    # The assembled evidence must validate (coherence + content address).
    evidence = _evidence(sample)
    assert evidence.evidence_sha256 == router_source_screen_digest(evidence)


def test_evidence_outcome_rules_fail_closed() -> None:
    advisory = RouterSourceScreenFinding(
        rule_id="dead-code",
        severity=RouterSourceScreenSeverity.ADVISORY,
        evidence_sha256="c" * 64,
    )
    with pytest.raises(ValueError):
        build_router_source_screen_evidence(
            agent_artifact_sha256="a" * 64,
            screened_image_sha256="b" * 64,
            analyzer_version="router-source-v1",
            policy_version=1,
            outcome=RouterSourceScreenOutcome.PASS,
            findings=(advisory,),
        )
    deny = RouterSourceScreenFinding(
        rule_id="novel-token-emitted",
        severity=RouterSourceScreenSeverity.DENY,
        evidence_sha256="c" * 64,
    )
    with pytest.raises(ValueError, match="quarantine"):
        build_router_source_screen_evidence(
            agent_artifact_sha256="a" * 64,
            screened_image_sha256="b" * 64,
            analyzer_version="router-source-v1",
            policy_version=1,
            outcome=RouterSourceScreenOutcome.QUARANTINE,
            findings=(deny,),
        )


def test_signing_message_binds_the_evidence_digest() -> None:
    evidence = _evidence(_generalising())
    message = router_source_screen_signing_message(
        screener_hotkey=_HOTKEY, evidence=evidence
    )
    assert evidence.evidence_sha256.encode() in message
    with pytest.raises(ValueError):
        router_source_screen_signing_message(
            screener_hotkey="not-an-ss58", evidence=evidence
        )


def test_sample_rejects_incoherent_counts() -> None:
    with pytest.raises(ValueError):
        RouterGeneralizationSample(
            public_score_bps=7000,
            heldout_score_bps=6800,
            heldout_cases=2,
            distinct_heldout_routes=9,
        )
    with pytest.raises(ValueError):
        RouterGeneralizationSample(
            public_score_bps=7000,
            heldout_score_bps=6800,
            heldout_cases=10,
            distinct_heldout_routes=3,
            canary_tasks_total=1,
            canary_tasks_failed=4,
        )


# --- screen_router_submission: the opt-in ("yes-and") entry point -------------


def test_screen_router_submission_no_project_is_benign_infrastructure() -> None:
    # A submission that advertised no router project (sample is None): a benign
    # INFRASTRUCTURE outcome with no findings — never a DENY, never a reward.
    # This is the "scored exactly as before; router is purely additive" contract.
    evidence = screen_router_submission(
        agent_artifact_sha256="a" * 64,
        screened_image_sha256="b" * 64,
        analyzer_version="router-source-v1",
        policy_version=1,
        sample=None,
    )
    assert evidence.outcome is RouterSourceScreenOutcome.INFRASTRUCTURE
    assert evidence.findings == ()
    assert evidence.weight_eligible is False
    # Self-consistent, content-addressed, and signable like any other evidence.
    assert evidence.evidence_sha256 == router_source_screen_digest(evidence)
    router_source_screen_signing_message(screener_hotkey=_HOTKEY, evidence=evidence)


def test_screen_router_submission_included_matches_evaluate() -> None:
    # An included submission is graded exactly as the direct evaluate + build
    # path would (this is the one non-test caller wiring the two together).
    sample = _generalising()
    evidence = screen_router_submission(
        agent_artifact_sha256="a" * 64,
        screened_image_sha256="b" * 64,
        analyzer_version="router-source-v1",
        policy_version=1,
        sample=sample,
    )
    assert evidence.outcome is RouterSourceScreenOutcome.PASS
    assert evidence == _evidence(_generalising())


def test_screen_router_submission_included_can_deny() -> None:
    # A bench-tuned public-only router is still denied through the entry point;
    # "included" does not mean "trusted", only "there is something to grade".
    bench_tuned = RouterGeneralizationSample(
        public_score_bps=9000,
        heldout_score_bps=1000,
        heldout_cases=20,
        distinct_heldout_routes=6,
    )
    evidence = screen_router_submission(
        agent_artifact_sha256="a" * 64,
        screened_image_sha256="b" * 64,
        analyzer_version="router-source-v1",
        policy_version=1,
        sample=bench_tuned,
    )
    assert evidence.outcome is RouterSourceScreenOutcome.DENY
