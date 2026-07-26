"""Unit tests for :mod:`ditto.miner_cli.__main__`.

Argparse wiring + exit-code mapping. Heavy subcommand logic is
exercised in the per-subcommand tests; here we just pin the wiring.
"""

from __future__ import annotations

import pytest

from ditto.miner_cli.__main__ import _build_parser, main


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
            main(["--network", "staging-canary", "verify", "--path", "/tmp/x.tar.gz"])
        # argparse exits 2 on invalid choices.
        assert ex.value.code == 2

    def test_verify_subcommand_dispatches_to_run(
        self, good_tar, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["verify", "--path", str(good_tar)])
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
        rc = main([flag, "local", "verify", "--path", str(good_tar)])
        assert rc == 0

    @pytest.mark.parametrize(
        "flag",
        ["--subtensor.chain_endpoint", "--chain-endpoint"],
    )
    def test_chain_endpoint_aliases_resolve_to_same_dest(self, flag: str) -> None:
        """Both flag aliases store onto ``args.chain_endpoint`` so the
        bittensor-dotted form stays the canonical reference while the
        shorter alias remains a typing convenience."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "--network",
                "local",
                flag,
                "ws://example.org:9944",
                "verify",
                "--path",
                "/tmp/x",
            ]
        )
        assert args.chain_endpoint == "ws://example.org:9944"

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
            rc = main(
                [
                    "upload",
                    "--path",
                    str(good_tar),
                    "--name",
                    "smoke",
                    *flags,
                    "--yes",
                ]
            )

        assert rc == 0
        assert sentinel.coldkey == "miner"
        assert sentinel.hotkey == "default"


class TestAttestFlagWiring:
    """``attest`` carries two wallet pairs, and mixing them up would sign the
    halves with the wrong keys. Pin every alias to its destination."""

    @pytest.mark.parametrize(
        "flags",
        [
            [
                "--old-wallet.name",
                "old-miner",
                "--old-wallet.hotkey",
                "old-default",
                "--wallet.name",
                "miner",
                "--wallet.hotkey",
                "default",
            ],
            [
                "--old-coldkey",
                "old-miner",
                "--old-hotkey-name",
                "old-default",
                "--coldkey",
                "miner",
                "--hotkey",
                "default",
            ],
            [  # mixed forms
                "--old-wallet.name",
                "old-miner",
                "--old-hotkey-name",
                "old-default",
                "--coldkey",
                "miner",
                "--wallet.hotkey",
                "default",
            ],
        ],
    )
    def test_wallet_flag_aliases_parse_to_distinct_dests(self, flags) -> None:
        parser = _build_parser()
        args = parser.parse_args(["attest", *flags])

        assert args.old_coldkey_name == "old-miner"
        assert args.old_hotkey_name == "old-default"
        assert args.coldkey_name == "miner"
        assert args.hotkey_name == "default"

    def test_netuid_defaults_to_118_and_yes_print_only_default_false(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["attest"])

        assert args.netuid == 118
        assert args.yes is False
        assert args.print_only is False

    def test_netuid_flag_and_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parser = _build_parser()
        assert parser.parse_args(["attest", "--netuid", "42"]).netuid == 42

        monkeypatch.setenv("NETUID", "7")
        assert _build_parser().parse_args(["attest"]).netuid == 7

    @pytest.mark.parametrize("flag", ["-y", "--yes"])
    def test_yes_aliases(self, flag: str) -> None:
        parser = _build_parser()
        assert parser.parse_args(["attest", flag]).yes is True

    def test_print_only_sets_the_flag(self) -> None:
        parser = _build_parser()
        assert parser.parse_args(["attest", "--print-only"]).print_only is True

    def test_attest_dispatches_to_run(self) -> None:
        import argparse
        from unittest.mock import patch

        with patch("ditto.miner_cli.commands.attest.run", return_value=0) as fake_run:
            rc = main(["attest", "--old-coldkey", "old", "--coldkey", "new"])

        assert rc == 0
        assert isinstance(fake_run.call_args.args[0], argparse.Namespace)

    def test_help_states_the_scope_without_overstating_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The one thing a miner must not misread is what the link grants."""
        with pytest.raises(SystemExit) as ex:
            main(["attest", "--help"])
        assert ex.value.code == 0

        # argparse hard-wraps to terminal width; compare on collapsed
        # whitespace so the assertions pin wording, not line breaks.
        out = " ".join(capsys.readouterr().out.split())
        assert "plagiarism screening against the old hotkey's earlier work" in out
        assert "ONLY" in out
        assert "does NOT grant an additional emission slot" in out


class TestNetworkFlagPosition:
    """The shared top-level flags (``--network``, ``--chain-endpoint``,
    ``-v``) must accept BOTH the position before the subcommand AND
    the position after it. Mirrors the kubectl / aws / gh CLI norm.
    """

    def test_network_flag_before_subcommand(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--network", "local", "verify", "--path", "/tmp/x"])
        assert args.network == "local"

    def test_network_flag_after_subcommand(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["verify", "--path", "/tmp/x", "--network", "local"])
        assert args.network == "local"

    def test_chain_endpoint_before_subcommand(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "--network",
                "local",
                "--chain-endpoint",
                "ws://x:9944",
                "verify",
                "--path",
                "/tmp/x",
            ]
        )
        assert args.chain_endpoint == "ws://x:9944"

    def test_chain_endpoint_after_subcommand(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "verify",
                "--path",
                "/tmp/x",
                "--network",
                "local",
                "--chain-endpoint",
                "ws://x:9944",
            ]
        )
        assert args.chain_endpoint == "ws://x:9944"

    def test_default_network_is_finney_chain_endpoint_is_none(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["verify", "--path", "/tmp/x"])
        assert args.network == "finney"
        assert args.chain_endpoint is None
        assert args.verbose is False

    def test_chain_endpoint_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DITTO_SUBTENSOR_CHAIN_ENDPOINT", "ws://env:9944")
        parser = _build_parser()
        args = parser.parse_args(["verify", "--path", "/tmp/x"])
        assert args.chain_endpoint == "ws://env:9944"
