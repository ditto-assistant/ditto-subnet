"""One-shot, credential-minimal consumer for the fan-out shadow queue."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from uuid import UUID

import httpx

from ditto_screener.enrollment import _materialize_source_review_secret
from ditto_screener.fanout_review import MODEL, review_archive
from ditto_screener.policy import builtin_policy_manifest
from ditto_screener.source_review_job import (
    _download_verified,
    _required,
    _stage_source_review_secret,
)

_SHADOW_ROUTER_URL = "https://router.heyditto.ai/v1"


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if not low <= value <= high:
        raise ValueError(f"{name} is outside its bounded range")
    return value


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    value = float(os.environ.get(name, str(default)))
    if not low < value <= high:
        raise ValueError(f"{name} is outside its bounded range")
    return value


def _shadow_inference_route() -> tuple[str, str]:
    provider = _required("SCREENER_REVIEW_INFERENCE_PROVIDER")
    if provider != "ditto":
        raise ValueError("fanout shadow requires the dedicated Ditto Router")
    base_url = _required("SCREENER_SOURCE_REVIEW_BASE_URL").rstrip("/")
    if base_url != _SHADOW_ROUTER_URL:
        raise ValueError("fanout shadow Router URL changed")
    return provider, base_url


async def _amain() -> int:
    platform = _required("DITTO_PLATFORM_URL").rstrip("/")
    if not platform.startswith("https://"):
        raise ValueError("invalid Platform URL")
    shadow_id = UUID(_required("DITTO_FANOUT_SHADOW_ID"))
    expected_sha256 = _required("DITTO_FANOUT_SHADOW_ARTIFACT_SHA256")
    expected_policy = int(_required("DITTO_FANOUT_SHADOW_POLICY_VERSION"))
    expected_manifest_profile = _required("DITTO_FANOUT_SHADOW_POLICY_MANIFEST_PROFILE")
    expected_manifest_rotation = _required(
        "DITTO_FANOUT_SHADOW_POLICY_MANIFEST_ROTATION_ID"
    )
    expected_manifest_digest = _required("DITTO_FANOUT_SHADOW_POLICY_MANIFEST_DIGEST")
    manifest = builtin_policy_manifest(
        expected_manifest_profile, expected_manifest_rotation
    )
    if manifest.digest != expected_manifest_digest:
        raise ValueError("fanout policy manifest is unavailable in this image")
    token = _required("DITTO_FANOUT_SHADOW_JOB_TOKEN")
    os.environ.pop("DITTO_FANOUT_SHADOW_JOB_TOKEN", None)
    os.environ.setdefault(
        "SCREENER_NODE_CREDENTIAL_FILE", "/tmp/ditto-source-review/node.json"
    )
    await _materialize_source_review_secret()
    key_file = _stage_source_review_secret(
        _required("SCREENER_SOURCE_REVIEW_API_KEY_FILE")
    )
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{platform}/api/v1/screener/fanout-shadow-reviews/{shadow_id}"
    archive_path: str | None = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(960, connect=30)) as client:
            source_response = await client.get(f"{base}/source", headers=headers)
            source_response.raise_for_status()
            source = source_response.json()
            if source.get("artifact_sha256") != expected_sha256:
                raise ValueError("Platform fanout source binding changed")
            if source.get("policy_version") != expected_policy:
                raise ValueError("Platform fanout policy binding changed")
            if (
                source.get("policy_manifest_profile") != expected_manifest_profile
                or source.get("policy_manifest_rotation_id")
                != expected_manifest_rotation
                or source.get("policy_manifest_digest") != expected_manifest_digest
            ):
                raise ValueError("Platform fanout policy manifest binding changed")
            source_url = base64.b64decode(
                str(source["source_url_b64"]), validate=True
            ).decode()
            archive_path = await _download_verified(client, source_url, expected_sha256)
            provider, base_url = _shadow_inference_route()
            model = os.environ.get("SCREENER_FANOUT_SHADOW_MODEL", MODEL)
            if model != MODEL:
                raise ValueError("fanout shadow pricing envelope model changed")
            report = await review_archive(
                Path(archive_path),
                artifact_sha256=expected_sha256,
                api_key_file=key_file,
                model=model,
                base_url=base_url,
                inference_provider=provider,
                partition="hybrid",
                concurrency=_bounded_int("SCREENER_FANOUT_SHADOW_CONCURRENCY", 2, 1, 4),
                max_steps=_bounded_int("SCREENER_FANOUT_SHADOW_MAX_STEPS", 4, 1, 8),
                max_groups=_bounded_int("SCREENER_FANOUT_SHADOW_MAX_GROUPS", 4, 1, 8),
                max_requests=_bounded_int(
                    "SCREENER_FANOUT_SHADOW_MAX_REQUESTS", 40, 1, 64
                ),
                max_total_tokens=_bounded_int(
                    "SCREENER_FANOUT_SHADOW_MAX_TOTAL_TOKENS",
                    1_500_000,
                    10_000,
                    2_000_000,
                ),
                max_reported_cost_usd=_bounded_float(
                    "SCREENER_FANOUT_SHADOW_MAX_COST_USD", 3.0, 0, 10
                ),
                global_timeout_seconds=_bounded_float(
                    "SCREENER_FANOUT_SHADOW_TIMEOUT_SECONDS", 900, 0, 1_800
                ),
                timeout_seconds=240,
                policy_version=expected_policy,
                policy_manifest_profile=expected_manifest_profile,
                policy_manifest_rotation_id=expected_manifest_rotation,
                policy_manifest_digest=expected_manifest_digest,
                files_per_group=8,
                group_bytes=128_000,
            )
            outcome = report["outcome"]
            status = "incomplete" if outcome == "incomplete" else "succeeded"
            complete = await client.post(
                f"{base}/complete",
                headers=headers,
                json={
                    "status": status,
                    "outcome": outcome,
                    "report": report,
                    "error_code": (
                        "fanout-review-incomplete" if status == "incomplete" else None
                    ),
                },
            )
            complete.raise_for_status()
        return 0
    finally:
        if archive_path is not None:
            Path(archive_path).unlink(missing_ok=True)
        Path(key_file).unlink(missing_ok=True)


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
