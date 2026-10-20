"""Sign in with Ditto for miners: link, callback binding, replay, revoke."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.api_server.ditto_link import DittoLinkClient, DittoLinkConfig
from ditto.api_server.endpoints import miner_ditto_link
from ditto.api_server.miner_session import login_message
from ditto.db.models import MinerDittoLinkAttempt
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


def _accept_params(response: httpx.Response) -> dict[str, str]:
    """The callback now hands the SIGNED-IN browser to the accept page."""
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("https://dittobench.ai/api/v1/miner-auth/ditto/accept?")
    q = parse_qs(urlparse(location).query)
    return {"attempt": q["attempt"][0], "t": q["t"][0]}


async def _decide(
    client: httpx.AsyncClient, accept: dict[str, str], decision: str
) -> httpx.Response:
    """Submit the accept page's form: only ``attempt`` rides in the URL."""
    return await client.post(
        "/api/v1/miner-auth/ditto/accept",
        params={"attempt": accept["attempt"]},
        data={"t": accept["t"], "decision": decision},
    )


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
    # The dashboard binding cookie is Secure, so this browser speaks https.
    client.base_url = httpx.URL("https://test")
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
    # It does NOT link and does not even mark the attempt authenticated: it
    # sends the browser that signed in to a page naming the hotkey.
    done = await client.get(
        "/api/v1/miner-auth/ditto/callback", params={"code": "code-1", "state": state}
    )
    accept = _accept_params(done)
    assert accept["attempt"] == body["attempt_id"]
    # The token exchange used PKCE and the app secret, never the user's word.
    assert provider.token_requests[-1]["code_verifier"]
    assert provider.token_requests[-1]["grant_type"] == ["authorization_code"]

    # Before the Ditto side accepts, the hotkey side learns nothing about who
    # signed in, and cannot confirm.
    assert (await client.get("/api/v1/me/ditto-link", headers=auth)).json()[
        "link"
    ] is None
    parked = await client.get(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}", headers=auth
    )
    assert parked.json()["status"] == "identity_verified"
    assert parked.json()["ditto_email"] is None
    assert parked.json()["ditto_user_id"] is None
    early = await client.post(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}/confirm", headers=auth
    )
    assert early.status_code == 409

    # The accept page shows the account holder exactly which hotkey wants them.
    page = await client.get("/api/v1/miner-auth/ditto/accept", params=accept)
    assert page.status_code == 200, page.text
    assert alice.ss58_address in page.text
    assert "miner@example.com" in page.text
    assert "Not me" in page.text
    # The form posts the token in its body; no form URL carries it.
    actions = re.findall(r'action="([^"]*)"', page.text)
    assert actions
    assert all(accept["t"] not in action for action in actions)
    # A wrong token shows nothing.
    assert (
        await client.get(
            "/api/v1/miner-auth/ditto/accept",
            params={"attempt": accept["attempt"], "t": "nope"},
        )
    ).status_code == 404
    # A token in the POST query string is not read at all.
    in_query = await client.post(
        "/api/v1/miner-auth/ditto/accept", params={**accept, "decision": "accept"}
    )
    assert in_query.status_code == 422
    accepted = await _decide(client, accept, "accept")
    assert accepted.status_code == 303, accepted.text
    result = parse_qs(urlparse(accepted.headers["location"].replace("#/", "/")).query)
    assert result["ditto"] == ["confirm"]
    assert result["attempt"] == [body["attempt_id"]]
    # The accept token is single-use.
    assert (await _decide(client, accept, "accept")).status_code == 404

    # Now the hotkey side sees who accepted and may confirm.
    parked = await client.get(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}", headers=auth
    )
    assert parked.json()["status"] == "authenticated"
    assert parked.json()["ditto_email"] == "miner@example.com"
    assert parked.json()["miner_hotkey"] == alice.ss58_address
    assert (await client.get("/api/v1/me/ditto-link", headers=auth)).json()[
        "link"
    ] is None
    # Another hotkey's session cannot confirm Alice's attempt (account-binding CSRF).
    bob = bittensor.Keypair.create_from_uri("//Bob")
    bob_auth = {"authorization": f"Bearer {await _sign_in(client, bob)}"}
    stolen = await client.post(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}/confirm", headers=bob_auth
    )
    assert stolen.status_code == 404
    assert (await client.get("/api/v1/me/ditto-link", headers=bob_auth)).json()[
        "link"
    ] is None
    confirmed = await client.post(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}/confirm", headers=auth
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "linked"
    assert confirmed.json()["link"]["ditto_user_id"] == "ditto-user-1"
    # Confirming twice is idempotent.
    again = await client.post(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}/confirm", headers=auth
    )
    assert again.json()["status"] == "linked"

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
    # The default dashboard attempt's binding cookie is Secure: speak https.
    client.base_url = httpx.URL("https://test")
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
    assert (
        await client.post(
            f"/api/v1/me/ditto-link/attempts/{started.json()['attempt_id']}/confirm",
            headers=auth,
        )
    ).status_code == 409
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


async def test_attacker_hotkey_cannot_capture_a_victims_account(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Reverse-direction phish: Mallory starts an attempt for HER hotkey and hands
    the authorize URL to a victim. The victim's sign-in must never bind their
    account to Mallory's hotkey, and Mallory must learn nothing about them."""
    _install(app, session_maker)
    provider = FakeProvider()
    provider.subject = "victim-ditto-user"
    app.state.ditto_link = DittoLinkClient(_config(), transport=provider.transport())
    mallory = bittensor.Keypair.create_from_uri("//Bob")
    mallory_auth = {"authorization": f"Bearer {await _sign_in(client, mallory)}"}
    # CLI attempts carry no browser cookie, so this is the path a phish would use.
    started = await client.post(
        "/api/v1/me/ditto-link/start", headers=mallory_auth, json={"client": "cli"}
    )
    body = started.json()
    query = parse_qs(urlparse(body["authorize_url"]).query)
    provider.nonce_for_code["code-v"] = query["nonce"][0]

    # The victim's browser completes the callback.
    done = await client.get(
        "/api/v1/miner-auth/ditto/callback",
        params={"code": "code-v", "state": query["state"][0]},
    )
    accept = _accept_params(done)

    # Mallory sees no identity and cannot confirm.
    seen = await client.get(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}", headers=mallory_auth
    )
    assert seen.json()["status"] == "identity_verified"
    assert seen.json()["ditto_user_id"] is None
    assert seen.json()["ditto_email"] is None
    assert "victim" not in seen.text
    assert (
        await client.post(
            f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}/confirm",
            headers=mallory_auth,
        )
    ).status_code == 409

    # The victim's page names Mallory's hotkey; the victim declines.
    page = await client.get("/api/v1/miner-auth/ditto/accept", params=accept)
    assert mallory.ss58_address in page.text
    declined = await _decide(client, accept, "decline")
    assert declined.status_code == 303
    assert parse_qs(urlparse(declined.headers["location"].replace("#/", "/")).query)[
        "ditto"
    ] == ["error"]

    after = await client.get(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}", headers=mallory_auth
    )
    assert after.json()["status"] == "failed"
    assert after.json()["ditto_user_id"] is None
    assert (
        await client.post(
            f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}/confirm",
            headers=mallory_auth,
        )
    ).status_code == 409
    assert (await client.get("/api/v1/me/ditto-link", headers=mallory_auth)).json()[
        "link"
    ] is None
    assert (
        await client.get(f"/api/v1/public/feedback-track/{mallory.ss58_address}")
    ).json()["linked"] is False


async def test_dashboard_attempt_is_bound_to_the_starting_browser(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    provider = FakeProvider()
    app.state.ditto_link = DittoLinkClient(_config(), transport=provider.transport())
    alice = bittensor.Keypair.create_from_uri("//Alice")
    auth = {"authorization": f"Bearer {await _sign_in(client, alice)}"}
    started = await client.post(
        "/api/v1/me/ditto-link/start", headers=auth, json={"client": "dashboard"}
    )
    assert started.status_code == 200, started.text
    body = started.json()
    cookie_names = [c.name for c in client.cookies.jar]
    assert any(n.startswith("ditto_link_") for n in cookie_names)
    # Secure follows the https redirect URL even though this request, like
    # one forwarded by the TLS-terminating proxy, arrived over http.
    cookie_flags = started.headers["set-cookie"].lower().split(";")
    assert "secure" in [flag.strip() for flag in cookie_flags]
    query = parse_qs(urlparse(body["authorize_url"]).query)
    provider.nonce_for_code["code-d"] = query["nonce"][0]

    # A different browser (no cookie) presenting the callback is refused
    # before any token exchange happens.
    other_browser = httpx.AsyncClient(
        transport=client._transport,  # noqa: SLF001 - same app, fresh cookie jar
        base_url=str(client.base_url),
    )
    async with other_browser:
        done = await other_browser.get(
            "/api/v1/miner-auth/ditto/callback",
            params={"code": "code-d", "state": query["state"][0]},
        )
    result = _callback_result(done)
    assert result["ditto"] == ["error"]
    assert "different browser" in result["reason"][0]
    assert provider.token_requests == []
    attempt = await client.get(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}", headers=auth
    )
    assert attempt.json()["status"] == "failed"


async def test_concurrent_accept_and_decline_consume_the_token_once(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    provider = FakeProvider()
    app.state.ditto_link = DittoLinkClient(_config(), transport=provider.transport())
    alice = bittensor.Keypair.create_from_uri("//Alice")
    auth = {"authorization": f"Bearer {await _sign_in(client, alice)}"}
    started = await client.post(
        "/api/v1/me/ditto-link/start", headers=auth, json={"client": "cli"}
    )
    body = started.json()
    query = parse_qs(urlparse(body["authorize_url"]).query)
    provider.nonce_for_code["code-c"] = query["nonce"][0]
    done = await client.get(
        "/api/v1/miner-auth/ditto/callback",
        params={"code": "code-c", "state": query["state"][0]},
    )
    accept = _accept_params(done)

    # Hold each answer between reading the attempt and writing it, so both
    # would read the unconsumed token if the read did not lock the row.
    expire = miner_ditto_link.expire_stale_attempt

    async def slow_expire(
        session: AsyncSession, *, attempt: MinerDittoLinkAttempt, now: datetime
    ) -> MinerDittoLinkAttempt:
        await asyncio.sleep(0.2)
        return await expire(session, attempt=attempt, now=now)

    monkeypatch.setattr(miner_ditto_link, "expire_stale_attempt", slow_expire)
    # The row lock serializes the two answers: exactly one consumes the token
    # and the attempt ends in that answer's state, not last-write-wins.
    accepted, declined = await asyncio.gather(
        _decide(client, accept, "accept"), _decide(client, accept, "decline")
    )
    assert sorted([accepted.status_code, declined.status_code]) == [303, 404]
    attempt = await client.get(
        f"/api/v1/me/ditto-link/attempts/{body['attempt_id']}", headers=auth
    )
    expected = "authenticated" if accepted.status_code == 303 else "failed"
    assert attempt.json()["status"] == expected
