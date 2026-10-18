"""Real signatures and PostgreSQL through OAuth consent, linking, and revocation."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
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
from sqlalchemy.exc import DBAPIError, IntegrityError

from ditto.api_models.contributor_identity import (
    github_link_message,
    github_unlink_message,
)
from ditto.api_models.http_errors import HttpErrorEnvelope
from ditto.api_server import github_link as github_link_module
from ditto.api_server.dependencies import get_session
from ditto.api_server.github_link import (
    ATTEMPT_TTL_SECONDS,
    GithubLinkClient,
    GithubLinkConfig,
    GithubLinkRejected,
    GithubProviderUnavailable,
)
from ditto.chain.models import BlockInfo
from ditto.db.models import (
    MinerGithubActionNonce,
    MinerGithubLink,
    MinerGithubLinkAttempt,
    MinerGithubOperatorRevocation,
    MinerSession,
)
from ditto.db.queries.contributor_identity import active_github_link
from ditto.tests.api_server.endpoints.test_admin_artifact_release_settings import (
    _install as admin_install,
)
from ditto.tests.api_server.endpoints.test_contributor_cla import (
    publish as publish_cla,
)
from ditto.tests.api_server.endpoints.test_contributor_cla import signed as signed_cla
from ditto.tests.api_server.endpoints.test_miner_ditto_link import _install, _sign_in

pytestmark = pytest.mark.asyncio
TOKEN = "fixture-github-access-token-must-not-be-stored"
SECRET = "fixture-github-client-secret"


class Provider:
    user_id = 100
    login = "miner-fixture"
    scope = ""
    revoke_status = 204
    revoke_transport_error = False
    user_status = 200
    user_delay = 0.0
    # Replaces the code exchange's JSON body when set.
    token_reply: dict | None = None
    # Public lookups (/users/{login}): a replacement reply, a delay, headers,
    # and further accounts by login beside the current one.
    accounts: dict[str, int] = {}
    users_reply = None
    users_delay = 0.0
    users_headers: dict[str, str] = {}
    # When set, a revocation waits for it before GitHub answers.
    revoke_gate: asyncio.Event | None = None

    def __init__(self):
        self.requests = []

    def transport(self):
        async def handler(request):
            self.requests.append(request)
            if request.url.path == "/login/oauth/access_token":
                if self.token_reply is not None:
                    return httpx.Response(200, json=self.token_reply)
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
                if self.user_delay:
                    await asyncio.sleep(self.user_delay)
                return httpx.Response(
                    self.user_status, json={"id": self.user_id, "login": self.login}
                )
            if request.url.path == "/applications/fixture-client/token":
                assert request.method == "DELETE"
                expected = base64.b64encode(
                    f"fixture-client:{SECRET}".encode()
                ).decode()
                assert request.headers["authorization"] == f"Basic {expected}"
                assert json.loads(request.content) == {"access_token": TOKEN}
                if self.revoke_transport_error:
                    raise httpx.ConnectError(f"fixture transport {TOKEN} {SECRET}")
                if self.revoke_gate is not None:
                    await self.revoke_gate.wait()
                return httpx.Response(self.revoke_status)
            if request.url.path.startswith("/users/"):
                expected = base64.b64encode(
                    f"fixture-client:{SECRET}".encode()
                ).decode()
                assert request.headers["authorization"] == f"Basic {expected}"
                if self.users_delay:
                    await asyncio.sleep(self.users_delay)
                if self.users_reply is not None:
                    return self.users_reply()
                login = request.url.path.removeprefix("/users/")
                return httpx.Response(
                    200,
                    json=(
                        {"id": self.accounts[login], "login": login}
                        if login in self.accounts
                        else {"id": self.user_id, "login": self.login}
                    ),
                    headers=self.users_headers,
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
    # Approval lets this hotkey claim bounty credit as the login, so the page
    # warns against approving a link someone else started.
    assert "Only approve a link you started from your own miner CLI" in callback.text
    assert "claim GitHub bounty credit as" in callback.text
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


@pytest.mark.parametrize(
    "url,origin",
    [
        ("https://example.com:443/callback", "https://example.com"),
        ("http://localhost:80/callback", "http://localhost"),
        ("http://localhost:8080/callback", "http://localhost:8080"),
        ("https://[::1]:443/callback", "https://[::1]"),
        ("http://[::1]:8080/callback", "http://[::1]:8080"),
    ],
)
async def test_callback_origin_matches_browser_serialization(url, origin):
    assert GithubLinkConfig(redirect_url=url).origin == origin


async def test_default_https_port_accepts_browser_consent(app, session_maker):
    provider = setup(app, session_maker)
    config = replace(
        app.state.github_link.config,
        redirect_url="https://dittobench.ai:443/api/v1/miner-auth/github/callback",
    )
    app.state.config = replace(app.state.config, github_link=config)
    app.state.github_link = GithubLinkClient(config, transport=provider.transport())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://dittobench.ai"
    ) as browser:
        token = await _sign_in(browser, bittensor.Keypair.create_from_uri("//Alice"))
        started, _, consent = await browser_flow(browser, token, accept=False)
        accepted = await browser.post(
            "/api/v1/miner-auth/github/accept",
            headers={"Origin": "https://dittobench.ai"},
            data={
                "attempt_id": started["attempt_id"],
                "accept_token": consent,
                "decision": "accept",
            },
        )
    assert config.origin == "https://dittobench.ai"
    assert accepted.status_code == 200, accepted.text


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"state": ""},
        {"state": "short"},
        {"state": "x" * 129},
        {"state": "x" * 20, "code": "x" * 4097},
    ],
)
async def test_malformed_callback_returns_html_without_provider_exchange(
    app, client, session_maker, params
):
    provider = setup(app, session_maker)
    response = await client.get("/api/v1/miner-auth/github/callback", params=params)
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "Invalid sign-in" in response.text
    assert "Start a new link from the CLI" in response.text
    assert not provider.requests


async def test_operator_recovery_frees_hostile_link_without_rewriting_signed_proof(
    app, client, session_maker
):
    from ditto.tests.api_server.endpoints.test_admin_artifact_release_settings import (
        _HEADERS,
    )
    from ditto.tests.api_server.endpoints.test_admin_artifact_release_settings import (
        _install as install_admin,
    )

    setup(app, session_maker)
    install_admin(app, session_maker)
    attacker = bittensor.Keypair.create_from_uri("//Alice")
    victim = bittensor.Keypair.create_from_uri("//Bob")
    attack_token = await _sign_in(client, attacker)
    attack_attempt, _, _ = await browser_flow(client, attack_token)
    linked = await client.post(
        "/api/v1/me/github-link/confirm",
        headers={"authorization": f"Bearer {attack_token}"},
        json=signed_link(attacker, attack_attempt["attempt_id"]),
    )
    assert linked.status_code == 200, linked.text
    original = linked.json()
    link_id = original["link_id"]
    victim_token = await _sign_in(client, victim)
    victim_attempt, _, _ = await browser_flow(client, victim_token)
    victim_body = signed_link(victim, victim_attempt["attempt_id"])
    auth = {"authorization": f"Bearer {victim_token}"}
    blocked = await client.post(
        "/api/v1/me/github-link/confirm", headers=auth, json=victim_body
    )
    assert blocked.status_code == 409, blocked.text
    path = f"/api/v1/admin/github-links/{link_id}"
    payload = {
        # A body actor is ignored: the audit row names Backroom's header actor.
        "actor": "claimed-in-body",
        "reason": "victim verified a phishing-induced hostile link",
        "confirmation": f"REVOKE GITHUB LINK {link_id}",
    }
    operator = {**_HEADERS, "X-Admin-Actor": "operator@omniaura.ai"}
    assert (await client.post(path + "/revoke", json=payload)).status_code == 401
    anonymous = await client.post(path + "/revoke", headers=_HEADERS, json=payload)
    assert anonymous.status_code == 422 and "X-Admin-Actor" in anonymous.text
    inspected = await client.get(path, headers=_HEADERS)
    assert inspected.status_code == 200 and inspected.json()["can_revoke"]
    assert inspected.json()["operator_revocation"] is None
    wrong = await client.post(
        path + "/revoke",
        headers=operator,
        json={**payload, "confirmation": "wrong target"},
    )
    assert wrong.status_code == 409
    revoked = await client.post(path + "/revoke", headers=operator, json=payload)
    assert revoked.status_code == 200, revoked.text
    record = revoked.json()
    assert record["link"]["signature"] == original["signature"]
    assert record["link"]["nonce"] == original["nonce"]
    assert not record["can_revoke"] and record["link"]["revoked_at"]
    assert record["operator_revocation"]["actor"] == "operator@omniaura.ai"
    assert record["operator_revocation"]["reason"] == payload["reason"]
    assert (await client.get(path, headers=_HEADERS)).json() == record
    assert (
        await client.post(path + "/revoke", headers=operator, json=payload)
    ).status_code == 409
    recovered = await client.post(
        "/api/v1/me/github-link/confirm", headers=auth, json=victim_body
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["hotkey"] == victim.ss58_address
    assert recovered.json()["github_user_id"] == original["github_user_id"]
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(MinerGithubOperatorRevocation)
            )
            == 1
        )
        for sql in (
            "DELETE FROM miner_github_operator_revocations",
            "UPDATE miner_github_operator_revocations SET actor='rewrite'",
            "TRUNCATE miner_github_operator_revocations",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(text(sql))
            await session.rollback()


async def test_operator_recovery_rejects_stale_preflight_after_miner_relinks(
    app, client, session_maker
):
    from ditto.tests.api_server.endpoints.test_admin_artifact_release_settings import (
        _HEADERS,
    )
    from ditto.tests.api_server.endpoints.test_admin_artifact_release_settings import (
        _install as install_admin,
    )

    setup(app, session_maker)
    install_admin(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    auth = {"authorization": f"Bearer {token}"}
    first, _, _ = await browser_flow(client, token)
    linked = await client.post(
        "/api/v1/me/github-link/confirm",
        headers=auth,
        json=signed_link(alice, first["attempt_id"]),
    )
    old_id = linked.json()["link_id"]
    path = f"/api/v1/admin/github-links/{old_id}"
    before = (await client.get(path, headers=_HEADERS)).json()
    assert before["can_revoke"]
    next_attempt, _, _ = await browser_flow(client, token)
    relinked = await client.post(
        "/api/v1/me/github-link/confirm",
        headers=auth,
        json=signed_link(alice, next_attempt["attempt_id"]),
    )
    assert relinked.status_code == 200
    stale = await client.post(
        path + "/revoke",
        headers={**_HEADERS, "X-Admin-Actor": "operator-fixture"},
        json={
            "reason": "stale exact target must not revoke the replacement",
            "confirmation": before["confirmation"],
        },
    )
    assert stale.status_code == 409
    assert (await client.get("/api/v1/me/github-link", headers=auth)).json()["link"][
        "link_id"
    ] == relinked.json()["link_id"]
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(MinerGithubOperatorRevocation)
            )
            == 0
        )


@pytest.mark.parametrize("case", ["success", "scope", "identity", "cleanup"])
async def test_exchange_revokes_only_the_issued_token_on_every_outcome(
    app, session_maker, case
):
    provider = setup(app, session_maker)
    if case == "scope":
        provider.scope = "repo"
    elif case == "identity":
        provider.user_id = 0
    elif case == "cleanup":
        provider.revoke_status = 503
    if case == "success":
        assert (
            await app.state.github_link.exchange(
                code="fixture-code", verifier="fixture-verifier"
            )
        ).user_id == provider.user_id
    else:
        with pytest.raises(GithubLinkRejected) as failure:
            await app.state.github_link.exchange(
                code="fixture-code", verifier="fixture-verifier"
            )
        assert TOKEN not in str(failure.value) and SECRET not in str(failure.value)
    deletes = [request for request in provider.requests if request.method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0].url.path == "/applications/fixture-client/token"
    assert json.loads(deletes[0].content) == {"access_token": TOKEN}


@pytest.mark.parametrize(
    "reply,verdict",
    [
        # GitHub's own answers about this sign-in: an expired or reused code,
        # an unverified account. The miner starts again.
        ({"error": "bad_verification_code"}, True),
        ({"error": "unverified_user_email"}, True),
        # The app's configuration or a malformed body: a provider fault.
        ({"error": "incorrect_client_credentials"}, False),
        ({"error": "redirect_uri_mismatch"}, False),
        ({}, False),
        ({"access_token": TOKEN, "token_type": "mac", "scope": ""}, False),
    ],
)
async def test_token_responses_are_classified_by_cause(
    app, session_maker, reply, verdict
):
    provider = setup(app, session_maker)
    provider.token_reply = reply
    with pytest.raises(GithubLinkRejected) as failure:
        await app.state.github_link.exchange(code="fixture", verifier="fixture")
    assert isinstance(failure.value, GithubProviderUnavailable) is not verdict
    assert TOKEN not in str(failure.value) and SECRET not in str(failure.value)
    # A token that was issued is still revoked, whatever else was wrong.
    deletes = [r for r in provider.requests if r.method == "DELETE"]
    assert len(deletes) == (1 if "access_token" in reply else 0)


@pytest.mark.parametrize("user_id", [0, 2**53 - 1, 2**53])
async def test_sign_in_accepts_only_ids_every_client_reads_exactly(
    app, session_maker, user_id
):
    provider = setup(app, session_maker)
    provider.user_id = user_id
    if 0 < user_id < 2**53:
        identity = await app.state.github_link.exchange(
            code="fixture", verifier="fixture"
        )
        assert identity.user_id == user_id
        return
    with pytest.raises(GithubProviderUnavailable, match="invalid account id"):
        await app.state.github_link.exchange(code="fixture", verifier="fixture")


@pytest.mark.parametrize("verification", ["scope", "identity", "success"])
@pytest.mark.parametrize("cleanup", ["status", "transport"])
async def test_cleanup_preserves_original_rejection_and_blocks_success(
    app, session_maker, verification, cleanup, caplog
):
    provider = setup(app, session_maker)
    if verification == "scope":
        provider.scope = "repo"
        expected = "public-profile-only"
    elif verification == "identity":
        provider.user_id = 0
        expected = "invalid account id"
    else:
        expected = "token cleanup failed"
    if cleanup == "status":
        provider.revoke_status = 503
    else:
        provider.revoke_transport_error = True
    with (
        caplog.at_level(logging.WARNING, logger="ditto.api_server.github_link"),
        pytest.raises(GithubLinkRejected, match=expected) as rejected,
    ):
        await app.state.github_link.exchange(code="fixture", verifier="fixture")
    assert TOKEN not in str(rejected.value) and SECRET not in str(rejected.value)
    assert len([r for r in provider.requests if r.method == "DELETE"]) == 1
    # A rejection hides the cleanup failure from the miner, so it is logged
    # without the token, the app secret or the provider body.
    assert "GitHub transient token revocation failed" in caplog.text
    assert ("status=503" if cleanup == "status" else "error_type=") in caplog.text
    assert TOKEN not in caplog.text and SECRET not in caplog.text


@pytest.mark.parametrize(
    "change",
    [
        {"status": "failed"},
        {"github_login": "someone-else"},
        {"github_user_id": 101},
        {"hotkey": "5" + "F" * 47},
        {"session_id": uuid4()},
    ],
)
async def test_linked_attempt_is_immutable_but_session_cleanup_remains_allowed(
    app, client, session_maker, change
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started, _, _ = await browser_flow(client, token)
    linked = await client.post(
        "/api/v1/me/github-link/confirm",
        headers={"authorization": f"Bearer {token}"},
        json=signed_link(alice, started["attempt_id"]),
    )
    assert linked.status_code == 200, linked.text
    async with session_maker() as session:
        attempt = await session.get(MinerGithubLinkAttempt, UUID(started["attempt_id"]))
        assert attempt is not None and attempt.status == "linked"
        for column, value in change.items():
            setattr(attempt, column, value)
        with pytest.raises(DBAPIError, match="linked GitHub attempts are immutable"):
            await session.flush()
        await session.rollback()
    async with session_maker() as session, session.begin():
        attempt = await session.get(MinerGithubLinkAttempt, UUID(started["attempt_id"]))
        assert attempt is not None and attempt.status == "linked"
        attempt.session_id = None
        await session.flush()


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


@pytest.mark.parametrize("login", ["miner-fixture", "A--A", "c--"])
async def test_roundtrip_uses_verified_numeric_id_and_never_stores_or_serves_token(
    app, client, session_maker, caplog, login
):
    provider = setup(app, session_maker)
    provider.login = login
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started, _, _ = await browser_flow(client, token)
    auth = {"authorization": f"Bearer {token}"}
    status = await client.get(
        f"/api/v1/me/github-link/attempts/{started['attempt_id']}", headers=auth
    )
    assert status.json()["github_user_id"] == 100
    assert status.json()["github_login"] == login
    body = signed_link(alice, started["attempt_id"], login=login)
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


@pytest.mark.parametrize("status", [301, 308, 404])
async def test_live_identity_unavailable_or_redirected_requires_relink(
    app, session_maker, status
):
    setup(app, session_maker)
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"Location": f"https://untrusted.invalid/{SECRET}"}
        )

    github = GithubLinkClient(
        app.state.github_link.config, transport=httpx.MockTransport(redirect)
    )
    with pytest.raises(GithubLinkRejected, match="re-link before claiming") as rejected:
        await github.public_identity("miner-fixture")
    # The account itself is gone or moved: a verdict, not a provider fault.
    assert not isinstance(rejected.value, GithubProviderUnavailable)
    assert len(requests) == 1
    assert requests[0].url.host == "api.github.com"
    assert SECRET not in str(rejected.value)


def _raise(exc):
    raise exc


@pytest.mark.parametrize(
    "reply",
    [
        lambda: httpx.Response(500),
        lambda: httpx.Response(503),
        lambda: httpx.Response(403, json={"message": "API rate limit exceeded"}),
        lambda: httpx.Response(429),
        lambda: httpx.Response(401, json={"message": SECRET}),
        lambda: httpx.Response(304),
        lambda: httpx.Response(302, headers={"Location": "https://api.github.com/"}),
        lambda: httpx.Response(307, headers={"Location": "https://api.github.com/"}),
        lambda: httpx.Response(200, content=b"<html>not json</html>"),
        lambda: httpx.Response(200, json=["not", "an", "object"]),
        lambda: httpx.Response(200, json={"id": 0, "login": "miner-fixture"}),
        lambda: httpx.Response(200, json={"id": 2**53, "login": "miner-fixture"}),
        lambda: httpx.Response(200, json={"id": "100", "login": "miner-fixture"}),
        lambda: httpx.Response(200, json={"id": 100, "login": "bad/login"}),
        lambda: _raise(httpx.ConnectError(f"fixture network {SECRET}")),
        lambda: _raise(httpx.ReadTimeout(f"fixture timeout {SECRET}")),
    ],
    ids=[
        "500",
        "503",
        "rate-limit-403",
        "429",
        "credentials-401",
        "bare-304",
        "temporary-302",
        "temporary-307",
        "not-json",
        "not-an-object",
        "invalid-id",
        "unsafe-id",
        "string-id",
        "invalid-login",
        "network",
        "timeout",
    ],
)
async def test_public_identity_provider_faults_stay_retryable(
    app, session_maker, reply
):
    provider = setup(app, session_maker)
    provider.users_reply = reply
    with pytest.raises(GithubProviderUnavailable, match="retry before claiming") as e:
        await app.state.github_link.public_identity(provider.login)
    assert SECRET not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


async def test_public_identity_has_one_total_deadline(app, session_maker, monkeypatch):
    provider = setup(app, session_maker)
    monkeypatch.setattr(github_link_module, "PUBLIC_IDENTITY_DEADLINE_SECONDS", 0.05)
    provider.users_delay = 5
    with pytest.raises(GithubProviderUnavailable, match="retry before claiming"):
        await app.state.github_link.public_identity(provider.login)


async def test_public_lookups_share_one_pool_and_never_carry_cookies(
    app, session_maker
):
    provider = setup(app, session_maker)
    # A cookie set while serving one miner must never reach another's lookup.
    provider.users_headers = {"set-cookie": "session=other-miner; Path=/"}
    github = app.state.github_link
    await github.public_identity(provider.login)
    pooled = github._public_client()
    await github.public_identity(provider.login)
    assert github._public_client() is pooled
    lookups = [r for r in provider.requests if r.url.path.startswith("/users/")]
    assert len(lookups) == 2
    assert all("cookie" not in request.headers for request in lookups)
    await github.aclose()
    assert pooled.is_closed
    # A lookup after shutdown opens a fresh pool instead of failing.
    assert (await github.public_identity(provider.login)).user_id == 100
    assert github._public_client() is not pooled


@pytest.mark.parametrize("login", ["", "invalid/name", "invalid:login", "a" * 40])
async def test_malformed_stored_login_is_rejected_without_network_or_input_details(
    app, session_maker, login
):
    provider = setup(app, session_maker)
    with pytest.raises(GithubLinkRejected, match="identity is unavailable") as rejected:
        await app.state.github_link.public_identity(login)
    # A bad recorded label needs a re-link; retrying GitHub cannot fix it.
    assert not isinstance(rejected.value, GithubProviderUnavailable)
    assert "re-link" in str(rejected.value)
    assert provider.requests == []
    assert "validation" not in str(rejected.value)


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
    # Platform cannot ask GitHub: retryable after an operator fix, not a verdict.
    with pytest.raises(
        GithubProviderUnavailable, match="configured OAuth app credentials"
    ):
        await unconfigured.public_identity(provider.login)
    assert len(provider.requests) == 2


@pytest.mark.parametrize("unexpected", [False, True])
async def test_exchange_failure_is_terminal_and_redacts_unexpected_details(
    app, client, session_maker, caplog, unexpected
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    auth = {"authorization": f"Bearer {token}"}
    started = await client.post("/api/v1/me/github-link/start", headers=auth)
    assert started.status_code == 200, started.text
    body = started.json()
    params = await launch_in_browser(client, body)
    error = (
        RuntimeError(TOKEN + SECRET)
        if unexpected
        else GithubLinkRejected("provider refused")
    )
    app.state.github_link.exchange = AsyncMock(side_effect=error)
    response = await client.get(
        "/api/v1/miner-auth/github/callback",
        params={"state": params["state"][0], "code": "fixture-code"},
    )
    # A rejected identity is the miner's outcome (400); an unexpected fault is
    # ours (500). Both end the attempt.
    assert response.status_code == (500 if unexpected else 400), response.text
    polled = await client.get(
        f"/api/v1/me/github-link/attempts/{body['attempt_id']}", headers=auth
    )
    assert polled.json()["status"] == "failed"
    expected = (
        "GitHub identity verification failed; start again"
        if unexpected
        else "provider refused"
    )
    assert polled.json()["error"] == expected
    assert TOKEN not in response.text + polled.text + caplog.text
    assert SECRET not in response.text + polled.text + caplog.text
    # Unexpected faults leave a sanitized operator diagnostic; expected
    # provider rejections are ordinary outcomes and are not logged as errors.
    diagnostic = f"attempt_id={body['attempt_id']} error_type=RuntimeError"
    assert (diagnostic in caplog.text) is unexpected
    async with session_maker() as session:
        attempt = await session.get(MinerGithubLinkAttempt, UUID(body["attempt_id"]))
        assert attempt.status == "failed" and attempt.error == expected
        # The verifier is spent before the exchange and no accept token was
        # issued; only the browser binding stays, so a replay reads as used.
        assert (attempt.code_verifier, attempt.accept_token_hash) == (None, None)
        assert attempt.browser_binding_hash is not None


async def test_expired_status_poll_does_not_lock_or_mutate_the_attempt(
    app, client, session_maker
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    auth = {"authorization": f"Bearer {token}"}
    started = await client.post("/api/v1/me/github-link/start", headers=auth)
    assert started.status_code == 200, started.text
    attempt_id = UUID(started.json()["attempt_id"])
    async with session_maker() as session, session.begin():
        attempt = await session.get(MinerGithubLinkAttempt, attempt_id)
        attempt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    async with session_maker() as session, session.begin():
        await session.get(MinerGithubLinkAttempt, attempt_id, with_for_update=True)
        response = await asyncio.wait_for(
            client.get(f"/api/v1/me/github-link/attempts/{attempt_id}", headers=auth),
            timeout=2,
        )
        assert response.status_code == 200 and response.json()["status"] == "expired"
        assert response.json()["github_user_id"] is None
    async with session_maker() as session:
        assert (
            await session.get(MinerGithubLinkAttempt, attempt_id)
        ).status == "pending"


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
    assert denied.json()["message"] == "GitHub account already linked to another hotkey"
    provider.login = "renamed-fixture"
    second, _, _ = await browser_flow(client, token)
    replayed = signed_link(
        alice, second["attempt_id"], login=provider.login, nonce=UUID(proof["nonce"])
    )
    replay = await client.post(
        "/api/v1/me/github-link/confirm", headers=auth, json=replayed
    )
    assert replay.status_code == 409
    assert replay.json()["message"] == "nonce already used"
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
    # Another replica's clock ran ahead when it recorded the link; revoking at
    # this replica's earlier time must not trip the history trigger.
    ahead = datetime.now(UTC) + timedelta(minutes=5)
    async with session_maker() as session, session.begin():
        await session.execute(
            text("ALTER TABLE miner_github_links DISABLE TRIGGER USER")
        )
        await session.execute(
            text("UPDATE miner_github_links SET linked_at = :at WHERE link_id = :id"),
            {"at": ahead, "id": link_id},
        )
        await session.execute(
            text("ALTER TABLE miner_github_links ENABLE TRIGGER USER")
        )
    assert (
        await client.post("/api/v1/me/github-link/unlink", headers=auth, json=body)
    ).status_code == 204
    assert (
        await client.post("/api/v1/me/github-link/unlink", headers=auth, json=body)
    ).status_code == 409
    async with session_maker() as session:
        unlinked = await session.get(MinerGithubLink, link_id)
        assert unlinked is not None and unlinked.revoked_at == ahead
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


@pytest.mark.parametrize("action", ["confirm", "unlink"])
async def test_unrelated_integrity_failures_are_not_reported_as_conflicts(
    app, client, session_maker, monkeypatch, action
):
    """Only the nonce and link conflicts a write can cause become a 409."""
    from ditto.api_server.endpoints import miner_github_link as endpoint

    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)

    class ForeignKeyViolation(Exception):
        sqlstate, constraint_name = "23503", "fk_fixture"

    driver = Exception("fixture")
    driver.__cause__ = ForeignKeyViolation()

    async def failing_flush(*_args, **_kwargs):
        raise IntegrityError("INSERT", {}, driver)

    monkeypatch.setattr(endpoint, "_authorize_action", failing_flush)
    body = (
        signed_link(alice, str(uuid4()))
        if action == "confirm"
        else {
            "netuid": 118,
            "hotkey": alice.ss58_address,
            "link_id": str(uuid4()),
            "nonce": str(uuid4()),
            "issued_at": datetime.now(UTC).isoformat(),
            "proof": {
                "key_kind": "hotkey",
                "signer": alice.ss58_address,
                "signature": "ab" * 64,
            },
        }
    )
    failed = await client.post(
        f"/api/v1/me/github-link/{action}",
        headers={"authorization": f"Bearer {token}"},
        json=body,
    )
    # A real fault, reported as one, not as a nonce or link conflict.
    assert failed.status_code == 500, failed.text
    assert "nonce" not in failed.text and "already linked" not in failed.text


async def test_relink_clamps_link_history_but_not_the_action_nonce(
    app, client, session_maker
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    auth = {"authorization": f"Bearer {token}"}
    first, _, _ = await browser_flow(client, token)
    linked = await client.post(
        "/api/v1/me/github-link/confirm",
        headers=auth,
        json=signed_link(alice, first["attempt_id"]),
    )
    assert linked.status_code == 200, linked.text
    previous_id = UUID(linked.json()["link_id"])
    # Another replica's clock ran ahead when it recorded the first link.
    ahead = datetime.now(UTC) + timedelta(minutes=5)
    async with session_maker() as session, session.begin():
        await session.execute(
            text("ALTER TABLE miner_github_links DISABLE TRIGGER USER")
        )
        await session.execute(
            text("UPDATE miner_github_links SET linked_at = :at WHERE link_id = :id"),
            {"at": ahead, "id": previous_id},
        )
        await session.execute(
            text("ALTER TABLE miner_github_links ENABLE TRIGGER USER")
        )
    second, _, _ = await browser_flow(client, token)
    before = datetime.now(UTC)
    relinked = await client.post(
        "/api/v1/me/github-link/confirm",
        headers=auth,
        json=signed_link(alice, second["attempt_id"]),
    )
    assert relinked.status_code == 200, relinked.text
    async with session_maker() as session:
        previous = await session.get(MinerGithubLink, previous_id)
        current = await session.get(MinerGithubLink, UUID(relinked.json()["link_id"]))
        assert previous is not None and current is not None
        assert previous.revoked_at == current.linked_at == ahead
        action = await session.scalar(
            select(MinerGithubActionNonce).where(
                MinerGithubActionNonce.link_id == current.link_id
            )
        )
        assert action is not None and before <= action.created_at < ahead


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
    assert len(provider.requests) == 2
    assert provider.requests[-1].method == "DELETE"
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
        BountyIdentityUnavailable,
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
    # A bounty claim always needs the current CLA, even with the gate off.
    admin_install(app, session_maker)
    agreement = await publish_cla(client)
    receipt = await client.post("/api/v1/cla/sign", json=signed_cla(alice, agreement))
    assert receipt.status_code == 200, receipt.text
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
    assert verified.cla_signature_id == UUID(receipt.json()["signature_id"])
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
        # A client-supplied object, not prepared evidence.
        {"netuid": 118, "hotkey": alice.ss58_address},
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
    provider.user_id = 100
    # A GitHub outage stays retryable and is never reported as a rejection.
    provider.users_reply = lambda: httpx.Response(503)
    with pytest.raises(BountyIdentityUnavailable, match="retry before claiming") as e:
        await verify(provider.login)
    assert not isinstance(e.value, BountyIdentityRejected)
    assert isinstance(e.value.__cause__, GithubProviderUnavailable)
    # An account that is gone is the rejection.
    provider.users_reply = lambda: httpx.Response(404)
    with pytest.raises(BountyIdentityRejected, match="not found") as gone:
        await verify(provider.login)
    assert type(gone.value.__cause__) is GithubLinkRejected


async def test_a_cancelled_consent_keeps_no_verifier_or_accept_token(
    app, client, session_maker
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started, _, consent = await browser_flow(client, token, accept=False)
    cancelled = await client.post(
        "/api/v1/miner-auth/github/accept",
        data={
            "attempt_id": started["attempt_id"],
            "accept_token": consent,
            "decision": "cancel",
        },
        headers={"origin": "http://localhost"},
    )
    assert cancelled.status_code == 200, cancelled.text
    async with session_maker() as session:
        attempt = await session.get(MinerGithubLinkAttempt, UUID(started["attempt_id"]))
        assert attempt.status == "failed"
        # No verifier or accept token survives the decision; the browser
        # binding stays so a replay is told the sign-in was used.
        assert (attempt.code_verifier, attempt.accept_token_hash) == (None, None)
        assert attempt.browser_binding_hash is not None


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


async def test_start_refreshes_expiry_cutoff_after_lock_wait(
    app, client, session_maker, monkeypatch
):
    from ditto.api_server.endpoints import miner_github_link

    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    before = datetime.now(UTC)
    after = before + timedelta(seconds=2)

    class Clock(datetime):
        current = before

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)

    async with session_maker() as session, session.begin():
        for _ in range(5):
            session.add(
                MinerGithubLinkAttempt(
                    netuid=118,
                    hotkey=alice.ss58_address,
                    launch_hash=uuid4().hex,
                    status="pending",
                    created_at=before,
                    expires_at=before + timedelta(seconds=1),
                )
            )

    original_lock = miner_github_link.lock_identity_writes

    async def delayed_lock(session, *, namespace):
        await original_lock(session, namespace=namespace)
        Clock.current = after

    monkeypatch.setattr(miner_github_link, "datetime", Clock)
    monkeypatch.setattr(miner_github_link, "lock_identity_writes", delayed_lock)
    response = await client.post(
        "/api/v1/me/github-link/start",
        headers={"authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    async with session_maker() as session:
        attempt = await session.get(
            MinerGithubLinkAttempt, UUID(response.json()["attempt_id"])
        )
        assert attempt.created_at == after
        assert attempt.expires_at == after + timedelta(
            seconds=miner_github_link.ATTEMPT_TTL_SECONDS
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
    if not expire_during_exchange:
        # A verified identity gets the attempt's full TTL back to finish linking.
        async with session_maker() as session:
            attempt = await session.get(
                MinerGithubLinkAttempt, UUID(started["attempt_id"])
            )
            assert attempt.expires_at == attempt.created_at + timedelta(
                seconds=ATTEMPT_TTL_SECONDS
            )


async def test_abandoned_exchange_expires_and_frees_its_pending_slot(
    app, client, session_maker, monkeypatch
):
    from ditto.api_server.endpoints import miner_github_link

    setup(app, session_maker)
    monkeypatch.setattr(miner_github_link, "EXCHANGE_WINDOW_SECONDS", 0)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    auth = {"authorization": f"Bearer {token}"}
    started = (await client.post("/api/v1/me/github-link/start", headers=auth)).json()
    state = (await launch_in_browser(client, started))["state"][0]
    entered = asyncio.Event()

    async def hung_exchange(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.github_link, "exchange", hung_exchange)
    task = asyncio.create_task(
        client.get(
            "/api/v1/miner-auth/github/callback",
            params={"state": state, "code": "fixture-code"},
        )
    )
    await asyncio.wait_for(entered.wait(), 10)
    # The worker dies after consuming the verifier, before recording an outcome.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    polled = await client.get(
        f"/api/v1/me/github-link/attempts/{started['attempt_id']}", headers=auth
    )
    assert polled.json()["status"] == "expired"
    # The abandoned attempt no longer counts against the five pending attempts.
    for _ in range(5):
        fresh = await client.post("/api/v1/me/github-link/start", headers=auth)
        assert fresh.status_code == 200, fresh.text


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


async def test_start_prunes_abandoned_attempts_without_touching_links(
    app, client, session_maker
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    now = datetime.now(UTC)

    def attempt(status, *, created, expires, verifier=None):
        return MinerGithubLinkAttempt(
            netuid=118,
            hotkey="5" + uuid4().hex[:47],
            launch_hash=uuid4().hex,
            status=status,
            code_verifier=verifier,
            accept_token_hash="fixture-accept" if verifier else None,
            browser_binding_hash="fixture-binding" if verifier else None,
            created_at=created,
            expires_at=expires,
        )

    # Another hotkey's abandoned callback, still holding its PKCE verifier.
    abandoned = attempt(
        "identity_verified",
        created=now - timedelta(minutes=20),
        expires=now - timedelta(minutes=10),
        verifier="fixture-verifier",
    )
    live = attempt(
        "pending",
        created=now,
        expires=now + timedelta(minutes=10),
        verifier="live-verifier",
    )
    # Confirmation refuses an accepted attempt past expiry, so pruning it only
    # scrubs secrets that can no longer be used; a live one is left alone.
    expired_accepted = attempt(
        "accepted",
        created=now - timedelta(minutes=20),
        expires=now - timedelta(minutes=10),
        verifier="expired-accepted",
    )
    live_accepted = attempt(
        "accepted",
        created=now,
        expires=now + timedelta(minutes=10),
        verifier="live-accepted",
    )
    old_failed = attempt(
        "failed", created=now - timedelta(days=31), expires=now - timedelta(days=31)
    )
    recent_failed = attempt(
        "failed", created=now - timedelta(days=1), expires=now - timedelta(days=1)
    )
    async with session_maker() as session, session.begin():
        session.add_all(
            [
                abandoned,
                live,
                expired_accepted,
                live_accepted,
                old_failed,
                recent_failed,
            ]
        )
    started = await client.post(
        "/api/v1/me/github-link/start", headers={"authorization": f"Bearer {token}"}
    )
    assert started.status_code == 200, started.text
    async with session_maker() as session:
        rows = {
            row.attempt_id: row
            for row in await session.scalars(select(MinerGithubLinkAttempt))
        }
    assert old_failed.attempt_id not in rows
    assert rows[recent_failed.attempt_id].status == "failed"
    scrubbed = rows[abandoned.attempt_id]
    assert scrubbed.status == "expired"
    assert scrubbed.code_verifier is None and scrubbed.accept_token_hash is None
    assert scrubbed.browser_binding_hash is None
    assert rows[live.attempt_id].code_verifier == "live-verifier"
    pruned_accepted = rows[expired_accepted.attempt_id]
    assert pruned_accepted.status == "expired"
    assert pruned_accepted.accept_token_hash is None
    assert pruned_accepted.browser_binding_hash is None
    kept = rows[live_accepted.attempt_id]
    assert kept.status == "accepted" and kept.accept_token_hash == "fixture-accept"
    assert kept.browser_binding_hash == "fixture-binding"


async def test_attempt_status_rejects_a_malformed_attempt_id(
    app, client, session_maker
):
    setup(app, session_maker)
    token = await _sign_in(client, bittensor.Keypair.create_from_uri("//Alice"))
    response = await client.get(
        "/api/v1/me/github-link/attempts/not-a-uuid",
        headers={"authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422, response.text
    assert HttpErrorEnvelope.model_validate(response.json()).error_code != 3000


async def test_private_link_and_cla_responses_are_never_cached(
    app, client, session_maker
):
    setup(app, session_maker)
    token = await _sign_in(client, bittensor.Keypair.create_from_uri("//Alice"))
    auth = {"authorization": f"Bearer {token}"}
    for path, headers, status in (
        (f"/api/v1/me/github-link/attempts/{uuid4()}", auth, 404),
        ("/api/v1/me/github-link/attempts/not-a-uuid", auth, 422),
        (f"/api/v1/me/github-link/attempts/{uuid4()}", {}, 401),
        ("/api/v1/public/cla/versions/9", {}, 404),
        # A redirect returned directly, which FastAPI does not merge with the
        # router dependency's headers.
        ("/api/v1/miner-auth/ditto/callback", {}, 302),
    ):
        response = await client.get(path, headers=headers)
        assert response.status_code == status, (path, response.text)
        assert response.headers.get("cache-control") == "no-store", path


async def _callback(client, token, case):
    auth = {"authorization": f"Bearer {token}"}
    started = (await client.post("/api/v1/me/github-link/start", headers=auth)).json()
    params = await launch_in_browser(client, started)
    query = {"state": params["state"][0], "code": "fixture-code"}
    if case == "declined":
        query = {"state": params["state"][0], "error": "access_denied"}
    return started, await client.get("/api/v1/miner-auth/github/callback", params=query)


@pytest.mark.parametrize(
    "case,status", [("declined", 400), ("provider", 502), ("bug", 500)]
)
async def test_callback_status_separates_declines_outages_and_bugs(
    app, client, session_maker, monkeypatch, case, status
):
    provider = setup(app, session_maker)
    if case == "provider":
        provider.user_status = 503
    if case == "bug":

        async def broken(**_kwargs):
            raise RuntimeError("fixture bug")

        monkeypatch.setattr(app.state.github_link, "exchange", broken)
    token = await _sign_in(client, bittensor.Keypair.create_from_uri("//Alice"))
    started, callback = await _callback(client, token, case)
    assert callback.status_code == status, callback.text
    assert callback.headers["cache-control"] == "no-store"
    async with session_maker() as session:
        attempt = await session.get(MinerGithubLinkAttempt, UUID(started["attempt_id"]))
        assert attempt.status == "failed" and attempt.code_verifier is None
    if case == "provider":
        # The transient token issued before the outage is still revoked.
        assert [r.method for r in provider.requests].count("DELETE") == 1


async def test_exchange_deadline_fails_as_an_outage_and_still_revokes(
    app, client, session_maker, monkeypatch
):
    from ditto.api_server import github_link

    provider = setup(app, session_maker)
    provider.user_delay = 5.0
    monkeypatch.setattr(github_link, "EXCHANGE_DEADLINE_SECONDS", 0.2)
    token = await _sign_in(client, bittensor.Keypair.create_from_uri("//Alice"))
    _started, callback = await _callback(client, token, "provider")
    assert callback.status_code == 502, callback.text
    assert "did not answer in time" in callback.text
    assert [r.method for r in provider.requests].count("DELETE") == 1


async def test_a_deadline_reached_while_revoking_keeps_the_rejection(
    app, session_maker, monkeypatch
):
    from ditto.api_server import github_link

    provider = setup(app, session_maker)
    provider.scope = "repo"
    provider.revoke_gate = asyncio.Event()
    monkeypatch.setattr(github_link, "EXCHANGE_DEADLINE_SECONDS", 0.2)
    exchange = asyncio.create_task(
        app.state.github_link.exchange(code="fixture", verifier="fixture")
    )
    while not any(r.method == "DELETE" for r in provider.requests):
        await asyncio.sleep(0.01)
    # GitHub answered in time; only the revocation outlasts the deadline.
    await asyncio.sleep(0.3)
    provider.revoke_gate.set()
    with pytest.raises(GithubLinkRejected, match="public-profile-only") as rejected:
        await exchange
    assert not isinstance(rejected.value, GithubProviderUnavailable)


async def test_a_cancelled_exchange_still_finishes_its_revocation(app, session_maker):
    provider = setup(app, session_maker)
    github = app.state.github_link
    provider.revoke_gate = asyncio.Event()
    exchange = asyncio.create_task(
        github.exchange(code="fixture-code", verifier="fixture-verifier")
    )
    while not any(r.method == "DELETE" for r in provider.requests):
        await asyncio.sleep(0.01)
    exchange.cancel()
    with pytest.raises(asyncio.CancelledError):
        await exchange
    # The client, not the cancelled request, holds the pending revocation.
    [pending] = github._revocations
    assert not pending.done()
    provider.revoke_gate.set()
    await github.aclose()
    assert pending.result() is True
    assert not github._revocations


async def test_confirm_stops_when_linking_is_disabled(app, client, session_maker):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    token = await _sign_in(client, alice)
    started, _, _ = await browser_flow(client, token)
    app.state.github_link = GithubLinkClient(
        replace(app.state.github_link.config, enabled=False)
    )
    confirmed = await client.post(
        "/api/v1/me/github-link/confirm",
        headers={"authorization": f"Bearer {token}"},
        json=signed_link(alice, started["attempt_id"]),
    )
    assert confirmed.status_code == 503, confirmed.text
    async with session_maker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(MinerGithubLink)) == 0
        )


async def test_new_link_is_ordered_after_every_event_of_its_account(
    app, client, session_maker
):
    """A replica with a fast clock revoked the account's previous link."""
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    charlie = bittensor.Keypair.create_from_uri("//Charlie")
    alice_token = await _sign_in(client, alice)
    started, _, _ = await browser_flow(client, alice_token)
    first = await client.post(
        "/api/v1/me/github-link/confirm",
        headers={"authorization": f"Bearer {alice_token}"},
        json=signed_link(alice, started["attempt_id"]),
    )
    assert first.status_code == 200, first.text
    ahead = datetime.now(UTC) + timedelta(minutes=5)
    async with session_maker() as session, session.begin():
        previous = await session.get(MinerGithubLink, UUID(first.json()["link_id"]))
        previous.revoked_at = ahead
    charlie_token = await _sign_in(client, charlie)
    started, _, _ = await browser_flow(client, charlie_token)
    second = await client.post(
        "/api/v1/me/github-link/confirm",
        headers={"authorization": f"Bearer {charlie_token}"},
        json=signed_link(charlie, started["attempt_id"]),
    )
    assert second.status_code == 200, second.text
    async with session_maker() as session:
        current = await session.get(MinerGithubLink, UUID(second.json()["link_id"]))
        # The same GitHub account is never recorded as linked to two hotkeys.
        assert current.linked_at >= ahead
