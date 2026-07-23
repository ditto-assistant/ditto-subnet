"""Tests for KOTH+ATH knob parsing/validation in the validator config."""

from __future__ import annotations

import pytest

from ditto.validator.config import FINNEY_BURN_HOTKEY, parse_validator_config_from_env
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
        assert cfg.koth_margin == 0.007
        assert cfg.koth_tail_size == 4
        assert cfg.koth_rank_shares == (0.65, 0.14, 0.10, 0.07, 0.04)
        assert cfg.koth_dethrone_z == 1.64
        assert cfg.koth_confirmation_seeds == 3
        assert cfg.miner_emission_share == 1.0
        assert cfg.burn_hotkey == FINNEY_BURN_HOTKEY
        # Cadence knobs stay env-driven, with these defaults.
        assert cfg.sweep_seconds == 30
        assert cfg.epoch_seconds == 3600
        assert cfg.dittobench_timeout_seconds == 4500
        assert cfg.embed_preflight_timeout_seconds == 5

    def test_env_cannot_override_frozen_knobs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        for var in (
            "VALIDATOR_KOTH_MARGIN",
            "VALIDATOR_KOTH_TAIL_SIZE",
            "VALIDATOR_KOTH_CHAMPION_SHARE",
            "VALIDATOR_KOTH_RANK_SHARES",
            "VALIDATOR_KOTH_DETHRONE_Z",
            "VALIDATOR_KOTH_CONFIRMATION_SEEDS",
            "VALIDATOR_MINER_EMISSION_SHARE",
            "VALIDATOR_BURN_HOTKEY",
        ):
            monkeypatch.setenv(var, "999")
        cfg = parse_validator_config_from_env()
        assert cfg.koth_margin == 0.007
        assert cfg.koth_tail_size == 4
        assert cfg.koth_rank_shares == (0.65, 0.14, 0.10, 0.07, 0.04)
        assert cfg.koth_dethrone_z == 1.64
        assert cfg.koth_confirmation_seeds == 3
        assert cfg.miner_emission_share == 1.0
        assert cfg.burn_hotkey == FINNEY_BURN_HOTKEY

    def test_localnet_burns_to_local_owner_validator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("SUBTENSOR_NETWORK", "local")
        cfg = parse_validator_config_from_env()
        assert cfg.burn_hotkey == _HOTKEY

    @pytest.mark.parametrize(
        "network",
        ["wss://archive.chain.opentensor.ai:443", "wss://finney.example.com/ws"],
    )
    def test_custom_finney_endpoint_burns_to_fixed_owner(
        self, monkeypatch: pytest.MonkeyPatch, network: str
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("SUBTENSOR_NETWORK", network)
        assert parse_validator_config_from_env().burn_hotkey == FINNEY_BURN_HOTKEY

    @pytest.mark.parametrize(
        "network", ["localhost", "127.0.0.1", "ws://127.0.0.1:9944", "ws://[::1]:9944"]
    )
    def test_local_endpoint_burns_to_local_owner_validator(
        self, monkeypatch: pytest.MonkeyPatch, network: str
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("SUBTENSOR_NETWORK", network)
        assert parse_validator_config_from_env().burn_hotkey == _HOTKEY


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

    def test_embed_preflight_url_required_without_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.delenv("VALIDATOR_DITTOBENCH_MOCK", raising=False)
        monkeypatch.setenv("VALIDATOR_DITTOBENCH_API_URL", "http://scorer")
        monkeypatch.delenv("VALIDATOR_EMBED_PREFLIGHT_URL", raising=False)
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()

    @pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "bad"])
    def test_embed_preflight_timeout_must_be_positive_finite(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_EMBED_PREFLIGHT_TIMEOUT_SECONDS", value)
        with pytest.raises(ValidatorConfigError):
            parse_validator_config_from_env()


class TestCompatibilityEpoch:
    def test_matching_epoch_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_EXPECTED_COMPATIBILITY_EPOCH", "2")
        parse_validator_config_from_env()

    @pytest.mark.parametrize("value", ["0", "1", "invalid", ""])
    def test_mismatch_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_EXPECTED_COMPATIBILITY_EPOCH", value)
        with pytest.raises(ValidatorConfigError, match="compatibility epoch mismatch"):
            parse_validator_config_from_env()
