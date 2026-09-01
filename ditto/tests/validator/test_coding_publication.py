from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from ditto.api_models.coding_evidence_upload import (
    CodingSealedEvidenceFinalization,
    CodingSealedEvidenceKind,
)
from ditto.validator.coding_publication import (
    CodingPublicationClient,
    PublicationAuthority,
)
from ditto.validator.errors import PlatformInfrastructureError

_TOKEN = "coding-publication-control-token-0000000000000001"
_REQUEST = b'{"signed":"request"}\n'
_ACK = b'{"accepted":true}\n'
_REQUEST_SHA256 = hashlib.sha256(_REQUEST).hexdigest()
_ACK_SHA256 = hashlib.sha256(_ACK).hexdigest()


def _authority() -> PublicationAuthority:
    return PublicationAuthority(
        agent_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        bench_version=12,
        run_row_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        coding_run_id="coding-run-001",
        screened_image_sha256="aa" * 32,
        run_manifest_sha256="bb" * 32,
        task_set_manifest_sha256="cc" * 32,
        evidence_sha256="dd" * 32,
    )


def _finalization() -> CodingSealedEvidenceFinalization:
    return CodingSealedEvidenceFinalization(
        schema="dittobench-coding-sealed-evidence-finalized-v1",
        coding_contract_version=1,
        weight_eligible=False,
        ticket_id=UUID("33333333-3333-4333-8333-333333333333"),
        claim_generation=3,
        upload_id=UUID("44444444-4444-4444-8444-444444444444"),
        evidence_kind=(CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT),
        sha256=_ACK_SHA256,
        size_bytes=len(_ACK),
        finalized_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        accepted=True,
        idempotent=False,
    )


async def test_publication_client_preserves_exact_request_and_acknowledgement() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
        operation = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        calls.append((operation, payload))
        base: dict[str, object] = {
            "schema": "dittobench-coding-publication-result-v1",
            "coding_contract_version": 1,
            "weight_eligible": False,
            "operation": operation,
        }
        if operation == "prepare":
            assert base64.b64decode(payload["body_base64"], validate=True) == _REQUEST
            base.update(
                {
                    "record_id": "11" * 32,
                    "artifact": {
                        "object_key": "sha256/" + _REQUEST_SHA256,
                        "sha256": _REQUEST_SHA256,
                        "size_bytes": len(_REQUEST),
                    },
                }
            )
        elif operation == "acknowledge":
            assert base64.b64decode(payload["body_base64"], validate=True) == _ACK
            base["record_id"] = "11" * 32
            base["artifact"] = {
                "object_key": "sha256/" + _ACK_SHA256,
                "sha256": _ACK_SHA256,
                "size_bytes": len(_ACK),
            }
        elif operation == "release":
            assert payload == {
                "schema": "dittobench-coding-publication-command-v1",
                "ticket_id": "33333333-3333-4333-8333-333333333333",
                "record_id": "11" * 32,
                "terminal_evidence_sha256": "dd" * 32,
                "finalization": _finalization().model_dump(mode="json", by_alias=True),
            }
            base["record_id"] = "11" * 32
        elif operation == "pending":
            base["pending"] = []
        elif operation == "open":
            base["record_id"] = "11" * 32
            base["body_base64"] = base64.b64encode(_REQUEST).decode()
        elif operation == "lookup":
            base["record_id"] = "11" * 32
            base["publication"] = {
                "record_id": "11" * 32,
                "ticket_id": "33333333-3333-4333-8333-333333333333",
                "stage": "terminal_result",
                "authority": _authority().model_dump(mode="json"),
                "request": {
                    "object_key": "sha256/" + _REQUEST_SHA256,
                    "sha256": _REQUEST_SHA256,
                    "size_bytes": len(_REQUEST),
                },
                "acknowledgement": None,
            }
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json=base,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        client = CodingPublicationClient(
            base_url="http://127.0.0.1:18081",
            control_token=_TOKEN,
            client=http,
        )
        record_id, artifact = await client.prepare(
            ticket_id="33333333-3333-4333-8333-333333333333",
            stage="terminal_result",
            authority=_authority(),
            body=_REQUEST,
        )
        assert record_id == "11" * 32 and artifact.sha256 == _REQUEST_SHA256
        acknowledged = await client.acknowledge(
            ticket_id="33333333-3333-4333-8333-333333333333",
            stage="terminal_result",
            request_sha256=_REQUEST_SHA256,
            body=_ACK,
        )
        assert acknowledged.sha256 == _ACK_SHA256
        await client.release(
            ticket_id="33333333-3333-4333-8333-333333333333",
            record_id=record_id,
            terminal_evidence_sha256="dd" * 32,
            finalization=_finalization(),
        )
        assert await client.pending() == []
        looked_up = await client.lookup(
            ticket_id="33333333-3333-4333-8333-333333333333",
            stage="terminal_result",
        )
        assert looked_up.record_id == record_id
        assert (
            await client.open(
                record_id=record_id,
                stage="terminal_result",
                expected=artifact,
            )
            == _REQUEST
        )
        with pytest.raises(PlatformInfrastructureError, match="identity"):
            await client.open(
                record_id=record_id,
                stage="terminal_result",
                expected=artifact.model_copy(update={"sha256": "44" * 32}),
            )
    assert [operation for operation, _ in calls] == [
        "prepare",
        "acknowledge",
        "release",
        "pending",
        "lookup",
        "open",
        "open",
    ]


async def test_publication_client_rejects_redirects_bad_identity_and_secret_leaks() -> (
    None
):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json={
                "schema": "dittobench-coding-publication-result-v1",
                "coding_contract_version": 1,
                "weight_eligible": False,
                "operation": "open",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        client = CodingPublicationClient(
            base_url="http://127.0.0.1:18081",
            control_token=_TOKEN,
            client=http,
        )
        with pytest.raises(PlatformInfrastructureError) as captured:
            await client.pending()
    assert _TOKEN not in str(client)
    assert _TOKEN not in str(captured.value)
    with pytest.raises(ValueError):
        CodingPublicationClient(
            base_url="http://0.0.0.0:18081",
            control_token=_TOKEN,
            client=http,
        )


async def test_publication_client_streams_and_verifies_sealed_evidence() -> None:
    ticket_id = "33333333-3333-4333-8333-333333333333"
    record_id = "11" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/coding/evidence/open"
        assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
        payload = json.loads(request.content)
        assert payload == {
            "schema": "dittobench-coding-sealed-evidence-open-command-v1",
            "ticket_id": ticket_id,
            "record_id": record_id,
            "evidence_kind": "terminal-publication-request",
            "sha256": _REQUEST_SHA256,
            "size_bytes": len(_REQUEST),
        }
        return httpx.Response(
            200,
            headers={
                "Cache-Control": "no-store",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(_REQUEST)),
                "X-Ditto-Evidence-Kind": "terminal-publication-request",
                "X-Ditto-Evidence-SHA256": _REQUEST_SHA256,
            },
            content=_REQUEST,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        client = CodingPublicationClient(
            base_url="http://127.0.0.1:18081",
            control_token=_TOKEN,
            client=http,
        )
        async with client.stream_evidence(
            ticket_id=ticket_id,
            record_id=record_id,
            evidence_kind="terminal-publication-request",
            sha256=_REQUEST_SHA256,
            size_bytes=len(_REQUEST),
        ) as chunks:
            body = b"".join([chunk async for chunk in chunks])
        assert body == _REQUEST


async def test_publication_client_parses_canonical_evidence_manifest() -> None:
    ticket_id = "33333333-3333-4333-8333-333333333333"
    record_id = "11" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/coding/evidence/manifest"
        payload = json.loads(request.content)
        assert payload["record_id"] == record_id
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json={
                "schema": "dittobench-coding-sealed-evidence-manifest-v1",
                "coding_contract_version": 1,
                "weight_eligible": False,
                "ticket_id": ticket_id,
                "record_id": record_id,
                "evidence": [
                    {
                        "evidence_kind": "authoring-transcript",
                        "sha256": "aa" * 32,
                        "size_bytes": 4096,
                    },
                    {
                        "evidence_kind": "frozen-submission",
                        "sha256": "bb" * 32,
                        "size_bytes": 2048,
                    },
                    {
                        "evidence_kind": "authoring-publication-request",
                        "sha256": _REQUEST_SHA256,
                        "size_bytes": len(_REQUEST),
                    },
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        client = CodingPublicationClient(
            base_url="http://127.0.0.1:18081",
            control_token=_TOKEN,
            client=http,
        )
        manifest = await client.evidence_manifest(
            ticket_id=ticket_id,
            record_id=record_id,
        )
    assert [item.evidence_kind for item in manifest.evidence] == [
        "authoring-transcript",
        "frozen-submission",
        "authoring-publication-request",
    ]


async def test_publication_client_rejects_unconsumed_or_truncated_evidence() -> None:
    ticket_id = "33333333-3333-4333-8333-333333333333"
    record_id = "11" * 32

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Cache-Control": "no-store",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(_REQUEST)),
                "X-Ditto-Evidence-Kind": "terminal-publication-request",
                "X-Ditto-Evidence-SHA256": _REQUEST_SHA256,
            },
            content=_REQUEST[:-1],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        client = CodingPublicationClient(
            base_url="http://127.0.0.1:18081",
            control_token=_TOKEN,
            client=http,
        )
        with pytest.raises(PlatformInfrastructureError, match="identity"):
            async with client.stream_evidence(
                ticket_id=ticket_id,
                record_id=record_id,
                evidence_kind="terminal-publication-request",
                sha256=_REQUEST_SHA256,
                size_bytes=len(_REQUEST),
            ) as chunks:
                _ = b"".join([chunk async for chunk in chunks])
        with pytest.raises(PlatformInfrastructureError, match="identity"):
            async with client.stream_evidence(
                ticket_id=ticket_id,
                record_id=record_id,
                evidence_kind="terminal-publication-request",
                sha256=_REQUEST_SHA256,
                size_bytes=len(_REQUEST),
            ):
                pass
