from __future__ import annotations

from pathlib import Path

import pytest

from ditto.miner_cli.errors import (
    WalletNotFoundError,
    WalletSelectionCancelledError,
)
from ditto.miner_cli.wallet import (
    WalletPair,
    list_local_wallet_pairs,
    resolve_wallet_names,
)


def _touch_wallet(root: Path, cold: str, hot: str) -> None:
    path = root / cold / "hotkeys"
    path.mkdir(parents=True)
    (path / hot).write_text("keyfile")


def test_list_local_wallet_pairs_skips_pub_files(tmp_path: Path) -> None:
    _touch_wallet(tmp_path, "miner", "default")
    (tmp_path / "miner" / "hotkeys" / "defaultpub.txt").write_text("pub")
    (tmp_path / "empty").mkdir()
    assert list_local_wallet_pairs(root=tmp_path) == [
        WalletPair(coldkey_name="miner", hotkey_name="default")
    ]


def test_resolve_uses_explicit_names(tmp_path: Path) -> None:
    assert resolve_wallet_names(
        coldkey_name="miner",
        hotkey_name="default",
        interactive=False,
        root=tmp_path,
    ) == ("miner", "default")


def test_resolve_requires_names_when_not_interactive(tmp_path: Path) -> None:
    with pytest.raises(WalletNotFoundError, match="required"):
        resolve_wallet_names(
            coldkey_name=None,
            hotkey_name=None,
            interactive=False,
            root=tmp_path,
        )


def test_resolve_single_wallet_confirms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch_wallet(tmp_path, "miner", "default")
    answers = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert resolve_wallet_names(
        coldkey_name=None,
        hotkey_name=None,
        interactive=True,
        root=tmp_path,
    ) == ("miner", "default")


def test_resolve_declining_search_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch_wallet(tmp_path, "miner", "default")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    with pytest.raises(WalletSelectionCancelledError):
        resolve_wallet_names(
            coldkey_name=None,
            hotkey_name=None,
            interactive=True,
            root=tmp_path,
        )


def test_resolve_numbered_picker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch_wallet(tmp_path, "miner", "default")
    _touch_wallet(tmp_path, "old", "vali")
    monkeypatch.setattr("ditto.miner_cli.wallet._pick_with_fzf", lambda _pairs: None)
    answers = iter(["y", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert resolve_wallet_names(
        coldkey_name=None,
        hotkey_name=None,
        interactive=True,
        root=tmp_path,
    ) == ("old", "vali")
