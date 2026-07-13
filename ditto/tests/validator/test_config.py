"""Tests for KOTH+ATH knob parsing/validation in the validator config."""

from __future__ import annotations

import pytest

from ditto.validator.config import parse_validator_config_from_env
from ditto.validator.errors import ValidatorConfigError

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_MNEMONIC = "bottom drive obey lake curtain smoke basket hold race lonely fit walk"


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal env under which parse succeeds (mock + SDK skip most requirements)."""
    monkeypatch.setenv("VALIDATOR_DITTOBENCH_MOCK", "true")
    monkeypatch.setenv("VALIDATOR_USE_SDK_WEIGHTS", "true")
    monkeypatch.setenv("VALIDATOR_HOTKEY", _HOTKEY)
    monkeypatch.setenv("VALIDATOR_MNEMONIC", _MNEMONIC)
    for k in (
        "VALIDATOR_KOTH_MARGIN",
        "VALIDATOR_KOTH_TAIL_SIZE",
        "VALIDATOR_KOTH_CHAMPION_SHARE",
    ):
        monkeypatch.delenv(k, raising=False)


class TestKothConfig:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        cfg = parse_validator_config_from_env()
        # v2 / bench_version 2 retune: 5% margin ≥ 3σ/composite at the σ ≤ 0.01
        # target (was 1%; see config.py B8 note).
        assert cfg.koth_margin == 0.05
        assert cfg.koth_tail_size == 4
        assert cfg.koth_champion_share == 0.9
        # Weight-set cadence is decoupled from the (faster) scoring sweep.
        assert cfg.sweep_seconds == 120
        assert cfg.epoch_seconds == 3600
        # version_key defaults to the package spec version so it advances with
        # releases; every validator on a network must agree on it.
        from ditto import __spec_version__

        assert cfg.weight_version_key == __spec_version__

    def test_weight_version_key_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_WEIGHT_VERSION_KEY", "7")
        assert parse_validator_config_from_env().weight_version_key == 7

    @pytest.mark.parametrize("val", ["nan", "inf", "-inf", "0", "-0.5"])
    def test_bad_margin_rejected(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        # NaN/Inf are the ones that slip past a bare ``<= 0`` and would silently
        # disable the ATH gate → validator diverges from consensus.
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_KOTH_MARGIN", val)
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()

    def test_non_numeric_margin_is_typed_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_KOTH_MARGIN", "abc")
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()

    @pytest.mark.parametrize("val", ["nan", "inf", "1.5", "0", "-0.1"])
    def test_bad_champion_share_rejected(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_KOTH_CHAMPION_SHARE", val)
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()

    def test_negative_tail_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_KOTH_TAIL_SIZE", "-1")
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()

    def test_non_numeric_tail_is_typed_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_KOTH_TAIL_SIZE", "4.5")
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()


class TestMinStakeConfig:
    def test_default_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        monkeypatch.delenv("VALIDATOR_MIN_STAKE_TAO", raising=False)
        assert parse_validator_config_from_env().min_stake_tao == 0.0

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_MIN_STAKE_TAO", "1000")
        assert parse_validator_config_from_env().min_stake_tao == 1000.0

    @pytest.mark.parametrize("val", ["nan", "inf", "-1", "abc"])
    def test_bad_min_stake_rejected(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_MIN_STAKE_TAO", val)
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()


class TestRoleConfig:
    """The scoring / weight role flags gate which env is required."""

    def test_defaults_both_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        cfg = parse_validator_config_from_env()
        assert cfg.enable_scoring is True
        assert cfg.enable_weights is True

    def test_weights_only_needs_no_dittobench(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An independent (weights-only) validator: scoring off, so no
        # dittobench-api URL is required outside mock mode either.
        monkeypatch.setenv("VALIDATOR_HOTKEY", _HOTKEY)
        monkeypatch.setenv("VALIDATOR_MNEMONIC", _MNEMONIC)
        monkeypatch.setenv("VALIDATOR_USE_SDK_WEIGHTS", "true")
        monkeypatch.setenv("VALIDATOR_ENABLE_SCORING", "false")
        for k in (
            "VALIDATOR_DITTOBENCH_MOCK",
            "VALIDATOR_DITTOBENCH_API_URL",
        ):
            monkeypatch.delenv(k, raising=False)
        cfg = parse_validator_config_from_env()
        assert cfg.enable_scoring is False
        assert cfg.enable_weights is True

    def test_scoring_only_needs_no_pylon_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The central scorer: weights off, so no Pylon identity is required even
        # without the SDK-weights escape hatch.
        monkeypatch.setenv("VALIDATOR_HOTKEY", _HOTKEY)
        monkeypatch.setenv("VALIDATOR_MNEMONIC", _MNEMONIC)
        monkeypatch.setenv("VALIDATOR_DITTOBENCH_MOCK", "true")
        monkeypatch.setenv("VALIDATOR_ENABLE_WEIGHTS", "false")
        for k in (
            "VALIDATOR_USE_SDK_WEIGHTS",
            "PYLON_IDENTITY_NAME",
            "PYLON_IDENTITY_TOKEN",
        ):
            monkeypatch.delenv(k, raising=False)
        cfg = parse_validator_config_from_env()
        assert cfg.enable_weights is False
        assert cfg.enable_scoring is True

    def test_both_disabled_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_ENABLE_SCORING", "false")
        monkeypatch.setenv("VALIDATOR_ENABLE_WEIGHTS", "false")
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()
