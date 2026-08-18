"""Wallet loading wrapper around the bittensor SDK.

The CLI uses the raw ``bittensor`` SDK directly for any chain or wallet
operation (the documented exception from the ``ChainClient`` rule, since
the SDK exposes balance transfers + key loading that Pylon does not).
This module exists to:

- centralise the SDK call so other modules import a stable seam
- raise our typed :class:`WalletNotFoundError` when keyfiles are missing
- return a frozen :class:`WalletHandle` for safe logging + payload
  construction alongside the live (mutable) wallet object that callers
  need to sign or submit extrinsics
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ditto.miner_cli.errors import (
    WalletNotFoundError,
    WalletSelectionCancelledError,
)
from ditto.miner_cli.models import WalletHandle

if TYPE_CHECKING:
    import bittensor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WalletPair:
    """A coldkey/hotkey name pair found under the local wallets directory."""

    coldkey_name: str
    hotkey_name: str

    def label(self) -> str:
        return f"{self.coldkey_name} / {self.hotkey_name}"


def wallets_root() -> Path:
    """Directory btcli uses for named wallets."""
    override = os.environ.get("BT_WALLET_PATH") or os.environ.get(
        "BITTENSOR_WALLET_PATH"
    )
    if override:
        return Path(override).expanduser()
    return Path.home() / ".bittensor" / "wallets"


def list_local_wallet_pairs(*, root: Path | None = None) -> list[WalletPair]:
    """Return every ``hotkeys/<name>`` file or directory under each coldkey."""
    base = root if root is not None else wallets_root()
    if not base.is_dir():
        return []
    pairs: list[WalletPair] = []
    for cold in sorted(base.iterdir(), key=lambda path: path.name.lower()):
        if not cold.is_dir() or cold.name.startswith("."):
            continue
        hot_dir = cold / "hotkeys"
        if not hot_dir.is_dir():
            continue
        for hot in sorted(hot_dir.iterdir(), key=lambda path: path.name.lower()):
            name = hot.name
            if name.startswith(".") or name.endswith("pub.txt"):
                continue
            if hot.is_file() or hot.is_dir():
                pairs.append(
                    WalletPair(coldkey_name=cold.name, hotkey_name=name)
                )
    return pairs


def _ask_yes(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError as exc:
        raise WalletSelectionCancelledError(
            "wallet selection cancelled: EOF on stdin"
        ) from exc
    return answer in {"", "y", "yes"}


def _pick_with_fzf(pairs: list[WalletPair]) -> WalletPair | None:
    if shutil.which("fzf") is None:
        return None
    lines = "\n".join(pair.label() for pair in pairs)
    try:
        completed = subprocess.run(
            [
                "fzf",
                "--prompt=wallet> ",
                "--height=40%",
                "--reverse",
                "--info=inline",
            ],
            input=lines,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    chosen = completed.stdout.strip()
    for pair in pairs:
        if pair.label() == chosen:
            return pair
    return None


def _pick_from_list(pairs: list[WalletPair]) -> WalletPair:
    print("Local wallets:", file=sys.stderr)
    for index, pair in enumerate(pairs, start=1):
        print(f"  {index}) {pair.label()}", file=sys.stderr)
    try:
        raw = input("Select a wallet [1]: ").strip()
    except EOFError as exc:
        raise WalletSelectionCancelledError(
            "wallet selection cancelled: EOF on stdin"
        ) from exc
    if raw == "":
        return pairs[0]
    if raw.isdigit():
        index = int(raw)
        if 1 <= index <= len(pairs):
            return pairs[index - 1]
    if "/" in raw:
        cold, _, hot = raw.partition("/")
        cold, hot = cold.strip(), hot.strip()
        for pair in pairs:
            if pair.coldkey_name == cold and pair.hotkey_name == hot:
                return pair
    raise WalletSelectionCancelledError(f"not a wallet selection: {raw!r}")


def resolve_wallet_names(
    *,
    coldkey_name: str | None,
    hotkey_name: str | None,
    interactive: bool,
    root: Path | None = None,
) -> tuple[str, str]:
    """Return coldkey/hotkey names, prompting to search locally when omitted."""
    if coldkey_name and hotkey_name:
        return coldkey_name, hotkey_name
    if not interactive:
        raise WalletNotFoundError(
            "wallet name and hotkey are required "
            "(pass --coldkey/--hotkey, or run without --yes to pick locally)"
        )
    base = root if root is not None else wallets_root()
    if not _ask_yes(f"Search {base} for a local wallet? [Y/n]: "):
        raise WalletSelectionCancelledError(
            "pass --coldkey NAME --hotkey NAME to skip discovery"
        )
    pairs = list_local_wallet_pairs(root=base)
    if coldkey_name:
        pairs = [pair for pair in pairs if pair.coldkey_name == coldkey_name]
    if hotkey_name:
        pairs = [pair for pair in pairs if pair.hotkey_name == hotkey_name]
    if not pairs:
        raise WalletNotFoundError(f"no wallets found under {base}")
    if len(pairs) == 1:
        pair = pairs[0]
        if not _ask_yes(f"Use {pair.label()}? [Y/n]: "):
            raise WalletSelectionCancelledError("wallet selection cancelled")
        return pair.coldkey_name, pair.hotkey_name
    picked = _pick_with_fzf(pairs)
    if picked is None:
        picked = _pick_from_list(pairs)
    print(f"using {picked.label()}", file=sys.stderr)
    return picked.coldkey_name, picked.hotkey_name


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
