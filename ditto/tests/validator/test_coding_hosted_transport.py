from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID

import bittensor
import httpx
import pytest

from ditto.api_models.coding_hosted import (
    HostedCodingRequest,
    HostedCodingResult,
    HostedCodingStatus,
    hosted_message_digest,
    hosted_signing_bytes,
)
from ditto.tests.validator.test_coding_hosted import NOW, _body, _case
from ditto.validator.coding_hosted import HostedResultExpectation
from ditto.validator.coding_hosted_transport import (
    HOSTED_CONTROL_PATH,
    HostedCodingTransport,
    HostedCodingTransportError,
)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, hang: bool = False):
        self.chunks = chunks
        self.hang = hang
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.hang:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


def _exchange_case() -> tuple[
    HostedCodingRequest, HostedCodingResult, HostedResultExpectation, bittensor.Keypair
]:
    result, expected, platform = _case()
    validator = bittensor.Keypair.create_from_uri("//Bob")
    request = HostedCodingRequest.model_validate(
        {
            "schema": "dittobench-coding-hosted-request-v2",
            "coding_contract_version": 2,
            "shadow_only": True,
            "weight_eligible": False,
            "evaluation_id": expected.evaluation_id,
            "validator_hotkey": expected.validator_hotkey,
            "artifact_sha256": expected.artifact_sha256,
            "assignment_sha256": expected.assignment_sha256,
            "policy_sha256": expected.policy_sha256,
            "operation": "status",
            "result_sha256": None,
            "nonce": UUID(int=10),
            "issued_at_unix": NOW,
            "expires_at_unix": NOW + 60,
            "signature": "0" * 128,
        }
    )
    request = request.model_copy(
        update={"signature": validator.sign(hosted_signing_bytes(request)).hex()}
    )
    digest = hosted_message_digest(request)
    result = result.model_copy(update={"request_sha256": digest})
    result = result.model_copy(
        update={"signature": platform.sign(hosted_signing_bytes(result)).hex()}
    )
    return request, result, replace(expected, request_sha256=digest), platform


def _response(
    body: bytes, *, headers: dict[str, str] | None = None, status: int = 200
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
            **(headers or {}),
        },
        stream=Chunks([body]),
    )


async def test_transport_only_returns_verified_projection() -> None:
    request, result, expected, key = _exchange_case()
    calls: list[httpx.Request] = []

    def respond(outgoing: httpx.Request) -> httpx.Response:
        calls.append(outgoing)
        assert outgoing.url == f"https://platform.example{HOSTED_CONTROL_PATH}"
        assert outgoing.headers["accept-encoding"] == "identity"
        assert outgoing.headers["cache-control"] == "no-store"
        assert outgoing.headers["x-validator-hotkey"] == expected.validator_hotkey
        assert HostedCodingRequest.model_validate_json(outgoing.content) == request
        return _response(
            _body(result), headers={"Content-Length": str(len(_body(result)))}
        )

    async with HostedCodingTransport(
        platform_origin="https://platform.example",
        trusted_verifiers={key.ss58_address: key},
        clock=lambda: NOW,
        transport=httpx.MockTransport(respond),
    ) as client:
        assert await client.exchange(request=request, expected=expected) == result
    assert len(calls) == 1


@pytest.mark.parametrize(
    "http_status,pending,accepted",
    [(202, True, True), (200, True, False), (202, False, False)],
)
async def test_transport_distinguishes_pending_status_from_terminal_result(
    http_status: int, pending: bool, accepted: bool
) -> None:
    request, result, expected, key = _exchange_case()
    projection: HostedCodingResult | HostedCodingStatus = result
    if pending:
        projection = HostedCodingStatus.model_validate(
            {
                **result.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude={"schema_", "outcome", "evidence_sha256"},
                ),
                "schema": "dittobench-coding-hosted-status-v2",
                "state": "admitted",
            }
        )
        projection = projection.model_copy(
            update={"signature": key.sign(hosted_signing_bytes(projection)).hex()}
        )
    async with HostedCodingTransport(
        platform_origin="https://platform.example",
        trusted_verifiers={key.ss58_address: key},
        clock=lambda: NOW,
        transport=httpx.MockTransport(
            lambda _: _response(_body(projection), status=http_status)
        ),
    ) as client:
        if accepted:
            actual = await client.exchange(request=request, expected=expected)
            assert actual == projection and isinstance(actual, HostedCodingStatus)
        else:
            with pytest.raises(HostedCodingTransportError):
                await client.exchange(request=request, expected=expected)


@pytest.mark.parametrize(
    "origin",
    [
        "http://platform.example",
        "https://user:password@platform.example",
        "https://platform.example/private",
        "https://platform.example?secret=PRIVATE_MARKER",
        "https://platform.example#fragment",
        "https://platform.example//",
    ],
)
def test_origin_is_trusted_canonical_https_only(origin: str) -> None:
    _, _, _, key = _exchange_case()
    with pytest.raises(HostedCodingTransportError) as caught:
        HostedCodingTransport(
            platform_origin=origin, trusted_verifiers={key.ss58_address: key}
        )
    assert "PRIVATE_MARKER" not in str(caught.value)


@pytest.mark.parametrize("status", [202, 302, 400, 401, 429, 500, 503])
async def test_nonterminal_http_response_is_not_retried_or_exposed(status: int) -> None:
    request, _, expected, key = _exchange_case()
    calls = 0
    stream = Chunks([b"PRIVATE_MARKER"])

    def respond(outgoing: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert outgoing.url.host == "platform.example"
        return httpx.Response(
            status,
            headers={"Location": "https://elsewhere.invalid/PRIVATE_MARKER"},
            stream=stream,
        )

    async with HostedCodingTransport(
        platform_origin="https://platform.example",
        trusted_verifiers={key.ss58_address: key},
        clock=lambda: NOW,
        transport=httpx.MockTransport(respond),
    ) as client:
        with pytest.raises(HostedCodingTransportError) as caught:
            await client.exchange(request=request, expected=expected)
    assert "PRIVATE_MARKER" not in str(caught.value)
    assert calls == 1 and stream.closed


@pytest.mark.parametrize(
    "headers",
    [
        {"Cache-Control": "public"},
        {"Content-Type": "text/plain"},
        {"Content-Encoding": "gzip"},
        {"Content-Length": "99999"},
        {"Content-Length": "12"},
        {"Content-Length": "-1"},
    ],
)
async def test_transport_rejects_unbounded_or_cacheable_responses(
    headers: dict[str, str],
) -> None:
    request, result, expected, key = _exchange_case()
    async with HostedCodingTransport(
        platform_origin="https://platform.example",
        trusted_verifiers={key.ss58_address: key},
        clock=lambda: NOW,
        transport=httpx.MockTransport(
            lambda _: _response(_body(result), headers=headers)
        ),
    ) as client:
        with pytest.raises(HostedCodingTransportError):
            await client.exchange(request=request, expected=expected)


async def test_actual_chunk_bytes_are_bounded_and_slow_response_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, expected, key = _exchange_case()
    monkeypatch.setattr(
        "ditto.validator.coding_hosted_transport.HOSTED_CONTROL_TIMEOUT_SECONDS", 0.01
    )
    for stream in (Chunks([b"x" * 4000, b"x" * 5000]), Chunks([b"{"], hang=True)):

        def respond(_: httpx.Request, body_stream: Chunks = stream) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "Cache-Control": "no-store",
                    "Content-Type": "application/json",
                },
                stream=body_stream,
            )

        async with HostedCodingTransport(
            platform_origin="https://platform.example",
            trusted_verifiers={key.ss58_address: key},
            clock=lambda: NOW,
            transport=httpx.MockTransport(respond),
        ) as client:
            with pytest.raises(HostedCodingTransportError):
                await client.exchange(request=request, expected=expected)
        assert stream.closed


async def test_request_mismatch_is_rejected_before_network() -> None:
    request, _, expected, key = _exchange_case()
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("request must not leave validator")

    async with HostedCodingTransport(
        platform_origin="https://platform.example",
        trusted_verifiers={key.ss58_address: key},
        clock=lambda: NOW,
        transport=httpx.MockTransport(respond),
    ) as client:
        with pytest.raises(HostedCodingTransportError):
            await client.exchange(
                request=request, expected=replace(expected, artifact_sha256="0" * 64)
            )
    assert calls == 0


async def test_valid_http_does_not_bypass_signature_verification() -> None:
    request, result, expected, key = _exchange_case()
    async with HostedCodingTransport(
        platform_origin="https://platform.example",
        trusted_verifiers={key.ss58_address: key},
        clock=lambda: NOW,
        transport=httpx.MockTransport(
            lambda _: _response(_body(result, evidence_sha256="0" * 64))
        ),
    ) as client:
        with pytest.raises(HostedCodingTransportError):
            await client.exchange(request=request, expected=expected)
