from __future__ import annotations

import argparse
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

from ditto.api_models.miner_session import (
    MinerDevicePublicResponse,
    MinerDeviceStartResponse,
    MinerDeviceStatusResponse,
    MinerSessionView,
)
from ditto.miner_cli.commands.login import run

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "login_command": "approve",
        "user_code": "ABCD-EFGH",
        "hours": 24,
        "scopes": "read,profile",
        "coldkey_name": "miner",
        "hotkey_name": "default",
        "key_kind": "hotkey",
        "netuid": 118,
        "yes": True,
        "print_only": False,
        "network": "local",
        "chain_endpoint": None,
        "verbose": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _wallet() -> tuple[MagicMock, MagicMock]:
    handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")
    wallet = MagicMock()
    wallet.hotkey.ss58_address = HOTKEY
    wallet.hotkey.sign.return_value = b"\xaa" * 64
    return handle, wallet


def test_print_only_does_not_approve() -> None:
    handle, wallet = _wallet()
    public = MinerDevicePublicResponse(
        user_code="ABCD-EFGH",
        grant_id=uuid4(),
        status="pending",
        scopes=["read", "profile"],
        ttl_seconds=3600,
        expires_in=800,
        login_command="ditto --network local login --code ABCD-EFGH",
    )
    client = MagicMock()
    client.get_miner_device.return_value = public
    ctor = MagicMock()
    ctor.return_value.__enter__.return_value = client
    ctor.return_value.__exit__.return_value = False
    with (
        patch(
            "ditto.miner_cli.commands.login.load_wallet",
            return_value=(handle, wallet),
        ),
        patch("ditto.miner_cli.commands.login.ApiClient", ctor),
    ):
        rc = run(_args(print_only=True))
    assert rc == 0
    client.approve_miner_device.assert_not_called()


def test_approve_saves_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(tmp_path / "config.json"))
    handle, wallet = _wallet()
    grant_id = uuid4()
    now = datetime.now(UTC)
    public = MinerDevicePublicResponse(
        user_code="ABCD-EFGH",
        grant_id=grant_id,
        status="pending",
        scopes=["read", "profile"],
        ttl_seconds=3600,
        expires_in=800,
        login_command="ditto --network local login --code ABCD-EFGH",
    )
    result = MinerDeviceStatusResponse(
        user_code="ABCD-EFGH",
        status="approved",
        scopes=["read", "profile"],
        ttl_seconds=3600,
        access_token="ditto_ms_" + "ab" * 32,
        token_type="Bearer",
        session=MinerSessionView(
            session_id=uuid4(),
            miner_hotkey=HOTKEY,
            scopes=["read", "profile"],
            label="dashboard",
            created_at=now,
            expires_at=now,
            expires_in=3600,
        ),
    )
    client = MagicMock()
    client.get_miner_device.return_value = public
    client.approve_miner_device.return_value = result
    ctor = MagicMock()
    ctor.return_value.__enter__.return_value = client
    ctor.return_value.__exit__.return_value = False
    with (
        patch(
            "ditto.miner_cli.commands.login.load_wallet",
            return_value=(handle, wallet),
        ),
        patch("ditto.miner_cli.commands.login.ApiClient", ctor),
    ):
        rc = run(_args())
    assert rc == 0
    client.approve_miner_device.assert_called_once()


def test_start_without_code_prints_url() -> None:
    handle, wallet = _wallet()
    started = MinerDeviceStartResponse(
        user_code="ABCD-EFGH",
        poll_token="poll",
        verification_uri="http://localhost:8000/#/reviews",
        verification_uri_complete="http://localhost:8000/#/reviews?code=ABCD-EFGH",
        expires_in=900,
        scopes=["read"],
        ttl_seconds=3600,
        login_command="ditto --network local login --code ABCD-EFGH",
    )
    public = MinerDevicePublicResponse(
        user_code="ABCD-EFGH",
        grant_id=uuid4(),
        status="pending",
        scopes=["read"],
        ttl_seconds=3600,
        expires_in=800,
        login_command="ditto --network local login --code ABCD-EFGH",
    )
    client = MagicMock()
    client.start_miner_device.return_value = started
    client.get_miner_device.return_value = public
    ctor = MagicMock()
    ctor.return_value.__enter__.return_value = client
    ctor.return_value.__exit__.return_value = False
    with (
        patch(
            "ditto.miner_cli.commands.login.load_wallet",
            return_value=(handle, wallet),
        ),
        patch("ditto.miner_cli.commands.login.ApiClient", ctor),
    ):
        rc = run(_args(user_code=None, print_only=True))
    assert rc == 0
    client.start_miner_device.assert_called_once()
