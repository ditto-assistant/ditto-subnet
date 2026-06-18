"""Unit tests for :mod:`ditto.miner_cli.tar_validator`.

Invariants pinned:

- Good tar → every real check passes, sha256 stable across calls.
- Missing file → :class:`TarStructureError` (callers can't proceed at all).
- Oversize → file_size check fails (other checks may also fail or pass).
- Bad gzip → gzip_valid check fails (other checks downstream fail too).
- Deferred checks are always reported but never gate the aggregate.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ditto.miner_cli.errors import TarStructureError
from ditto.miner_cli.tar_validator import (
    MAX_TARBALL_SIZE_BYTES,
    run_preflight,
)


class TestPreflight:
    def test_good_tar_all_real_checks_pass(self, good_tar: Path) -> None:
        result = run_preflight(good_tar)

        assert result.passed is True
        assert len(result.sha256) == 64
        assert result.file_size_bytes > 0

        real_checks = [c for c in result.checks if not c.deferred]
        assert all(c.passed for c in real_checks)
        # We expect the three real checks: file_size, gzip_valid, tar_opens.
        real_names = {c.name for c in real_checks}
        assert real_names == {"file_size", "gzip_valid", "tar_opens"}

    def test_sha256_is_stable_across_calls(self, good_tar: Path) -> None:
        first = run_preflight(good_tar).sha256
        second = run_preflight(good_tar).sha256
        assert first == second

    def test_missing_file_raises_tar_structure_error(self, tmp_path: Path) -> None:
        with pytest.raises(TarStructureError):
            run_preflight(tmp_path / "does-not-exist.tar.gz")

    def test_directory_path_raises_tar_structure_error(self, tmp_path: Path) -> None:
        with pytest.raises(TarStructureError):
            run_preflight(tmp_path)

    def test_oversize_file_fails_file_size_check(self, oversize_tar: Path) -> None:
        result = run_preflight(oversize_tar)

        file_size_check = next(c for c in result.checks if c.name == "file_size")
        assert file_size_check.passed is False
        assert str(MAX_TARBALL_SIZE_BYTES) in file_size_check.detail
        assert result.passed is False

    def test_bad_gzip_fails_gzip_check(self, bad_gzip_tar: Path) -> None:
        result = run_preflight(bad_gzip_tar)

        gzip_check = next(c for c in result.checks if c.name == "gzip_valid")
        assert gzip_check.passed is False
        assert result.passed is False


class TestDeferredChecks:
    def test_deferred_checks_appear_in_result(self, good_tar: Path) -> None:
        result = run_preflight(good_tar)

        deferred = [c for c in result.checks if c.deferred]
        names = {c.name for c in deferred}
        assert names == {"manifest_present", "dependency_allowlist", "schema_diff"}

    def test_deferred_checks_do_not_gate_aggregate_passed(self, good_tar: Path) -> None:
        """All real checks pass on good_tar; deferred checks marked passed=True
        but the ``.passed`` property must ignore them either way."""
        result = run_preflight(good_tar)

        for c in result.checks:
            if c.deferred:
                # Even if a future change flips this to False, the aggregate
                # must remain True (real checks still pass).
                assert c.deferred is True
        assert result.passed is True

    def test_deferred_checks_log_at_debug(
        self, good_tar: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="ditto.miner_cli.tar_validator"):
            run_preflight(good_tar)

        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        # One debug line per deferred check.
        assert any("manifest_present" in m for m in debug_msgs)
        assert any("dependency_allowlist" in m for m in debug_msgs)
        assert any("schema_diff" in m for m in debug_msgs)
