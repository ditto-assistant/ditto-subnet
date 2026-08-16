"""Unit tests for ``ditto avatar``."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

from ditto.api_models.miner_avatar import MinerAvatarResponse
from ditto.miner_cli.commands.avatar import run

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
COLDKEY = "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "avatar_command": "set",
        "file": None,
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
    wallet.coldkeypub.ss58_address = COLDKEY
    wallet.coldkey.ss58_address = COLDKEY
    wallet.coldkey.sign.return_value = b"\xbb" * 64
    return handle, wallet


def test_set_print_only_does_not_submit(tmp_path: Path, capsys: object) -> None:
    image = tmp_path / "me.png"
    image.write_bytes(_PNG)
    handle, wallet = _wallet()
    loader = MagicMock(return_value=(handle, wallet))
    with (
        patch("ditto.miner_cli.commands.avatar.load_wallet", loader),
        patch("ditto.miner_cli.commands.avatar.ApiClient") as ctor,
    ):
        rc = run(_args(file=str(image), print_only=True))
    assert rc == 0
    ctor.assert_not_called()
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert HOTKEY in out


def test_set_submits_file(tmp_path: Path) -> None:
    image = tmp_path / "me.png"
    image.write_bytes(_PNG)
    handle, wallet = _wallet()
    result = MinerAvatarResponse(
        miner_hotkey=HOTKEY,
        avatar_url=f"/api/v1/public/miners/{HOTKEY}/avatar",
        content_type="image/png",
        sha256="a" * 64,
        updated_at=None,
    )
    client = MagicMock()
    client.post_miner_avatar.return_value = result
    ctor = MagicMock()
    ctor.return_value.__enter__.return_value = client
    ctor.return_value.__exit__.return_value = False
    with (
        patch(
            "ditto.miner_cli.commands.avatar.load_wallet",
            return_value=(handle, wallet),
        ),
        patch("ditto.miner_cli.commands.avatar.ApiClient", ctor),
    ):
        rc = run(_args(file=str(image)))
    assert rc == 0
    client.post_miner_avatar.assert_called_once()
    kwargs = client.post_miner_avatar.call_args.kwargs
    assert kwargs["image"] == _PNG
    assert kwargs["filename"] == "me.png"
    assert kwargs["body"].miner_hotkey == HOTKEY
