from __future__ import annotations

import base64
import hashlib
import json
from uuid import UUID

import httpx
import pytest

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
        elif operation == "pending":
            base["pending"] = []
        elif operation == "open":
            base["record_id"] = "11" * 32
            base["body_base64"] = base64.b64encode(_REQUEST).decode()
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
        assert await client.pending() == []
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
        "pending",
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
