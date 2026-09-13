"""Sign in with Ditto for miners: link, callback binding, replay, revoke."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.api_server.ditto_link import DittoLinkClient, DittoLinkConfig
from ditto.api_server.miner_session import login_message
from ditto.tests.api_server.test_ditto_link import (
    CLIENT_ID,
    ISSUER,
    jwks_for,
    make_key,
    sign_id_token,
)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _config(**over: object) -> DittoLinkConfig:
    base: dict[str, object] = {
        "enabled": True,
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": "app-secret",
        "redirect_url": "https://dittobench.ai/api/v1/miner-auth/ditto/callback",
        "return_url": "https://dittobench.ai/#/reviews",
    }
    base.update(over)
    return DittoLinkConfig(**base)  # type: ignore[arg-type]


class FakeProvider:
    """A Ditto OpenID provider: JWKS + token endpoint minting id_tokens."""

    def __init__(self) -> None:
        self.key = make_key()
        self.nonce_for_code: dict[str, str] = {}
        self.token_requests: list[dict[str, list[str]]] = []
        self.subject = "ditto-user-1"

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/.well-known/jwks.json":
                return httpx.Response(200, json=jwks_for(self.key))
            if request.url.path == "/token":
                form = parse_qs(request.content.decode())
                self.token_requests.append(form)
                code = form.get("code", [""])[0]
                if code not in self.nonce_for_code:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                if form.get("client_secret") != ["app-secret"]:
                    return httpx.Response(400, json={"error": "invalid_client"})
                now = int(time.time())
                token = sign_id_token(
                    self.key,
                    {
                        "iss": ISSUER,
                        "aud": CLIENT_ID,
                        "sub": self.subject,
                        "nonce": self.nonce_for_code[code],
                        "iat": now,
                        "exp": now + 300,
                        "email": "miner@example.com",
                        "email_verified": True,
                    },
                )
                return httpx.Response(
                    200, json={"access_token": "at", "id_token": token}
                )
            return httpx.Response(404)

        return httpx.MockTransport(handler)


async def _sign_in(client: httpx.AsyncClient, keypair: bittensor.Keypair) -> str:
    started = await client.post(
        "/api/v1/miner-auth/device",
        json={"scopes": ["read", "profile"], "ttl_seconds": 3600},
    )
    assert started.status_code == 200, started.text
    user_code = started.json()["user_code"]
    public = await client.get(f"/api/v1/miner-auth/device/{user_code}")
    grant_id = public.json()["grant_id"]
    nonce = uuid4()
    issued_at = datetime.now(UTC)
    payload = login_message(
        netuid=118,
        miner_hotkey=keypair.ss58_address,
        user_code=user_code,
        grant_id=grant_id,
        ttl_seconds=3600,
        scopes="profile,read",
        nonce=nonce,
        issued_at=issued_at,
        key_kind="hotkey",
        signer=keypair.ss58_address,
    )
    approved = await client.post(
        f"/api/v1/miner-auth/device/{user_code}/approve",
        json={
            "netuid": 118,
            "miner_hotkey": keypair.ss58_address,
            "nonce": str(nonce),
            "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "proof": {
                "key_kind": "hotkey",
                "signer": keypair.ss58_address,
                "signature": keypair.sign(payload).hex(),
            },
        },
    )
    assert approved.status_code == 200, approved.text
    return str(approved.json()["access_token"])


def _callback_result(response: httpx.Response) -> dict[str, list[str]]:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("https://dittobench.ai/#/reviews")
    return parse_qs(urlparse(location.replace("#/", "/")).query)


async def test_link_binds_verified_ditto_identity_to_the_session_hotkey(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    provider = FakeProvider()
    app.state.ditto_link = DittoLinkClient(_config(), transport=provider.transport())
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    auth = {"authorization": f"Bearer {token}"}

    before = await client.get("/api/v1/me/ditto-link", headers=auth)
    assert before.status_code == 200
    assert before.json() == {"enabled": True, "link": None}

    started = await client.post(
        "/api/v1/me/ditto-link/start",
        headers=auth,
        json={"client": "dashboard", "return_to": "https://evil.example/#/reviews"},
    )
    assert started.status_code == 200, started.text
    body = started.json()
    query = parse_qs(urlparse(body["authorize_url"]).query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["code_challenge_method"] == ["S256"]
    state = query["state"][0]
    provider.nonce_for_code["code-1"] = query["nonce"][0]

    pending = await client.get(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}", headers=auth
    )
    assert pending.json()["status"] == "pending"

    # The public callback carries no bearer; the hashed state binds it to Alice.
    done = await client.get(
        "/api/v1/miner-auth/ditto/callback", params={"code": "code-1", "state": state}
    )
    result = _callback_result(done)
    assert result["ditto"] == ["linked"]
    # The token exchange used PKCE and the app secret, never the user's word.
    assert provider.token_requests[-1]["code_verifier"]
    assert provider.token_requests[-1]["grant_type"] == ["authorization_code"]

    linked = await client.get("/api/v1/me/ditto-link", headers=auth)
    assert linked.status_code == 200
    link = linked.json()["link"]
    assert link["miner_hotkey"] == alice.ss58_address
    assert link["ditto_user_id"] == "ditto-user-1"
    assert link["ditto_email"] == "miner@example.com"
    assert link["linked_via"] == "dashboard"

    attempt = await client.get(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}", headers=auth
    )
    assert attempt.json()["status"] == "linked"
    assert attempt.json()["link"]["ditto_user_id"] == "ditto-user-1"

    # Replaying the same callback cannot redeem twice.
    replay = await client.get(
        "/api/v1/miner-auth/ditto/callback", params={"code": "code-1", "state": state}
    )
    assert _callback_result(replay)["ditto"] == ["error"]
    assert len(provider.token_requests) == 1

    # Another hotkey's session sees neither the attempt nor the link.
    bob = bittensor.Keypair.create_from_uri("//Bob")
    bob_auth = {"authorization": f"Bearer {await _sign_in(client, bob)}"}
    other = await client.get(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}", headers=bob_auth
    )
    assert other.status_code == 404
    assert (await client.get("/api/v1/me/ditto-link", headers=bob_auth)).json()[
        "link"
    ] is None

    # Revoking is the miner's call and needs the profile scope.
    revoked = await client.delete("/api/v1/me/ditto-link", headers=auth)
    assert revoked.status_code == 204
    assert (await client.get("/api/v1/me/ditto-link", headers=auth)).json()[
        "link"
    ] is None


async def test_callback_rejects_denied_consent_unknown_state_and_bad_tokens(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    provider = FakeProvider()
    app.state.ditto_link = DittoLinkClient(_config(), transport=provider.transport())
    alice = bittensor.Keypair.create_from_uri("//Alice")
    auth = {"authorization": f"Bearer {await _sign_in(client, alice)}"}

    unknown = await client.get(
        "/api/v1/miner-auth/ditto/callback", params={"code": "x", "state": "nope"}
    )
    assert _callback_result(unknown)["reason"] == ["unknown link attempt"]

    # The user declined on Ditto's consent screen.
    started = await client.post(
        "/api/v1/me/ditto-link/start", headers=auth, json={"client": "cli"}
    )
    state = parse_qs(urlparse(started.json()["authorize_url"]).query)["state"][0]
    denied = await client.get(
        "/api/v1/miner-auth/ditto/callback",
        params={
            "state": state,
            "error": "access_denied",
            "error_description": "user declined",
        },
    )
    assert _callback_result(denied)["reason"] == ["user declined"]
    attempt = await client.get(
        f"/api/v1/me/ditto-link/attempts/{started.json()['attempt_id']}", headers=auth
    )
    assert attempt.json()["status"] == "failed"
    assert (await client.get("/api/v1/me/ditto-link", headers=auth)).json()[
        "link"
    ] is None

    # A token minted for a different nonce (or a different app) never links.
    started = await client.post("/api/v1/me/ditto-link/start", headers=auth, json={})
    query = parse_qs(urlparse(started.json()["authorize_url"]).query)
    provider.nonce_for_code["code-2"] = "not-the-nonce"
    bad = await client.get(
        "/api/v1/miner-auth/ditto/callback",
        params={"code": "code-2", "state": query["state"][0]},
    )
    assert "nonce" in _callback_result(bad)["reason"][0]
    assert (await client.get("/api/v1/me/ditto-link", headers=auth)).json()[
        "link"
    ] is None


async def test_link_endpoints_are_inert_until_configured(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (
        not app.state.ditto_link.enabled
    )  # default config from make_api_server_config
    alice = bittensor.Keypair.create_from_uri("//Alice")
    auth = {"authorization": f"Bearer {await _sign_in(client, alice)}"}
    status = await client.get("/api/v1/me/ditto-link", headers=auth)
    assert status.status_code == 200
    assert status.json() == {"enabled": False, "link": None}
    started = await client.post("/api/v1/me/ditto-link/start", headers=auth, json={})
    assert started.status_code == 503
    anonymous = await client.post("/api/v1/me/ditto-link/start", json={})
    assert anonymous.status_code in (401, 503)
    callback = await client.get(
        "/api/v1/miner-auth/ditto/callback", params={"state": "s"}
    )
    assert callback.status_code == 302
    assert json.dumps(callback.headers["location"]).count("ditto=error") == 1
