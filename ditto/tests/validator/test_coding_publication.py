from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

from ditto.api_models.coding_executor_control import CodingExecutorControlEnvelope
from ditto.validator.coding_publication import (
    CodingExecutorRequestAuthority,
    CodingPublicationClient,
    PublicationAuthority,
)
from ditto.validator.errors import PlatformInfrastructureError

_TOKEN = "coding-publication-control-token-0000000000000001"
_REQUEST = b'{"signed":"request"}\n'
_ACK = b'{"accepted":true}\n'
_REQUEST_SHA256 = hashlib.sha256(_REQUEST).hexdigest()
_ACK_SHA256 = hashlib.sha256(_ACK).hexdigest()
_NOW = datetime(2026, 9, 2, 10, tzinfo=UTC)
_TICKET = UUID("33333333-3333-4333-8333-333333333333")
_HOTKEY = "5" + "V" * 47


class _Keypair:
    def sign(self, _: bytes) -> bytes:
        return b"\xab" * 64


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


def _executor_authority() -> CodingExecutorRequestAuthority:
    return CodingExecutorRequestAuthority(
        agent_id=_authority().agent_id,
        agent_artifact_sha256="ee" * 32,
        coding_run_id=_authority().coding_run_id,
        ticket_id=_TICKET,
        deadline=_NOW + timedelta(hours=1),
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
        "pending",
        "lookup",
        "open",
        "open",
    ]


async def test_remote_publication_client_signs_every_exact_operation() -> None:
    observed: list[CodingExecutorControlEnvelope] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        encoded = request.headers["X-Dittobench-Coding-Control"]
        envelope = CodingExecutorControlEnvelope.model_validate_json(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        payload = json.loads(request.content)
        operation = request.url.path.rsplit("/", 1)[-1]
        assert envelope.operation.value == f"publications.{operation}"
        assert (
            envelope.request_body_sha256 == hashlib.sha256(request.content).hexdigest()
        )
        assert envelope.agent_id == _authority().agent_id
        assert envelope.agent_artifact_sha256 == "ee" * 32
        assert envelope.coding_run_id == _authority().coding_run_id
        assert envelope.ticket_id == _TICKET
        assert payload["agent_id"] == str(_authority().agent_id)
        assert payload["agent_artifact_sha256"] == "ee" * 32
        assert payload["ticket_id"] == str(_TICKET)
        assert payload["coding_run_id"] == _authority().coding_run_id
        observed.append(envelope)
        result: dict[str, object] = {
            "schema": "dittobench-coding-publication-result-v1",
            "coding_contract_version": 1,
            "weight_eligible": False,
            "operation": operation,
        }
        if operation in {"prepare", "acknowledge"}:
            body = _REQUEST if operation == "prepare" else _ACK
            digest = hashlib.sha256(body).hexdigest()
            result.update(
                record_id="11" * 32,
                artifact={
                    "object_key": "sha256/" + digest,
                    "sha256": digest,
                    "size_bytes": len(body),
                },
            )
        elif operation == "pending":
            result["pending"] = []
        elif operation == "open":
            result.update(
                record_id="11" * 32,
                body_base64=base64.b64encode(_REQUEST).decode(),
            )
        else:
            result.update(
                record_id="11" * 32,
                publication={
                    "record_id": "11" * 32,
                    "ticket_id": str(_TICKET),
                    "stage": "terminal_result",
                    "authority": _authority().model_dump(mode="json"),
                    "request": {
                        "object_key": "sha256/" + _REQUEST_SHA256,
                        "sha256": _REQUEST_SHA256,
                        "size_bytes": len(_REQUEST),
                    },
                    "acknowledgement": None,
                },
            )
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json=result,
        )

    authority = _executor_authority()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        client = CodingPublicationClient(
            base_url="http://127.0.0.1:18081",
            control_token=None,
            client=http,
            keypair=_Keypair(),
            validator_hotkey=_HOTKEY,
            executor_base_url="https://10.23.0.10:9443",
            clock=lambda: _NOW,
        )
        record_id, artifact = await client.prepare(
            ticket_id=str(_TICKET),
            stage="terminal_result",
            authority=_authority(),
            body=_REQUEST,
            executor_authority=authority,
        )
        await client.acknowledge(
            ticket_id=str(_TICKET),
            stage="terminal_result",
            request_sha256=_REQUEST_SHA256,
            body=_ACK,
            executor_authority=authority,
        )
        await client.pending(limit=1, executor_authority=authority)
        await client.open(
            record_id=record_id,
            stage="terminal_result",
            expected=artifact,
            executor_authority=authority,
        )
        await client.lookup(
            ticket_id=str(_TICKET),
            stage="terminal_result",
            executor_authority=authority,
        )
    assert [item.operation.value for item in observed] == [
        "publications.prepare",
        "publications.acknowledge",
        "publications.pending",
        "publications.open",
        "publications.lookup",
    ]
    assert len({item.nonce for item in observed}) == 5


async def test_remote_publication_requires_live_matching_authority() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request")),
        trust_env=False,
    ) as http:
        client = CodingPublicationClient(
            base_url="http://127.0.0.1:18081",
            control_token=None,
            client=http,
            keypair=_Keypair(),
            validator_hotkey=_HOTKEY,
            executor_base_url="https://10.23.0.10:9443",
            clock=lambda: _NOW,
        )
        with pytest.raises(ValueError, match="authority is required"):
            await client.pending(limit=1)
        with pytest.raises(ValueError, match="disagrees"):
            await client.prepare(
                ticket_id=str(_TICKET),
                stage="terminal_result",
                authority=_authority(),
                body=_REQUEST,
                executor_authority=replace(
                    _executor_authority(),
                    agent_id=UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
                ),
            )
        with pytest.raises(ValueError, match="expired"):
            await client.pending(
                limit=1,
                executor_authority=CodingExecutorRequestAuthority(
                    agent_id=_authority().agent_id,
                    agent_artifact_sha256="ee" * 32,
                    coding_run_id=_authority().coding_run_id,
                    ticket_id=_TICKET,
                    deadline=_NOW,
                ),
            )


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
