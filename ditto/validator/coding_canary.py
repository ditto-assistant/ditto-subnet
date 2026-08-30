"""Default-off public-canary worker bound to a qualified certification lease."""

from __future__ import annotations

import asyncio
import logging
from base64 import urlsafe_b64encode
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from Crypto.PublicKey import ECC

from ditto.api_models.coding import (
    CodingCapabilityCertificationReceipt,
    SubmitCodingCertificationResponse,
)
from ditto.api_models.coding_certification_leases import (
    CodingCertificationHarnessLaunchResponse,
    CodingCertificationLeaseAuthority,
    CodingCertificationLeaseResponse,
    CodingCertificationLeaseStatus,
)
from ditto.api_models.coding_inference_grants import (
    CodingCertificationInferenceExchangeResponse,
    CodingCertificationInferenceGrantOffer,
    CodingCertificationInferenceRevokeResponse,
)
from ditto.validator.errors import (
    PlatformError,
    PlatformInfrastructureError,
    ValidatorInfrastructureError,
)

logger = logging.getLogger(__name__)
_MAX_QUEUE = 32


@dataclass(frozen=True)
class CodingCanaryOutcome:
    authority: CodingCertificationLeaseAuthority
    receipt: CodingCapabilityCertificationReceipt
    capabilities_revoked: Literal[True]
    harness_destroyed: Literal[True]


class CodingCanaryRuntime(Protocol):
    async def require_available(self) -> None: ...

    async def certify(
        self,
        lease: CodingCertificationLeaseResponse,
        harness: CodingCertificationHarnessLaunchResponse,
        grant: CodingCertificationInferenceExchangeResponse,
        *,
        broker_public_key: str,
        broker_private_key: str,
    ) -> CodingCanaryOutcome: ...


class CodingCanaryPlatform(Protocol):
    async def issue_coding_certification_lease(
        self, agent_id: UUID, *, bench_version: int
    ) -> CodingCertificationLeaseResponse | None: ...

    async def claim_coding_certification_lease(
        self, lease_id: UUID
    ) -> CodingCertificationLeaseResponse: ...

    async def abort_coding_certification_lease(
        self, lease_id: UUID
    ) -> CodingCertificationLeaseResponse: ...

    async def request_coding_certification_harness_launch(
        self, lease_id: UUID
    ) -> CodingCertificationHarnessLaunchResponse: ...

    async def request_coding_certification_inference_grant(
        self, lease_id: UUID
    ) -> CodingCertificationInferenceGrantOffer: ...

    async def exchange_coding_certification_inference_grant(
        self,
        offer: CodingCertificationInferenceGrantOffer,
        *,
        broker_public_key: str,
    ) -> CodingCertificationInferenceExchangeResponse: ...

    async def revoke_coding_certification_inference_grant(
        self,
        *,
        grant_id: UUID,
        generation: int,
    ) -> CodingCertificationInferenceRevokeResponse: ...

    async def submit_coding_certification(
        self,
        agent_id: UUID,
        *,
        bench_version: int,
        lease_id: UUID,
        screened_image_sha256: str,
        receipt: CodingCapabilityCertificationReceipt,
        signature: str,
    ) -> SubmitCodingCertificationResponse: ...


class CodingCanaryWorker:
    """Claim one public-canary lease and run codingcertifier. Default off."""

    def __init__(
        self,
        *,
        platform: CodingCanaryPlatform,
        runtime: CodingCanaryRuntime,
        sign_receipt: Callable[
            [
                CodingCertificationLeaseResponse,
                CodingCapabilityCertificationReceipt,
            ],
            str,
        ],
        poll_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= poll_seconds <= 300:
            raise ValueError("coding canary worker configuration is invalid")
        self._platform = platform
        self._runtime = runtime
        self._sign_receipt = sign_receipt
        self._poll_seconds = poll_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("coding canary worker clock is invalid")
        self.busy = False
        self._drain_requested: asyncio.Event | None = None
        self._queue: asyncio.Queue[tuple[UUID, int]] = asyncio.Queue(maxsize=_MAX_QUEUE)

    def offer(self, agent_id: UUID, bench_version: int) -> None:
        if agent_id.int == 0 or type(bench_version) is not int or bench_version < 7:
            return
        try:
            self._queue.put_nowait((agent_id, bench_version))
        except asyncio.QueueFull:
            logger.warning("coding canary offer queue is full agent=%s", agent_id)

    async def run_forever(
        self,
        stop: asyncio.Event,
        *,
        drain_requested: asyncio.Event,
    ) -> None:
        self._drain_requested = drain_requested
        while not stop.is_set():
            if drain_requested.is_set() and not self.busy:
                await _wait_or_stop(stop, self._poll_seconds)
                continue
            self.busy = True
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "coding canary attempt failed type=%s",
                    type(error).__name__,
                )
                worked = False
            finally:
                self.busy = False
            if not worked:
                await _wait_or_stop(stop, self._poll_seconds)

    async def run_once(self) -> bool:
        try:
            await self._runtime.require_available()
        except PlatformInfrastructureError:
            return False
        if self._drain_requested is not None and self._drain_requested.is_set():
            return False
        try:
            agent_id, bench_version = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return False
        try:
            issued = await self._issue(agent_id, bench_version)
        except PlatformInfrastructureError:
            self._requeue(agent_id, bench_version)
            raise
        if issued is None:
            return False
        if issued.status is not CodingCertificationLeaseStatus.ISSUED:
            raise PlatformInfrastructureError(
                "coding certification lease was not issued exclusively"
            )
        claimed: CodingCertificationLeaseResponse | None = None
        offer: CodingCertificationInferenceGrantOffer | None = None
        exchange: CodingCertificationInferenceExchangeResponse | None = None
        outcome: CodingCanaryOutcome | None = None
        primary_error: BaseException | None = None
        revoke_error: BaseException | None = None
        try:
            claimed = await self._platform.claim_coding_certification_lease(
                issued.authority.lease_id
            )
            if claimed.status is not CodingCertificationLeaseStatus.CLAIMED:
                raise PlatformInfrastructureError(
                    "coding certification lease claim did not become exclusive"
                )
            harness = await self._platform.request_coding_certification_harness_launch(
                claimed.authority.lease_id
            )
            if (
                harness.lease_id != claimed.authority.lease_id
                or harness.agent_id != claimed.authority.agent_id
                or harness.agent_artifact_sha256
                != claimed.authority.agent_artifact_sha256
                or harness.screened_image_sha256
                != claimed.authority.screened_image_sha256
                or harness.weight_eligible
            ):
                raise PlatformInfrastructureError(
                    "coding certification harness launch authority is invalid"
                )
            public_key, private_key = _ed25519_broker_pair()
            offer = await self._platform.request_coding_certification_inference_grant(
                claimed.authority.lease_id
            )
            if offer.lease_id != claimed.authority.lease_id or offer.weight_eligible:
                raise PlatformInfrastructureError(
                    "coding certification inference grant authority is invalid"
                )
            exchange = (
                await self._platform.exchange_coding_certification_inference_grant(
                    offer, broker_public_key=public_key
                )
            )
            if (
                exchange.lease_id != claimed.authority.lease_id
                or exchange.grant_id != offer.grant_id
                or exchange.weight_eligible
            ):
                raise PlatformInfrastructureError(
                    "coding certification inference exchange authority is invalid"
                )
            outcome = await self._runtime.certify(
                claimed,
                harness,
                exchange,
                broker_public_key=public_key,
                broker_private_key=private_key,
            )
        except Exception as error:
            primary_error = error
            if claimed is None:
                await self._abort_issued(issued.authority.lease_id)
        finally:
            authority = exchange or offer
            if authority is not None:
                try:
                    await asyncio.shield(
                        self._platform.revoke_coding_certification_inference_grant(
                            grant_id=authority.grant_id,
                            generation=authority.generation,
                        )
                    )
                except BaseException as error:
                    revoke_error = error
        if revoke_error is not None:
            raise ValidatorInfrastructureError(
                "coding certification inference grant revocation failed"
            ) from revoke_error
        if primary_error is not None:
            raise primary_error
        if claimed is None or outcome is None:
            raise PlatformInfrastructureError("coding canary outcome is unavailable")
        if (
            not outcome.capabilities_revoked
            or not outcome.harness_destroyed
            or outcome.receipt.weight_eligible
            or outcome.authority.lease_id != claimed.authority.lease_id
            or outcome.receipt.agent_artifact_sha256
            != claimed.authority.agent_artifact_sha256
        ):
            raise PlatformInfrastructureError(
                "coding canary outcome authority is invalid"
            )
        submitted = await self._platform.submit_coding_certification(
            claimed.authority.agent_id,
            bench_version=claimed.authority.bench_version,
            lease_id=claimed.authority.lease_id,
            screened_image_sha256=claimed.authority.screened_image_sha256,
            receipt=outcome.receipt,
            signature=self._sign_receipt(claimed, outcome.receipt),
        )
        if not submitted.accepted or submitted.status != outcome.receipt.status:
            raise PlatformInfrastructureError(
                "coding certification receipt was not accepted"
            )
        logger.info(
            "coding canary finished lease=%s status=%s",
            claimed.authority.lease_id,
            outcome.receipt.status,
        )
        return True

    async def _issue(
        self, agent_id: UUID, bench_version: int
    ) -> CodingCertificationLeaseResponse | None:
        try:
            return await self._platform.issue_coding_certification_lease(
                agent_id, bench_version=bench_version
            )
        except PlatformInfrastructureError:
            raise
        except PlatformError:
            return None

    def _requeue(self, agent_id: UUID, bench_version: int) -> None:
        try:
            self._queue.put_nowait((agent_id, bench_version))
        except asyncio.QueueFull:
            logger.warning("coding canary requeue is full agent=%s", agent_id)

    async def _abort_issued(self, lease_id: UUID) -> None:
        try:
            await self._platform.abort_coding_certification_lease(lease_id)
        except Exception as error:
            logger.warning(
                "coding canary abort failed lease=%s type=%s",
                lease_id,
                type(error).__name__,
            )


def _ed25519_broker_pair() -> tuple[str, str]:
    key = ECC.generate(curve="Ed25519")
    seed = bytes(key.seed)
    public = key.public_key().export_key(format="raw")
    if len(seed) != 32 or len(public) != 32:
        raise ValidatorInfrastructureError("coding canary broker key is invalid")
    return (
        urlsafe_b64encode(public).decode().rstrip("="),
        urlsafe_b64encode(seed + public).decode().rstrip("="),
    )


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return
