from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import bittensor
import httpx
import pytest

from ditto.api_models.coding_claims import CodingClaimResponse
from ditto.api_models.coding_evidence_upload import (
    CodingSealedEvidenceFinalization,
    CodingSealedEvidenceKind,
    CodingSealedEvidenceUploadCapability,
)
from ditto.validator.coding_evidence_uploader import CodingSealedEvidenceUploader
from ditto.validator.coding_publication import CodingPublicationClient
from ditto.validator.errors import PlatformInfrastructureError

_TOKEN = "coding-publication-control-token-0000000000000001"
_BODY = b'{"sequence":1}\n'
_SHA256 = hashlib.sha256(_BODY).hexdigest()
_RECORD_ID = "11" * 32
_TICKET_ID = UUID("33333333-3333-4333-8333-333333333333")
_UPLOAD_ID = UUID("55555555-5555-4555-8555-555555555555")
_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _claim() -> CodingClaimResponse:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    return CodingClaimResponse.model_validate(
        {
            "schema": "dittobench-coding-ticket-claim-v1",
            "coding_contract_version": 1,
            "weight_eligible": False,
            "validator_hotkey": keypair.ss58_address,
            "instance_id": "coding-worker-instance-001",
            "claim_generation": 7,
            "claim_expires_at": _NOW + timedelta(minutes=2),
            "claim_started_at": _NOW,
            "idempotent": False,
            "agent_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "run_row_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "ticket_id": _TICKET_ID,
            "ticket_deadline": _NOW + timedelta(hours=1),
            "bench_version": 12,
            "coding_run_id": "coding-run-001",
            "agent_artifact_sha256": "aa" * 32,
            "screened_image_sha256": "bb" * 32,
            "run_manifest_sha256": "cc" * 32,
            "task_set_manifest_sha256": "dd" * 32,
        }
    )


def _capability() -> CodingSealedEvidenceUploadCapability:
    return CodingSealedEvidenceUploadCapability(
        schema="dittobench-coding-sealed-evidence-upload-capability-v1",
        coding_contract_version=1,
        weight_eligible=False,
        ticket_id=_TICKET_ID,
        claim_generation=7,
        ticket_deadline=_NOW + timedelta(hours=1),
        upload_id=_UPLOAD_ID,
        evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
        sha256=_SHA256,
        size_bytes=len(_BODY),
        content_type="application/octet-stream",
        checksum_sha256_b64=base64.b64encode(bytes.fromhex(_SHA256)).decode(),
        url=(
            "https://storage.test/coding-evidence/v1/authoring-transcript/"
            f"sha256/{_SHA256}?X-Amz-Date=20260901T120000Z"
            "&X-Amz-Expires=120&X-Amz-Signature=synthetic"
        ),
        expires_at=_NOW + timedelta(minutes=2),
    )


class _Platform:
    def __init__(self) -> None:
        self.capability = _capability()
        self.finalize_calls = 0

    async def request_coding_evidence_upload_capability(
        self,
        claim: CodingClaimResponse,
        *,
        evidence_kind: CodingSealedEvidenceKind,
        sha256: str,
        size_bytes: int,
    ) -> CodingSealedEvidenceUploadCapability:
        assert claim.ticket_id == _TICKET_ID
        assert evidence_kind is CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT
        assert sha256 == _SHA256 and size_bytes == len(_BODY)
        return self.capability

    async def finalize_coding_evidence_upload(
        self,
        claim: CodingClaimResponse,
        capability: CodingSealedEvidenceUploadCapability,
    ) -> CodingSealedEvidenceFinalization:
        assert claim.ticket_id == _TICKET_ID and capability == self.capability
        self.finalize_calls += 1
        return CodingSealedEvidenceFinalization(
            schema="dittobench-coding-sealed-evidence-finalized-v1",
            coding_contract_version=1,
            weight_eligible=False,
            ticket_id=_TICKET_ID,
            claim_generation=7,
            upload_id=_UPLOAD_ID,
            evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
            sha256=_SHA256,
            size_bytes=len(_BODY),
            finalized_at=_NOW,
            accepted=True,
            idempotent=False,
        )


def _local_handler(body: bytes = _BODY):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["record_id"] == _RECORD_ID
        return httpx.Response(
            200,
            headers={
                "Cache-Control": "no-store",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(_BODY)),
                "X-Ditto-Evidence-Kind": "authoring-transcript",
                "X-Ditto-Evidence-SHA256": _SHA256,
            },
            content=body,
        )

    return handler


async def test_uploader_streams_exact_headers_then_finalizes() -> None:
    platform = _Platform()
    observed: list[bytes] = []

    async def storage_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.headers["Content-Length"] == str(len(_BODY))
        assert request.headers["Content-Type"] == "application/octet-stream"
        assert request.headers["X-Amz-Meta-Sha256"] == _SHA256
        assert request.headers["X-Amz-Meta-Evidence-Kind"] == "authoring-transcript"
        assert (
            request.headers["X-Amz-Checksum-SHA256"]
            == _capability().checksum_sha256_b64
        )
        observed.append(await request.aread())
        return httpx.Response(200)

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(_local_handler()), trust_env=False
        ) as local_http,
        httpx.AsyncClient(
            transport=httpx.MockTransport(storage_handler), trust_env=False
        ) as storage_http,
    ):
        outbox = CodingPublicationClient(
            base_url="http://127.0.0.1:18081",
            control_token=_TOKEN,
            client=local_http,
        )
        uploader = CodingSealedEvidenceUploader(
            platform=platform,
            outbox=outbox,
            storage_client=storage_http,
            clock=lambda: _NOW,
        )
        capability = await uploader.reserve(
            _claim(),
            evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
            sha256=_SHA256,
            size_bytes=len(_BODY),
        )
        assert observed == []
        finalized = await uploader.upload_reserved(
            _claim(),
            record_id=_RECORD_ID,
            capability=capability,
        )
    assert observed == [_BODY]
    assert platform.finalize_calls == 1
    assert finalized.upload_id == _UPLOAD_ID
    assert _capability().url not in repr(uploader)


@pytest.mark.parametrize("failure", ["redirect", "corrupt"])
async def test_uploader_never_finalizes_rejected_or_corrupt_upload(
    failure: str,
) -> None:
    platform = _Platform()

    async def storage_handler(request: httpx.Request) -> httpx.Response:
        _ = await request.aread()
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://evil.invalid/"})
        return httpx.Response(200)

    local_body = _BODY if failure == "redirect" else b"x" * len(_BODY)
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(_local_handler(local_body)), trust_env=False
        ) as local_http,
        httpx.AsyncClient(
            transport=httpx.MockTransport(storage_handler), trust_env=False
        ) as storage_http,
    ):
        uploader = CodingSealedEvidenceUploader(
            platform=platform,
            outbox=CodingPublicationClient(
                base_url="http://127.0.0.1:18081",
                control_token=_TOKEN,
                client=local_http,
            ),
            storage_client=storage_http,
            clock=lambda: _NOW,
        )
        with pytest.raises(PlatformInfrastructureError):
            await uploader.upload(
                _claim(),
                record_id=_RECORD_ID,
                evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
                sha256=_SHA256,
                size_bytes=len(_BODY),
            )
    assert platform.finalize_calls == 0


async def test_uploader_rejects_expired_capability_before_opening_stream() -> None:
    platform = _Platform()
    platform.capability = platform.capability.model_copy(update={"expires_at": _NOW})
    local_calls = 0

    def local_handler(_: httpx.Request) -> httpx.Response:
        nonlocal local_calls
        local_calls += 1
        raise AssertionError("expired capability opened local evidence")

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(local_handler), trust_env=False
        ) as local_http,
        httpx.AsyncClient(
            transport=httpx.MockTransport(local_handler), trust_env=False
        ) as storage_http,
    ):
        uploader = CodingSealedEvidenceUploader(
            platform=platform,
            outbox=CodingPublicationClient(
                base_url="http://127.0.0.1:18081",
                control_token=_TOKEN,
                client=local_http,
            ),
            storage_client=storage_http,
            clock=lambda: _NOW,
        )
        with pytest.raises(PlatformInfrastructureError, match="authority"):
            await uploader.upload(
                _claim(),
                record_id=_RECORD_ID,
                evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
                sha256=_SHA256,
                size_bytes=len(_BODY),
            )
    assert local_calls == 0
    assert platform.finalize_calls == 0
