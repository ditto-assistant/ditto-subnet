"""Unit tests for :mod:`ditto.miner_cli.models`.

Pin the contract callers depend on:
- frozen: mutation raises
- ``PreflightResult.passed`` ignores deferred checks
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from ditto.miner_cli.models import (
    NetworkConfig,
    PreflightCheckResult,
    PreflightResult,
    WalletHandle,
)


class TestFrozenContract:
    def test_network_config_is_frozen(self) -> None:
        cfg = NetworkConfig(name="x", api_url="y", subtensor_network="z")
        with pytest.raises(FrozenInstanceError):
            cfg.api_url = "other"  # type: ignore[misc]

    def test_wallet_handle_is_frozen(self) -> None:
        h = WalletHandle(coldkey_name="c", hotkey_name="h", hotkey_ss58="5G...")
        with pytest.raises(FrozenInstanceError):
            h.hotkey_ss58 = "other"  # type: ignore[misc]


class TestPreflightResultPassed:
    def test_all_checks_pass(self) -> None:
        result = PreflightResult(
            sha256="ab" * 32,
            file_size_bytes=100,
            checks=(
                PreflightCheckResult(name="a", passed=True, detail=""),
                PreflightCheckResult(name="b", passed=True, detail=""),
            ),
        )

        assert result.passed is True

    def test_any_real_check_failing_flips_passed_false(self) -> None:
        result = PreflightResult(
            sha256="ab" * 32,
            file_size_bytes=100,
            checks=(
                PreflightCheckResult(name="a", passed=True, detail=""),
                PreflightCheckResult(name="b", passed=False, detail="boom"),
            ),
        )

        assert result.passed is False

    def test_deferred_check_failures_are_ignored(self) -> None:
        """Deferred checks are placeholders; their pass status doesn't gate
        the upload. Pre-flight ``.passed`` must look only at real checks."""
        result = PreflightResult(
            sha256="ab" * 32,
            file_size_bytes=100,
            checks=(
                PreflightCheckResult(name="a", passed=True, detail=""),
                PreflightCheckResult(
                    name="manifest_present", passed=False, detail="x", deferred=True
                ),
            ),
        )

        assert result.passed is True
