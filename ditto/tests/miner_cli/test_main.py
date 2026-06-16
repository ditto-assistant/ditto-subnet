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
