"""One independent, report-only V13 public verification replay lease.

This lane records at most the seven public mechanical/runtime observations.
It cannot complete the mandatory private profile or clear a quarantine.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ditto_screener.errors import PlatformError
from ditto_screener.gate import BuildGate, BuiltImageArtifact, LeaseDeadline
from ditto_screener.policy import ScreeningOutcome
from ditto_screener.replay_api import ReplayApiClient
from ditto_screening_protocol.mechanical_verification import mechanical_evidence_sha256

PUBLIC_CHECKS = frozenset(
    {
        "archive_sha",
        "build_image_digest",
        "health",
        "ordinary_model_run",
        "tool_selection_run",
        "seed_memory_run",
        "two_user_isolation",
    }
)
logger = logging.getLogger(__name__)


class ReplayLease(BaseModel):
    model_config = ConfigDict(extra="ignore")

    replay_id: UUID
    agent_id: UUID
    source_attempt_id: UUID
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: Literal[13]
    image_upload_id: UUID | None
    image_sha256: str | None
    image_size_bytes: int | None = None
    image_id: str | None = None
    image_verified_at: datetime | None = None
    lease_deadline: datetime
    status: Literal["running"]


class ReplayInputs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    replay: ReplayLease
    bench_version: int = Field(ge=7)
    miner_hotkey: str
    artifact_url: str
    image_url: str | None
    urls_expire_at: datetime


class ReplayPublicRunner:
    """Run the existing isolated build gate under a separate replay lease."""

    def __init__(
        self,
        *,
        api: ReplayApiClient,
        gate: BuildGate,
        http: httpx.AsyncClient,
    ) -> None:
        self._api = api
        self._gate = gate
        self._http = http

    async def run_once(self) -> UUID | None:
        claimed = await self._api.claim()
        if claimed is None:
            return None
        lease = ReplayLease.model_validate(claimed)
        await self.run_lease(lease)
        return lease.replay_id

    async def run_lease(self, lease: ReplayLease) -> None:
        """Finish one claimed lease as reported or failed, never as CLEAR."""
        image_sha256 = lease.image_sha256
        failure_code: str | None = None
        existing_image_path: str | None = None
        try:
            inputs = ReplayInputs.model_validate(
                await self._api.inputs(lease.replay_id)
            )
            if inputs.replay != lease:
                raise PlatformError("replay-input-binding-changed")
            if inputs.urls_expire_at <= datetime.now(UTC):
                raise PlatformError("replay-input-url-expired")
            existing_image = lease.image_upload_id is not None
            if existing_image:
                if (
                    lease.image_sha256 is None
                    or lease.image_verified_at is None
                    or lease.image_size_bytes is None
                    or not 0 < lease.image_size_bytes <= 8 * 1024**3
                    or lease.image_id is None
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", lease.image_id) is None
                    or inputs.image_url is None
                ):
                    raise PlatformError("verified-image-replay-binding-incomplete")
                existing_image_path = await self._download_existing_image(
                    inputs.image_url,
                    expected_sha256=lease.image_sha256,
                    expected_size=lease.image_size_bytes,
                )
            elif lease.image_sha256 is not None or inputs.image_url is not None:
                raise PlatformError("replay-image-binding-inconsistent")
            # The current gate uses this selector to emit bounded observations
            # after the local build and isolated smoke. The caller must create
            # it with shadow receipts enabled; checking here prevents a report
            # that silently omitted the runtime lane.
            if self._gate._config.v13_runtime_receipts_mode != "shadow":
                raise PlatformError("runtime-receipts-disabled")
            deadline_seconds = (
                lease.lease_deadline - datetime.now(UTC)
            ).total_seconds()
            if deadline_seconds <= 45:
                raise PlatformError("replay-lease-insufficient")
            deadline = LeaseDeadline(
                asyncio.get_running_loop().time() + deadline_seconds
            )
            renewal_stop = asyncio.Event()

            async def keep_lease() -> None:
                while not renewal_stop.is_set():
                    remaining = deadline.expires_at - asyncio.get_running_loop().time()
                    try:
                        await asyncio.wait_for(
                            renewal_stop.wait(), timeout=max(1.0, remaining - 540.0)
                        )
                        return
                    except TimeoutError:
                        pass
                    try:
                        renewed = ReplayLease.model_validate(
                            await self._api.renew(lease.replay_id)
                        )
                    except Exception:  # noqa: BLE001 - gate retains old deadline
                        logger.warning(
                            "public replay renewal failed replay_id=%s",
                            lease.replay_id,
                        )
                        return
                    if (
                        renewed.replay_id != lease.replay_id
                        or renewed.agent_id != lease.agent_id
                        or renewed.artifact_sha256 != lease.artifact_sha256
                    ):
                        logger.warning(
                            "public replay renewal binding changed replay_id=%s",
                            lease.replay_id,
                        )
                        return
                    new_remaining = (
                        renewed.lease_deadline - datetime.now(UTC)
                    ).total_seconds()
                    if new_remaining <= 0:
                        return
                    deadline.renew(asyncio.get_running_loop().time() + new_remaining)

            recorded: set[str] = set()

            async def receipt(check_code: str, evidence_sha256: str) -> None:
                if check_code not in PUBLIC_CHECKS:
                    raise PlatformError("unsupported-replay-check")
                await self._api.receipt(
                    lease.replay_id,
                    {
                        "artifact_sha256": lease.artifact_sha256,
                        "policy_version": 13,
                        "image_upload_id": lease.image_upload_id,
                        "image_sha256": image_sha256,
                        "check_code": check_code,
                        "evidence_sha256": evidence_sha256,
                    },
                )
                recorded.add(check_code)

            async def archive_receipt() -> None:
                await receipt(
                    "archive_sha",
                    mechanical_evidence_sha256(
                        check_code="archive_sha",
                        artifact_sha256=lease.artifact_sha256,
                        image_sha256=None,
                    ),
                )

            async def publish_image(image: BuiltImageArtifact) -> None:
                nonlocal image_sha256
                if image_sha256 is not None:
                    raise PlatformError("replay-image-already-pinned")
                payload = {
                    "artifact_sha256": lease.artifact_sha256,
                    "image_sha256": image.sha256,
                    "size_bytes": image.size_bytes,
                    "image_id": image.image_id,
                }
                upload = await self._api.build_upload(lease.replay_id, payload)
                # Platform pins the candidate SHA before minting the PUT. A
                # later upload failure must still finish against that binding.
                image_sha256 = image.sha256
                url = upload.get("upload_url")
                headers = upload.get("required_headers")
                if not isinstance(url, str) or not isinstance(headers, dict):
                    raise PlatformError("replay-upload-response-invalid")
                with Path(image.path).open("rb") as source:

                    async def chunks() -> AsyncIterator[bytes]:
                        while chunk := await asyncio.to_thread(
                            source.read, 1024 * 1024
                        ):
                            yield chunk

                    response = await self._http.put(
                        url,
                        content=chunks(),
                        headers=headers,
                        follow_redirects=False,
                        timeout=300.0,
                    )
                if response.status_code not in (200, 201, 204):
                    raise PlatformError(
                        f"replay image PUT returned HTTP {response.status_code}"
                    )
                verified = await self._api.build_verify(lease.replay_id, payload)
                if (
                    verified.get("image_sha256") != image.sha256
                    or verified.get("image_verified_at") is None
                ):
                    raise PlatformError("replay-image-verification-mismatch")
                image_sha256 = image.sha256
                await receipt(
                    "build_image_digest",
                    mechanical_evidence_sha256(
                        check_code="build_image_digest",
                        artifact_sha256=lease.artifact_sha256,
                        image_sha256=image.sha256,
                    ),
                )

            async def record_existing_image() -> None:
                assert image_sha256 is not None
                await receipt(
                    "build_image_digest",
                    mechanical_evidence_sha256(
                        check_code="build_image_digest",
                        artifact_sha256=lease.artifact_sha256,
                        image_sha256=image_sha256,
                    ),
                )

            renewal_task = asyncio.create_task(keep_lease())
            try:
                result = await self._gate.screen(
                    agent_id=lease.agent_id,
                    attempt_id=lease.source_attempt_id,
                    bench_version=inputs.bench_version,
                    miner_hotkey=inputs.miner_hotkey,
                    sha256=lease.artifact_sha256,
                    download_url=inputs.artifact_url,
                    deadline=deadline,
                    publish_image=None if existing_image else publish_image,
                    preverified_image=(existing_image_path, lease.image_id)
                    if existing_image_path is not None and lease.image_id is not None
                    else None,
                    record_preverified_image=record_existing_image
                    if existing_image
                    else None,
                    record_archive_verification=archive_receipt,
                    record_runtime_verification=receipt,
                    build_only=True,
                    replay_runtime_probes=True,
                    policy_version=13,
                )
            finally:
                renewal_stop.set()
                renewal_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewal_task
            if result.outcome != ScreeningOutcome.PASS:
                raise PlatformError("replay-gate-did-not-pass")
            if recorded != PUBLIC_CHECKS:
                raise PlatformError("replay-public-receipts-incomplete")
        except Exception as error:  # noqa: BLE001 - always settle the report-only lease
            logger.warning(
                "public replay failed replay_id=%s error_type=%s",
                lease.replay_id,
                type(error).__name__,
            )
            failure_code = (
                str(error)
                if isinstance(error, PlatformError)
                and str(error)
                in {
                    "verified-image-replay-binding-incomplete",
                    "verified-image-replay-download-failed",
                    "replay-image-binding-inconsistent",
                    "replay-input-binding-changed",
                    "replay-input-url-expired",
                    "runtime-receipts-disabled",
                    "replay-lease-insufficient",
                    "unsupported-replay-check",
                    "replay-image-already-pinned",
                    "replay-upload-response-invalid",
                    "replay-image-verification-mismatch",
                    "replay-gate-did-not-pass",
                    "replay-public-receipts-incomplete",
                }
                else "replay-public-runner-failed"
            )
        if failure_code is not None and image_sha256 is None:
            # A lost build-upload response may have pinned the image at
            # Platform. Read the lease once before attempting failure finish;
            # never retry the non-idempotent mutation with new bytes.
            with contextlib.suppress(Exception):
                current = ReplayLease.model_validate(
                    (await self._api.inputs(lease.replay_id))["replay"]
                )
                if (
                    current.replay_id == lease.replay_id
                    and current.artifact_sha256 == lease.artifact_sha256
                ):
                    image_sha256 = current.image_sha256
        if existing_image_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(existing_image_path)
        await self._api.finish(
            lease.replay_id,
            status="failed" if failure_code is not None else "reported",
            artifact_sha256=lease.artifact_sha256,
            image_sha256=image_sha256,
            failure_code=failure_code,
        )

    async def _download_existing_image(
        self, url: str, *, expected_sha256: str, expected_size: int
    ) -> str:
        """Stage only the exact Platform-verified image tar under the 8 GiB cap."""
        fd, path = tempfile.mkstemp(prefix="ditto-replay-image-", suffix=".tar")
        keep = False
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as target:
                async with self._http.stream(
                    "GET", url, follow_redirects=False
                ) as response:
                    if response.status_code != 200:
                        raise PlatformError("verified-image-replay-download-failed")
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > expected_size:
                            raise PlatformError("verified-image-replay-download-failed")
                        digest.update(chunk)
                        target.write(chunk)
            if size != expected_size or digest.hexdigest() != expected_sha256:
                raise PlatformError("verified-image-replay-download-failed")
            keep = True
            return path
        finally:
            if not keep:
                with contextlib.suppress(OSError):
                    os.unlink(path)
