from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import bittensor
import pytest

from ditto.api_server.miner_session import (
    MinerSessionRejected,
    login_message,
    normalize_discord_handle,
    normalize_github_url,
    normalize_scopes,
    normalize_user_code,
    normalize_x_url,
    verify_signed_action,
)


def test_normalize_scopes_sorts_and_rejects_unknown() -> None:
    assert normalize_scopes(["upload", "read"]) == ("read", "upload")
    with pytest.raises(MinerSessionRejected):
        normalize_scopes(["admin"])


def test_user_code_round_trip() -> None:
    assert normalize_user_code("abcd efgh") == "ABCD-EFGH"
    with pytest.raises(MinerSessionRejected):
        normalize_user_code("short")


def test_login_command_uses_uvx_from_git() -> None:
    from ditto.api_server.miner_session import login_clone_command, login_command

    one = login_command(user_code="abcd efgh", network="finney")
    assert one.startswith(
        "uvx --from git+https://github.com/ditto-assistant/ditto-subnet.git "
    )
    assert one.endswith("ditto --network finney login --code ABCD-EFGH")
    clone = login_clone_command(user_code="ABCD-EFGH", network="finney")
    assert "git clone https://github.com/ditto-assistant/ditto-subnet.git" in clone
    assert "uv run ditto --network finney login --code ABCD-EFGH" in clone


def test_social_normalization() -> None:
    assert normalize_x_url("x.com/jupiter") == "https://x.com/jupiter"
    assert normalize_github_url("github.com/ditto-assistant") == (
        "https://github.com/ditto-assistant"
    )
    assert normalize_discord_handle("@Jupiter_01") == "Jupiter_01"
    with pytest.raises(MinerSessionRejected):
        normalize_x_url("https://example.com/x")
    with pytest.raises(MinerSessionRejected):
        normalize_x_url("x.com/" + ("a" * 200))


def test_login_message_binds_oauth_client() -> None:
    nonce = uuid4()
    issued = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    grant_id = uuid4()
    payload = login_message(
        netuid=118,
        miner_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        user_code="ABCD-EFGH",
        grant_id=grant_id,
        ttl_seconds=86400,
        scopes="profile,read,upload",
        nonce=nonce,
        issued_at=issued,
        key_kind="hotkey",
        signer="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        oauth_client_id="mcp_client",
        redirect_uri="http://127.0.0.1:8757/cb",
    )
    assert payload.startswith(b"ditto-miner-login:v1:118:")
    assert b":mcp_client:http://127.0.0.1:8757/cb" in payload
    assert b":read,profile,upload:" in payload


def test_expire_stale_grant_covers_approved() -> None:
    from datetime import timedelta

    from ditto.db.models import MinerDeviceGrant
    from ditto.db.queries.miner_sessions import expire_stale_grant

    now = datetime.now(UTC)
    grant = MinerDeviceGrant(
        grant_id=uuid4(),
        user_code="ABCD-EFGH",
        poll_token_hash="a" * 64,
        status="approved",
        scopes="read",
        ttl_seconds=3600,
        expires_at=now - timedelta(seconds=1),
    )

    class _Session:
        async def flush(self) -> None:
            return None

    async def _run() -> None:
        expired = await expire_stale_grant(_Session(), grant=grant, now=now)  # type: ignore[arg-type]
        assert expired.status == "expired"

    import asyncio

    asyncio.run(_run())


def test_pkce_and_redirect_rules() -> None:
    from ditto.api_server.miner_session import validate_pkce, validate_redirect_uri

    assert validate_pkce("a" * 43) == "a" * 43
    with pytest.raises(MinerSessionRejected):
        validate_pkce("short")
    assert validate_redirect_uri("https://app.example/cb") == "https://app.example/cb"
    assert validate_redirect_uri("http://127.0.0.1:8757/cb").startswith("http://")
    with pytest.raises(MinerSessionRejected):
        validate_redirect_uri("javascript:alert(1)")
    with pytest.raises(MinerSessionRejected):
        validate_redirect_uri("http://evil.example/cb")


def test_login_signature_round_trip() -> None:
    signer = bittensor.Keypair.create_from_uri("//Alice")
    nonce = uuid4()
    issued = datetime.now(UTC)
    grant_id = uuid4()
    payload = login_message(
        netuid=118,
        miner_hotkey=signer.ss58_address,
        user_code="ABCD-EFGH",
        grant_id=grant_id,
        ttl_seconds=86400,
        scopes="read,profile",
        nonce=nonce,
        issued_at=issued,
        key_kind="hotkey",
        signer=signer.ss58_address,
    )
    verify_signed_action(
        payload=payload,
        hotkey=signer.ss58_address,
        key_kind="hotkey",
        signer=signer.ss58_address,
        signature=signer.sign(payload).hex(),
        bound_coldkey=None,
    )
    with pytest.raises(MinerSessionRejected):
        verify_signed_action(
            payload=payload,
            hotkey=signer.ss58_address,
            key_kind="hotkey",
            signer=signer.ss58_address,
            signature="00" * 64,
            bound_coldkey=None,
        )
