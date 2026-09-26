"""Idle-node L2 audit with a separate lease and no screening verdict path."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto_screener.config import ScreenerConfig
from ditto_screener.gate import BuildGate, LeaseDeadline
from ditto_screener.heartbeat import ScreenerProgressStage
from ditto_screener.platform import PlatformClient
from ditto_screener.policy import ReviewJournal, load_policy_engine
from ditto_screener.review_settings import EffectiveReviewSettings
from ditto_screening_protocol import ScoredRuntimeEvidenceLease

logger = logging.getLogger(__name__)


class L2CanaryClaim(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    canary_id: UUID
    agent_id: UUID
    source_attempt_id: UUID
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    bench_version: int
    policy_version: int
    run_mode: Literal["source_only", "full_runtime"] = "source_only"
    miner_hotkey: str
    lease_token: str
    lease_expires_at: datetime
    download_url: str
    scored_runtime_evidence: ScoredRuntimeEvidenceLease


def _identity_report(
    claim: L2CanaryClaim, settings: EffectiveReviewSettings
) -> dict[str, Any]:
    return {
        "kind": "l2_report_canary_v1",
        "authority": "none",
        "review_mode": (
            "enforce_preview" if claim.run_mode == "full_runtime" else "shadow"
        ),
        "canary_id": str(claim.canary_id),
        "agent_id": str(claim.agent_id),
        "source_attempt_id": str(claim.source_attempt_id),
        "artifact_sha256": claim.artifact_sha256,
        "policy_version": claim.policy_version,
        "run_mode": claim.run_mode,
        "challenge_status": "not_run",
        "challenge_evidence_codes": [],
        "settings_revision": settings.revision,
        "settings_checksum": settings.checksum,
        "scored_runtime_evidence": claim.scored_runtime_evidence.model_dump(
            mode="json"
        ),
    }


def _report(
    *,
    claim: L2CanaryClaim,
    decision: Any,
    l2_result: Any | None,
    settings: EffectiveReviewSettings,
    l1_observation: Any | None = None,
) -> dict[str, Any]:
    report = _identity_report(claim, settings)
    if l1_observation is not None:
        report["l1"] = {
            "ok": l1_observation.ok,
            "risk_level": l1_observation.risk_level,
            "categories": list(l1_observation.categories),
            "clearance_certified": l1_observation.clearance_certified,
            "finding_digest": l1_observation.finding_digest,
            "finding": l1_observation.finding,
        }
    report["decision_outcome"] = str(decision.outcome)
    codes = [item.code for item in decision.evidence]
    report["decision_evidence_codes"] = codes
    challenge_codes = [
        code for code in codes if code.startswith(("challenge-", "behavioral-oracle-"))
    ]
    report["challenge_evidence_codes"] = challenge_codes
    if claim.run_mode == "source_only" or not challenge_codes:
        report["challenge_status"] = "not_run"
    elif any(
        code
        not in {
            "challenge-observed",
            "challenge-model-call-missing",
            "challenge-gateway-token-missing",
            "challenge-shape-anomaly",
            "behavioral-oracle-passed",
            "behavioral-oracle-wrong-answer",
            "behavioral-oracle-implausibly-fast",
        }
        for code in challenge_codes
    ):
        report["challenge_status"] = "inconclusive"
    else:
        report["challenge_status"] = "completed"
    if l2_result is None:
        report["l2"] = None
        return report
    observation = l2_result.observation
    report["l2"] = {
        "ok": observation.ok,
        "risk_level": observation.risk_level,
        "finding_digest": observation.finding_digest,
        "categories": list(observation.categories),
        "error_code": observation.error_code,
        "failure_disposition": observation.failure_disposition,
        "clearance_certified": observation.clearance_certified,
        "finding": observation.finding,
        "review_audit": observation.review_audit,
        "notes": list(observation.notes),
        "usage": asdict(l2_result.usage),
        "cache_hit": l2_result.cache_hit,
        "response_models": list(l2_result.response_models),
        "response_providers": list(l2_result.response_providers),
        "resolution_basis": l2_result.resolution_basis,
        "clearance_path": l2_result.clearance_path,
        "critic_disposition": l2_result.critic_disposition,
        "dossier_complete": l2_result.dossier_complete,
        "direct_clear_graph_complete": l2_result.direct_clear_graph_complete,
        "failure_subcode": l2_result.failure_subcode,
        "inconclusive_model_audit": observation.inconclusive_model_audit,
    }
    return report


async def consume(
    *,
    config: ScreenerConfig,
    platform: PlatformClient,
    primary_gate: BuildGate,
    settings: EffectiveReviewSettings,
    instance_id: str,
    on_claim: Callable[[L2CanaryClaim], None] | None = None,
    progress: Callable[[ScreenerProgressStage], None] | None = None,
) -> bool:
    """Claim one independent job only after the primary queue is empty."""
    try:
        payload = await platform.claim_l2_report_canary(
            instance_id=instance_id,
            settings_revision=settings.revision,
            settings_checksum=settings.checksum,
        )
    except Exception:
        # A rolling Platform deploy or audit-lane outage cannot stall the
        # authoritative primary queue.
        logger.warning("report-only L2 claim unavailable", exc_info=True)
        return False
    if payload is None:
        return False
    claim = L2CanaryClaim.model_validate_json(json.dumps(payload))
    logger.info(
        "report-only L2 canary claimed canary_id=%s agent_id=%s lease_expires_at=%s",
        claim.canary_id,
        claim.agent_id,
        claim.lease_expires_at.isoformat(),
    )
    if on_claim is not None:
        on_claim(claim)
    if claim.policy_version != 13 or claim.bench_version != 13:
        logger.error("report-only L2 claim is not v13: %s", claim.canary_id)
        await platform.complete_l2_report_canary(
            claim.canary_id,
            lease_token=claim.lease_token,
            lease_expires_at=claim.lease_expires_at,
            status="incomplete",
            report=_identity_report(claim, settings),
            error_code="unsupported-policy-version",
        )
        return True
    if (
        claim.scored_runtime_evidence.attempt_id != claim.source_attempt_id
        or claim.scored_runtime_evidence.artifact_sha256 != claim.artifact_sha256
        or claim.scored_runtime_evidence.policy_version != claim.policy_version
        or claim.scored_runtime_evidence.bench_version != claim.bench_version
    ):
        logger.error("report-only L2 claim has mismatched runtime packet")
        await platform.complete_l2_report_canary(
            claim.canary_id,
            lease_token=claim.lease_token,
            lease_expires_at=claim.lease_expires_at,
            status="incomplete",
            report=_identity_report(claim, settings),
            error_code="runtime-packet-mismatch",
        )
        return True
    remaining = (claim.lease_expires_at - datetime.now(UTC)).total_seconds()
    if remaining <= 60:
        await platform.complete_l2_report_canary(
            claim.canary_id,
            lease_token=claim.lease_token,
            lease_expires_at=claim.lease_expires_at,
            status="incomplete",
            report=_identity_report(claim, settings),
            error_code="lease-budget-exhausted",
        )
        return True
    deadline = LeaseDeadline(asyncio.get_running_loop().time() + remaining - 30)
    effective = settings.apply_to(config)
    # Keep caches and append-only local audit files out of the authoritative
    # attempt's namespace. A replay of the same source attempt must perform a
    # fresh L2 run unless this canary's own isolated cache supplies the result.
    canary_root = (
        Path(config.l2_cache_dir).parent / "report-canaries" / str(claim.canary_id)
    )
    canary_config = replace(
        effective,
        l2_review_mode=("enforce" if claim.run_mode == "full_runtime" else "shadow"),
        l2_always_escalate=True,
        require_signed_runtime_lease=True,
        # L1 preparation may outlast the ordinary five-minute packet window.
        # The job's 45-minute lease still bounds this exact signed packet.
        signed_runtime_lease_max_age_seconds=45 * 60,
        l2_cache_dir=str(canary_root / "cache"),
        l2_audit_journal_file=str(canary_root / "l2-audit.jsonl"),
        static_preflight_audit_file=str(canary_root / "preflight-audit.jsonl"),
        review_journal_file=str(canary_root / "review.jsonl"),
    )
    gate = BuildGate(
        canary_config,
        primary_gate._client,
        policy=load_policy_engine(
            canary_config.policy_manifest_file,
            l2_mode=canary_config.l2_review_mode,
            manifest_profile=settings.settings.policy_manifest_profile,
            rotation_id=settings.settings.policy_manifest_rotation_id,
        ),
        journal=ReviewJournal(canary_config.review_journal_file),
        capture_enforce_result=claim.run_mode == "full_runtime",
    )
    try:
        decision = await gate.screen(
            agent_id=claim.agent_id,
            attempt_id=claim.source_attempt_id,
            bench_version=13,
            miner_hotkey=claim.miner_hotkey,
            sha256=claim.artifact_sha256,
            download_url=claim.download_url,
            deadline=deadline,
            policy_version=13,
            scored_runtime_evidence=claim.scored_runtime_evidence,
            progress=progress,
            execution_namespace=(
                claim.canary_id if claim.run_mode == "full_runtime" else None
            ),
            policy_only=claim.run_mode == "source_only",
        )
        l2_result = gate.pop_shadow_review(claim.source_attempt_id)
        l1_observation = (
            gate.pop_preview_l1_review(claim.source_attempt_id)
            if claim.run_mode == "full_runtime"
            else None
        )
        report = _report(
            claim=claim,
            decision=decision,
            l2_result=l2_result,
            settings=settings,
            l1_observation=l1_observation,
        )
        status = "succeeded" if l2_result is not None else "incomplete"
        error_code = None if l2_result is not None else "l2-not-run"
    except Exception:
        logger.exception("report-only L2 canary failed canary_id=%s", claim.canary_id)
        report = _identity_report(claim, settings)
        status = "incomplete"
        error_code = "worker-exception"
    await platform.complete_l2_report_canary(
        claim.canary_id,
        lease_token=claim.lease_token,
        lease_expires_at=claim.lease_expires_at,
        status=status,
        report=report,
        error_code=error_code,
    )
    return True
