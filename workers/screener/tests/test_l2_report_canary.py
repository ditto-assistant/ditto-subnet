"""The idle L2 canary never enters the authoritative verdict client path."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ditto_screener import l2_report_canary
from ditto_screener.l2_review import L2RunResult, L2Usage
from ditto_screener.policy import (
    PolicyEvidence,
    ScreeningDecision,
    ScreeningOutcome,
    SourceReviewObservation,
    core_decision,
)
from ditto_screener.review_settings import bootstrap_review_settings
from ditto_screening_protocol import ScoredRuntimeEvidenceLease


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_mode", "lease_minutes"),
    [("source_only", 70), ("full_runtime", 120)],
)
async def test_report_only_l2_previews_full_runtime_enforcement_without_verdict(
    make_config, monkeypatch: pytest.MonkeyPatch, run_mode: str, lease_minutes: int
) -> None:
    config = make_config()
    settings = bootstrap_review_settings(config)
    agent_id, attempt_id, canary_id = uuid4(), uuid4(), uuid4()
    revision = "a" * 40
    keys = ("SAFE_KEY",)
    digest = hashlib.sha256(
        ("scored-runtime-env-v1\n13\n" + revision + "\n" + "\n".join(keys)).encode()
    ).hexdigest()
    packet = ScoredRuntimeEvidenceLease(
        attempt_id=attempt_id,
        artifact_sha256="b" * 64,
        policy_version=13,
        bench_version=13,
        scorer_source_revision=revision,
        release_descriptor_digest="sha256:" + "c" * 64,
        scorer_image_digest="sha256:" + "d" * 64,
        scorer_env_sha256=digest,
        injected_keys=keys,
        validator_count=3,
        observed_at=int(datetime.now(UTC).timestamp()),
    )
    claim = {
        "canary_id": str(canary_id),
        "agent_id": str(agent_id),
        "source_attempt_id": str(attempt_id),
        "artifact_sha256": "b" * 64,
        "bench_version": 13,
        "policy_version": 13,
        "run_mode": run_mode,
        "miner_hotkey": "miner",
        "lease_token": "token",
        "lease_expires_at": (
            datetime.now(UTC) + timedelta(minutes=lease_minutes)
        ).isoformat(),
        "download_url": "https://example.test/source",
        "scored_runtime_evidence": packet.model_dump(mode="json"),
    }
    completions = []
    claimed = []
    progress_stages = []
    loaded_modes = []

    class Platform:
        async def claim_l2_report_canary(self, **kwargs):
            assert kwargs["settings_revision"] == settings.revision
            return claim

        async def complete_l2_report_canary(self, *args, **kwargs):
            completions.append((args, kwargs))

        async def submit_result(self, *_args, **_kwargs):
            raise AssertionError("report-only lane posted a screening verdict")

    class Gate:
        def __init__(self, canary_config, *_args, **kwargs):
            assert canary_config.l2_review_mode == (
                "enforce" if run_mode == "full_runtime" else "shadow"
            )
            assert kwargs["capture_enforce_result"] is (run_mode == "full_runtime")
            assert canary_config.l2_always_escalate
            assert canary_config.require_signed_runtime_lease
            assert canary_config.signed_runtime_lease_max_age_seconds == math.ceil(
                datetime.fromisoformat(claim["lease_expires_at"]).timestamp()
                - packet.observed_at
            )
            assert str(canary_id) in canary_config.l2_cache_dir
            assert canary_config.l2_cache_dir != config.l2_cache_dir
            assert canary_config.l2_audit_journal_file != config.l2_audit_journal_file
            assert canary_config.review_journal_file != config.review_journal_file

        async def screen(self, **kwargs):
            assert kwargs["policy_only"] is (run_mode == "source_only")
            assert kwargs["execution_namespace"] == (
                canary_id if run_mode == "full_runtime" else None
            )
            assert kwargs.get("publish_image") is None
            assert kwargs.get("record_runtime_verification") is None
            assert kwargs["scored_runtime_evidence"] == packet
            kwargs["progress"]("source_review_0")
            if run_mode == "full_runtime":
                return ScreeningDecision(
                    outcome=ScreeningOutcome.PASS,
                    detail="isolated runtime passed",
                    manifest_digest="a" * 64,
                    evidence=(
                        PolicyEvidence(
                            "oracle", "behavioral-oracle-passed", "runtime observed"
                        ),
                        PolicyEvidence(
                            "challenge", "challenge-observed", "challenge observed"
                        ),
                    ),
                    policy_version=13,
                )
            return core_decision(
                ScreeningOutcome.INCONCLUSIVE,
                code="source-review-inconclusive",
                summary="audit only",
                detail="audit only",
                policy_version=13,
            )

        def pop_shadow_review(self, _attempt_id):
            return L2RunResult(
                observation=SourceReviewObservation(
                    ok=True,
                    risk_level="low",
                    finding_digest=None,
                    categories=(),
                    clearance_certified=True,
                ),
                analyzed_files=(),
                causal_path=(),
                tools=(),
                usage=L2Usage(estimated_cost_usd=0.05),
                cache_hit=False,
                failure_subcode="no_tool_call_after_corrections",
            )

        def pop_preview_l1_review(self, _attempt_id):
            assert run_mode == "full_runtime"
            return SourceReviewObservation(
                ok=True,
                risk_level="low",
                finding_digest="c" * 64,
                categories=("none",),
                clearance_certified=True,
                finding={"summary": "clean L1", "evidence": []},
            )

    monkeypatch.setattr(l2_report_canary, "BuildGate", Gate)

    def load_policy(*_args, **kwargs):
        loaded_modes.append(kwargs["l2_mode"])
        return object()

    monkeypatch.setattr(l2_report_canary, "load_policy_engine", load_policy)
    consumed = await l2_report_canary.consume(
        config=config,
        platform=Platform(),
        primary_gate=SimpleNamespace(_client=object(), _journal=object()),
        settings=settings,
        instance_id="subnet-screener-1-worker-1",
        on_claim=claimed.append,
        progress=progress_stages.append,
    )
    assert consumed
    assert len(completions) == 1
    assert claimed[0].canary_id == canary_id
    assert progress_stages == ["source_review_0"]
    assert completions[0][1]["status"] == "succeeded"
    report = completions[0][1]["report"]
    assert report["authority"] == "none"
    assert report["review_mode"] == (
        "enforce_preview" if run_mode == "full_runtime" else "shadow"
    )
    assert loaded_modes == ["enforce" if run_mode == "full_runtime" else "shadow"]
    assert report["run_mode"] == run_mode
    assert report["challenge_status"] == (
        "completed" if run_mode == "full_runtime" else "not_run"
    )
    assert report["source_attempt_id"] == str(attempt_id)
    assert report["l2"]["risk_level"] == "low"
    assert report["l2"]["failure_subcode"] == "no_tool_call_after_corrections"
    if run_mode == "full_runtime":
        assert report["l1"]["clearance_certified"] is True
        assert report["l1"]["finding"]["summary"] == "clean L1"
    else:
        assert "l1" not in report


def test_inconclusive_model_audit_is_report_only() -> None:
    audit = {
        "artifact_sha256": "b" * 64,
        "disposition": "inconclusive",
        "invariants": [{"invariant": "i7_model_tool_planning", "disposition": "pass"}],
    }
    claim = SimpleNamespace(
        canary_id=uuid4(),
        agent_id=uuid4(),
        source_attempt_id=uuid4(),
        artifact_sha256="b" * 64,
        policy_version=13,
        run_mode="source_only",
        scored_runtime_evidence=SimpleNamespace(model_dump=lambda **_: {}),
    )
    settings = SimpleNamespace(revision=134, checksum="c" * 64)
    shadow = L2RunResult(
        observation=SourceReviewObservation(
            ok=False,
            risk_level=None,
            finding_digest=None,
            categories=(),
            error_code="l2-model-inconclusive",
            failure_disposition="inconclusive",
            inconclusive_model_audit=audit,
        ),
        analyzed_files=(),
        causal_path=(),
        tools=(),
        usage=L2Usage(),
        cache_hit=False,
    )
    report = l2_report_canary._report(
        claim=claim,
        decision=SimpleNamespace(outcome="inconclusive", evidence=()),
        l2_result=shadow,
        settings=settings,
    )
    assert report["authority"] == "none"
    assert report["l2"]["inconclusive_model_audit"] == audit


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("challenge-inconclusive", "inconclusive"),
        ("challenge-compatibility-timeout", "inconclusive"),
        ("behavioral-oracle-insufficient-round-trips", "inconclusive"),
        ("challenge-observed", "completed"),
        ("source-review-inconclusive", "not_run"),
    ],
)
def test_full_runtime_report_distinguishes_challenge_state(
    code: str, expected: str
) -> None:
    claim = SimpleNamespace(
        canary_id=uuid4(),
        agent_id=uuid4(),
        source_attempt_id=uuid4(),
        artifact_sha256="b" * 64,
        policy_version=13,
        run_mode="full_runtime",
        scored_runtime_evidence=SimpleNamespace(model_dump=lambda **_: {}),
    )
    report = l2_report_canary._report(
        claim=claim,
        decision=core_decision(
            ScreeningOutcome.INCONCLUSIVE,
            code=code,
            summary="held",
            detail="held",
            policy_version=13,
        ),
        l2_result=None,
        settings=SimpleNamespace(revision=134, checksum="c" * 64),
    )
    assert report["challenge_status"] == expected
    assert report["authority"] == "none"
