"""Tests for KOTH+ATH knob parsing/validation in the validator config."""

from __future__ import annotations

import pytest

from ditto.validator.config import parse_validator_config_from_env
from ditto.validator.errors import ValidatorConfigError

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_MNEMONIC = "bottom drive obey lake curtain smoke basket hold race lonely fit walk"


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal env under which parse succeeds (mock scoring + Pylon identity)."""
    monkeypatch.setenv("VALIDATOR_DITTOBENCH_MOCK", "true")
    monkeypatch.setenv("VALIDATOR_HOTKEY", _HOTKEY)
    monkeypatch.setenv("VALIDATOR_MNEMONIC", _MNEMONIC)
    monkeypatch.setenv("PYLON_IDENTITY_NAME", "ditto")
    monkeypatch.setenv("PYLON_IDENTITY_TOKEN", "tok")


class TestKothConfig:
    def test_frozen_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        cfg = parse_validator_config_from_env()
        # Consensus-critical mechanism values are frozen in code (the KOTH_*
        # constants), not env, so every validator folds identically.
        assert cfg.koth_margin == 0.05
        assert cfg.koth_tail_size == 4
        assert cfg.koth_champion_share == 0.9
        assert cfg.koth_dethrone_z == 1.64
        # Cadence knobs stay env-driven, with these defaults.
        assert cfg.sweep_seconds == 120
        assert cfg.epoch_seconds == 3600

    def test_env_cannot_override_frozen_knobs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        for var in (
            "VALIDATOR_KOTH_MARGIN",
            "VALIDATOR_KOTH_TAIL_SIZE",
            "VALIDATOR_KOTH_CHAMPION_SHARE",
            "VALIDATOR_KOTH_DETHRONE_Z",
        ):
            monkeypatch.setenv(var, "999")
        cfg = parse_validator_config_from_env()
        assert cfg.koth_margin == 0.05
        assert cfg.koth_tail_size == 4
        assert cfg.koth_champion_share == 0.9
        assert cfg.koth_dethrone_z == 1.64


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
        # An independent (weights-only) validator: scoring off, so no dittobench-api
        # URL is required even outside mock mode. It still needs Pylon identity to
        # write weights.
        monkeypatch.setenv("VALIDATOR_HOTKEY", _HOTKEY)
        monkeypatch.setenv("VALIDATOR_MNEMONIC", _MNEMONIC)
        monkeypatch.setenv("PYLON_IDENTITY_NAME", "ditto")
        monkeypatch.setenv("PYLON_IDENTITY_TOKEN", "tok")
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
        # A scoring-only instance: weights off, so no Pylon identity is required
        # even without the SDK-weights escape hatch.
        monkeypatch.setenv("VALIDATOR_HOTKEY", _HOTKEY)
        monkeypatch.setenv("VALIDATOR_MNEMONIC", _MNEMONIC)
        monkeypatch.setenv("VALIDATOR_DITTOBENCH_MOCK", "true")
        monkeypatch.setenv("VALIDATOR_ENABLE_WEIGHTS", "false")
        for k in (
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
