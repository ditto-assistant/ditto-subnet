"""Wallet loading wrapper around the bittensor SDK.

Per ``context-docs/architecture/02-code-architecture.md §miner_cli``,
the CLI uses the raw ``bittensor`` SDK directly for any chain or wallet
operation. This module exists to:

- centralise the SDK call so other modules import a stable seam
- raise our typed :class:`WalletNotFoundError` when keyfiles are missing
- return a frozen :class:`WalletHandle` for safe logging + payload
  construction alongside the live (mutable) wallet object that callers
  need to sign or submit extrinsics
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ditto.miner_cli.errors import WalletNotFoundError
from ditto.miner_cli.models import WalletHandle

if TYPE_CHECKING:
    import bittensor

logger = logging.getLogger(__name__)


def load_wallet(
    *, coldkey_name: str, hotkey_name: str
) -> tuple[WalletHandle, bittensor.Wallet]:
    """Load a wallet by name and return (handle, live_wallet).

    The frozen :class:`WalletHandle` carries the identifying strings
    callers need for logging and signing-payload construction. The
    mutable :class:`bittensor.Wallet` object is returned
    alongside so callers that need to sign or submit extrinsics have
    access without violating the frozen dataclass contract.

    Args:
        coldkey_name: Coldkey name as resolved from CLI flag or env.
        hotkey_name: Hotkey name as resolved from CLI flag or env.

    Raises:
        WalletNotFoundError: When the keyfiles cannot be found on disk
            (most commonly because the supplied names do not match any
            wallet under ``~/.bittensor/wallets/``).
    """
    # Lazy import so unit tests that do not exercise the wallet path
    # are not slowed by bittensor's heavy import surface.
    import bittensor

    wallet = bittensor.Wallet(name=coldkey_name, hotkey=hotkey_name)
    try:
        hotkey_ss58 = wallet.hotkey.ss58_address
    except Exception as e:
        # bittensor raises a variety of FileNotFoundError / RuntimeError /
        # KeyFileError shapes here depending on version. Catch broadly
        # and translate; we surface the keyfile path in the message so
        # the miner can diagnose without rerunning under -v.
        raise WalletNotFoundError(
            f"could not load hotkey for coldkey={coldkey_name!r} "
            f"hotkey={hotkey_name!r}: {e}"
        ) from e

    handle = WalletHandle(
        coldkey_name=coldkey_name,
        hotkey_name=hotkey_name,
        hotkey_ss58=hotkey_ss58,
    )
    logger.info(f"loaded wallet coldkey={coldkey_name} hotkey_ss58={hotkey_ss58}")
    return handle, wallet
