"""One-shot, credential-minimal Targon/Cloud Run source-review worker.

The rental reads source but never executes it. L1 then L2/L3 run in this
process using the in-process analyzer (no GCE Docker). It receives one
attempt-bound Platform capability and a short-lived GCP bootstrap token used
only to materialize the source-review provider key into a mode-0600 file.

Point ``SCREENER_SOURCE_REVIEW_BASE_URL`` at a preview fault proxy
(``ditto.preview`` / PR 1067) for local injected-OpenRouter tests.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx

from ditto_screener.adjudicator import SourceReviewAdjudicator
from ditto_screener.enrollment import _materialize_source_review_secret
from ditto_screener.l2_review import (
    InProcessAnalyzerHarness,
    KimiSolSourceReviewAgent,
    L2AuditJournal,
    LayeredSourceReviewAgent,
)
from ditto_screener.source_review import OpenRouterSourceReviewAgent
from ditto_screening_protocol import (
    SourceReviewAdjudication,
    SourceReviewNote,
    SourceReviewObservationPayload,
)

_MAX_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_PROVIDER_KEY_BYTES = 16 * 1024


def _stage_source_review_secret(key_file: str) -> str:
    """Copy group-readable secret mounts into the job's private tmpfs."""
    source = Path(key_file)
    source_stat = source.stat()
    if not stat.S_ISREG(source_stat.st_mode):
        raise OSError("source review API key file is not a regular file")
    if not source_stat.st_mode & 0o077:
        return key_file
    if source_stat.st_size > _MAX_PROVIDER_KEY_BYTES:
        raise OSError("source review API key file is too large")

    credential_path = os.environ.get("SCREENER_NODE_CREDENTIAL_FILE")
    if not credential_path:
        raise OSError("SCREENER_NODE_CREDENTIAL_FILE is required for source review")
    target = Path(credential_path).with_name("source-review-api-key.staged")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(source, read_flags), "rb") as handle:
        value = handle.read(_MAX_PROVIDER_KEY_BYTES + 1)
    if len(value) > _MAX_PROVIDER_KEY_BYTES:
        raise OSError("source review API key file is too large")

    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        with os.fdopen(os.open(target, write_flags, 0o600), "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    os.environ["SCREENER_SOURCE_REVIEW_API_KEY_FILE"] = str(target)
    return str(target)


async def _notify_provider_event(
    client: httpx.AsyncClient,
    *,
    platform: str,
    headers: dict[str, str],
    review_id: UUID,
    status: int,
    started_at: datetime,
) -> None:
    """Best-effort relay signal; an older rolling relay must not burn the review."""
    payload = {
        "review_id": str(review_id),
        "status": status,
        "started_at": started_at.isoformat(),
    }
    for _attempt in range(3):
        try:
            response = await client.post(
                f"{platform}/api/v1/inference/source-review/provider-event",
                headers=headers,
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
            return
        except httpx.HTTPError:
            await asyncio.sleep(0.25)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"missing {name}")
    return value


async def _download_verified(
    client: httpx.AsyncClient, url: str, expected_sha256: str
) -> str:
    descriptor, path = tempfile.mkstemp(prefix="ditto-source-review-", suffix=".tgz")
    os.fchmod(descriptor, 0o600)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > _MAX_SOURCE_BYTES:
                        raise ValueError("source archive exceeded its bound")
                    digest.update(chunk)
                    handle.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("source archive digest mismatch")
        return path
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def _parse_bool(name: str, default: str) -> bool:
    value = os.environ.get(name, default).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean {name}")


def _parse_csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _build_reviewer(
    *, key_file: str, timeout_seconds: float
) -> LayeredSourceReviewAgent:
    """L1 then L2/L3 in this rental. No GCE Docker worker is required."""
    credential = os.environ.get(
        "SCREENER_NODE_CREDENTIAL_FILE", "/tmp/ditto-source-review/node.json"
    )
    workdir = Path(credential).parent
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    l1 = OpenRouterSourceReviewAgent(
        api_key_file=key_file,
        model=os.environ.get("SCREENER_SOURCE_REVIEW_MODEL", "openai/gpt-5.6-luna"),
        base_url=os.environ.get(
            "SCREENER_SOURCE_REVIEW_BASE_URL",
            "https://openrouter.ai/api/v1",
        ),
        timeout_seconds=timeout_seconds,
        max_steps=int(os.environ.get("SCREENER_SOURCE_REVIEW_MAX_STEPS", "200")),
        max_read_bytes=int(
            os.environ.get("SCREENER_SOURCE_REVIEW_MAX_READ_BYTES", "8000000")
        ),
        max_completion_tokens=int(
            os.environ.get("SCREENER_SOURCE_REVIEW_MAX_COMPLETION_TOKENS", "8000")
        ),
        reasoning_effort=os.environ.get(
            "SCREENER_SOURCE_REVIEW_REASONING_EFFORT", "high"
        ),
        static_preflight_v2_mode=os.environ.get(
            "SCREENER_STATIC_PREFLIGHT_V2_MODE", "off"
        ),
        concern_hold_count=int(
            os.environ.get("SCREENER_REVIEW_CONCERN_HOLD_COUNT", "3")
        ),
        clear_min_notes=int(os.environ.get("SCREENER_REVIEW_CLEAR_MIN_NOTES", "3")),
    )
    l2 = KimiSolSourceReviewAgent(
        api_key_file=key_file,
        base_url=os.environ.get(
            "SCREENER_SOURCE_REVIEW_BASE_URL",
            "https://openrouter.ai/api/v1",
        ),
        harness=InProcessAnalyzerHarness(),
        cache_dir=str(workdir / "l2-cache"),
        audit_journal=L2AuditJournal(
            str(workdir / "l2-audit.jsonl"),
            retention_days=int(
                os.environ.get("SCREENER_L2_AUDIT_RETENTION_DAYS", "30")
            ),
        ),
        timeout_seconds=float(os.environ.get("SCREENER_L2_TIMEOUT_SECONDS", "1200")),
        max_steps=int(os.environ.get("SCREENER_L2_MAX_STEPS", "32")),
        max_input_tokens=int(os.environ.get("SCREENER_L2_MAX_INPUT_TOKENS", "425000")),
        max_output_tokens=int(os.environ.get("SCREENER_L2_MAX_OUTPUT_TOKENS", "20000")),
        max_completion_tokens=int(
            os.environ.get("SCREENER_L2_MAX_COMPLETION_TOKENS", "2400")
        ),
        max_cost_usd=float(os.environ.get("SCREENER_L2_MAX_COST_USD", "6.00")),
        analyst_reasoning_effort=os.environ.get(
            "SCREENER_L2_ANALYST_REASONING_EFFORT", "model_default"
        ),
        critic_reasoning_effort=os.environ.get(
            "SCREENER_L2_CRITIC_REASONING_EFFORT", "medium"
        ),
        cache_ttl_seconds=float(
            os.environ.get("SCREENER_L2_CACHE_TTL_SECONDS", str(7 * 86_400))
        ),
        model=os.environ.get("SCREENER_L2_REVIEW_MODEL", "moonshotai/kimi-k3"),
        fallback_models=_parse_csv(
            "SCREENER_L2_FALLBACK_MODELS", "z-ai/glm-5.2,openai/gpt-5.6-sol"
        ),
        l3_enabled=_parse_bool("SCREENER_L3_REVIEW_ENABLED", "true"),
        critic_model=os.environ.get("SCREENER_L3_REVIEW_MODEL", "openai/gpt-5.6-sol"),
        critic_provider=os.environ.get("SCREENER_L3_REVIEW_PROVIDER", "openrouter"),
    )
    adjudicator_mode = os.environ.get("SCREENER_ADJUDICATOR_MODE", "off")
    adjudicator = (
        None
        if adjudicator_mode == "off"
        else SourceReviewAdjudicator(
            api_key_file=key_file,
            base_url=os.environ.get(
                "SCREENER_SOURCE_REVIEW_BASE_URL",
                "https://openrouter.ai/api/v1",
            ),
            model=os.environ.get("SCREENER_ADJUDICATOR_MODEL", "z-ai/glm-5.3-flash"),
            timeout_seconds=float(
                os.environ.get("SCREENER_ADJUDICATOR_TIMEOUT_SECONDS", "600")
            ),
            max_steps=int(os.environ.get("SCREENER_ADJUDICATOR_MAX_STEPS", "24")),
        )
    )
    return LayeredSourceReviewAgent(
        l1=l1,
        l2=l2,
        mode=os.environ.get("SCREENER_L2_REVIEW_MODE", "off"),
        concern_hold_count=int(
            os.environ.get("SCREENER_REVIEW_CONCERN_HOLD_COUNT", "3")
        ),
        clear_min_notes=int(os.environ.get("SCREENER_REVIEW_CLEAR_MIN_NOTES", "3")),
        adjudicator=adjudicator,
    )


async def _amain() -> int:
    platform = _required("DITTO_PLATFORM_URL").rstrip("/")
    if not platform.startswith("https://"):
        raise ValueError("invalid Platform URL")
    review_id = UUID(_required("DITTO_SOURCE_REVIEW_ID"))
    attempt_id = UUID(_required("DITTO_SOURCE_REVIEW_ATTEMPT_ID"))
    expected_sha256 = _required("DITTO_SOURCE_REVIEW_ARTIFACT_SHA256")
    if len(expected_sha256) != 64:
        raise ValueError("invalid source digest")
    token = _required("DITTO_SOURCE_REVIEW_JOB_TOKEN")
    os.environ.pop("DITTO_SOURCE_REVIEW_JOB_TOKEN", None)
    os.environ.setdefault(
        "SCREENER_NODE_CREDENTIAL_FILE", "/tmp/ditto-source-review/node.json"
    )
    await _materialize_source_review_secret()
    key_file = _stage_source_review_secret(
        _required("SCREENER_SOURCE_REVIEW_API_KEY_FILE")
    )
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{platform}/api/v1/screener/submission-source-reviews/{review_id}"
    archive_path: str | None = None
    timeout_seconds = float(
        os.environ.get("SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS", "3600")
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(max(300.0, timeout_seconds + 60), connect=30.0)
        ) as client:
            source_response = await client.get(f"{base}/source", headers=headers)
            source_response.raise_for_status()
            source = source_response.json()
            if source.get("artifact_sha256") != expected_sha256:
                raise ValueError("Platform source binding changed")
            source_url = base64.b64decode(
                str(source["source_url_b64"]), validate=True
            ).decode()
            archive_path = await _download_verified(client, source_url, expected_sha256)
            reviewer = _build_reviewer(
                key_file=key_file, timeout_seconds=timeout_seconds
            )
            provider_started_at = datetime.now(UTC)
            observation = await reviewer.review(
                archive_path,
                artifact_sha256=expected_sha256,
                attempt_id=attempt_id,
                deadline=asyncio.get_running_loop().time() + timeout_seconds,
            )
            payload = SourceReviewObservationPayload(
                ok=observation.ok,
                risk_level=observation.risk_level,
                finding_digest=observation.finding_digest,
                categories=list(observation.categories),
                error_code=observation.error_code,
                finding=observation.finding,
                failure_disposition=observation.failure_disposition,
                clearance_certified=observation.clearance_certified,
                review_audit=observation.review_audit,
                notes=[
                    SourceReviewNote.model_validate(note)
                    for note in observation.notes[:48]
                ],
                adjudication=(
                    SourceReviewAdjudication.model_validate(observation.adjudication)
                    if observation.adjudication is not None
                    else None
                ),
            )
            provider_status = (
                429
                if observation.error_code == "source-review-http-429"
                else (
                    200
                    if observation.ok
                    or observation.failure_disposition != "retryable_infra"
                    else None
                )
            )
            if provider_status is not None:
                await _notify_provider_event(
                    client,
                    platform=platform,
                    headers=headers,
                    review_id=review_id,
                    status=provider_status,
                    started_at=provider_started_at,
                )
            complete = await client.post(
                f"{base}/complete",
                headers=headers,
                json={"observation": payload.model_dump(mode="json")},
            )
            complete.raise_for_status()
        # Rentals are persistent; give the controller time to observe the
        # committed result and delete this workload before Targon restarts it.
        await asyncio.sleep(600)
        return 0
    finally:
        if archive_path is not None:
            Path(archive_path).unlink(missing_ok=True)
        Path(key_file).unlink(missing_ok=True)


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
