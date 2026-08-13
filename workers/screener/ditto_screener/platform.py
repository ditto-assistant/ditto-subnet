"""Async client for the platform's ``/screener/*`` HTTP API.

The worker is HTTP-decoupled from the platform: it pulls work and posts verdicts
over the public ``/screener/*`` contract, authenticating every request with a
bearer token and the ``X-Screener-Hotkey`` header. Verdict POSTs additionally
carry an sr25519 signature. It never touches the platform DB.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

import httpx

from ditto_screener.enrollment import (
    NodeCredential,
    load_node_credential,
    refresh_signing_message,
    store_node_credential,
)
from ditto_screener.errors import PlatformError
from ditto_screener.heartbeat import (
    ScreenerHeartbeatRequest,
    ScreenerHeartbeatResponse,
)
from ditto_screener.review_settings import (
    EffectiveReviewSettings,
    ReviewSettingsCache,
    ShadowReviewObservationRequest,
    ShadowReviewObservationResponse,
    bootstrap_review_settings,
)
from ditto_screening_protocol import (
    ArtifactResponse,
    ScreenedImageCompletedPart,
    ScreenedImagePartUploadRequest,
    ScreenedImagePartUploadResponse,
    ScreenedImageUploadAbortRequest,
    ScreenedImageUploadAbortResponse,
    ScreenedImageUploadCompleteRequest,
    ScreenedImageUploadCompleteResponse,
    ScreenedImageUploadRequest,
    ScreenedImageUploadResponse,
    ScreenerQueueResponse,
    ScreenEvidenceItem,
    ScreenResultOutcome,
    ScreenResultRequest,
    ScreenResultResponse,
    ScreenReviewAudit,
    SourceReviewFinding,
    SourceReviewObservationPayload,
    SubmissionImageBuildRequest,
    SubmissionImageBuildResponse,
    SubmissionSourceReviewRequest,
    SubmissionSourceReviewResponse,
)

if TYPE_CHECKING:
    from ditto_screener.config import ScreenerConfig

logger = logging.getLogger(__name__)

_PREFIX = "/api/v1/screener"
_IMAGE_REQUEST_TIMEOUT = httpx.Timeout(300.0, connect=30.0, pool=30.0)
_IMAGE_UPLOAD_ATTEMPTS = 3
# A screened image can take many minutes to build, while a Platform rollout is
# normally a sub-minute interruption. Keep the already-computed signed verdict
# in memory across that window instead of abandoning the 70-minute lease after
# 3.5 seconds. These sleeps spend no model-call budget and do not rerun the
# build; they only replay the same idempotent attempt-bound payload.
_VERDICT_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
_TRANSIENT_VERDICT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_REMOTE_BUILD_POLL_SECONDS = 5.0


@dataclass(frozen=True)
class RemoteImageArchive:
    build_id: UUID
    path: str
    sha256: str
    size_bytes: int


class PlatformClient:
    """HTTP client for one platform base URL, screener-flavoured."""

    def __init__(
        self,
        config: ScreenerConfig,
        client: httpx.AsyncClient,
        *,
        keypair: Any | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._base = config.platform_api_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {config.api_token}",
            "X-Screener-Hotkey": config.screener_hotkey,
        }
        self._keypair = keypair
        self._credential_lock = asyncio.Lock()
        self._review_settings_cache = ReviewSettingsCache(
            config.review_settings_cache_file
        )
        self._review_settings: EffectiveReviewSettings | None = None
        self._review_settings_fetched_at = float("-inf")
        self._review_settings_source: Literal["platform", "cache", "bootstrap"] = (
            "bootstrap"
        )

    async def _auth_headers(self) -> dict[str, str]:
        """Return current auth, rotating enrolled-node authority before expiry."""
        path = self._config.node_credential_file
        if path is None:
            return dict(self._headers)
        async with self._credential_lock:
            credential = load_node_credential(path)
            expires_at = credential.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at > datetime.now(UTC) + timedelta(minutes=15):
                self._headers["Authorization"] = f"Bearer {credential.api_token}"
                return dict(self._headers)
            if self._keypair is None:
                raise PlatformError(
                    "enrolled node cannot rotate without its signing key"
                )
            refresh_id = credential.pending_refresh_id or uuid4()
            if credential.pending_refresh_id is None:
                credential = credential.model_copy(
                    update={"pending_refresh_id": refresh_id}
                )
                store_node_credential(path, credential)
            timestamp = int(time.time())
            signature = self._keypair.sign(
                refresh_signing_message(
                    node_id=credential.node_id,
                    screener_hotkey=credential.screener_hotkey,
                    timestamp=timestamp,
                    refresh_id=refresh_id,
                )
            ).hex()
            url = f"{self._base}{_PREFIX}/nodes/refresh"
            response: httpx.Response | None = None
            try:
                for attempt in range(3):
                    try:
                        response = await self._client.post(
                            url,
                            json={
                                "node_id": credential.node_id,
                                "screener_hotkey": credential.screener_hotkey,
                                "timestamp": timestamp,
                                "signature": signature,
                                "refresh_id": str(refresh_id),
                            },
                            headers={
                                "Authorization": f"Bearer {credential.api_token}",
                                "X-Screener-Hotkey": credential.screener_hotkey,
                            },
                        )
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise
                        continue
                    if response.status_code not in {
                        408,
                        425,
                        429,
                        500,
                        502,
                        503,
                        504,
                    }:
                        break
            except httpx.HTTPError as error:
                raise PlatformError(
                    "screener node credential refresh failed"
                ) from error
            if response is None:
                raise PlatformError("screener node credential refresh failed")
            if response.status_code != 200:
                raise PlatformError(
                    "screener node credential refresh rejected "
                    f"({response.status_code})"
                )
            try:
                body = response.json()
                rotated = NodeCredential.model_validate(
                    {
                        **credential.model_dump(mode="python"),
                        "api_token": body["api_token"],
                        "expires_at": body["expires_at"],
                        "pending_refresh_id": None,
                    }
                )
            except (KeyError, TypeError, ValueError) as error:
                raise PlatformError(
                    "screener node credential refresh response is invalid"
                ) from error
            store_node_credential(path, rotated)
            self._headers["Authorization"] = f"Bearer {rotated.api_token}"
            return dict(self._headers)

    @property
    def review_settings_source(self) -> Literal["platform", "cache", "bootstrap"]:
        return self._review_settings_source

    async def get_review_settings(self, instance_id: str) -> EffectiveReviewSettings:
        """Fetch settings before a claim, falling back only to bounded valid state."""
        now = time.monotonic()
        if (
            self._review_settings is not None
            and now - self._review_settings_fetched_at
            < self._review_settings.max_age_seconds
        ):
            return self._review_settings
        url = f"{self._base}{_PREFIX}/review-settings"
        try:
            response = await self._client.get(
                url,
                params={"instance_id": instance_id},
                headers=await self._auth_headers(),
            )
            if response.status_code != 200:
                raise PlatformError(
                    "review settings rejected "
                    f"({response.status_code}): {response.text[:200]}"
                )
            effective = EffectiveReviewSettings.model_validate_json(response.text)
            self._review_settings_cache.store(effective)
            self._review_settings = effective
            self._review_settings_source = "platform"
            self._review_settings_fetched_at = now
            return effective
        except (httpx.HTTPError, ValueError, OSError, PlatformError) as error:
            cached = self._review_settings_cache.load()
            if cached is not None:
                age = max(0, int(time.time()) - cached.cached_at)
                if age <= self._config.review_settings_max_stale_seconds:
                    logger.warning(
                        "using cached review settings revision=%d age_s=%d: %s",
                        cached.effective.revision,
                        age,
                        error,
                    )
                    self._review_settings = cached.effective
                    self._review_settings_source = "cache"
                    self._review_settings_fetched_at = now
                    return cached.effective
                if cached.effective.settings.mode == "enforce":
                    raise PlatformError(
                        "enforced review settings expired; refusing new claims"
                    ) from error
            bootstrap = bootstrap_review_settings(self._config)
            if bootstrap.settings.mode == "enforce":
                raise PlatformError(
                    "platform review settings unavailable in enforce mode"
                ) from error
            logger.warning("review settings unavailable; using bootstrap %s", error)
            self._review_settings = bootstrap
            self._review_settings_source = "bootstrap"
            self._review_settings_fetched_at = now
            return bootstrap

    async def submit_heartbeat(
        self, request: ScreenerHeartbeatRequest
    ) -> ScreenerHeartbeatResponse:
        """Publish a best-effort signed fleet-health report."""
        url = f"{self._base}{_PREFIX}/heartbeat"
        try:
            resp = await self._client.post(
                url,
                json=request.model_dump(mode="json"),
                headers=await self._auth_headers(),
            )
        except httpx.HTTPError as error:
            raise PlatformError(f"screener heartbeat failed: {error}") from error
        if resp.status_code != 200:
            raise PlatformError(
                f"screener heartbeat rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return ScreenerHeartbeatResponse.model_validate(resp.json())

    async def submit_shadow_review(
        self, agent_id: UUID, request: ShadowReviewObservationRequest
    ) -> ShadowReviewObservationResponse:
        """Persist bounded attempt-owned telemetry without changing outcome."""
        url = f"{self._base}{_PREFIX}/agent/{agent_id}/shadow-review"
        try:
            resp = await self._client.post(
                url,
                json=request.model_dump(mode="json"),
                headers=await self._auth_headers(),
            )
        except httpx.HTTPError as error:
            raise PlatformError(f"shadow review submission failed: {error}") from error
        if resp.status_code != 200:
            raise PlatformError(
                f"shadow review rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return ShadowReviewObservationResponse.model_validate(resp.json())

    async def get_required_policy_version(self) -> int:
        """Read the platform policy without claiming or mutating queue state."""
        url = f"{self._base}{_PREFIX}/queue"
        try:
            resp = await self._client.get(
                url, params={"limit": 1}, headers=await self._auth_headers()
            )
        except httpx.HTTPError as e:
            raise PlatformError(f"screening policy check failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"screening policy check rejected ({resp.status_code}): "
                f"{resp.text[:200]}"
            )
        return ScreenerQueueResponse.model_validate(resp.json()).required_policy_version

    async def claim_next(self, *, policy_version: int) -> ScreenerQueueResponse:
        """Lease one agent for screening, oldest eligible first."""
        url = f"{self._base}{_PREFIX}/claim"
        params = {"limit": 1, "policy_version": policy_version}
        try:
            resp = await self._client.post(
                url, params=params, headers=await self._auth_headers()
            )
        except httpx.HTTPError as e:
            raise PlatformError(f"screening claim failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"screening claim rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return ScreenerQueueResponse.model_validate(resp.json())

    async def get_artifact(
        self, agent_id: UUID, *, attempt_id: UUID | None = None
    ) -> ArtifactResponse:
        """Get a presigned tarball download URL for ``agent_id``.

        Pass the claim ``attempt_id`` so the platform can bind the download to
        the active screening lease.
        """
        url = f"{self._base}{_PREFIX}/agent/{agent_id}/artifact"
        params = {"attempt_id": str(attempt_id)} if attempt_id is not None else None
        try:
            resp = await self._client.get(
                url, headers=await self._auth_headers(), params=params
            )
        except httpx.HTTPError as e:
            raise PlatformError(f"artifact fetch failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"artifact rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return ArtifactResponse.model_validate(resp.json())

    async def build_submission_image(
        self,
        agent_id: UUID,
        *,
        attempt_id: UUID,
        timeout: float,
    ) -> RemoteImageArchive | None:
        """Prefer one verified Targon Kaniko archive, otherwise authorize local."""
        base_url = f"{self._base}{_PREFIX}/agent/{agent_id}/submission-image-builds"
        try:
            response = await self._client.post(
                base_url,
                json=SubmissionImageBuildRequest(attempt_id=attempt_id).model_dump(
                    mode="json"
                ),
                headers=await self._auth_headers(),
            )
            if response.status_code != 200:
                logger.warning(
                    "remote build queue unavailable status=%d; using local builder",
                    response.status_code,
                )
                return None
            build = SubmissionImageBuildResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as error:
            logger.warning(
                "remote build queue unavailable; using local builder: %s", error
            )
            return None
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout)
        try:
            while asyncio.get_running_loop().time() < deadline:
                if build.status == "succeeded":
                    archive = await self._download_remote_image(build)
                    if archive is not None:
                        return archive
                    break
                if build.status in {"fallback_required", "canceled", "consumed"}:
                    logger.warning(
                        "remote build unavailable code=%s; using local builder",
                        build.error_code or "TARGON_SUBMISSION_BUILD_UNAVAILABLE",
                    )
                    return None
                await asyncio.sleep(
                    min(
                        _REMOTE_BUILD_POLL_SECONDS,
                        max(0.0, deadline - asyncio.get_running_loop().time()),
                    )
                )
                response = await self._client.get(
                    f"{base_url}/{build.build_id}",
                    params={"attempt_id": str(attempt_id)},
                    headers=await self._auth_headers(),
                )
                if response.status_code != 200:
                    raise PlatformError(
                        f"remote build poll rejected ({response.status_code})"
                    )
                build = SubmissionImageBuildResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError, PlatformError) as error:
            logger.warning("remote build failed; using local builder: %s", error)
        await self.discard_submission_image_build(
            agent_id, attempt_id=attempt_id, build_id=build.build_id
        )
        return None

    async def _download_remote_image(
        self, build: SubmissionImageBuildResponse
    ) -> RemoteImageArchive | None:
        if (
            build.download_url is None
            or build.output_sha256 is None
            or build.output_size_bytes is None
        ):
            return None
        fd, path = tempfile.mkstemp(prefix="ditto-targon-image-", suffix=".tar")
        os.fchmod(fd, 0o600)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                async with self._client.stream(
                    "GET", build.download_url, timeout=_IMAGE_REQUEST_TIMEOUT
                ) as response:
                    if response.status_code != 200:
                        raise PlatformError(
                            f"remote image download rejected ({response.status_code})"
                        )
                    async for chunk in response.aiter_bytes(8 * 1024**2):
                        total += len(chunk)
                        if total > build.output_size_bytes:
                            raise PlatformError("remote image exceeded declared size")
                        digest.update(chunk)
                        await asyncio.to_thread(handle.write, chunk)
            if total != build.output_size_bytes:
                raise PlatformError("remote image ended before declared size")
            actual = digest.hexdigest()
            if actual != build.output_sha256:
                raise PlatformError("remote image digest did not match Platform")
            return RemoteImageArchive(
                build_id=build.build_id,
                path=path,
                sha256=actual,
                size_bytes=total,
            )
        except (httpx.HTTPError, OSError, PlatformError) as error:
            with contextlib.suppress(OSError):
                os.unlink(path)
            logger.warning(
                "remote image download failed; using local builder: %s", error
            )
            return None

    async def discard_submission_image_build(
        self,
        agent_id: UUID,
        *,
        attempt_id: UUID,
        build_id: UUID,
    ) -> None:
        """Revoke an active job or delete a successfully imported temp object."""
        try:
            response = await self._client.delete(
                f"{self._base}{_PREFIX}/agent/{agent_id}/"
                f"submission-image-builds/{build_id}",
                params={"attempt_id": str(attempt_id)},
                headers=await self._auth_headers(),
            )
            if response.status_code not in {204, 404, 409}:
                logger.warning(
                    "remote build cleanup rejected status=%d", response.status_code
                )
        except httpx.HTTPError as error:
            logger.warning("remote build cleanup failed: %s", error)

    async def review_submission_source(
        self,
        agent_id: UUID,
        *,
        attempt_id: UUID,
        timeout: float,
    ) -> SourceReviewObservationPayload | None:
        """Prefer one bounded Targon review and return None for local fallback."""
        base_url = f"{self._base}{_PREFIX}/agent/{agent_id}/submission-source-reviews"
        review: SubmissionSourceReviewResponse | None = None
        try:
            response = await self._client.post(
                base_url,
                json=SubmissionSourceReviewRequest(attempt_id=attempt_id).model_dump(
                    mode="json"
                ),
                headers=await self._auth_headers(),
            )
            if response.status_code != 200:
                logger.warning(
                    "remote source-review queue unavailable status=%d; "
                    "using local reviewer",
                    response.status_code,
                )
                return None
            review = SubmissionSourceReviewResponse.model_validate(response.json())
            deadline = asyncio.get_running_loop().time() + max(1.0, timeout)
            while asyncio.get_running_loop().time() < deadline:
                if review.status == "succeeded":
                    observation = review.observation
                    if (
                        observation is not None
                        and observation.ok
                        and observation.risk_level == "low"
                        and observation.clearance_certified
                    ):
                        return observation
                    logger.info(
                        "remote source review requires authoritative local follow-up"
                    )
                    return None
                if review.status in {"fallback_required", "canceled", "consumed"}:
                    logger.warning(
                        "remote source review unavailable code=%s; "
                        "using local reviewer",
                        review.error_code or "TARGON_SOURCE_REVIEW_UNAVAILABLE",
                    )
                    return None
                await asyncio.sleep(
                    min(
                        _REMOTE_BUILD_POLL_SECONDS,
                        max(0.0, deadline - asyncio.get_running_loop().time()),
                    )
                )
                response = await self._client.get(
                    f"{base_url}/{review.review_id}",
                    params={"attempt_id": str(attempt_id)},
                    headers=await self._auth_headers(),
                )
                if response.status_code != 200:
                    raise PlatformError(
                        f"remote source-review poll rejected ({response.status_code})"
                    )
                review = SubmissionSourceReviewResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError, PlatformError) as error:
            logger.warning(
                "remote source review failed; using local reviewer: %s", error
            )
        finally:
            if review is not None:
                try:
                    response = await self._client.delete(
                        f"{base_url}/{review.review_id}",
                        params={"attempt_id": str(attempt_id)},
                        headers=await self._auth_headers(),
                    )
                    if response.status_code not in {204, 404, 409}:
                        logger.warning(
                            "remote source-review cleanup rejected status=%d",
                            response.status_code,
                        )
                except httpx.HTTPError as error:
                    logger.warning("remote source-review cleanup failed: %s", error)
        return None

    async def submit_result(
        self,
        agent_id: UUID,
        *,
        signature: str,
        passed: bool,
        policy_version: int,
        detail: str = "",
        attempt_id: UUID,
        outcome: ScreenResultOutcome | None = None,
        manifest_digest: str | None = None,
        finding_digest: str | None = None,
        review_audit_digest: str | None = None,
        review_settings_revision: int | None = None,
        review_settings_instance_id: str | None = None,
        review_settings_scope: str | None = None,
        review_settings_checksum: str | None = None,
        reason_code: str | None = None,
        evidence: list[ScreenEvidenceItem] | None = None,
        finding: SourceReviewFinding | None = None,
        review_audit: ScreenReviewAudit | None = None,
        image_sha256: str | None = None,
        image_size_bytes: int | None = None,
        image_id: str | None = None,
        image_ref: str | None = None,
        image_upload_id: UUID | None = None,
        build_only: bool = False,
        deferred_source_review: bool = False,
    ) -> ScreenResultResponse:
        """Report a signed pass/fail verdict for ``agent_id``."""
        url = f"{self._base}{_PREFIX}/agent/{agent_id}/result"
        payload = ScreenResultRequest(
            screener_hotkey=self._config.screener_hotkey,
            signature=signature,
            passed=passed,
            policy_version=policy_version,
            detail=detail,
            attempt_id=attempt_id,
            outcome=outcome,
            manifest_digest=manifest_digest,
            finding_digest=finding_digest,
            review_audit_digest=review_audit_digest,
            review_settings_revision=review_settings_revision,
            review_settings_instance_id=review_settings_instance_id,
            review_settings_scope=review_settings_scope,
            review_settings_checksum=review_settings_checksum,
            reason_code=reason_code,
            evidence=evidence,
            finding=finding,
            review_audit=review_audit,
            image_sha256=image_sha256,
            image_size_bytes=image_size_bytes,
            image_id=image_id,
            image_ref=image_ref,
            image_upload_id=image_upload_id,
            build_only=build_only,
            deferred_source_review=deferred_source_review,
        )
        body = payload.model_dump(mode="json")
        for attempt in range(len(_VERDICT_RETRY_DELAYS_SECONDS) + 1):
            try:
                resp = await self._client.post(
                    url, json=body, headers=await self._auth_headers()
                )
            except httpx.HTTPError as error:
                if attempt >= len(_VERDICT_RETRY_DELAYS_SECONDS):
                    raise PlatformError(f"verdict submit failed: {error}") from error
                logger.warning(
                    "verdict submit transport failure; retrying attempt=%d: %s",
                    attempt + 2,
                    error,
                )
            else:
                if resp.status_code == 200:
                    return ScreenResultResponse.model_validate(resp.json())
                if (
                    resp.status_code not in _TRANSIENT_VERDICT_STATUSES
                    or attempt >= len(_VERDICT_RETRY_DELAYS_SECONDS)
                ):
                    raise PlatformError(
                        f"verdict rejected ({resp.status_code}): {resp.text[:200]}"
                    )
                logger.warning(
                    "verdict submit transiently rejected status=%d; retrying "
                    "attempt=%d",
                    resp.status_code,
                    attempt + 2,
                )
            await asyncio.sleep(_VERDICT_RETRY_DELAYS_SECONDS[attempt])
        raise RuntimeError("unreachable")

    async def upload_screened_image(
        self,
        agent_id: UUID,
        *,
        attempt_id: UUID,
        path: str,
        sha256: str,
        size_bytes: int,
        image_id: str,
        image_ref: str,
    ) -> UUID:
        """Upload an image in bounded parts and return its verified upload id."""
        archive = Path(path)
        if archive.stat().st_size != size_bytes:
            raise PlatformError("screened image changed before multipart upload")
        request = ScreenedImageUploadRequest(
            attempt_id=attempt_id,
            sha256=sha256,
            size_bytes=size_bytes,
            image_id=image_id,
            image_ref=image_ref,
        )
        base_url = f"{self._base}{_PREFIX}/agent/{agent_id}/screened-image-upload"
        upload: ScreenedImageUploadResponse | None = None
        try:
            response = await self._image_request(
                "POST",
                base_url,
                operation="image upload initiate",
                json=request.model_dump(mode="json"),
                headers=await self._auth_headers(),
            )
            upload = ScreenedImageUploadResponse.model_validate(response.json())
            completed: list[ScreenedImageCompletedPart] = []
            with archive.open("rb") as handle:
                part_number = 1
                uploaded_bytes = 0
                while uploaded_bytes < size_bytes:
                    part = await asyncio.to_thread(handle.read, upload.part_size_bytes)
                    if not part:
                        raise PlatformError(
                            "screened image ended before declared multipart size"
                        )
                    uploaded_bytes += len(part)
                    part_request = ScreenedImagePartUploadRequest(
                        attempt_id=attempt_id,
                        storage_upload_id=upload.storage_upload_id,
                        part_number=part_number,
                        size_bytes=len(part),
                    )
                    part_response = await self._image_request(
                        "POST",
                        f"{base_url}/{upload.image_upload_id}/part",
                        operation=f"image part {part_number} mint",
                        json=part_request.model_dump(mode="json"),
                        headers=await self._auth_headers(),
                    )
                    part_upload = ScreenedImagePartUploadResponse.model_validate(
                        part_response.json()
                    )
                    stored = await self._image_request(
                        "PUT",
                        part_upload.upload_url,
                        operation=f"image part {part_number} upload",
                        content=part,
                        headers=part_upload.required_headers,
                        accepted=frozenset({200, 201, 204}),
                    )
                    etag = stored.headers.get("etag")
                    if not etag:
                        raise PlatformError(
                            f"image part {part_number} upload returned no ETag"
                        )
                    completed.append(
                        ScreenedImageCompletedPart(
                            part_number=part_number,
                            etag=etag,
                        )
                    )
                    part_number += 1
            if uploaded_bytes != size_bytes or archive.stat().st_size != size_bytes:
                raise PlatformError(
                    "screened image size changed during multipart upload"
                )
            complete = ScreenedImageUploadCompleteRequest(
                attempt_id=attempt_id,
                storage_upload_id=upload.storage_upload_id,
                sha256=sha256,
                size_bytes=size_bytes,
                image_id=image_id,
                image_ref=image_ref,
                parts=completed,
            )
            completed_response = await self._image_request(
                "POST",
                f"{base_url}/{upload.image_upload_id}/complete",
                operation="image upload complete",
                json=complete.model_dump(mode="json"),
                headers=await self._auth_headers(),
            )
            ScreenedImageUploadCompleteResponse.model_validate(
                completed_response.json()
            )
            return upload.image_upload_id
        except BaseException:
            if upload is not None:
                with contextlib.suppress(Exception):
                    await asyncio.shield(
                        self._abort_screened_image_upload(
                            base_url,
                            upload,
                            attempt_id=attempt_id,
                        )
                    )
            raise

    async def _image_request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        accepted: frozenset[int] = frozenset({200}),
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue one finite image request, retrying transport and server failures."""
        for attempt in range(1, _IMAGE_UPLOAD_ATTEMPTS + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    timeout=_IMAGE_REQUEST_TIMEOUT,
                    **kwargs,
                )
            except httpx.HTTPError as error:
                if attempt == _IMAGE_UPLOAD_ATTEMPTS:
                    raise PlatformError(f"{operation} failed: {error}") from error
            else:
                if response.status_code in accepted:
                    return response
                if response.status_code not in {429, 500, 502, 503, 504}:
                    raise PlatformError(
                        f"{operation} rejected ({response.status_code}): "
                        f"{response.text[:200]}"
                    )
                if attempt == _IMAGE_UPLOAD_ATTEMPTS:
                    raise PlatformError(
                        f"{operation} failed after retries ({response.status_code}): "
                        f"{response.text[:200]}"
                    )
            await asyncio.sleep(0.5 * attempt)
        raise AssertionError("image request retry loop exhausted")

    async def _abort_screened_image_upload(
        self,
        base_url: str,
        upload: ScreenedImageUploadResponse,
        *,
        attempt_id: UUID,
    ) -> None:
        """Best-effort abort so failed multipart parts do not accumulate."""
        request = ScreenedImageUploadAbortRequest(
            attempt_id=attempt_id,
            storage_upload_id=upload.storage_upload_id,
        )
        try:
            response = await self._image_request(
                "POST",
                f"{base_url}/{upload.image_upload_id}/abort",
                operation="image upload abort",
                json=request.model_dump(mode="json"),
                headers=await self._auth_headers(),
            )
            ScreenedImageUploadAbortResponse.model_validate(response.json())
        except PlatformError as error:
            logger.warning(
                "failed to abort image_upload_id=%s: %s",
                upload.image_upload_id,
                error,
            )
