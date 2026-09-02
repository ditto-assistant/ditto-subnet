"""Tests for KOTH+ATH knob parsing/validation in the validator config."""

from __future__ import annotations

from uuid import UUID

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
        assert cfg.top5_max_confirmation_seeds == 15
        assert cfg.miner_emission_share == 1.0
        assert cfg.burn_hotkey == FINNEY_BURN_HOTKEY
        # Cadence knobs stay env-driven, with these defaults.
        assert cfg.sweep_seconds == 30
        assert cfg.epoch_seconds == 3600
        assert cfg.dittobench_timeout_seconds == 9900

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


class TestLongMemCapacity:
    @pytest.mark.parametrize(
        ("benchmark_capacity", "expected"),
        [(1, 1), (2, 1), (3, 2), (8, 4)],
    )
    def test_defaults_to_half_ordinary_capacity_rounded_up(
        self,
        monkeypatch: pytest.MonkeyPatch,
        benchmark_capacity: int,
        expected: int,
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_BENCHMARK_CAPACITY", str(benchmark_capacity))
        monkeypatch.delenv("VALIDATOR_LONGMEM_CAPACITY", raising=False)

        assert parse_validator_config_from_env().longmem_capacity == expected

    @pytest.mark.parametrize(
        ("benchmark_capacity", "longmem_capacity"),
        [(1, 2), (2, 2), (8, 5), (8, -1)],
    )
    def test_rejects_unsafe_independent_capacity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        benchmark_capacity: int,
        longmem_capacity: int,
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_BENCHMARK_CAPACITY", str(benchmark_capacity))
        monkeypatch.setenv("VALIDATOR_LONGMEM_CAPACITY", str(longmem_capacity))

        with pytest.raises(ValidatorConfigError, match="VALIDATOR_LONGMEM_CAPACITY"):
            parse_validator_config_from_env()

    def test_zero_explicitly_disables_lane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_BENCHMARK_CAPACITY", "8")
        monkeypatch.setenv("VALIDATOR_LONGMEM_CAPACITY", "0")

        assert parse_validator_config_from_env().longmem_capacity == 0


class TestCodingShadowConfig:
    def test_default_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        config = parse_validator_config_from_env()
        assert config.coding_shadow_enabled is False
        assert config.coding_shadow_run_id is None
        assert config.coding_executor_remote_enabled is False
        assert config.coding_executor_connectivity_canary_enabled is False
        assert config.coding_executor_base_url == ""

    def test_enable_requires_stable_identity_and_control_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_CODING_SHADOW_ENABLED", "true")
        with pytest.raises(ValidatorConfigError, match="shadow coding"):
            parse_validator_config_from_env()
        monkeypatch.setenv(
            "VALIDATOR_DITTOBENCH_CONTROL_TOKEN",
            "coding-shadow-control-token-0000000000000001",
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_SHADOW_INSTANCE_ID", "coding-shadow-primary"
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_SHADOW_RUN_ID",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        config = parse_validator_config_from_env()
        assert config.coding_shadow_enabled is True
        assert config.coding_shadow_instance_id == "coding-shadow-primary"
        assert config.coding_shadow_run_id == UUID(
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        )

    def test_remote_executor_requires_complete_explicit_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv(
            "VALIDATOR_CODING_EXECUTOR_BASE_URL", "https://10.23.0.10:9443"
        )
        with pytest.raises(ValidatorConfigError, match="explicit executor gate"):
            parse_validator_config_from_env()

        monkeypatch.setenv("VALIDATOR_CODING_EXECUTOR_REMOTE_ENABLED", "true")
        with pytest.raises(ValidatorConfigError, match="enabled coding executor"):
            parse_validator_config_from_env()

        monkeypatch.setenv("VALIDATOR_CODING_SHADOW_ENABLED", "true")
        monkeypatch.setenv(
            "VALIDATOR_CODING_SHADOW_INSTANCE_ID", "coding-shadow-primary"
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_SHADOW_RUN_ID",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        monkeypatch.setenv(
            "VALIDATOR_DITTOBENCH_CONTROL_TOKEN",
            "coding-shadow-control-token-0000000000000001",
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_EXECUTOR_CA_PATH", "/run/secrets/executor-ca.pem"
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_EXECUTOR_CLIENT_CERT_PATH",
            "/run/secrets/validator-client.pem",
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_EXECUTOR_CLIENT_KEY_PATH",
            "/run/secrets/validator-client-key.pem",
        )
        config = parse_validator_config_from_env()
        assert config.coding_executor_remote_enabled is True
        assert config.coding_executor_base_url == "https://10.23.0.10:9443"
        assert config.coding_executor_timeout_seconds == 30.0

    def test_connectivity_canary_is_ticketless_and_exclusive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv(
            "VALIDATOR_CODING_EXECUTOR_CONNECTIVITY_CANARY_ENABLED", "true"
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_EXECUTOR_BASE_URL", "https://10.23.0.10:9443"
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_EXECUTOR_CA_PATH", "/run/secrets/executor-ca.pem"
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_EXECUTOR_CLIENT_CERT_PATH",
            "/run/secrets/validator-client.pem",
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_EXECUTOR_CLIENT_KEY_PATH",
            "/run/secrets/validator-client-key.pem",
        )
        config = parse_validator_config_from_env()
        assert config.coding_executor_connectivity_canary_enabled is True
        assert config.coding_executor_remote_enabled is False
        assert config.coding_shadow_enabled is False
        assert config.coding_shadow_run_id is None

        monkeypatch.setenv("VALIDATOR_CODING_SHADOW_ENABLED", "true")
        monkeypatch.setenv(
            "VALIDATOR_CODING_SHADOW_INSTANCE_ID", "coding-shadow-primary"
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_SHADOW_RUN_ID",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        monkeypatch.setenv(
            "VALIDATOR_DITTOBENCH_CONTROL_TOKEN",
            "coding-shadow-control-token-0000000000000001",
        )
        with pytest.raises(ValidatorConfigError, match="ticketless and exclusive"):
            parse_validator_config_from_env()

    @pytest.mark.parametrize(
        "value",
        [
            "http://10.23.0.10:9443",
            "https://8.8.8.8:9443",
            "https://192.0.2.1:9443",
            "https://10.23.0.10:443",
            "https://executor.internal:9443",
            "https://10.23.0.10:9443/path",
        ],
    )
    def test_remote_executor_rejects_nonprivate_origin(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_CODING_SHADOW_ENABLED", "true")
        monkeypatch.setenv(
            "VALIDATOR_CODING_SHADOW_INSTANCE_ID", "coding-shadow-primary"
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_SHADOW_RUN_ID",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        monkeypatch.setenv(
            "VALIDATOR_DITTOBENCH_CONTROL_TOKEN",
            "coding-shadow-control-token-0000000000000001",
        )
        monkeypatch.setenv("VALIDATOR_CODING_EXECUTOR_REMOTE_ENABLED", "true")
        monkeypatch.setenv("VALIDATOR_CODING_EXECUTOR_BASE_URL", value)
        monkeypatch.setenv("VALIDATOR_CODING_EXECUTOR_CA_PATH", "/run/ca.pem")
        monkeypatch.setenv(
            "VALIDATOR_CODING_EXECUTOR_CLIENT_CERT_PATH", "/run/client.pem"
        )
        monkeypatch.setenv(
            "VALIDATOR_CODING_EXECUTOR_CLIENT_KEY_PATH", "/run/client-key.pem"
        )
        with pytest.raises(ValidatorConfigError, match="enabled coding executor"):
            parse_validator_config_from_env()

    @pytest.mark.parametrize(
        "value",
        ["not-a-uuid", "00000000-0000-0000-0000-000000000000"],
    )
    def test_rejects_invalid_run_fence(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_CODING_SHADOW_RUN_ID", value)
        with pytest.raises(ValidatorConfigError, match="CODING_SHADOW_RUN_ID"):
            parse_validator_config_from_env()


class TestCodingCanaryConfig:
    def test_default_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch)
        config = parse_validator_config_from_env()
        assert config.coding_canary_enabled is False

    def test_enable_requires_control_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_CODING_CANARY_ENABLED", "true")
        with pytest.raises(ValidatorConfigError, match="coding canary"):
            parse_validator_config_from_env()
        monkeypatch.setenv(
            "VALIDATOR_DITTOBENCH_CONTROL_TOKEN",
            "coding-canary-control-token-0000000000000001",
        )
        config = parse_validator_config_from_env()
        assert config.coding_canary_enabled is True


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

    def test_dittobench_control_token_is_read_and_trimmed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.setenv("VALIDATOR_DITTOBENCH_CONTROL_TOKEN", "  scorer-token \n")
        assert (
            parse_validator_config_from_env().dittobench_control_token == "scorer-token"
        )

    def test_dittobench_control_token_defaults_to_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base_env(monkeypatch)
        monkeypatch.delenv("VALIDATOR_DITTOBENCH_CONTROL_TOKEN", raising=False)
        assert parse_validator_config_from_env().dittobench_control_token == ""


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
