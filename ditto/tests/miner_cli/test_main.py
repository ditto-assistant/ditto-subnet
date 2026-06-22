"""Unit tests for :mod:`ditto.miner_cli.__main__`.

Argparse wiring + exit-code mapping. Heavy subcommand logic is
exercised in the per-subcommand tests; here we just pin the wiring.
"""

from __future__ import annotations

import pytest

from ditto.miner_cli.__main__ import main


class TestMain:
    def test_no_args_exits_with_subparser_required_message(self) -> None:
        with pytest.raises(SystemExit) as ex:
            main([])
        assert ex.value.code == 2

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as ex:
            main(["--help"])
        assert ex.value.code == 0
        out = capsys.readouterr().out
        assert "upload" in out or "verify" in out

    def test_unknown_network_rejected_by_argparse(self) -> None:
        with pytest.raises(SystemExit) as ex:
            main(["--network", "staging-canary", "verify", "/tmp/x.tar.gz"])
        # argparse exits 2 on invalid choices.
        assert ex.value.code == 2

    def test_verify_subcommand_dispatches_to_run(
        self, good_tar, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["verify", str(good_tar)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "PASS" in out


class TestFlagAliases:
    """Pin the bittensor-dotted flag names + their short aliases all
    parse to the same destination so renames in the future do not
    silently drop one of the entry points miners are typing."""

    @pytest.mark.parametrize(
        "flag",
        ["--subtensor.network", "--network"],
    )
    def test_network_aliases_accepted(self, flag: str, good_tar) -> None:
        rc = main([flag, "local", "verify", str(good_tar)])
        assert rc == 0

    @pytest.mark.parametrize(
        "flags",
        [
            ["--wallet.name", "miner", "--wallet.hotkey", "default"],
            ["--coldkey", "miner", "--hotkey", "default"],
            ["--wallet.name", "miner", "--hotkey", "default"],  # mixed forms
        ],
    )
    def test_upload_wallet_flag_aliases_parse(self, flags, good_tar) -> None:
        """``upload`` requires both wallet flags. All three alias
        combinations must satisfy the argparse layout (we do not run
        the subcommand here; we just confirm the parser accepts the
        flags + reaches the subcommand handler).
        """
        import argparse
        from unittest.mock import patch

        sentinel = argparse.Namespace(parsed=True)

        def _fake_run(args: argparse.Namespace) -> int:
            sentinel.coldkey = args.coldkey_name
            sentinel.hotkey = args.hotkey_name
            return 0

        with patch("ditto.miner_cli.commands.upload.run", side_effect=_fake_run):
            rc = main(["upload", str(good_tar), "--name", "smoke", *flags, "--yes"])

        assert rc == 0
        assert sentinel.coldkey == "miner"
        assert sentinel.hotkey == "default"
