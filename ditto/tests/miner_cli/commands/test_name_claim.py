"""Unit tests for ``ditto name``."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

from ditto.api_models.name_claim import NameClaimListResponse, NameClaimResponse
from ditto.miner_cli.commands.name_claim import run

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
COLDKEY = "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy"


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "name_command": "claim",
        "name": "Jupiter-ditto-v10",
        "coldkey_name": "miner",
        "hotkey_name": "default",
        "key_kind": "hotkey",
        "netuid": 118,
        "yes": True,
        "print_only": False,
        "network": "local",
        "chain_endpoint": None,
        "verbose": False,
        "claim_id": None,
        "name_stem": None,
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


def _client(result: object) -> MagicMock:
    client = MagicMock()
    client.post_name_claim.return_value = result
    client.list_name_claims.return_value = result
    ctor = MagicMock()
    ctor.return_value.__enter__.return_value = client
    ctor.return_value.__exit__.return_value = False
    return ctor


def test_claim_print_only_does_not_submit(capsys: object) -> None:
    handle, wallet = _wallet()
    loader = MagicMock(return_value=(handle, wallet))
    with (
        patch("ditto.miner_cli.commands.name_claim.load_wallet", loader),
        patch("ditto.miner_cli.commands.name_claim.ApiClient") as ctor,
    ):
        rc = run(_args(print_only=True))
    assert rc == 0
    ctor.assert_not_called()
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "jupiter" in out or "Jupiter" in out


def test_claim_submits_signed_body() -> None:
    handle, wallet = _wallet()
    result = NameClaimResponse(
        claim_id=uuid4(),
        netuid=118,
        name_stem="jupiter",
        claimant_hotkey=HOTKEY,
        status="pending",
        endorsement_count=0,
        endorsement_threshold=3,
        created_at=datetime.now(UTC),
    )
    ctor = _client(result)
    with (
        patch(
            "ditto.miner_cli.commands.name_claim.load_wallet",
            return_value=(handle, wallet),
        ),
        patch("ditto.miner_cli.commands.name_claim.ApiClient", ctor),
    ):
        rc = run(_args())
    assert rc == 0
    body = ctor.return_value.__enter__.return_value.post_name_claim.call_args[0][0]
    assert body.name == "Jupiter-ditto-v10"
    assert body.claimant_hotkey == HOTKEY
    assert body.proof.key_kind == "hotkey"
    assert len(body.proof.signature) == 128


def test_list_prints_json(capsys: object) -> None:
    listing = NameClaimListResponse(
        generated_at=datetime.now(UTC),
        endorsement_threshold=3,
        claims=[],
    )
    ctor = _client(listing)
    with patch("ditto.miner_cli.commands.name_claim.ApiClient", ctor):
        rc = run(_args(name_command="list"))
    assert rc == 0
    assert "endorsement_threshold" in capsys.readouterr().out  # type: ignore[attr-defined]
