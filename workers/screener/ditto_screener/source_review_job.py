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
import tempfile
from pathlib import Path
from uuid import UUID

import httpx

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
    return LayeredSourceReviewAgent(
        l1=l1,
        l2=l2,
        mode=os.environ.get("SCREENER_L2_REVIEW_MODE", "off"),
        concern_hold_count=int(
            os.environ.get("SCREENER_REVIEW_CONCERN_HOLD_COUNT", "3")
        ),
        clear_min_notes=int(os.environ.get("SCREENER_REVIEW_CLEAR_MIN_NOTES", "3")),
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
    key_file = _required("SCREENER_SOURCE_REVIEW_API_KEY_FILE")
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
