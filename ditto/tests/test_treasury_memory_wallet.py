"""The in-memory treasury signer must satisfy the SDK's wallet unlock check.

Kept apart from ``test_treasury_execution.py`` so this fix and the pending
treasury execution changes do not edit the same lines.
"""

from __future__ import annotations

from types import SimpleNamespace

import bittensor as bt
import pytest
from bittensor.core.types import ExtrinsicResponse

from ditto.treasury import execution


def _mnemonic() -> str:
    return bt.Keypair.generate_mnemonic(24)


def test_memory_wallet_passes_the_sdk_unlock_precondition() -> None:
    keypair = bt.Keypair.create_from_mnemonic(_mnemonic())
    wallet = execution._memory_wallet(keypair)

    # unstake/add_stake/transfer/transfer_stake all run this before building
    # the extrinsic; a wallet without unlock_coldkey raised AttributeError here.
    unlocked = ExtrinsicResponse.unlock_wallet(
        wallet,  # type: ignore[arg-type]
        raise_error=True,
    )

    assert unlocked.success is True
    assert wallet.coldkeypub.ss58_address == keypair.ss58_address
    assert wallet.coldkey is keypair


def test_loaded_signer_wallet_unlocks_without_a_keyfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mnemonic = _mnemonic()
    monkeypatch.setattr(execution, "_host_name", lambda: execution.HOST_NAME)
    monkeypatch.setattr(
        execution.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=mnemonic.encode()),
    )

    wallet = execution._load_wallet("ditto-test-project")

    unlocked = ExtrinsicResponse.unlock_wallet(
        wallet,  # type: ignore[arg-type]
        raise_error=True,
    )
    assert unlocked.success is True
    assert (
        wallet.coldkeypub.ss58_address
        == bt.Keypair.create_from_mnemonic(mnemonic).ss58_address
    )
