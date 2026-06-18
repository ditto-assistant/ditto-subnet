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
        for name in ("manifest_present", "dependency_allowlist", "schema_diff"):
            assert name in out


class TestVerifyBoundaries:
    """Edge cases + boundary values per TESTING-STRATEGY §"How to add a test"
    step 5: empty input, invalid input, boundary values, error paths.

    Each test pins one invariant a refactor could silently break."""

    def test_oversize_tar_exits_one_with_file_size_fail(
        self, oversize_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Boundary: a tarball above MAX_TARBALL_SIZE_BYTES must fail
        the file_size check AND map to verify exit 1. The validator
        alone is tested elsewhere; this pins the verify subcommand's
        pass/fail mapping for the oversize path."""
        exit_code = run(make_args(oversize_tar))

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "result: FAIL" in captured.out
        # The file_size row shows FAIL, not DEFERRED.
        file_size_lines = [
            line for line in captured.out.splitlines() if "file_size" in line
        ]
        assert any("FAIL" in line for line in file_size_lines)

    def test_directory_path_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Invalid input: a directory path (not a regular file) raises
        TarStructureError inside run_preflight; verify must catch and
        exit 1 with a stderr message, never print the check table."""
        exit_code = run(make_args(tmp_path))

        captured = capsys.readouterr()
        assert exit_code == 1
        assert captured.err
        # No partial check table on the input-validation error path.
        assert "CHECK" not in captured.out

    def test_empty_valid_tar_passes(
        self, empty_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Edge: a structurally valid .tar.gz with zero entries. The
        container parses; real checks pass. Verify only validates the
        wrapper (gzip + tar structure + size + sha256); content
        validation lands when the deferred checks ship."""
        exit_code = run(make_args(empty_tar))

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "result: PASS" in captured.out
        # 0 entries is the legitimate case here, surfaced in the detail.
        tar_opens_lines = [
            line for line in captured.out.splitlines() if "tar_opens" in line
        ]
        assert any("0 entries" in line for line in tar_opens_lines)
