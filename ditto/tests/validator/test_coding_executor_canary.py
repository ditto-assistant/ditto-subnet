from __future__ import annotations

import httpx
import pytest

from ditto.validator.coding_executor_canary import CodingExecutorConnectivityCanary
from ditto.validator.errors import ValidatorInfrastructureError

_BASE = "https://10.23.0.10:9443"


def _response(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "dittobench-coding-executor-readiness-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "transport": "mtls",
        "supervisor_ready": True,
        "publication_ready": True,
        "ticket_authority_used": False,
    }
    value.update(updates)
    return value


async def test_canary_probes_readiness_without_ticket_or_signing_authority() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/coding/ready"
        assert "X-Dittobench-Coding-Control" not in request.headers
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        assert request.content == b""
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
            json=_response(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as client:
        canary = CodingExecutorConnectivityCanary(base_url=_BASE, client=client)
        await canary.probe()
    assert len(observed) == 1
    assert "private=True" in repr(canary)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(307, headers={"Location": "https://evil.invalid"}),
        httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
            json=_response(ticket_authority_used=True),
        ),
        httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
            content=b"x" * ((4 << 10) + 1),
        ),
    ],
)
async def test_canary_rejects_transport_and_response_drift(
    response: httpx.Response,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response), trust_env=False
    ) as client:
        canary = CodingExecutorConnectivityCanary(base_url=_BASE, client=client)
        with pytest.raises(ValidatorInfrastructureError):
            await canary.probe()


@pytest.mark.parametrize("base_url", ["https://8.8.8.8:9443", "https://192.0.2.1:9443"])
async def test_canary_rejects_public_or_reserved_origins(base_url: str) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(ValueError):
            CodingExecutorConnectivityCanary(base_url=base_url, client=client)


async def test_canary_rejects_proxy_inheriting_clients() -> None:
    async with httpx.AsyncClient(trust_env=True) as client:
        with pytest.raises(ValueError):
            CodingExecutorConnectivityCanary(base_url=_BASE, client=client)
