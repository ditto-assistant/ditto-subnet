"""Unit tests for :mod:`ditto.miner_cli.commands.verify`."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ditto.miner_cli.commands.verify import run


def make_args(tar_path: Path) -> argparse.Namespace:
    return argparse.Namespace(tar_path=tar_path, network="local", verbose=False)


class TestVerify:
    def test_good_tar_exits_zero_and_prints_pass(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = run(make_args(good_tar))

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "result: PASS" in captured.out
        assert "sha256:" in captured.out

    def test_bad_gzip_exits_one_and_marks_check_failed(
        self, bad_gzip_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = run(make_args(bad_gzip_tar))

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "result: FAIL" in captured.out
        # The gzip_valid row shows FAIL, not DEFERRED.
        assert "gzip_valid" in captured.out
        assert "FAIL" in captured.out

    def test_missing_file_exits_one_and_prints_to_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = run(make_args(tmp_path / "nope.tar.gz"))

        captured = capsys.readouterr()
        assert exit_code == 1
        assert captured.err
        # Per-check table should not have been printed.
        assert "CHECK" not in captured.out

    def test_deferred_checks_appear_as_deferred_not_fail(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(make_args(good_tar))

        out = capsys.readouterr().out
        assert "DEFERRED" in out
        # All three deferred names show up.
        for name in ("manifest_present", "go_import_allowlist", "schema_diff"):
            assert name in out
