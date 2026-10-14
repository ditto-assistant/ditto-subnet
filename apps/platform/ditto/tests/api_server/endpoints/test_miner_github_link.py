"""Real signatures and PostgreSQL through OAuth consent, linking, and revocation."""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from ditto.api_models.contributor_identity import (
    github_link_message,
    github_unlink_message,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.github_link import (
    GithubLinkClient,
    GithubLinkConfig,
    GithubLinkRejected,
)
from ditto.chain.models import BlockInfo
from ditto.db.models import (
    MinerGithubActionNonce,
    MinerGithubLink,
    MinerGithubLinkAttempt,
    MinerSession,
)
from ditto.db.queries.contributor_identity import active_github_link
from ditto.tests.api_server.endpoints.test_miner_ditto_link import _install, _sign_in

pytestmark = pytest.mark.asyncio
TOKEN = "fixture-github-access-token-must-not-be-stored"
SECRET = "fixture-github-client-secret"


class Provider:
    user_id = 100
    login = "miner-fixture"
    scope = ""

    def __init__(self):
        self.requests = []

    def transport(self):
        def handler(request):
            self.requests.append(request)
            if request.url.path == "/login/oauth/access_token":
                return httpx.Response(
                    200,
                    json={
                        "access_token": TOKEN,
                        "token_type": "bearer",
                        "scope": self.scope,
                    },
                )
            if request.url.path == "/user":
                assert request.headers["authorization"] == f"Bearer {TOKEN}"
                return httpx.Response(
                    200, json={"id": self.user_id, "login": self.login}
                )
            if request.url.path.startswith("/users/"):
                expected = base64.b64encode(
                    f"fixture-client:{SECRET}".encode()
                ).decode()
                assert request.headers["authorization"] == f"Basic {expected}"
                return httpx.Response(
                    200, json={"id": self.user_id, "login": self.login}
                )
            return httpx.Response(404)

        return httpx.MockTransport(handler)


def setup(app, maker):
    _install(app, maker)
    config = GithubLinkConfig(
        enabled=True,
        client_id="fixture-client",
        client_secret=SECRET,
        redirect_url="http://localhost/api/v1/miner-auth/github/callback",
    )
    app.state.config = replace(app.state.config, github_link=config)
    provider = Provider()
    app.state.github_link = GithubLinkClient(config, transport=provider.transport())
    owner = bittensor.Keypair.create_from_uri("//Bob").ss58_address
    app.state.chain = SimpleNamespace(
        get_finalized_block=AsyncMock(
            return_value=BlockInfo(number=123, hash="0x" + "ab" * 32)
        ),
        get_coldkey_for_hotkey=AsyncMock(return_value=owner),
    )
    return provider


async def launch_in_browser(client, started):
    url = urlsplit(started["authorize_url"])
    assert not url.query
    launch = parse_qs(url.fragment)["launch"][0]
    page = await client.get(url.path)
    assert page.status_code == 200, page.text
    assert launch not in page.text
    assert "history.replaceState" in page.text
    assert "script-src 'nonce-" in page.headers["content-security-policy"]
    # Simulate the page's fragment-clearing script: only the POST body carries
    # the capability. The GET URL alone cannot consume or start an attempt.
    launched = await client.post(url.path, data={"launch": launch})
    assert launched.status_code == 302, launched.text
    return parse_qs(
        urlsplit(launched.headers["location"]).query, keep_blank_values=True
    )


async def browser_flow(client, token, *, accept=True):
    auth = {"authorization": f"Bearer {token}"}
    started = await client.post("/api/v1/me/github-link/start", headers=auth)
    assert started.status_code == 200, started.text
    body = started.json()
    params = await launch_in_browser(client, body)
    assert params["scope"] == [""]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"][0] not in started.text
    callback = await client.get(
        "/api/v1/miner-auth/github/callback",
        params={"state": params["state"][0], "code": "fixture-code"},
    )
    assert callback.status_code == 200, callback.text
    assert callback.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in callback.headers["content-security-policy"]
    consent = re.search(r'name="accept_token" value="([^"]+)"', callback.text).group(1)
    if accept:
        approved = await client.post(
            "/api/v1/miner-auth/github/accept",
            data={
                "attempt_id": body["attempt_id"],
                "accept_token": consent,
                "decision": "accept",
            },
            headers={"origin": "http://localhost"},
        )
        assert approved.status_code == 200, approved.text
    return body, params, consent


def signed_link(
    key,
    attempt_id,
    *,
    user_id=100,
    login="miner-fixture",
    nonce=None,
    issued=None,
    signer=None,
    kind="hotkey",
    netuid=118,
):
    nonce, issued = nonce or uuid4(), issued or datetime.now(UTC)
    actual_signer = signer or key
    payload = github_link_message(
        netuid=netuid,
        hotkey=key.ss58_address,
        github_user_id=user_id,
        github_login=login,
        link_attempt_id=UUID(attempt_id),
        nonce=nonce,
        issued_at=issued,
        key_kind=kind,
        signer=actual_signer.ss58_address,
    )
    return {
        "netuid": netuid,
        "hotkey": key.ss58_address,
        "github_user_id": user_id,
        "github_login": login,
        "link_attempt_id": attempt_id,
        "nonce": str(nonce),
        "issued_at": issued.isoformat(),
        "proof": {
            "key_kind": kind,
            "signer": actual_signer.ss58_address,
            "signature": actual_signer.sign(payload).hex(),
        },
    }


async def test_roundtrip_uses_verified_numeric_id_and_never_stores_or_serves_token(
    app, client, session_maker, caplog
):
    provider = setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started, _, _ = await browser_flow(client, token)
    auth = {"authorization": f"Bearer {token}"}
    status = await client.get(
        f"/api/v1/me/github-link/attempts/{started['attempt_id']}", headers=auth
    )
    assert status.json()["github_user_id"] == 100
    assert status.json()["github_login"] == "miner-fixture"
    body = signed_link(alice, started["attempt_id"])
    linked = await client.post(
        "/api/v1/me/github-link/confirm", headers=auth, json=body
    )
    assert linked.status_code == 200, linked.text
    assert (
        linked.json()["coldkey"]
        == bittensor.Keypair.create_from_uri("//Bob").ss58_address
    )
    assert linked.json()["owner_block"] == 123
    public = await client.get(f"/api/v1/public/github-link/{alice.ss58_address}")
    reverse = await client.get("/api/v1/public/github-account/100")
    assert public.json()["link"] == reverse.json()["link"] == linked.json()
    async with session_maker() as session:
        attempt = await session.get(MinerGithubLinkAttempt, UUID(started["attempt_id"]))
        assert attempt.code_verifier is None
        action = await session.get(MinerGithubActionNonce, UUID(body["nonce"]))
        assert action.netuid == linked.json()["netuid"] == 118
        assert (
            await active_github_link(
                session, hotkey=alice.ss58_address, github_user_id=100
            )
        ).link_id == UUID(linked.json()["link_id"])
        assert (
            await active_github_link(
                session, hotkey=alice.ss58_address, github_user_id=200
            )
            is None
        )
        assert (
            await active_github_link(
                session, hotkey="different-hotkey", github_user_id=100
            )
            is None
        )
        with pytest.raises(ValueError, match="hotkey or numeric GitHub user id"):
            await active_github_link(session)
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT to_jsonb(a)::text FROM miner_github_link_attempts a "
                        "UNION ALL SELECT to_jsonb(l)::text FROM miner_github_links l"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert TOKEN not in "".join(rows)
        assert SECRET not in "".join(rows)
    assert TOKEN not in public.text + status.text + linked.text + caplog.text
    assert SECRET not in public.text + status.text + linked.text + caplog.text + repr(
        app.state.config
    )
    exchanged = parse_qs(provider.requests[0].content.decode())
    assert exchanged["client_secret"] == [SECRET]
    assert "code_verifier" in exchanged
    # No UID/registration lookup exists in this fake: owner resolution suffices.
    assert app.state.chain.get_finalized_block.await_count == 1


@pytest.mark.parametrize(
    "change,status",
    [
        ("wrong_id", 409),
        ("stale", 400),
        ("future", 400),
        ("wrong_signer", 400),
        ("wrong_netuid", 400),
        ("bad_signature", 400),
    ],
)
async def test_link_rejects_unapproved_identity_and_invalid_proofs(
    app, client, session_maker, change, status
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started, _, _ = await browser_flow(client, token)
    kwargs = {}
    if change == "wrong_id":
        kwargs["user_id"] = 101
    if change == "stale":
        kwargs["issued"] = datetime.now(UTC) - timedelta(hours=25)
    if change == "future":
        kwargs["issued"] = datetime.now(UTC) + timedelta(minutes=6)
    if change == "wrong_signer":
        kwargs["signer"] = bittensor.Keypair.create_from_uri("//Charlie")
    if change == "wrong_netuid":
        kwargs["netuid"] = 119
    body = signed_link(alice, started["attempt_id"], **kwargs)
    if change == "bad_signature":
        body["proof"]["signature"] = "00" * 64
    result = await client.post(
        "/api/v1/me/github-link/confirm",
        headers={"authorization": f"Bearer {token}"},
        json=body,
    )
    assert result.status_code == status, result.text
    async with session_maker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(MinerGithubLink)) == 0
        )


async def test_browser_binding_consent_and_session_binding_are_required(
    app, client, session_maker
):
    setup(app, session_maker)
    alice, bob = (
        bittensor.Keypair.create_from_uri(name) for name in ("//Alice", "//Bob")
    )
    token, other = await _sign_in(client, alice), await _sign_in(client, bob)
    started, params, accept_token = await browser_flow(client, token, accept=False)
    attempt = started["attempt_id"]
    auth = {"authorization": f"Bearer {token}"}
    assert (
        await client.post(
            "/api/v1/me/github-link/confirm",
            headers=auth,
            json=signed_link(alice, attempt),
        )
    ).status_code == 409
    assert (
        await client.get(
            f"/api/v1/me/github-link/attempts/{attempt}",
            headers={"authorization": f"Bearer {other}"},
        )
    ).status_code == 404
    unaccepted = await client.get(
        f"/api/v1/me/github-link/attempts/{attempt}", headers=auth
    )
    assert unaccepted.json()["github_user_id"] is None
    assert (
        await client.post(
            "/api/v1/miner-auth/github/accept",
            headers={"origin": "https://evil.example"},
            data={
                "attempt_id": attempt,
                "accept_token": accept_token,
                "decision": "accept",
            },
        )
    ).status_code == 403
    client.cookies.clear()
    assert (
        await client.post(
            "/api/v1/miner-auth/github/accept",
            headers={"origin": "http://localhost"},
            data={
                "attempt_id": attempt,
                "accept_token": accept_token,
                "decision": "accept",
            },
        )
    ).status_code == 403
    assert (
        await client.get(
            "/api/v1/miner-auth/github/callback",
            params={"state": params["state"][0], "code": "fixture-code"},
        )
    ).status_code == 400


async def test_live_identity_uses_app_auth_and_never_reuses_stale_results(
    app, session_maker
):
    provider = setup(app, session_maker)
    assert (await app.state.github_link.public_identity(provider.login)).user_id == 100
    provider.user_id = 200
    assert (await app.state.github_link.public_identity(provider.login)).user_id == 200
    requests = [r for r in provider.requests if r.url.path.startswith("/users/")]
    assert len(requests) == 2
    assert all(SECRET not in str(r.url) for r in requests)
    unconfigured = GithubLinkClient(GithubLinkConfig(), transport=provider.transport())
    with pytest.raises(GithubLinkRejected, match="configured OAuth app credentials"):
        await unconfigured.public_identity(provider.login)
    assert len(provider.requests) == 2


@pytest.mark.parametrize("action", ["confirm", "unlink"])
@pytest.mark.parametrize(
    "race", [None, "session_revoked", "scope_removed", "target_changed"]
)
async def test_signed_actions_release_database_during_rpc_and_revalidate(
    app, client, session_maker, action, race
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    auth = {"authorization": f"Bearer {token}"}
    started, _, _ = await browser_flow(client, token)
    body = signed_link(alice, started["attempt_id"])
    if action == "unlink":
        linked = await client.post(
            "/api/v1/me/github-link/confirm", headers=auth, json=body
        )
        assert linked.status_code == 200, linked.text
        nonce, issued = uuid4(), datetime.now(UTC)
        link_id = UUID(linked.json()["link_id"])
        payload = github_unlink_message(
            netuid=118,
            hotkey=alice.ss58_address,
            link_id=link_id,
            nonce=nonce,
            issued_at=issued,
            key_kind="hotkey",
            signer=alice.ss58_address,
        )
        body = {
            "netuid": 118,
            "hotkey": alice.ss58_address,
            "link_id": str(link_id),
            "nonce": str(nonce),
            "issued_at": issued.isoformat(),
            "proof": {
                "key_kind": "hotkey",
                "signer": alice.ss58_address,
                "signature": alice.sign(payload).hex(),
            },
        }
    active_sessions = []

    async def tracked_session():
        async with session_maker() as session:
            active_sessions.append(session)
            yield session

    app.dependency_overrides[get_session] = tracked_session

    async def finalized_block():
        assert not active_sessions[-1].in_transaction()
        if race:
            async with session_maker() as session, session.begin():
                attempt = await session.get(
                    MinerGithubLinkAttempt, UUID(started["attempt_id"])
                )
                if race in ("session_revoked", "scope_removed"):
                    miner = await session.get(MinerSession, attempt.session_id)
                    if race == "session_revoked":
                        miner.revoked_at = datetime.now(UTC)
                    else:
                        miner.scopes = "read"
                elif action == "confirm":
                    attempt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                else:
                    link = await session.get(MinerGithubLink, link_id)
                    link.revoked_at = datetime.now(UTC)
        return BlockInfo(number=123, hash="0x" + "ab" * 32)

    async def coldkey(*_args):
        assert not active_sessions[-1].in_transaction()
        return bittensor.Keypair.create_from_uri("//Bob").ss58_address

    app.state.chain.get_finalized_block = AsyncMock(side_effect=finalized_block)
    app.state.chain.get_coldkey_for_hotkey = AsyncMock(side_effect=coldkey)
    response = await client.post(
        f"/api/v1/me/github-link/{action}", headers=auth, json=body
    )
    expected = {
        None: 200 if action == "confirm" else 204,
        "session_revoked": 401,
        "scope_removed": 403,
        "target_changed": 409,
    }[race]
    assert response.status_code == expected, response.text
    app.state.chain.get_finalized_block.assert_awaited_once()
    async with session_maker() as session:
        saved = await session.get(MinerGithubActionNonce, UUID(body["nonce"]))
        assert (saved is not None) == (race is None)


@pytest.mark.parametrize("same_hotkey", [True, False])
async def test_active_identity_is_globally_one_to_one_across_netuids(
    app, client, session_maker, same_hotkey
):
    provider = setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    bob = bittensor.Keypair.create_from_uri("//Bob")
    alice_token = await _sign_in(client, alice)
    bob_token = await _sign_in(client, bob)
    first, _, _ = await browser_flow(client, alice_token)
    response = await client.post(
        "/api/v1/me/github-link/confirm",
        headers={"authorization": f"Bearer {alice_token}"},
        json=signed_link(alice, first["attempt_id"]),
    )
    assert response.status_code == 200, response.text
    app.state.config = replace(
        app.state.config, chain=replace(app.state.config.chain, netuid=119)
    )
    for url in (
        f"/api/v1/public/github-link/{alice.ss58_address}",
        "/api/v1/public/github-account/100",
        "/api/v1/me/github-link",
    ):
        status = await client.get(
            url, headers={"authorization": f"Bearer {alice_token}"}
        )
        assert status.status_code == 200, status.text
        assert status.json()["netuid"] == 119
        assert status.json()["link"] == response.json()
        assert status.json()["link"]["netuid"] == 118
    key, token = (alice, alice_token) if same_hotkey else (bob, bob_token)
    if same_hotkey:
        provider.user_id, provider.login = 101, "second-fixture"
    other, _, _ = await browser_flow(client, token)
    relinked = await client.post(
        "/api/v1/me/github-link/confirm",
        headers={"authorization": f"Bearer {token}"},
        json=signed_link(
            key,
            other["attempt_id"],
            netuid=119,
            user_id=provider.user_id,
            login=provider.login,
        ),
    )
    assert relinked.status_code == (200 if same_hotkey else 409), relinked.text
    async with session_maker() as session:
        links = list(await session.scalars(select(MinerGithubLink)))
        assert len(links) == (2 if same_hotkey else 1)
        active = [row for row in links if row.revoked_at is None]
        assert len(active) == 1 and active[0].hotkey == alice.ss58_address
        assert active[0].netuid == (119 if same_hotkey else 118)
        if same_hotkey:
            previous = next(row for row in links if row.netuid == 118)
            assert previous.revoked_at is not None
            assert previous.signature == response.json()["signature"]


async def test_signed_unlink_of_global_identity_preserves_original_proof_netuid(
    app, client, session_maker
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started, _, _ = await browser_flow(client, token)
    auth = {"authorization": f"Bearer {token}"}
    response = await client.post(
        "/api/v1/me/github-link/confirm",
        headers=auth,
        json=signed_link(alice, started["attempt_id"]),
    )
    assert response.status_code == 200, response.text
    link_id = UUID(response.json()["link_id"])
    app.state.config = replace(
        app.state.config, chain=replace(app.state.config.chain, netuid=119)
    )
    nonce, issued = uuid4(), datetime.now(UTC)
    payload = github_unlink_message(
        netuid=119,
        hotkey=alice.ss58_address,
        link_id=link_id,
        nonce=nonce,
        issued_at=issued,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    body = {
        "netuid": 119,
        "hotkey": alice.ss58_address,
        "link_id": str(link_id),
        "nonce": str(nonce),
        "issued_at": issued.isoformat(),
        "proof": {
            "key_kind": "hotkey",
            "signer": alice.ss58_address,
            "signature": alice.sign(payload).hex(),
        },
    }
    revoked = await client.post(
        "/api/v1/me/github-link/unlink", headers=auth, json=body
    )
    assert revoked.status_code == 204, revoked.text
    assert (await client.get("/api/v1/me/github-link", headers=auth)).json()[
        "link"
    ] is None
    async with session_maker() as session:
        row = await session.get(MinerGithubLink, link_id)
        assert row.revoked_at is not None and row.netuid == 118
        assert row.signature == response.json()["signature"]
        action = await session.get(MinerGithubActionNonce, nonce)
        assert action.netuid == 119 and action.action == "unlink"
        recorded_message = github_unlink_message(
            netuid=action.netuid,
            hotkey=action.hotkey,
            link_id=action.link_id,
            nonce=action.nonce,
            issued_at=action.issued_at,
            key_kind=action.key_kind,
            signer=action.signer,
        )
        assert alice.verify(recorded_message, bytes.fromhex(action.signature))


async def test_unique_account_relink_rename_nonce_replay_and_signed_unlink(
    app, client, session_maker
):
    provider = setup(app, session_maker)
    alice, bob = (
        bittensor.Keypair.create_from_uri(name) for name in ("//Alice", "//Bob")
    )
    token = await _sign_in(client, alice)
    auth = {"authorization": f"Bearer {token}"}
    first, _, _ = await browser_flow(client, token)
    proof = signed_link(alice, first["attempt_id"])
    linked = await client.post(
        "/api/v1/me/github-link/confirm", headers=auth, json=proof
    )
    assert linked.status_code == 200, linked.text
    assert (
        await client.post("/api/v1/me/github-link/confirm", headers=auth, json=proof)
    ).status_code == 409
    bob_token = await _sign_in(client, bob)
    other, _, _ = await browser_flow(client, bob_token)
    denied = await client.post(
        "/api/v1/me/github-link/confirm",
        headers={"authorization": f"Bearer {bob_token}"},
        json=signed_link(bob, other["attempt_id"]),
    )
    assert denied.status_code == 409, denied.text
    provider.login = "renamed-fixture"
    second, _, _ = await browser_flow(client, token)
    replayed = signed_link(
        alice, second["attempt_id"], login=provider.login, nonce=UUID(proof["nonce"])
    )
    assert (
        await client.post("/api/v1/me/github-link/confirm", headers=auth, json=replayed)
    ).status_code == 409
    refreshed = await client.post(
        "/api/v1/me/github-link/confirm",
        headers=auth,
        json=signed_link(alice, second["attempt_id"], login=provider.login),
    )
    assert refreshed.status_code == 200, refreshed.text
    async with session_maker() as session:
        history = list(
            await session.scalars(
                select(MinerGithubLink).order_by(MinerGithubLink.linked_at)
            )
        )
        assert len(history) == 2 and history[0].github_login == "miner-fixture"
        assert history[0].revoked_at is not None and history[1].revoked_at is None
    link_id, nonce, issued = (
        UUID(refreshed.json()["link_id"]),
        uuid4(),
        datetime.now(UTC),
    )
    payload = github_unlink_message(
        netuid=118,
        hotkey=alice.ss58_address,
        link_id=link_id,
        nonce=nonce,
        issued_at=issued,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    body = {
        "netuid": 118,
        "hotkey": alice.ss58_address,
        "link_id": str(link_id),
        "nonce": str(nonce),
        "issued_at": issued.isoformat(),
        "proof": {
            "key_kind": "hotkey",
            "signer": alice.ss58_address,
            "signature": alice.sign(payload).hex(),
        },
    }
    assert (
        await client.post("/api/v1/me/github-link/unlink", headers=auth, json=body)
    ).status_code == 204
    assert (
        await client.post("/api/v1/me/github-link/unlink", headers=auth, json=body)
    ).status_code == 409
    assert (
        await client.get(f"/api/v1/public/github-link/{alice.ss58_address}")
    ).json()["link"] is None
    async with session_maker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(MinerGithubLink)) == 2
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(MinerGithubActionNonce)
            )
            == 3
        )
    for statement in (
        "UPDATE miner_github_links SET github_login='tampered'",
        "DELETE FROM miner_github_links",
        "UPDATE miner_github_links SET revoked_at=NULL",
    ):
        async with session_maker() as session:
            with pytest.raises(DBAPIError):
                await session.execute(text(statement))
            await session.rollback()


@pytest.mark.parametrize("uri,expected", [("//Bob", 200), ("//Charlie", 400)])
async def test_coldkey_link_requires_finalized_owner(
    app, client, session_maker, uri, expected
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started, _, _ = await browser_flow(client, token)
    result = await client.post(
        "/api/v1/me/github-link/confirm",
        headers={"authorization": f"Bearer {token}"},
        json=signed_link(
            alice,
            started["attempt_id"],
            kind="coldkey",
            signer=bittensor.Keypair.create_from_uri(uri),
        ),
    )
    assert result.status_code == expected, result.text


async def test_oauth_broad_scope_errors_do_not_leak_tokens(
    app, client, session_maker, caplog
):
    provider = setup(app, session_maker)
    provider.scope = "repo"
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started = await client.post(
        "/api/v1/me/github-link/start", headers={"authorization": f"Bearer {token}"}
    )
    state = (await launch_in_browser(client, started.json()))["state"][0]
    denied = await client.get(
        "/api/v1/miner-auth/github/callback",
        params={"state": state, "code": "fixture-code"},
    )
    assert denied.status_code == 400
    assert "public-profile-only" in denied.text
    assert (
        TOKEN not in denied.text + caplog.text
        and SECRET not in denied.text + caplog.text
    )
    assert len(provider.requests) == 1
    replayed = await client.get(
        "/api/v1/miner-auth/github/callback",
        params={"state": state, "code": "fixture-code"},
    )
    assert replayed.status_code == 409
    async with session_maker() as session:
        row = await session.get(
            MinerGithubLinkAttempt, UUID(started.json()["attempt_id"])
        )
        assert row.code_verifier is None and row.status == "failed"


async def test_expired_attempt_and_read_only_session_are_refused(
    app, client, session_maker
):
    from ditto.db.models import MinerSession

    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    auth = {"authorization": f"Bearer {token}"}
    started, _, _ = await browser_flow(client, token)
    async with session_maker() as session, session.begin():
        attempt = await session.get(MinerGithubLinkAttempt, UUID(started["attempt_id"]))
        attempt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (
        await client.post(
            "/api/v1/me/github-link/confirm",
            headers=auth,
            json=signed_link(alice, started["attempt_id"]),
        )
    ).status_code == 409
    status = await client.get(
        f"/api/v1/me/github-link/attempts/{started['attempt_id']}", headers=auth
    )
    assert status.json()["status"] == "expired"
    async with session_maker() as session, session.begin():
        miner = await session.scalar(
            select(MinerSession).where(MinerSession.miner_hotkey == alice.ss58_address)
        )
        miner.scopes = "read"
    assert (
        await client.post("/api/v1/me/github-link/start", headers=auth)
    ).status_code == 403


async def test_bounty_guard_refuses_false_logins_and_never_selects_payee(
    app, client, session_maker
):
    from ditto.api_server.bounty_identity import (
        BountyIdentityRejected,
        prepare_bounty_identity,
        require_verified_bounty_contributor,
    )

    provider = setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started, _, _ = await browser_flow(client, token)
    linked = await client.post(
        "/api/v1/me/github-link/confirm",
        headers={"authorization": f"Bearer {token}"},
        json=signed_link(alice, started["attempt_id"]),
    )
    assert linked.status_code == 200, linked.text
    original = app.state.github_link.public_identity

    async def lookup_without_connection(login):
        assert session_maker.kw["bind"].sync_engine.pool.checkedout() == 0
        return await original(login)

    app.state.github_link.public_identity = lookup_without_connection

    async def verify(login):
        evidence = await prepare_bounty_identity(
            session_maker,
            netuid=118,
            hotkey=alice.ss58_address,
            github_login=login,
            github=app.state.github_link,
        )
        async with session_maker() as session, session.begin():
            return await require_verified_bounty_contributor(
                session,
                netuid=118,
                hotkey=alice.ss58_address,
                github_login=login,
                evidence=evidence,
            )

    verified = await verify(provider.login)
    assert verified.github_user_id == 100 and verified.hotkey == alice.ss58_address
    assert not hasattr(verified, "coldkey")
    evidence = await prepare_bounty_identity(
        session_maker,
        netuid=118,
        hotkey=alice.ss58_address,
        github_login=provider.login,
        github=app.state.github_link,
    )
    for invalid in (
        replace(evidence, _seal=object()),
        replace(evidence, verified_at=datetime.now(UTC) - timedelta(minutes=3)),
        replace(evidence, netuid=119),
    ):
        async with session_maker() as session, session.begin():
            with pytest.raises(BountyIdentityRejected, match="fresh GitHub evidence"):
                await require_verified_bounty_contributor(
                    session,
                    netuid=118,
                    hotkey=alice.ss58_address,
                    github_login=provider.login,
                    evidence=invalid,
                )
    with pytest.raises(BountyIdentityRejected, match="active verified link"):
        await verify("victim")
    provider.user_id = 200
    with pytest.raises(BountyIdentityRejected, match="renamed or reassigned"):
        await verify(provider.login)
    app.state.github_link.public_identity = AsyncMock(
        side_effect=GithubLinkRejected("fixture provider unavailable")
    )
    with pytest.raises(BountyIdentityRejected, match="provider unavailable") as caught:
        await verify(provider.login)
    assert isinstance(caught.value.__cause__, GithubLinkRejected)


@pytest.mark.parametrize("failure", ["origin", "binding", "token", "decision"])
async def test_browser_consent_failures_provide_html_recovery(
    app, client, session_maker, failure
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started, _, consent = await browser_flow(client, token, accept=False)
    origin = "http://localhost"
    decision = "accept"
    expected = 403
    if failure == "origin":
        origin = "https://untrusted.invalid"
    elif failure == "binding":
        client.cookies.clear()
    elif failure == "token":
        consent, expected = "x" * 43, 409
    else:
        decision, expected = "invalid", 400
    response = await client.post(
        "/api/v1/miner-auth/github/accept",
        data={
            "attempt_id": started["attempt_id"],
            "accept_token": consent,
            "decision": decision,
        },
        headers={"origin": origin},
    )
    assert response.status_code == expected
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "Start a new link from the CLI" in response.text


async def test_concurrent_starts_cannot_exceed_five_pending_attempts(
    app, client, session_maker
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    async with session_maker() as session, session.begin():
        for _ in range(5):
            session.add(
                MinerGithubLinkAttempt(
                    netuid=119,
                    hotkey=alice.ss58_address,
                    launch_hash=uuid4().hex,
                    status="pending",
                    created_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(minutes=10),
                )
            )
    responses = await asyncio.gather(
        *[
            client.post(
                "/api/v1/me/github-link/start",
                headers={"authorization": f"Bearer {token}"},
            )
            for _ in range(8)
        ]
    )
    assert (
        sorted(response.status_code for response in responses) == [200] * 5 + [429] * 3
    )
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(MinerGithubLinkAttempt)
                .where(MinerGithubLinkAttempt.netuid == 118)
            )
            == 5
        )


async def test_clean_launch_url_cannot_consume_capability(app, client, session_maker):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started = (
        await client.post(
            "/api/v1/me/github-link/start",
            headers={"authorization": f"Bearer {token}"},
        )
    ).json()
    url = urlsplit(started["authorize_url"])
    assert not url.query
    for _ in range(2):
        page = await client.get(url.path)
        assert (
            page.status_code == 200 and page.headers["referrer-policy"] == "no-referrer"
        )
    assert (await client.post(url.path)).status_code == 422
    assert (await client.post(url.path, data={"launch": "x" * 43})).status_code == 400
    async with session_maker() as session:
        row = await session.get(MinerGithubLinkAttempt, UUID(started["attempt_id"]))
        assert row.state_hash is None and row.code_verifier is None
    params = await launch_in_browser(client, started)
    assert "state" in params
    replay = await client.post(
        url.path, data={"launch": parse_qs(url.fragment)["launch"][0]}
    )
    assert replay.status_code == 409


@pytest.mark.parametrize("expire_during_exchange", [False, True])
async def test_slow_callback_is_single_use_and_does_not_block_status_polling(
    app, client, session_maker, monkeypatch, expire_during_exchange
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    auth = {"authorization": f"Bearer {token}"}
    started = (await client.post("/api/v1/me/github-link/start", headers=auth)).json()
    state = (await launch_in_browser(client, started))["state"][0]
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.state.github_link.exchange

    async def slow_exchange(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    exchange = AsyncMock(side_effect=slow_exchange)
    monkeypatch.setattr(app.state.github_link, "exchange", exchange)
    path = "/api/v1/miner-auth/github/callback"
    params = {"state": state, "code": "fixture-code"}
    task = asyncio.create_task(client.get(path, params=params))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        replay = await asyncio.wait_for(client.get(path, params=params), 10)
        assert replay.status_code == 409, replay.text
        polling_path = f"/api/v1/me/github-link/attempts/{started['attempt_id']}"
        polled = await asyncio.wait_for(client.get(polling_path, headers=auth), 10)
        assert polled.status_code == 200 and polled.json()["status"] == "pending"
        if expire_during_exchange:
            async with session_maker() as session, session.begin():
                attempt = await session.get(
                    MinerGithubLinkAttempt, UUID(started["attempt_id"])
                )
                attempt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            expired = await asyncio.wait_for(client.get(polling_path, headers=auth), 10)
            assert expired.json()["status"] == "expired"
    finally:
        release.set()
        result = await asyncio.wait_for(task, 10)
    assert result.status_code == (409 if expire_during_exchange else 200), result.text
    exchange.assert_awaited_once()


@pytest.mark.parametrize("action", ["unlink", "relink"])
async def test_bounty_identity_rechecks_link_after_network_io(
    app, client, session_maker, monkeypatch, action
):
    from ditto.api_server.bounty_identity import (
        BountyIdentityRejected,
        prepare_bounty_identity,
        require_verified_bounty_contributor,
    )

    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    auth = {"authorization": f"Bearer {token}"}
    started, _, _ = await browser_flow(client, token)
    linked = await client.post(
        "/api/v1/me/github-link/confirm",
        headers=auth,
        json=signed_link(alice, started["attempt_id"]),
    )
    assert linked.status_code == 200, linked.text
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.state.github_link.public_identity

    async def slow_identity(login):
        identity = await original(login)
        entered.set()
        await release.wait()
        return identity

    monkeypatch.setattr(app.state.github_link, "public_identity", slow_identity)

    async def accept_claim():
        evidence = await prepare_bounty_identity(
            session_maker,
            netuid=118,
            hotkey=alice.ss58_address,
            github_login="miner-fixture",
            github=app.state.github_link,
        )
        async with session_maker() as session, session.begin():
            return await require_verified_bounty_contributor(
                session,
                netuid=118,
                hotkey=alice.ss58_address,
                github_login="miner-fixture",
                evidence=evidence,
            )

    task = asyncio.create_task(accept_claim())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if action == "unlink":
            nonce, issued = uuid4(), datetime.now(UTC)
            link_id = UUID(linked.json()["link_id"])
            payload = github_unlink_message(
                netuid=118,
                hotkey=alice.ss58_address,
                link_id=link_id,
                nonce=nonce,
                issued_at=issued,
                key_kind="hotkey",
                signer=alice.ss58_address,
            )
            body = {
                "netuid": 118,
                "hotkey": alice.ss58_address,
                "link_id": str(link_id),
                "nonce": str(nonce),
                "issued_at": issued.isoformat(),
                "proof": {
                    "key_kind": "hotkey",
                    "signer": alice.ss58_address,
                    "signature": alice.sign(payload).hex(),
                },
            }
            revoked = await asyncio.wait_for(
                client.post("/api/v1/me/github-link/unlink", headers=auth, json=body),
                10,
            )
            assert revoked.status_code == 204, revoked.text
        else:
            next_attempt, _, _ = await asyncio.wait_for(browser_flow(client, token), 10)
            relinked = await asyncio.wait_for(
                client.post(
                    "/api/v1/me/github-link/confirm",
                    headers=auth,
                    json=signed_link(alice, next_attempt["attempt_id"]),
                ),
                10,
            )
            assert relinked.status_code == 200, relinked.text
    finally:
        release.set()
        with pytest.raises(BountyIdentityRejected, match="changed during verification"):
            await asyncio.wait_for(task, 10)
