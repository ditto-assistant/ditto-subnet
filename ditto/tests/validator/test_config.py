"""Tests for KOTH+ATH knob parsing/validation in the validator config."""

from __future__ import annotations

import pytest

from ditto.validator.config import parse_validator_config_from_env
from ditto.validator.errors import ValidatorConfigError

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal env under which parse succeeds (mock scoring + wallet + Pylon)."""
    monkeypatch.setenv("VALIDATOR_DITTOBENCH_MOCK", "true")
    monkeypatch.setenv("VALIDATOR_HOTKEY", _HOTKEY)
    monkeypatch.setenv("VALIDATOR_WALLET_NAME", "coldkey")
    monkeypatch.setenv("VALIDATOR_WALLET_HOTKEY", "hotkey")
    monkeypatch.setenv("PYLON_IDENTITY_NAME", "ditto")
    monkeypatch.setenv("PYLON_TOKEN", "tok")


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
        assert cfg.koth_confirmation_seeds == 3
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
            "VALIDATOR_KOTH_CONFIRMATION_SEEDS",
        ):
            monkeypatch.setenv(var, "999")
        cfg = parse_validator_config_from_env()
        assert cfg.koth_margin == 0.05
        assert cfg.koth_tail_size == 4
        assert cfg.koth_champion_share == 0.9
        assert cfg.koth_dethrone_z == 1.64
        assert cfg.koth_confirmation_seeds == 3


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


class TestRequiredConfig:
    """Every validator both scores and sets weights, so all of it is required."""

    def test_one_pylon_token_used_for_both(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        cfg = parse_validator_config_from_env()
        # The single PYLON_TOKEN drives the identity write too.
        assert cfg.pylon_token == "tok"
        assert not hasattr(cfg, "pylon_identity_token")

    def test_pylon_token_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        monkeypatch.delenv("PYLON_TOKEN", raising=False)
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()

    def test_pylon_identity_name_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.delenv("PYLON_IDENTITY_NAME", raising=False)
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()

    def test_dittobench_url_required_without_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.delenv("VALIDATOR_DITTOBENCH_MOCK", raising=False)
        monkeypatch.delenv("VALIDATOR_DITTOBENCH_API_URL", raising=False)
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()
