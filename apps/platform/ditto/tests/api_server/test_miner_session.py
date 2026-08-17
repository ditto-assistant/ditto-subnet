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


def test_social_normalization() -> None:
    assert normalize_x_url("x.com/jupiter") == "https://x.com/jupiter"
    assert normalize_github_url("github.com/ditto-assistant") == (
        "https://github.com/ditto-assistant"
    )
    assert normalize_discord_handle("@Jupiter_01") == "Jupiter_01"
    with pytest.raises(MinerSessionRejected):
        normalize_x_url("https://example.com/x")


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
