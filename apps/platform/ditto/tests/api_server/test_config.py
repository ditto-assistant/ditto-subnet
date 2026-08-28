"""Unit tests for :mod:`ditto.api_server.config`."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ditto.api_models.inference_concurrency_settings import (
    DEFAULT_CHAT_REQUEST_BUDGET,
    MAX_CHAT_REQUEST_BUDGET,
    MAX_CHAT_TOKEN_BUDGET,
)
from ditto.api_server.config import check_config, parse_api_server_config_from_env
from ditto.api_server.errors import ApiServerConfigError
from ditto.api_server.validator_names import ValidatorNamesConfig
from ditto.tests.api_server.conftest import make_api_server_config


def _set_minimum_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum env vars to make every sub-config parser succeed."""
    monkeypatch.setenv("POSTGRES_USER", "u")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p")
    monkeypatch.setenv("POSTGRES_DB", "d")
    monkeypatch.setenv("PYLON_OPEN_ACCESS_TOKEN", "tok")
    monkeypatch.setenv(
        "DITTO_UPLOAD_PAYMENT_ADDRESS",
        "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    )
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("STORAGE_BUCKET", "ditto-agents")
    monkeypatch.setenv("STORAGE_ACCESS_KEY", "minio")
    monkeypatch.setenv("STORAGE_SECRET_KEY", "miniominio")
    monkeypatch.setenv(
        "SCREENER_HOTKEY",
        "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    )
    monkeypatch.setenv(
        "SCREENER_API_TOKEN", "test-screener-token-at-least-32-characters"
    )
    # Override unset by default; tested explicitly elsewhere.
    monkeypatch.delenv("TAO_PRICE_OVERRIDE_USD", raising=False)
    monkeypatch.delenv("DITTO_TAOSTATS_VALIDATOR_NAMES_URL", raising=False)
    monkeypatch.delenv("DITTO_TAOSTATS_API_KEY", raising=False)
    monkeypatch.delenv("DITTO_TARGON_API_KEY", raising=False)
    monkeypatch.delenv("DITTO_TARGON_API_KEY_FILE", raising=False)
    monkeypatch.delenv("DITTO_TARGON_PUBLIC_PLATFORM_URL", raising=False)
    monkeypatch.delenv("DITTO_TARGON_SUBMISSION_BUILDER_IMAGE", raising=False)
    monkeypatch.delenv("DITTO_TARGON_SMOKE_PROVISION_TIMEOUT_SECONDS", raising=False)


class TestParseApiServerConfigFromEnv:
    """Tests for the env-var builder."""

    def test_defaults_apply_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.delenv("API_HOST", raising=False)
        monkeypatch.delenv("API_PORT", raising=False)
        monkeypatch.delenv("API_LOG_LEVEL", raising=False)

        config = parse_api_server_config_from_env(commit_hash="abc")

        assert config.host == "0.0.0.0"
        assert config.port == 8000
        assert config.log_level == "INFO"
        assert config.commit_hash == "abc"
        assert config.validator_names.url is None
        assert config.validator_names.api_key is None
        assert config.validator_compatibility.minimum_software_version == "0.7.0"
        assert config.validator_compatibility.minimum_protocol_version == 4
        assert config.validator_compatibility.heartbeat_max_age_seconds == 300
        assert config.inference_proxy.routing_mode == "aggregate_throughput"
        assert config.inference_proxy.route_min_calibration_samples == 60
        assert config.inference_proxy.per_ticket_concurrency == 16
        assert config.inference_proxy.per_validator_concurrency == 48
        assert config.inference_proxy.global_concurrency == 96

    def test_free_taostats_key_config_is_optional(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _set_minimum_env(monkeypatch)
        url = "https://api.taostats.io/api/dtao/validator/available/v1?netuid=118"
        monkeypatch.setenv("DITTO_TAOSTATS_VALIDATOR_NAMES_URL", url)
        monkeypatch.setenv("DITTO_TAOSTATS_API_KEY", "free-api-key")

        config = parse_api_server_config_from_env(commit_hash="abc")

        assert config.validator_names.url == url
        assert config.validator_names.api_key == "free-api-key"
        assert config.validator_names.enabled is True

    def test_backroom_policy_is_not_environment_driven(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_INFERENCE_REQUEST_BUDGET", "1")
        monkeypatch.setenv("DITTO_INFERENCE_TOKEN_BUDGET", "1")
        monkeypatch.setenv("DITTO_INFERENCE_TICKET_CONCURRENCY", "1")
        monkeypatch.setenv("DITTO_INFERENCE_VALIDATOR_CONCURRENCY", "1")
        monkeypatch.setenv("DITTO_INFERENCE_GLOBAL_CONCURRENCY", "1")
        monkeypatch.setenv("DITTO_EMBEDDING_TICKET_CONCURRENCY", "1")
        monkeypatch.setenv("DITTO_EMBEDDING_VALIDATOR_CONCURRENCY", "1")
        monkeypatch.setenv("DITTO_EMBEDDING_GLOBAL_CONCURRENCY", "1")

        config = parse_api_server_config_from_env(commit_hash="abc")

        assert config.inference_proxy.request_budget == 16384
        assert config.inference_proxy.token_budget == 25_000_000
        assert config.inference_proxy.per_ticket_concurrency == 16
        assert config.inference_proxy.per_validator_concurrency == 48
        assert config.inference_proxy.global_concurrency == 96
        assert config.inference_proxy.embedding_per_ticket_concurrency == 12
        assert config.inference_proxy.embedding_per_validator_concurrency == 48
        assert config.inference_proxy.embedding_global_concurrency == 96

    def test_overrides_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("API_HOST", "127.0.0.1")
        monkeypatch.setenv("API_PORT", "9000")
        monkeypatch.setenv("API_LOG_LEVEL", "debug")

        config = parse_api_server_config_from_env(commit_hash="abc")

        assert config.host == "127.0.0.1"
        assert config.port == 9000
        assert config.log_level == "DEBUG"

    def test_composition_with_sub_configs(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("POSTGRES_USER", "alice")
        monkeypatch.setenv("PYLON_OPEN_ACCESS_TOKEN", "tok-xyz")

        config = parse_api_server_config_from_env(commit_hash="abc")

        assert config.postgres.user == "alice"
        assert config.chain.open_access_token == "tok-xyz"

    def test_non_integer_port_raises(self, monkeypatch: pytest.MonkeyPatch):
        """Parse-time failure: the value is not coercible to int."""
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("API_PORT", "not-a-port")

        with pytest.raises(ApiServerConfigError, match="API_PORT"):
            parse_api_server_config_from_env(commit_hash="abc")

    def test_missing_payment_address_raises(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.delenv("DITTO_UPLOAD_PAYMENT_ADDRESS", raising=False)

        with pytest.raises(ApiServerConfigError, match="DITTO_UPLOAD_PAYMENT_ADDRESS"):
            parse_api_server_config_from_env(commit_hash="abc")

    @pytest.mark.parametrize(
        "bad",
        [
            "REPLACE_WITH_DITTO_SS58_ADDRESS",  # the .env.example placeholder
            "not-an-ss58",
            "5short",
            "0OIl" * 12 + "1234567890ab",  # SS58 forbidden chars 0/O/I/l
        ],
    )
    def test_malformed_payment_address_raises(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_UPLOAD_PAYMENT_ADDRESS", bad)

        with pytest.raises(ApiServerConfigError, match="SS58"):
            parse_api_server_config_from_env(commit_hash="abc")

    def test_pricing_sub_config_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_UPLOAD_FEE_USD", "7.50")
        monkeypatch.setenv("DITTO_UPLOAD_FEE_BUFFER", "1.2")
        monkeypatch.setenv("PRICING_CACHE_TTL_SECONDS", "60")

        config = parse_api_server_config_from_env(commit_hash="abc")

        from decimal import Decimal

        assert config.pricing.fee_usd == Decimal("7.50")
        assert config.pricing.fee_buffer == Decimal("1.2")
        assert config.pricing.cache_ttl_seconds == 60

    def test_storage_sub_config_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("STORAGE_ENDPOINT_URL", "https://s3.example.com")
        monkeypatch.setenv("STORAGE_BUCKET", "custom-bucket")
        monkeypatch.setenv("STORAGE_REGION", "eu-west-1")
        monkeypatch.setenv("STORAGE_USE_TLS", "true")

        config = parse_api_server_config_from_env(commit_hash="abc")

        assert config.storage.endpoint_url == "https://s3.example.com"
        assert config.storage.bucket == "custom-bucket"
        assert config.storage.region == "eu-west-1"
        assert config.storage.use_tls is True


class TestCheckConfig:
    """Validation gates that the dataclass type system cannot enforce."""

    def test_valid_config_passes(self):
        check_config(make_api_server_config())

    def test_unknown_inference_routing_mode_fails_closed(self):
        base = make_api_server_config()
        config = replace(
            base,
            inference_proxy=replace(base.inference_proxy, routing_mode="unscoped"),
        )
        with pytest.raises(ApiServerConfigError, match="ROUTING_MODE"):
            check_config(config)

    def test_port_out_of_range_raises(self):
        config = replace(make_api_server_config(), port=0)
        with pytest.raises(ApiServerConfigError, match="port out of range"):
            check_config(config)

    def test_port_above_max_raises(self):
        config = replace(make_api_server_config(), port=70000)
        with pytest.raises(ApiServerConfigError, match="port out of range"):
            check_config(config)

    def test_unknown_log_level_raises(self):
        config = replace(make_api_server_config(), log_level="loud")
        with pytest.raises(ApiServerConfigError, match="log_level"):
            check_config(config)

    @pytest.mark.parametrize("version", ["latest", "v0.7.0", "0.7", ""])
    def test_validator_minimum_requires_stable_version(self, version: str):
        from ditto.api_server import ValidatorCompatibilityConfig

        config = replace(
            make_api_server_config(),
            validator_compatibility=ValidatorCompatibilityConfig(
                minimum_software_version=version,
                minimum_protocol_version=4,
                heartbeat_max_age_seconds=300,
            ),
        )
        with pytest.raises(ApiServerConfigError, match="stable X.Y.Z"):
            check_config(config)

    def test_screener_auth_may_be_disabled(self):
        from ditto.api_server import ScreenerAuthConfig

        config = replace(
            make_api_server_config(),
            screener_auth=ScreenerAuthConfig(hotkey=None, api_token=None),
        )
        check_config(config)

    def test_screener_auth_rejects_partial_config(self):
        from ditto.api_server import ScreenerAuthConfig

        config = replace(
            make_api_server_config(),
            screener_auth=ScreenerAuthConfig(
                hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
                api_token=None,
            ),
        )
        with pytest.raises(ApiServerConfigError, match="must be set together"):
            check_config(config)

    def test_screener_auth_rejects_short_token(self):
        from ditto.api_server import ScreenerAuthConfig

        config = replace(
            make_api_server_config(),
            screener_auth=ScreenerAuthConfig(
                hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
                api_token="too-short",
            ),
        )
        with pytest.raises(ApiServerConfigError, match="at least 32"):
            check_config(config)

    def test_validator_names_accept_only_taostats_https(self):
        config = replace(
            make_api_server_config(),
            validator_names=ValidatorNamesConfig(
                url="https://api.taostats.io/api/dtao/validator/available/v1?netuid=118",
                api_key="free-api-key",
            ),
        )
        check_config(config)

        for url in (
            "http://api.taostats.io/api/dtao/validator/available/v1?netuid=118",
            "https://taostats.io/subnets/118/metagraph",
            "https://example.com/taostats",
            "https://api.taostats.io/api/price/latest/v1?netuid=118",
            "https://api.taostats.io/api/dtao/validator/available/v1?netuid=13",
            "https://api.taostats.io:8443/api/dtao/validator/available/v1?netuid=118",
        ):
            rejected = replace(
                config,
                validator_names=replace(config.validator_names, url=url),
            )
            with pytest.raises(ApiServerConfigError, match="documented"):
                check_config(rejected)

    @pytest.mark.parametrize(
        "names",
        [
            ValidatorNamesConfig(
                url="https://api.taostats.io/api/dtao/validator/available/v1?netuid=118"
            ),
            ValidatorNamesConfig(api_key="free-api-key"),
        ],
    )
    def test_validator_names_reject_partial_auth_config(
        self, names: ValidatorNamesConfig
    ) -> None:
        config = replace(make_api_server_config(), validator_names=names)

        with pytest.raises(ApiServerConfigError, match="must be set together"):
            check_config(config)

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("timeout_seconds", 5.1, "between 0.1 and 5"),
            ("retry_seconds", 59, "at least 60"),
            ("refresh_seconds", 299, "at least the retry"),
            ("max_stale_seconds", 3599, "at least the refresh"),
        ],
    )
    def test_validator_name_cache_bounds(
        self, field: str, value: float, message: str
    ) -> None:
        names = ValidatorNamesConfig(
            url="https://api.taostats.io/api/dtao/validator/available/v1?netuid=118",
            api_key="free-api-key",
        )
        if field == "timeout_seconds":
            names = replace(names, timeout_seconds=value)
        elif field == "retry_seconds":
            names = replace(names, retry_seconds=int(value))
        elif field == "refresh_seconds":
            names = replace(names, refresh_seconds=int(value))
        else:
            names = replace(names, max_stale_seconds=int(value))
        config = replace(
            make_api_server_config(),
            validator_names=names,
        )

        with pytest.raises(ApiServerConfigError, match=message):
            check_config(config)


class TestEfficiencyBonusConfig:
    """Env parsing + boot validation for the relative efficiency bonus."""

    def test_default_is_fully_off(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        for name in (
            "DITTO_EFFICIENCY_BONUS_ENABLED",
            "DITTO_EFFICIENCY_BONUS_FOLD_ENABLED",
            "DITTO_EFFICIENCY_BONUS_CAP",
            "DITTO_EFFICIENCY_BONUS_FACTOR_ALPHA",
            "DITTO_EFFICIENCY_BONUS_MINIMUM_FACTOR",
            "DITTO_EFFICIENCY_BONUS_MAXIMUM_FACTOR",
        ):
            monkeypatch.delenv(name, raising=False)
        config = parse_api_server_config_from_env(commit_hash="abc")
        efficiency = config.efficiency_bonus
        assert efficiency.enabled is False
        assert efficiency.fold_enabled is False
        assert efficiency.cap == 0.05
        assert efficiency.deep_cap == 0.10
        assert efficiency.deep_frontier_ratio == 0.5
        assert efficiency.factor_alpha == 0.25
        assert efficiency.minimum_factor == 0.85
        assert efficiency.maximum_factor == 1.10
        assert efficiency.cohort_size == 25
        assert efficiency.min_cohort == 8
        assert efficiency.epoch_hours == 24
        check_config(config)

    def test_env_overrides_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_ENABLED", "true")
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_CAP", "0.06")
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_COHORT_SIZE", "30")
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_MIN_COHORT", "10")
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_DEEP_CAP", "0.08")
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_DEEP_FRONTIER_RATIO", "0.25")
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_FACTOR_ALPHA", "0.5")
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_MINIMUM_FACTOR", "0.9")
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_MAXIMUM_FACTOR", "1.08")
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_EPOCH_HOURS", "12")
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_QUALITY_FLOOR", "0.4")
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_MEMORY_FLOOR", "0.3")
        config = parse_api_server_config_from_env(commit_hash="abc")
        efficiency = config.efficiency_bonus
        assert efficiency.enabled is True
        assert efficiency.cap == 0.06
        assert efficiency.deep_cap == 0.08
        assert efficiency.deep_frontier_ratio == 0.25
        assert efficiency.factor_alpha == 0.5
        assert efficiency.minimum_factor == 0.9
        assert efficiency.maximum_factor == 1.08
        assert efficiency.cohort_size == 30
        assert efficiency.min_cohort == 10
        assert efficiency.epoch_hours == 12
        assert efficiency.quality_floor == 0.4
        assert efficiency.memory_floor == 0.3
        check_config(config)

    def test_non_numeric_knob_raises(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_EFFICIENCY_BONUS_CAP", "five-percent")
        with pytest.raises(ApiServerConfigError, match="numeric"):
            parse_api_server_config_from_env(commit_hash="abc")

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"fold_enabled": True}, "enabled before"),
            ({"enabled": True, "cap": 0.11}, "0.10"),
            ({"enabled": True, "cap": 0.0}, "0.10"),
            ({"enabled": True, "deep_cap": 0.04}, "cap <= deep_cap"),
            ({"enabled": True, "deep_cap": 0.11}, "cap <= deep_cap"),
            ({"enabled": True, "deep_frontier_ratio": 0.0}, "in \\(0, 1\\)"),
            ({"enabled": True, "deep_frontier_ratio": 1.0}, "in \\(0, 1\\)"),
            ({"enabled": True, "deep_frontier_ratio": -0.5}, "in \\(0, 1\\)"),
            ({"enabled": True, "factor_alpha": 0.0}, "FACTOR_ALPHA"),
            ({"enabled": True, "factor_alpha": 1.1}, "FACTOR_ALPHA"),
            ({"enabled": True, "minimum_factor": 0.0}, "MINIMUM_FACTOR"),
            ({"enabled": True, "minimum_factor": 1.01}, "MINIMUM_FACTOR"),
            ({"enabled": True, "maximum_factor": 0.99}, "MAXIMUM_FACTOR"),
            ({"enabled": True, "maximum_factor": 101.0}, "MAXIMUM_FACTOR"),
            ({"enabled": True, "min_cohort": 1}, "at least 2"),
            (
                {"enabled": True, "cohort_size": 5, "min_cohort": 8},
                "at least the minimum cohort",
            ),
            ({"enabled": True, "epoch_hours": 0}, "at least 1"),
            ({"enabled": True, "quality_floor": 1.5}, "QUALITY_FLOOR"),
            ({"enabled": True, "memory_floor": -0.1}, "MEMORY_FLOOR"),
        ],
    )
    def test_invalid_combinations_fail_boot(
        self, overrides: dict, message: str
    ) -> None:
        from ditto.api_server.config import EfficiencyBonusConfig

        config = replace(
            make_api_server_config(),
            efficiency_bonus=EfficiencyBonusConfig(**overrides),
        )
        with pytest.raises(ApiServerConfigError, match=message):
            check_config(config)


class TestInferenceRequestBudgetBound:
    """The boot check and the operator board must agree on the ceiling.

    They are now the same constant by import rather than two copies of a
    literal. The failure this prevents is specific and nasty: a board write
    inside its own limit but above the boot check's would be accepted, served
    for as long as the process lived, and then refuse to boot on the next
    restart -- turning a settings change into a delayed outage.
    """

    def test_the_shipped_default_is_within_the_bound(self):
        config = make_api_server_config()
        assert config.inference_proxy.request_budget <= MAX_CHAT_REQUEST_BUDGET
        check_config(config)

    def test_embedding_fallback_is_pinned_to_direct_perplexity(self):
        config = make_api_server_config()
        assert (
            config.inference_proxy.embedding_fallback_url
            == "https://api.perplexity.ai/v1/embeddings"
        )
        check_config(config)

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.perplexity.ai/v1/embeddings",
            "https://evil.example/v1/embeddings",
            "https://api.perplexity.ai/v1/chat/completions",
        ],
    )
    def test_embedding_fallback_refuses_unreviewed_egress(self, url: str):
        config = make_api_server_config()
        proxy = replace(config.inference_proxy, embedding_fallback_url=url)
        with pytest.raises(ApiServerConfigError, match="Perplexity embeddings"):
            check_config(replace(config, inference_proxy=proxy))

    def test_the_boot_check_rejects_exactly_what_the_board_rejects(self):
        config = make_api_server_config()
        over = replace(
            config.inference_proxy, request_budget=MAX_CHAT_REQUEST_BUDGET + 1
        )
        with pytest.raises(ApiServerConfigError, match="safety bound"):
            check_config(replace(config, inference_proxy=over))

        at_bound = replace(
            config.inference_proxy, request_budget=MAX_CHAT_REQUEST_BUDGET
        )
        check_config(replace(config, inference_proxy=at_bound))

    def test_token_budget_boot_bound_matches_the_operator_board(self):
        config = make_api_server_config()
        over = replace(config.inference_proxy, token_budget=MAX_CHAT_TOKEN_BUDGET + 1)
        with pytest.raises(ApiServerConfigError, match="safety bound"):
            check_config(replace(config, inference_proxy=over))

        at_bound = replace(config.inference_proxy, token_budget=MAX_CHAT_TOKEN_BUDGET)
        check_config(replace(config, inference_proxy=at_bound))


class TestTargonRentalConfig:
    """Targon screening is opt-in via Secret Manager-backed env, not a key file."""

    _COMMIT = "ab" * 20
    _KEY = "t" * 32
    _BUILDER = (
        "us-central1-docker.pkg.dev/ditto-app-dev/"
        "ditto-public-builders/submission-builder"
    )

    def test_absent_key_disables_targon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_minimum_env(monkeypatch)
        config = parse_api_server_config_from_env(commit_hash=self._COMMIT)
        assert config.targon is None

    def test_env_key_enables_targon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_TARGON_API_KEY", self._KEY)
        monkeypatch.setenv(
            "DITTO_TARGON_PUBLIC_PLATFORM_URL", "https://platform-api.heyditto.ai"
        )
        monkeypatch.setenv("DITTO_TARGON_SUBMISSION_BUILDER_IMAGE", self._BUILDER)
        monkeypatch.setenv(
            "DITTO_TARGON_CANDIDATE_PUSH_SA",
            "push@ditto-app-dev.iam.gserviceaccount.com",
        )
        config = parse_api_server_config_from_env(commit_hash=self._COMMIT)
        assert config.targon is not None
        assert config.targon.enabled is True
        assert config.targon.api_key == self._KEY
        assert (
            config.targon.submission_builder_image
            == f"{self._BUILDER}:sha-{self._COMMIT}"
        )
        assert self._KEY not in repr(config.targon)
        assert config.targon.max_inflight == 10
        assert config.targon.smoke_provision_timeout_seconds == 30.0

    def test_targon_smoke_provision_timeout_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_TARGON_API_KEY", self._KEY)
        monkeypatch.setenv(
            "DITTO_TARGON_PUBLIC_PLATFORM_URL", "https://platform-api.heyditto.ai"
        )
        monkeypatch.setenv("DITTO_TARGON_SUBMISSION_BUILDER_IMAGE", self._BUILDER)
        monkeypatch.setenv("DITTO_TARGON_SMOKE_PROVISION_TIMEOUT_SECONDS", "45")
        config = parse_api_server_config_from_env(commit_hash=self._COMMIT)
        assert config.targon is not None
        assert config.targon.smoke_provision_timeout_seconds == 45.0

    def test_targon_max_inflight_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_TARGON_API_KEY", self._KEY)
        monkeypatch.setenv(
            "DITTO_TARGON_PUBLIC_PLATFORM_URL", "https://platform-api.heyditto.ai"
        )
        monkeypatch.setenv("DITTO_TARGON_SUBMISSION_BUILDER_IMAGE", self._BUILDER)
        monkeypatch.setenv("DITTO_TARGON_MAX_INFLIGHT", "8")
        config = parse_api_server_config_from_env(commit_hash=self._COMMIT)
        assert config.targon is not None
        assert config.targon.max_inflight == 8

    def test_targon_max_inflight_rejects_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_TARGON_API_KEY", self._KEY)
        monkeypatch.setenv(
            "DITTO_TARGON_PUBLIC_PLATFORM_URL", "https://platform-api.heyditto.ai"
        )
        monkeypatch.setenv("DITTO_TARGON_SUBMISSION_BUILDER_IMAGE", self._BUILDER)
        monkeypatch.setenv("DITTO_TARGON_MAX_INFLIGHT", "0")
        with pytest.raises(ApiServerConfigError, match="DITTO_TARGON_MAX_INFLIGHT"):
            parse_api_server_config_from_env(commit_hash=self._COMMIT)

    def test_short_key_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_TARGON_API_KEY", "too-short")
        with pytest.raises(ApiServerConfigError, match="DITTO_TARGON_API_KEY"):
            parse_api_server_config_from_env(commit_hash=self._COMMIT)

    def test_key_file_still_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        _set_minimum_env(monkeypatch)
        key_file = tmp_path / "targon.key"
        key_file.write_text(self._KEY, encoding="utf-8")
        monkeypatch.setenv("DITTO_TARGON_API_KEY_FILE", str(key_file))
        monkeypatch.setenv(
            "DITTO_TARGON_PUBLIC_PLATFORM_URL", "https://platform-api.heyditto.ai"
        )
        monkeypatch.setenv(
            "DITTO_TARGON_SUBMISSION_BUILDER_IMAGE",
            f"{self._BUILDER}@sha256:{'cd' * 32}",
        )
        config = parse_api_server_config_from_env(commit_hash="abc")
        assert config.targon is not None
        assert config.targon.api_key == self._KEY

    def test_cloudrun_env_enables_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_CLOUDRUN_PROJECT", "ditto-app-dev")
        monkeypatch.setenv(
            "DITTO_CLOUDRUN_UNTRUSTED_SA",
            "ditto-screening-untrusted@ditto-app-dev.iam.gserviceaccount.com",
        )
        monkeypatch.setenv(
            "DITTO_CLOUDRUN_INVOKER_SA",
            "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com",
        )
        monkeypatch.setenv(
            "DITTO_CLOUDRUN_SOURCE_REVIEW_SERVICE", "ditto-source-review-warm"
        )
        config = parse_api_server_config_from_env(commit_hash=self._COMMIT)
        assert config.cloudrun is not None
        assert config.cloudrun.enabled is True
        assert config.cloudrun.region == "us-central1"
        assert config.cloudrun.source_review_service == "ditto-source-review-warm"

    def test_targon_requires_cloudrun_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_TARGON_API_KEY", self._KEY)
        monkeypatch.setenv(
            "DITTO_TARGON_PUBLIC_PLATFORM_URL", "https://platform-api.heyditto.ai"
        )
        monkeypatch.setenv("DITTO_TARGON_SUBMISSION_BUILDER_IMAGE", self._BUILDER)
        config = parse_api_server_config_from_env(commit_hash=self._COMMIT)

        with pytest.raises(ApiServerConfigError, match="Cloud Run fallback"):
            check_config(config)

        monkeypatch.setenv("DITTO_CLOUDRUN_PROJECT", "ditto-app-dev")
        monkeypatch.setenv(
            "DITTO_CLOUDRUN_UNTRUSTED_SA",
            "ditto-screening-untrusted@ditto-app-dev.iam.gserviceaccount.com",
        )
        monkeypatch.setenv(
            "DITTO_CLOUDRUN_INVOKER_SA",
            "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com",
        )
        check_config(parse_api_server_config_from_env(commit_hash=self._COMMIT))

    def test_the_default_is_an_actual_raise_over_the_number_it_replaced(self):
        """1024 was the ceiling a legitimate heavy strategy hit at check ~266.

        Pinned so that a future edit trimming the default back toward the old
        value has to argue with this test rather than quietly reintroduce the
        exhaustion.
        """
        assert DEFAULT_CHAT_REQUEST_BUDGET >= 5 * 1024
