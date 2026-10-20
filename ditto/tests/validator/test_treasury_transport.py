"""Actual identity-scoped HTTP guard discovery; no RPC or production token."""

import httpx
import pytest

from ditto.chain.client import ChainClient
from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import ChainConfig
from ditto.tests.validator.test_treasury_weights import fixture


@pytest.mark.parametrize("status", [200, 404, 405, 501, 401, 503])
async def test_guard_probe_uses_identity_auth_and_never_claims_unsupported_transport(
    monkeypatch, status
):
    p, _ = fixture()
    capability = p.fleet[0].model_dump(exclude={"validator_hotkey", "protocol_version"})
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.path == (
            "/api/_unstable/identity/test%2Fvalidator/subnet/118/ditto/treasury-capability"
        ).replace("%2F", "/")
        assert request.url.raw_path == (
            b"/api/_unstable/identity/test%2Fvalidator/subnet/118/ditto/treasury-capability"
        )
        assert request.headers["authorization"] == "Bearer test-only-token"
        return httpx.Response(status, json={"treasury": capability})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "ditto.chain.client.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    client = ChainClient(
        ChainConfig(
            pylon_url="http://pylon.invalid",
            identity_name="test/validator",
            identity_token="test-only-token",
            netuid=118,
        )
    )
    if status in (401, 503):
        with pytest.raises(ChainConnectionError):
            await client.get_treasury_weight_capability()
    else:
        actual = await client.get_treasury_weight_capability()
        assert (actual.model_dump() if actual else None) == (
            capability if status == 200 else None
        )
    assert len(calls) == 1


@pytest.mark.parametrize("raw", [[], {"treasury": {}}, {"treasury": "yes"}])
async def test_malformed_guard_response_refuses(monkeypatch, raw):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "ditto.chain.client.httpx.AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw)),
            **kwargs,
        ),
    )
    client = ChainClient(
        ChainConfig(
            pylon_url="http://pylon.invalid",
            identity_name="validator",
            identity_token="test-only-token",
            netuid=118,
        )
    )
    with pytest.raises(ValueError):
        await client.get_treasury_weight_capability()
