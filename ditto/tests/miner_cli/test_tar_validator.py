"""Unit tests for :mod:`ditto.miner_cli.tar_validator`.

Invariants pinned:

- Good tar → every real check passes, sha256 stable across calls.
- Missing file → :class:`TarStructureError` (callers can't proceed at all).
- Oversize → file_size check fails (other checks may also fail or pass).
- Bad gzip → gzip_valid check fails (other checks downstream fail too).
- Deferred checks are always reported but never gate the aggregate.
"""

from __future__ import annotations

import io
import logging
import tarfile
from pathlib import Path

import pytest

from ditto.miner_cli import tar_validator
from ditto.miner_cli.errors import TarStructureError
from ditto.miner_cli.models import PreflightCheckResult
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
        real_names = {c.name for c in real_checks}
        assert real_names == {
            "file_size",
            "gzip_valid",
            "tar_opens",
            "archive_contract",
        }

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
        assert any("manifest_present" in m for m in debug_msgs)
        assert any("dependency_allowlist" in m for m in debug_msgs)
        assert any("schema_diff" in m for m in debug_msgs)


def _archive(
    tmp_path: Path, members: list[tarfile.TarInfo | tuple[str, bytes]]
) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                tar.addfile(member)
                continue
            name, data = member
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    dest = tmp_path / "harness.tar.gz"
    dest.write_bytes(buf.getvalue())
    return dest


def _symlink(name: str, target: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info


_DOCKERFILE = ("Dockerfile", b"FROM scratch\n")


class TestArchiveContract:
    """Mirror of the screener's pre-build archive contract (gate.py)."""

    def _contract(self, path: Path) -> PreflightCheckResult:
        return next(
            c for c in run_preflight(path).checks if c.name == "archive_contract"
        )

    @pytest.mark.parametrize(
        ("members", "code"),
        [
            ([("src/main.rs", b"fn main() {}\n")], "SCR-CONTRACT-001"),
            ([("harness/Dockerfile", b"FROM scratch\n")], "SCR-CONTRACT-001"),
            ([("Dockerfile", b"\xff\xfe")], "SCR-CONTRACT-003"),
            ([_DOCKERFILE, _symlink("config", "/etc/passwd")], "SCR-ARCHIVE-003"),
            ([_DOCKERFILE, ("../escape", b"x")], "SCR-ARCHIVE-001"),
            ([_DOCKERFILE, ("/abs", b"x")], "SCR-ARCHIVE-001"),
            ([_DOCKERFILE, ("src//main.rs", b"x")], "SCR-ARCHIVE-001"),
            ([_DOCKERFILE, _DOCKERFILE], "SCR-ARCHIVE-002"),
        ],
        ids=[
            "no-dockerfile",
            "dockerfile-not-at-root",
            "non-utf8-dockerfile",
            "symlink",
            "parent-traversal",
            "absolute-path",
            "non-canonical-path",
            "duplicate-path",
        ],
    )
    def test_screener_rejection_fails_before_upload(
        self,
        tmp_path: Path,
        members: list[tarfile.TarInfo | tuple[str, bytes]],
        code: str,
    ) -> None:
        path = _archive(tmp_path, members)

        check = self._contract(path)

        assert check.passed is False
        assert check.detail.startswith(code)
        assert run_preflight(path).passed is False

    def test_dot_slash_prefixed_harness_passes(self, tmp_path: Path) -> None:
        # `tar -czf x.tgz .` writes ./Dockerfile; the screener accepts it.
        dot_root = tarfile.TarInfo(name=".")
        dot_root.type = tarfile.DIRTYPE
        path = _archive(
            tmp_path,
            [dot_root, ("./Dockerfile", b"FROM scratch\n"), ("./src/main.rs", b"x")],
        )

        assert self._contract(path).passed is True
        assert run_preflight(path).passed is True

    def test_member_and_unpacked_limits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _archive(tmp_path, [_DOCKERFILE, ("a", b"xx"), ("b", b"yy")])
        monkeypatch.setattr(tar_validator, "MAX_ARCHIVE_MEMBERS", 2)
        assert self._contract(path).detail.startswith("SCR-ARCHIVE-005")
        monkeypatch.setattr(tar_validator, "MAX_ARCHIVE_MEMBERS", 20_000)
        monkeypatch.setattr(tar_validator, "MAX_UNPACKED_BYTES", 16)
        assert self._contract(path).detail.startswith("SCR-ARCHIVE-004")

    def test_unreadable_tar_skips_the_contract_check(self, bad_gzip_tar: Path) -> None:
        names = [c.name for c in run_preflight(bad_gzip_tar).checks]
        assert "archive_contract" not in names
