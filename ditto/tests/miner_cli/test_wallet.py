"""Unit tests for :mod:`ditto.miner_cli.wallet`.

The bittensor SDK is mocked at the module boundary per TESTING-STRATEGY
§145. Two invariants pinned:

- Happy path: ``load_wallet`` returns ``(WalletHandle, live_wallet)``
  with the SS58 address read from the live wallet's ``.hotkey``.
- Failure path: when the SDK raises while reading ``.hotkey.ss58_address``
  (most commonly because the keyfile is missing), we re-raise as
  :class:`WalletNotFoundError` so callers can catch one symbol.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ditto.miner_cli.errors import WalletNotFoundError
from ditto.miner_cli.models import WalletHandle
from ditto.miner_cli.wallet import load_wallet


class TestLoadWallet:
    def test_happy_path_returns_handle_and_live_wallet(self, monkeypatch) -> None:
        fake_live_wallet = MagicMock()
        fake_live_wallet.hotkey.ss58_address = (
            "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
        )

        fake_wallet_ctor = MagicMock(return_value=fake_live_wallet)
        monkeypatch.setattr("bittensor.Wallet", fake_wallet_ctor)

        handle, live = load_wallet(coldkey_name="miner", hotkey_name="default")

        # Constructor called with the names we passed in.
        fake_wallet_ctor.assert_called_once_with(name="miner", hotkey="default")

        # Handle carries the names + SS58.
        assert isinstance(handle, WalletHandle)
        assert handle.coldkey_name == "miner"
        assert handle.hotkey_name == "default"
        assert handle.hotkey_ss58.startswith("5")

        # Live wallet is the same object we mocked.
        assert live is fake_live_wallet

    def test_keyfile_missing_raises_wallet_not_found(self, monkeypatch) -> None:
        fake_live_wallet = MagicMock()
        # bittensor raises various shapes; pick a generic Exception subclass
        # to exercise the broad catch.
        fake_live_wallet.hotkey = MagicMock()
        type(fake_live_wallet.hotkey).ss58_address = property(
            lambda _self: (_ for _ in ()).throw(FileNotFoundError("no keyfile"))
        )

        monkeypatch.setattr(
            "bittensor.Wallet", MagicMock(return_value=fake_live_wallet)
        )

        with pytest.raises(WalletNotFoundError) as excinfo:
            load_wallet(coldkey_name="bogus", hotkey_name="default")

        # Error message names the wallet so a miner can debug without -v.
        assert "bogus" in str(excinfo.value)
        assert "default" in str(excinfo.value)
        # Original exception chained for postmortem.
        assert isinstance(excinfo.value.__cause__, FileNotFoundError)
