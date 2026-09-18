"""Optional idle-worker consumer; the Platform kill switch is authoritative."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ditto_screener.conversation import (
    AssessmentFailure,
    AstraExaminer,
    Limits,
    MemoryHarness,
    evaluate,
)
from ditto_screener.conversation_runtime import ConversationRuntime
from ditto_screening_protocol.conversation import (
    JUDGE_MODEL,
    ConversationLaunch,
    ConversationReport,
)
from ditto_screening_protocol.conversation_story import story_digest

if TYPE_CHECKING:
    from ditto_screener.config import ScreenerConfig
    from ditto_screener.platform import PlatformClient

logger = logging.getLogger(__name__)


def private_write(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


async def consume(config: ScreenerConfig, platform: PlatformClient) -> bool:
    """Each failed claim identity remains terminal; future polls claim fresh work."""
    provider_file = os.environ.get("SCREENER_CONVERSATION_OPENROUTER_KEY_FILE")
    spool_dir = os.environ.get("SCREENER_CONVERSATION_SPOOL_DIR")
    if not (provider_file and spool_dir):
        return False
    if not config.require_rootless_docker:
        return False
    try:
        key_path = Path(provider_file)
        if key_path.stat().st_mode & 0o077:
            raise ValueError("provider credential must be owner-only")
        provider_key = key_path.read_text().strip()
        if not provider_key:
            raise ValueError("empty credential")
        spool = Path(spool_dir)
        spool.mkdir(mode=0o700, parents=True, exist_ok=True)
        offer = await platform.conversation_request("/claim")
        if offer is None:
            return False
        claim = ConversationLaunch.model_validate(offer)
        private_write(
            spool / f"{claim.assessment_id}.claim.json", claim.model_dump_json()
        )
    except Exception as exc:
        logger.warning(
            "conversation preflight/claim unavailable: %s", type(exc).__name__
        )
        return False
    logger.info(
        "conversation shadow started assessment_id=%s agent_id=%s",
        claim.assessment_id,
        claim.agent_id,
    )
    runtime = ConversationRuntime(config, claim, provider_key)
    report = None
    reason = "sandbox_unavailable"
    try:
        # Leave five minutes of the immutable lease for setup failure reporting,
        # provider settlement and cleanup. No judge request after its deadline.
        async with asyncio.timeout(600):
            url = await runtime.start()
        remaining = (claim.expires_at - datetime.now(UTC)).total_seconds() - 180
        if remaining <= 0:
            raise AssessmentFailure("conversation_lease_expired")
        limits = Limits(total_seconds=min(3600, remaining))
        async with (
            httpx.AsyncClient(
                timeout=120, trust_env=False, follow_redirects=False
            ) as harness_client,
            httpx.AsyncClient(
                timeout=120,
                trust_env=False,
                follow_redirects=False,
                headers={"Authorization": "Bearer " + provider_key},
            ) as judge_client,
        ):
            report = await evaluate(
                assessment_id=claim.assessment_id,
                agent_id=claim.agent_id,
                artifact_sha256=claim.artifact_sha256,
                screened_image_sha256=claim.screened_image_sha256,
                bench_version=claim.bench_version,
                seed=claim.seed,
                harness=MemoryHarness(harness_client, url, limits),
                examiner=AstraExaminer(judge_client, limits, provider="openrouter"),
            )
    except AssessmentFailure as exc:
        reason = str(exc)
    except Exception as exc:
        logger.warning(
            "conversation execution failed assessment_id=%s type=%s",
            claim.assessment_id,
            type(exc).__name__,
        )
    finally:
        usage = await runtime.stop()
    if report is None:
        report = ConversationReport(
            **{
                k: getattr(claim, k)
                for k in (
                    "assessment_id",
                    "agent_id",
                    "artifact_sha256",
                    "screened_image_sha256",
                    "bench_version",
                )
            },
            story_sha256=story_digest(claim.seed),
            provider_model=JUDGE_MODEL,
            status="incomplete",
            error_code=reason,
            exchanges=[],
            judge_requests=0,
            input_tokens=0,
            output_tokens=0,
            reserved_microusd=25_000_000,
            spent_microusd=0,
        )
    document = report.model_dump(mode="json")
    document["harness_usage"] = usage.model_dump(mode="json") if usage else None
    if (
        report.status == "completed"
        and (usage is None or usage.unmetered or usage.failed)
    ) or (usage is not None and usage.failed):
        document.update(
            status="incomplete", grades=None, error_code="harness_inference_incomplete"
        )
    report = ConversationReport.model_validate(document)
    private_write(
        spool / f"{claim.assessment_id}.report.json", report.model_dump_json()
    )
    try:
        await platform.conversation_request(
            f"/{claim.assessment_id}/result",
            {
                "lease_token": str(claim.lease_token),
                "report": report.model_dump(mode="json"),
            },
        )
    except Exception as exc:
        logger.warning(
            "conversation delivery failed; saved locally assessment_id=%s type=%s",
            claim.assessment_id,
            type(exc).__name__,
        )
    logger.info(
        "conversation shadow ended assessment_id=%s status=%s",
        claim.assessment_id,
        report.status,
    )
    return True
