"""One-shot, credential-minimal Targon source-review worker.

The Rental reads source but never executes it. It receives one attempt-bound
Platform capability and a short-lived GCP bootstrap token used only to
materialize the source-review provider key into a mode-0600 file.
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
from ditto_screener.source_review import OpenRouterSourceReviewAgent
from ditto_screening_protocol import SourceReviewObservationPayload

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


async def _amain() -> int:
    platform = _required("DITTO_PLATFORM_URL").rstrip("/")
    if not platform.startswith("https://"):
        raise ValueError("invalid Platform URL")
    review_id = UUID(_required("DITTO_SOURCE_REVIEW_ID"))
    UUID(_required("DITTO_SOURCE_REVIEW_ATTEMPT_ID"))
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
        os.environ.get("SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS", "1800")
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
            reviewer = OpenRouterSourceReviewAgent(
                api_key_file=key_file,
                model=os.environ.get(
                    "SCREENER_SOURCE_REVIEW_MODEL", "openai/gpt-5.6-luna"
                ),
                base_url=os.environ.get(
                    "SCREENER_SOURCE_REVIEW_BASE_URL",
                    "https://openrouter.ai/api/v1",
                ),
                timeout_seconds=timeout_seconds,
                max_steps=int(
                    os.environ.get("SCREENER_SOURCE_REVIEW_MAX_STEPS", "200")
                ),
                max_read_bytes=int(
                    os.environ.get("SCREENER_SOURCE_REVIEW_MAX_READ_BYTES", "8000000")
                ),
                reasoning_effort=os.environ.get(
                    "SCREENER_SOURCE_REVIEW_REASONING_EFFORT", "high"
                ),
                static_preflight_v2_mode=os.environ.get(
                    "SCREENER_STATIC_PREFLIGHT_V2_MODE", "off"
                ),
            )
            observation = await reviewer.review(
                archive_path,
                artifact_sha256=expected_sha256,
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
