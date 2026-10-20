"""Connect Ditto reuses provider consent without changing signed miner proofs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import bittensor
import pytest

from ditto.api_server.ditto_link import DittoLinkClient
from ditto.chain.models import BlockInfo
from ditto.tests.api_server.endpoints.test_miner_ditto_link import (
    FakeProvider,
    _accept_params,
    _config,
    _decide,
    _install,
    _sign_in,
)
from ditto.tests.api_server.endpoints.test_miner_github_link import signed_link

pytestmark = pytest.mark.asyncio


async def test_connect_ditto_then_sign_the_same_github_proof(
    app, client, session_maker
):
    _install(app, session_maker)
    provider = FakeProvider()
    app.state.ditto_link = DittoLinkClient(_config(), transport=provider.transport())
    owner = bittensor.Keypair.create_from_uri("//Bob").ss58_address
    app.state.chain = SimpleNamespace(
        get_finalized_block=AsyncMock(
            return_value=BlockInfo(number=123, hash="0x" + "ab" * 32)
        ),
        get_coldkey_for_hotkey=AsyncMock(return_value=owner),
    )
    key = bittensor.Keypair.create_from_uri("//Alice")
    auth = {"authorization": f"Bearer {await _sign_in(client, key)}"}
    started = await client.post(
        "/api/v1/me/ditto-link/start", headers=auth, json={"client": "cli"}
    )
    attempt = started.json()["attempt_id"]
    query = parse_qs(urlparse(started.json()["authorize_url"]).query)
    assert "connection:github:read" in query["scope"][0].split()
    provider.nonce_for_code["connect-code"] = query["nonce"][0]
    callback = await client.get(
        "/api/v1/miner-auth/ditto/callback",
        params={"code": "connect-code", "state": query["state"][0]},
    )
    body = signed_link(key, attempt, user_id=123, login="miner")
    early = await client.post("/api/v1/me/github-link/confirm", headers=auth, json=body)
    assert early.status_code == 409
    assert (
        await _decide(client, _accept_params(callback), "accept")
    ).status_code == 303
    early = await client.post("/api/v1/me/github-link/confirm", headers=auth, json=body)
    assert early.status_code == 409
    linked = await client.post(
        f"/api/v1/me/ditto-link/attempts/{attempt}/confirm", headers=auth
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["github_proof_attempt_id"] == attempt
    signed = await client.post(
        "/api/v1/me/github-link/confirm", headers=auth, json=body
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["github_user_id"] == 123
    assert signed.json()["signature"] == body["proof"]["signature"]
    assert signed.json()["owner_block"] == 123
    assert len(provider.token_requests) == 1
    replay = await client.post(
        "/api/v1/me/github-link/confirm", headers=auth, json=body
    )
    assert replay.status_code == 409


@pytest.mark.parametrize("wrong_subject", [False, True])
async def test_missing_or_other_accounts_connection_never_becomes_a_proof(
    app, client, session_maker, wrong_subject
):
    _install(app, session_maker)
    provider = FakeProvider()
    provider.connection_ready = wrong_subject
    provider.connection_subject = "another-account" if wrong_subject else None
    app.state.ditto_link = DittoLinkClient(_config(), transport=provider.transport())
    key = bittensor.Keypair.create_from_uri("//Alice")
    auth = {"authorization": f"Bearer {await _sign_in(client, key)}"}
    started = await client.post(
        "/api/v1/me/ditto-link/start", headers=auth, json={"client": "cli"}
    )
    query = parse_qs(urlparse(started.json()["authorize_url"]).query)
    provider.nonce_for_code["bad-code"] = query["nonce"][0]
    callback = await client.get(
        "/api/v1/miner-auth/ditto/callback",
        params={"code": "bad-code", "state": query["state"][0]},
    )
    assert "ditto=error" in callback.headers["location"]
    proof = await client.get(
        f"/api/v1/me/github-link/attempts/{started.json()['attempt_id']}", headers=auth
    )
    assert proof.status_code == 404
