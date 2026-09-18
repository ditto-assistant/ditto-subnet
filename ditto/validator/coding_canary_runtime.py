"""DittoBench control-plane client for one public certification canary."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ditto.api_models.coding import CodingCapabilityCertificationReceipt
from ditto.api_models.coding_certification_leases import (
    CodingCertificationHarnessLaunchResponse,
    CodingCertificationLeaseResponse,
)
from ditto.api_models.coding_inference_grants import (
    CodingCertificationInferenceExchangeResponse,
)
from ditto.validator.coding_canary import CodingCanaryOutcome, CodingCanaryReadiness
from ditto.validator.coding_executor_transport import scorer_control_origin
from ditto.validator.config import ValidatorConfig
from ditto.validator.errors import (
    PlatformInfrastructureError,
    ValidatorInfrastructureError,
)

_REQUEST_SCHEMA = "dittobench-coding-certification-canary-request-v1"
_RESPONSE_SCHEMA = "dittobench-coding-certification-canary-response-v1"
_READINESS_SCHEMA = "dittobench-coding-certification-canary-readiness-v1"
_CANARY_PATH = "/v1/coding/certifier/canary"
_READINESS_PATH = f"{_CANARY_PATH}/readiness"
_MAX_BODY_BYTES = 8 << 20
_MAX_READINESS_BYTES = 64 << 10
_SHA256 = re.compile(r"[0-9a-f]{64}")
# The scorer bounds its own probe at 20 seconds.
_READINESS_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# The lease deadline is the only bound on a certify call; asyncio.timeout
# enforces it for the whole exchange. There is deliberately no read timeout: a
# long, legitimate certification streams nothing until it finishes. Connecting,
# writing the request, and waiting for a pooled connection stay short.
_CERTIFY_TIMEOUT = httpx.Timeout(None, connect=10.0, write=60.0, pool=10.0)


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _CanaryResponse(_WireModel):
    schema_name: str = Field(alias="schema")
    lease_id: UUID
    capabilities_revoked: bool
    harness_destroyed: bool
    receipt: dict[str, Any]

    @model_validator(mode="after")
    def response_is_terminal(self) -> _CanaryResponse:
        if (
            self.schema_name != _RESPONSE_SCHEMA
            or self.lease_id.int == 0
            or not self.capabilities_revoked
            or not self.harness_destroyed
        ):
            raise ValueError("coding canary response is invalid")
        return self


class _CanaryReadiness(_WireModel):
    schema_name: str = Field(alias="schema")
    coding_contract_version: int
    weight_eligible: bool
    ready: bool
    failure: str
    pack_loaded: bool
    executor_daemon_ready: bool
    runtime_image_ready: bool
    canary_manifest_sha256: str
    runner_plan_sha256: str
    grader_plan_sha256: str
    resource_profile_sha256: str
    inference_policy_sha256: str

    def identity(self) -> CodingCanaryReadiness:
        digests = (
            self.canary_manifest_sha256,
            self.runner_plan_sha256,
            self.grader_plan_sha256,
            self.resource_profile_sha256,
            self.inference_policy_sha256,
        )
        if (
            self.schema_name != _READINESS_SCHEMA
            or self.coding_contract_version != 1
            or self.weight_eligible
            or not self.ready
            or self.failure
            or not self.pack_loaded
            or not self.executor_daemon_ready
            or not self.runtime_image_ready
            or any(_SHA256.fullmatch(digest) is None for digest in digests)
        ):
            raise ValueError("coding canary readiness is not ready")
        return CodingCanaryReadiness(*digests)


class CodingCanaryRuntime:
    """Call the protected scorer canary control plane."""

    def __init__(
        self,
        config: ValidatorConfig,
        client: httpx.AsyncClient,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        token = config.dittobench_control_token
        if (
            not scorer_control_origin(config.dittobench_api_url)
            or not _valid_control_token(token)
            # The bearer and the broker private key travel on this client; an
            # inherited proxy setting must never receive them.
            or getattr(client, "trust_env", True)
        ):
            raise ValueError("coding canary runtime configuration is invalid")
        self._base = config.dittobench_api_url.rstrip("/")
        self._token = token
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    async def require_ready(self) -> CodingCanaryReadiness:
        """Return the scorer's ready pack identity or refuse.

        This is read-only on the scorer: no harness, container, grant, or lease.
        Anything but an explicit, complete ready answer is a refusal, so the
        worker never issues or claims a lease against a scorer whose dedicated
        rootless daemon, runtime image, or pack would fail certify afterwards.
        """

        try:
            response = await self._client.get(
                f"{self._base}{_READINESS_PATH}",
                headers=self._headers(),
                follow_redirects=False,
                timeout=_READINESS_TIMEOUT,
            )
        except httpx.HTTPError as error:
            raise PlatformInfrastructureError(
                "coding canary runtime is unreachable"
            ) from error
        if (
            response.status_code != 200
            or not _private_json_headers(response.headers)
            or len(response.content) > _MAX_READINESS_BYTES
        ):
            raise PlatformInfrastructureError(
                f"coding canary runtime is unavailable ({response.status_code})"
            )
        try:
            readiness = _CanaryReadiness.model_validate_json(response.content)
        except (ValidationError, ValueError) as error:
            raise PlatformInfrastructureError(
                "coding canary runtime readiness is invalid"
            ) from error
        try:
            return readiness.identity()
        except ValueError as error:
            failure = readiness.failure if readiness.failure.isidentifier() else ""
            raise PlatformInfrastructureError(
                f"coding canary runtime is not ready (failure={failure or 'unknown'})"
            ) from error

    async def certify(
        self,
        lease: CodingCertificationLeaseResponse,
        harness: CodingCertificationHarnessLaunchResponse,
        grant: CodingCertificationInferenceExchangeResponse,
        *,
        broker_public_key: str,
        broker_private_key: str,
    ) -> CodingCanaryOutcome:
        if (
            grant.lease_id != lease.authority.lease_id
            or grant.weight_eligible
            or grant.status != "active"
        ):
            raise ValidatorInfrastructureError(
                "coding canary inference grant identity is invalid"
            )
        payload = {
            "schema": _REQUEST_SCHEMA,
            "operation_id": str(uuid4()),
            "lease_id": str(lease.authority.lease_id),
            "deadline": lease.authority.deadline.isoformat().replace("+00:00", "Z"),
            "agent_id": str(lease.authority.agent_id),
            "agent_artifact_sha256": lease.authority.agent_artifact_sha256,
            "screened_image_sha256": lease.authority.screened_image_sha256,
            "screened_image_id": harness.screened_image_id,
            "screened_image_ref": harness.screened_image_ref,
            "screened_image_upload_id": str(lease.screened_image_upload_id),
            "screened_image_size_bytes": harness.screened_image_size_bytes,
            "screening_policy_version": harness.screening_policy_version,
            "image_url": harness.image_url,
            "image_expires_at": harness.expires_at.isoformat().replace("+00:00", "Z"),
            "bench_version": lease.authority.bench_version,
            "canary_manifest_sha256": lease.authority.canary_manifest_sha256,
            "runner_plan_sha256": lease.authority.runner_plan_sha256,
            "grader_plan_sha256": lease.authority.grader_plan_sha256,
            "resource_profile_sha256": lease.authority.resource_profile_sha256,
            "inference_policy_sha256": lease.authority.inference_policy_sha256,
            "coding_contract_version": 1,
            "weight_eligible": False,
            "grant": {
                **grant.model_dump(mode="json", by_alias=True),
                "broker_public_key": broker_public_key,
                "broker_private_key": broker_private_key,
            },
        }
        budget = self._certify_budget_seconds(lease)
        body = bytearray()
        try:
            # Single shot: no retry. Leaving this block for any reason, including
            # the deadline or task cancellation, closes the stream so the scorer
            # observes the disconnect and tears the harness down.
            async with asyncio.timeout(budget):
                async with self._client.stream(
                    "POST",
                    f"{self._base}{_CANARY_PATH}",
                    headers=self._headers(),
                    json=payload,
                    follow_redirects=False,
                    timeout=_CERTIFY_TIMEOUT,
                ) as response:
                    if response.status_code != 200:
                        raise ValidatorInfrastructureError(
                            f"coding canary runtime rejected ({response.status_code})"
                        )
                    if not _private_json_headers(response.headers):
                        raise ValidatorInfrastructureError(
                            "coding canary runtime cache policy is invalid"
                        )
                    async for chunk in response.aiter_bytes(chunk_size=16 << 10):
                        if len(body) + len(chunk) > _MAX_BODY_BYTES:
                            raise ValidatorInfrastructureError(
                                "coding canary runtime response size is invalid"
                            )
                        body.extend(chunk)
        except TimeoutError as error:
            raise ValidatorInfrastructureError(
                "coding canary runtime deadline exceeded"
            ) from error
        except httpx.HTTPError as error:
            raise ValidatorInfrastructureError(
                "coding canary runtime request failed"
            ) from error
        try:
            parsed = _CanaryResponse.model_validate_json(body)
            receipt = CodingCapabilityCertificationReceipt.model_validate_json(
                json.dumps(parsed.receipt, separators=(",", ":"))
            )
        except (ValidationError, ValueError, json.JSONDecodeError) as error:
            raise ValidatorInfrastructureError(
                "coding canary runtime response is invalid"
            ) from error
        if parsed.lease_id != lease.authority.lease_id:
            raise ValidatorInfrastructureError(
                "coding canary runtime lease identity is invalid"
            )
        return CodingCanaryOutcome(
            authority=lease.authority,
            receipt=receipt,
            capabilities_revoked=True,
            harness_destroyed=True,
        )

    def _certify_budget_seconds(self, lease: CodingCertificationLeaseResponse) -> float:
        """Seconds left before the lease deadline, the certify call's only bound.

        Platform issues certification leases with a 20-minute deadline and the
        lease model rejects one more than 30 minutes after issue. The scorer
        stops the operation at the same deadline (and at its own 20-minute
        default), so no separate local cap applies.
        """

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidatorInfrastructureError("coding canary runtime clock is invalid")
        remaining = (
            lease.authority.deadline.astimezone(UTC) - now.astimezone(UTC)
        ).total_seconds()
        if remaining <= 0:
            raise ValidatorInfrastructureError(
                "coding canary lease deadline exceeded before certify"
            )
        return remaining

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        }


def _private_json_headers(headers: httpx.Headers) -> bool:
    return "no-store" in {
        directive.strip().lower()
        for directive in headers.get("Cache-Control", "").split(",")
    } and headers.get("Content-Type", "").lower().startswith("application/json")


def _valid_control_token(value: str) -> bool:
    return 32 <= len(value) <= 256 and all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in value
    )
