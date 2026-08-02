"""Unit tests for :mod:`ditto.api_server.revision`."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from ditto.api_server import revision


@pytest.fixture(autouse=True)
def _clear_cache():
    revision.reset_cache()
    yield
    revision.reset_cache()


class TestResolveCommitHash:
    def test_prefers_the_release_commit_without_a_git_checkout(self, monkeypatch):
        commit = "a" * 40
        monkeypatch.setenv("DITTO_BUILD_COMMIT", commit.upper())
        with patch("ditto.api_server.revision.subprocess.run") as run:
            assert revision.resolve_commit_hash() == commit
        run.assert_not_called()

    def test_invalid_release_commit_fails_closed(self, monkeypatch):
        monkeypatch.setenv("DITTO_BUILD_COMMIT", "main")
        with patch("ditto.api_server.revision.subprocess.run") as run:
            assert revision.resolve_commit_hash() == "unknown"
        run.assert_not_called()

    def test_returns_hex_on_success(self):
        result = MagicMock(returncode=0, stdout="abcdef1234567890\n")
        with patch("ditto.api_server.revision.subprocess.run", return_value=result):
            assert revision.resolve_commit_hash() == "abcdef1234567890"

    def test_non_zero_exit_falls_back_to_unknown(self):
        result = MagicMock(returncode=128, stdout="")
        with patch("ditto.api_server.revision.subprocess.run", return_value=result):
            assert revision.resolve_commit_hash() == "unknown"

    def test_empty_stdout_falls_back_to_unknown(self):
        result = MagicMock(returncode=0, stdout="\n")
        with patch("ditto.api_server.revision.subprocess.run", return_value=result):
            assert revision.resolve_commit_hash() == "unknown"

    def test_file_not_found_falls_back_to_unknown(self):
        with patch(
            "ditto.api_server.revision.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            assert revision.resolve_commit_hash() == "unknown"

    def test_timeout_falls_back_to_unknown(self):
        with patch(
            "ditto.api_server.revision.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=2),
        ):
            assert revision.resolve_commit_hash() == "unknown"


class TestCheckedOutCommit:
    """The checkout is re-read, but not once per request."""

    async def test_reports_the_current_checkout(self):
        with patch(
            "ditto.api_server.revision.resolve_commit_hash", return_value="aaa111"
        ):
            assert await revision.checked_out_commit() == "aaa111"

    async def test_caches_within_the_ttl(self):
        with patch(
            "ditto.api_server.revision.resolve_commit_hash", return_value="aaa111"
        ) as resolve:
            await revision.checked_out_commit()
            await revision.checked_out_commit()
            await revision.checked_out_commit()

        assert resolve.call_count == 1

    async def test_re_reads_after_the_ttl_expires(self):
        with patch(
            "ditto.api_server.revision.resolve_commit_hash", return_value="aaa111"
        ):
            assert await revision.checked_out_commit() == "aaa111"

        # A deploy moved the checkout; once the cache ages out, the probe must
        # see the new value rather than the boot-time one.
        with (
            patch(
                "ditto.api_server.revision.resolve_commit_hash", return_value="bbb222"
            ),
            patch("ditto.api_server.revision.CHECKED_OUT_TTL_SECONDS", -1.0),
        ):
            assert await revision.checked_out_commit() == "bbb222"

    async def test_unresolvable_checkout_is_unknown_not_an_error(self):
        with patch(
            "ditto.api_server.revision.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            assert await revision.checked_out_commit() == "unknown"


class TestCommitsDiverged:
    """Drift is only claimed when both revisions are actually known."""

    def test_true_when_both_known_and_different(self):
        assert revision.commits_diverged("aaa111", "bbb222") is True

    def test_false_when_equal(self):
        assert revision.commits_diverged("aaa111", "aaa111") is False

    @pytest.mark.parametrize(
        ("running", "checked_out"),
        [
            ("unknown", "bbb222"),
            ("aaa111", "unknown"),
            ("", "bbb222"),
            ("aaa111", ""),
            ("unknown", "unknown"),
        ],
    )
    def test_missing_revision_is_not_drift(self, running: str, checked_out: str):
        assert revision.commits_diverged(running, checked_out) is False
