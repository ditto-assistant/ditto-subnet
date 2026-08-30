"""DittoBench control-plane client for one public certification canary."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ditto.api_models.coding import CodingCapabilityCertificationReceipt
from ditto.api_models.coding_certification_leases import (
    CodingCertificationHarnessLaunchResponse,
    CodingCertificationLeaseResponse,
)
from ditto.validator.coding_canary import CodingCanaryOutcome
from ditto.validator.config import ValidatorConfig
from ditto.validator.errors import (
    PlatformInfrastructureError,
    ValidatorInfrastructureError,
)

_REQUEST_SCHEMA = "dittobench-coding-certification-canary-request-v1"
_RESPONSE_SCHEMA = "dittobench-coding-certification-canary-response-v1"
_MAX_BODY_BYTES = 8 << 20


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


class CodingCanaryRuntime:
    """Call the protected scorer canary control plane."""

    def __init__(
        self,
        config: ValidatorConfig,
        client: httpx.AsyncClient,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        parsed = urlsplit(config.dittobench_api_url)
        token = config.dittobench_control_token
        if (
            not _tls_or_loopback(parsed.scheme, parsed.hostname)
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not _valid_control_token(token)
        ):
            raise ValueError("coding canary runtime configuration is invalid")
        self._base = config.dittobench_api_url.rstrip("/")
        self._token = token
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    async def require_available(self) -> None:
        try:
            response = await self._client.post(
                f"{self._base}/v1/coding/certifier/canary",
                headers=self._headers(),
                json={},
            )
        except httpx.HTTPError as error:
            raise PlatformInfrastructureError(
                "coding canary runtime is unreachable"
            ) from error
        if response.status_code in {401, 404, 503} or response.status_code >= 500:
            raise PlatformInfrastructureError("coding canary runtime is unavailable")
        if not _private_json_headers(response.headers):
            raise PlatformInfrastructureError("coding canary runtime is unavailable")

    async def certify(
        self,
        lease: CodingCertificationLeaseResponse,
        harness: CodingCertificationHarnessLaunchResponse,
    ) -> CodingCanaryOutcome:
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
        }
        body = bytearray()
        try:
            async with self._client.stream(
                "POST",
                f"{self._base}/v1/coding/certifier/canary",
                headers=self._headers(),
                json=payload,
                follow_redirects=False,
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


def _tls_or_loopback(scheme: str, hostname: str | None) -> bool:
    if hostname is None or hostname == "":
        return False
    if scheme == "https":
        return True
    if scheme != "http":
        return False
    host = hostname.casefold()
    if host in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _valid_control_token(value: str) -> bool:
    return 32 <= len(value) <= 256 and all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in value
    )
