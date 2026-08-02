"""Resolved configuration for the API server process."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from ditto.api_models.inference_concurrency_settings import (
    DEFAULT_CHAT_REQUEST_BUDGET,
    DEFAULT_CHAT_TOKEN_BUDGET,
    MAX_CHAT_REQUEST_BUDGET,
    MAX_CHAT_TOKEN_BUDGET,
)
from ditto.api_server.datapipeline import (
    DataPipelineConfig,
    parse_data_pipeline_config_from_env,
)
from ditto.api_server.embedding import (
    EmbeddingConfig,
    parse_embedding_config_from_env,
)
from ditto.api_server.errors import ApiServerConfigError
from ditto.api_server.pricing import PricingConfig, parse_pricing_config_from_env
from ditto.api_server.storage import StorageConfig, parse_storage_config_from_env
from ditto.api_server.validator_names import (
    ValidatorNamesConfig,
    parse_validator_names_config_from_env,
)
from ditto.chain import ChainConfig, parse_chain_config_from_env
from ditto.db import PostgresConfig, parse_postgres_config_from_env

# Substrate SS58 base58 alphabet, 47-48 chars. Same shape Pydantic
# enforces on the wire; mirrored here so a bad payment address fails
# boot instead of running with a placeholder.
_SS58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")


@dataclass(frozen=True)
class ScreenerAuthConfig:
    """Credentials accepted by the platform-operated screener endpoints."""

    hotkey: str | None
    """Dedicated screener SS58 hotkey. It does not need an on-chain permit."""

    api_token: str | None
    """Bearer token required on every screener request."""

    @property
    def enabled(self) -> bool:
        return self.hotkey is not None and self.api_token is not None


@dataclass(frozen=True)
class ValidatorCompatibilityConfig:
    """Minimum validator build accepted at the scoring-ticket boundary."""

    minimum_software_version: str | None
    minimum_protocol_version: int
    heartbeat_max_age_seconds: int


@dataclass(frozen=True)
class InferenceProxyConfig:
    """Platform-owned OpenRouter proxy and ticket budget limits."""

    enabled: bool
    required: bool
    public_base_url: str
    openrouter_api_key: str | None
    upstream_url: str
    allowed_models: tuple[str, ...]
    provider: str
    routing_mode: str
    request_budget: int
    token_budget: int
    embedding_upstream_url: str
    embedding_model: str
    embedding_profile: str
    embedding_provider: str
    embedding_dimensions: int
    embedding_request_budget: int
    embedding_token_budget: int
    embedding_per_ticket_concurrency: int
    embedding_per_validator_concurrency: int
    embedding_global_concurrency: int
    embedding_per_ticket_requests_per_minute: int
    embedding_per_validator_requests_per_minute: int
    embedding_global_requests_per_minute: int
    embedding_request_body_bytes: int
    embedding_response_body_bytes: int
    per_ticket_concurrency: int
    per_validator_concurrency: int
    global_concurrency: int
    per_ticket_requests_per_minute: int
    per_validator_requests_per_minute: int
    global_requests_per_minute: int
    request_body_bytes: int
    response_body_bytes: int
    timeout_seconds: float
    max_output_tokens: int
    discovery_url_template: str = (
        "https://openrouter.ai/api/v1/models/{model}/endpoints"
    )
    discovery_interval_seconds: int = 300
    route_speed_weight: float = 0.65
    route_cost_weight: float = 0.25
    route_exploration_weight: float = 0.10
    route_ewma_alpha: float = 0.20
    route_min_tool_accuracy: float = 0.55
    route_min_composite: float = 0.15
    route_min_calibration_samples: int = 60
    route_exploration_ticket_budget: int = 3
    route_max_error_rate: float = 0.25
    route_max_timeout_rate: float = 0.15
    route_cooldown_seconds: int = 30
    reviewed_calibration_manifest_sha256: str | None = None


@dataclass(frozen=True)
class EfficiencyBonusConfig:
    """Platform-side relative token-efficiency bonus (bench_version >= 7).

    See :mod:`ditto.api_server.efficiency` and
    ``docs/relative-efficiency-bonus.md``. Everything is default-off: with
    ``enabled=False`` no snapshot is computed, no bonus row is written, and
    every API field stays null — v6-and-earlier behavior is byte-identical
    either way because the bonus never applies below bench_version 7.
    """

    enabled: bool = False
    """Master switch (``DITTO_EFFICIENCY_BONUS_ENABLED``). Turns on cohort
    snapshotting, frozen bonus assignment, and public API exposure."""

    fold_enabled: bool = False
    """Expose ``effective_composite`` on the validator scoring ledger
    (``DITTO_EFFICIENCY_BONUS_FOLD_ENABLED``). Default off: the validator
    weight fold lives in ditto-subnet and must ship its own consensus change
    before consuming the field; until then the ledger stays byte-identical."""

    cap: float = 0.05
    """Tier-1 maximum bonus fraction ``B_max`` (``DITTO_EFFICIENCY_BONUS_CAP``)
    — the curve's value at the P25 frontier. Default 5%; hard ceiling 10%
    enforced at boot and by DB check."""

    deep_cap: float = 0.10
    """Tier-2 saturation cap (``DITTO_EFFICIENCY_BONUS_DEEP_CAP``): the flat
    bonus at or below the deep frontier. Bounded ``cap <= deep_cap <= 0.10``
    at boot and by DB check, so the whole curve stays inside the agreed 5-10%
    envelope."""

    deep_frontier_ratio: float = 0.5
    """Deep frontier as a fraction of P25
    (``DITTO_EFFICIENCY_BONUS_DEEP_FRONTIER_RATIO``), in (0, 1). Below
    ``ratio x P25`` the bonus saturates flat at ``deep_cap`` — deliberately
    no extra reward for racing toward zero tokens."""

    cohort_size: int = 25
    """Top-N quality-qualified agents forming a cohort
    (``DITTO_EFFICIENCY_BONUS_COHORT_SIZE``)."""

    min_cohort: int = 8
    """Activation gate ``N_min`` (``DITTO_EFFICIENCY_BONUS_MIN_COHORT``):
    below this deduped cohort size no bonus is awarded (spec default 8)."""

    epoch_hours: int = 24
    """Length of one efficiency epoch in hours
    (``DITTO_EFFICIENCY_BONUS_EPOCH_HOURS``); fixed UTC windows."""

    quality_floor: float = 0.0
    """Static composite floor ``Q_min`` fallback
    (``DITTO_EFFICIENCY_BONUS_QUALITY_FLOOR``); superseded upward by the
    previous active cohort's median composite once one exists."""

    memory_floor: float = 0.0
    """Static memory-mean floor ``M_min`` fallback
    (``DITTO_EFFICIENCY_BONUS_MEMORY_FLOOR``); superseded upward by 0.8 x the
    previous active cohort's median memory_mean once one exists."""


@dataclass(frozen=True)
class ApiServerConfig:
    """Resolved configuration for the API server process.

    Composition over flattening: ``postgres``, ``chain``, ``pricing``,
    and ``storage`` carry their own typed dataclasses so the same
    sub-configs feed validator daemon + smoke scripts unchanged.
    """

    host: str
    """Interface to bind. ``0.0.0.0`` for compose / cloud, ``127.0.0.1`` locally."""

    port: int
    """TCP port. Defaults to 8000; Pylon shifts to 8001 in compose."""

    log_level: str
    """Root logger level. One of the stdlib level names (``DEBUG``, ``INFO``,
    ``WARNING``, ``ERROR``, ``CRITICAL``)."""

    commit_hash: str
    """Git revision the process was built from, or ``"unknown"`` outside a checkout.

    Resolved by :mod:`ditto.api_server.__main__` via ``git rev-parse HEAD``
    before the FastAPI app is built, so :func:`create_api_server` can stash
    it on ``app.state.commit_hash`` for the ``/health`` endpoint.
    """

    upload_payment_address: str
    """Ditto-controlled SS58 receive address for upload fees
    (``DITTO_UPLOAD_PAYMENT_ADDRESS``). Required at boot."""

    postgres: PostgresConfig
    """Connection parameters for the platform database."""

    chain: ChainConfig
    """Pylon + subtensor settings for chain reads."""

    pricing: PricingConfig
    """CoinGecko oracle + upload-fee parameters."""

    storage: StorageConfig
    """S3-compatible object store parameters for uploaded tarballs."""

    embedding: EmbeddingConfig
    """Code-embedding client parameters. Disabled by default (no
    ``CODE_EMBEDDER_URL``), so the platform runs unchanged until an operator points
    it at a self-hosted TEI service."""

    data_pipeline: DataPipelineConfig
    """Generate-service client parameters. Disabled by default (no
    ``DATA_PIPELINE_URL``); when set, the platform generates one dataset per
    submission at job-ready and pins (seed, dataset_sha256, run_size) on the
    agent."""

    screener_auth: ScreenerAuthConfig
    """Dedicated signer and bearer token for the platform-operated screener."""

    validator_names: ValidatorNamesConfig
    """Optional, background-only Taostats display-name decoration."""

    validator_compatibility: ValidatorCompatibilityConfig
    """Validator release and heartbeat requirements for scoring tickets."""

    inference_proxy: InferenceProxyConfig = field(
        default_factory=lambda: InferenceProxyConfig(
            enabled=False,
            required=False,
            public_base_url="http://localhost:8000",
            openrouter_api_key=None,
            upstream_url="https://openrouter.ai/api/v1/chat/completions",
            allowed_models=("qwen/qwen3-32b", "openai/gpt-oss-20b"),
            provider="nebius",
            routing_mode="aggregate_throughput",
            request_budget=DEFAULT_CHAT_REQUEST_BUDGET,
            token_budget=DEFAULT_CHAT_TOKEN_BUDGET,
            embedding_upstream_url="https://openrouter.ai/api/v1/embeddings",
            embedding_model="perplexity/pplx-embed-v1-0.6b",
            embedding_profile="dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1",
            embedding_provider="Perplexity",
            embedding_dimensions=768,
            embedding_request_budget=100_000,
            embedding_token_budget=1_000_000_000,
            embedding_per_ticket_concurrency=12,
            embedding_per_validator_concurrency=48,
            embedding_global_concurrency=96,
            embedding_per_ticket_requests_per_minute=10_000,
            embedding_per_validator_requests_per_minute=40_000,
            embedding_global_requests_per_minute=100_000,
            embedding_request_body_bytes=1 << 20,
            embedding_response_body_bytes=16 << 20,
            per_ticket_concurrency=8,
            per_validator_concurrency=24,
            global_concurrency=72,
            per_ticket_requests_per_minute=240,
            per_validator_requests_per_minute=960,
            global_requests_per_minute=2880,
            request_body_bytes=256 << 10,
            response_body_bytes=2 << 20,
            timeout_seconds=90.0,
            max_output_tokens=8192,
        )
    )
    """Dark-launchable, platform-owned ticket inference proxy."""

    admin_api_token: str | None = None
    """Bearer token for private Backroom/operator administration endpoints."""

    dashboard_enabled: bool = True
    """Serve the public dashboard SPA (``dashboard/index.html``) at ``/``.

    On by default so the platform doubles as the transparency front door
    (same-origin with the public read API — no CORS / separate host needed).
    Set ``DITTO_DASHBOARD_ENABLED=false`` to run headless (API only)."""

    dashboard_wandb_url: str = "https://wandb.ai/"
    """Public wandb project URL injected into the served dashboard's
    ``ditto:wandb-url`` meta tag (``DITTO_DASHBOARD_WANDB_URL``), e.g.
    ``https://wandb.ai/<entity>/ditto-sn118``. The committed HTML ships a bare
    default so the link still resolves before the project exists."""

    top5_backoff_base: int = 2
    """Base interval, in tempos (1 tempo = 360 blocks ≈ 72 min), between top-5
    shared-seed rescore rounds while the champion's reign is fresh
    (``TOP5_RESCORE_BACKOFF_BASE``). The gap grows as an exponential backoff over
    the reign -- ``min(base * 2**floor(reign_tempos / K), cap)`` -- so a fresh or
    contested king is rescored densely and a settled leader sparsely. Set ``0``
    to disable the lane entirely. Platform-authoritative (the subnet just retries
    and swallows not-due rejections), so this is not a cross-repo consensus knob;
    every validator still gets the same due decision because the platform is the
    single arbiter."""

    top5_backoff_doubling_tempos: int = 20
    """How many reign-tempos the interval holds at ``base`` before each doubling
    (``K`` in the backoff; ``TOP5_RESCORE_BACKOFF_K``). At the default ``20`` the
    densest rounds are front-loaded across the first ~20 tempos (~24 h), matching
    the king-source-reveal window (#277/#278) so the crown is hardened on the most
    shared seeds exactly as the code becomes public."""

    top5_backoff_cap: int = 8
    """Ceiling on the round interval in tempos (``TOP5_RESCORE_BACKOFF_CAP``). The
    backoff never reaches zero rate: a champion whose interval flatlines at the
    cap is itself the signal that the field has gone stagnant."""

    efficiency_bonus: EfficiencyBonusConfig = field(
        default_factory=EfficiencyBonusConfig
    )
    """Relative token-efficiency bonus knobs (bench_version >= 7); default-off."""


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

_TRUTHY = {"1", "true", "yes", "on"}


def parse_api_server_config_from_env(commit_hash: str) -> ApiServerConfig:
    """Build an :class:`ApiServerConfig` from ``API_*`` env vars plus
    the postgres + chain + pricing + storage sub-config parsers. Call
    :func:`check_config` after to validate ranges + set membership.

    Raises:
        ApiServerConfigError: When ``API_PORT`` is not parseable as int
            or ``DITTO_UPLOAD_PAYMENT_ADDRESS`` is unset.
    """
    host = os.environ.get("API_HOST", "0.0.0.0")
    raw_port = os.environ.get("API_PORT", "8000")
    log_level = os.environ.get("API_LOG_LEVEL", "INFO").upper()

    try:
        port = int(raw_port)
    except ValueError as e:
        raise ApiServerConfigError(
            f"API_PORT must be an integer, got {raw_port!r}"
        ) from e

    upload_payment_address = os.environ.get("DITTO_UPLOAD_PAYMENT_ADDRESS")
    if not upload_payment_address:
        raise ApiServerConfigError(
            "DITTO_UPLOAD_PAYMENT_ADDRESS must be set to a Ditto-controlled "
            "SS58 receive address"
        )
    if not _SS58_RE.fullmatch(upload_payment_address):
        raise ApiServerConfigError(
            "DITTO_UPLOAD_PAYMENT_ADDRESS does not look like an SS58 address; "
            "got a value that fails the base58 / length check"
        )

    dashboard_enabled = (
        os.environ.get("DITTO_DASHBOARD_ENABLED", "true").strip().lower() in _TRUTHY
    )
    dashboard_wandb_url = os.environ.get(
        "DITTO_DASHBOARD_WANDB_URL", "https://wandb.ai/"
    )
    screener_hotkey = os.environ.get("SCREENER_HOTKEY") or None
    screener_api_token = os.environ.get("SCREENER_API_TOKEN") or None
    minimum_validator_version = (
        os.environ.get("DITTO_MIN_VALIDATOR_SOFTWARE_VERSION", "0.7.0").strip() or None
    )
    try:
        minimum_validator_protocol = int(
            os.environ.get("DITTO_MIN_VALIDATOR_PROTOCOL_VERSION", "4")
        )
        validator_heartbeat_max_age = int(
            os.environ.get("DITTO_VALIDATOR_HEARTBEAT_MAX_AGE_SECONDS", "300")
        )
    except ValueError as error:
        raise ApiServerConfigError(
            "validator compatibility protocol and heartbeat age must be integers"
        ) from error
    inference_enabled = (
        os.environ.get("DITTO_INFERENCE_PROXY_ENABLED", "false").strip().lower()
        in _TRUTHY
    )
    try:
        inference_proxy = InferenceProxyConfig(
            enabled=inference_enabled,
            required=(
                os.environ.get("DITTO_INFERENCE_PROXY_REQUIRED", "false")
                .strip()
                .lower()
                in _TRUTHY
            ),
            public_base_url=os.environ.get(
                "DITTO_INFERENCE_PUBLIC_BASE_URL", "http://localhost:8000"
            ).rstrip("/"),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
            upstream_url=os.environ.get(
                "DITTO_INFERENCE_UPSTREAM_URL",
                "https://openrouter.ai/api/v1/chat/completions",
            ),
            allowed_models=tuple(
                model.strip()
                for model in os.environ.get(
                    "DITTO_INFERENCE_ALLOWED_MODELS",
                    "qwen/qwen3-32b,openai/gpt-oss-20b",
                ).split(",")
                if model.strip()
            ),
            provider=os.environ.get("DITTO_INFERENCE_PROVIDER", "nebius").strip(),
            routing_mode=os.environ.get(
                "DITTO_INFERENCE_ROUTING_MODE", "aggregate_throughput"
            ).strip(),
            request_budget=int(
                os.environ.get(
                    "DITTO_INFERENCE_REQUEST_BUDGET", str(DEFAULT_CHAT_REQUEST_BUDGET)
                )
            ),
            token_budget=int(
                os.environ.get(
                    "DITTO_INFERENCE_TOKEN_BUDGET", str(DEFAULT_CHAT_TOKEN_BUDGET)
                )
            ),
            embedding_upstream_url=os.environ.get(
                "DITTO_EMBEDDING_UPSTREAM_URL",
                "https://openrouter.ai/api/v1/embeddings",
            ),
            embedding_model=os.environ.get(
                "DITTO_EMBEDDING_MODEL", "perplexity/pplx-embed-v1-0.6b"
            ).strip(),
            embedding_profile=os.environ.get(
                "DITTO_EMBEDDING_PROFILE",
                "dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1",
            ).strip(),
            embedding_provider=os.environ.get(
                "DITTO_EMBEDDING_PROVIDER", "Perplexity"
            ).strip(),
            embedding_dimensions=int(
                os.environ.get("DITTO_EMBEDDING_DIMENSIONS", "768")
            ),
            embedding_request_budget=int(
                os.environ.get("DITTO_EMBEDDING_REQUEST_BUDGET", "100000")
            ),
            embedding_token_budget=int(
                os.environ.get("DITTO_EMBEDDING_TOKEN_BUDGET", "1000000000")
            ),
            embedding_per_ticket_concurrency=int(
                os.environ.get("DITTO_EMBEDDING_TICKET_CONCURRENCY", "12")
            ),
            embedding_per_validator_concurrency=int(
                os.environ.get("DITTO_EMBEDDING_VALIDATOR_CONCURRENCY", "48")
            ),
            embedding_global_concurrency=int(
                os.environ.get("DITTO_EMBEDDING_GLOBAL_CONCURRENCY", "96")
            ),
            embedding_per_ticket_requests_per_minute=int(
                os.environ.get("DITTO_EMBEDDING_TICKET_RPM", "10000")
            ),
            embedding_per_validator_requests_per_minute=int(
                os.environ.get("DITTO_EMBEDDING_VALIDATOR_RPM", "40000")
            ),
            embedding_global_requests_per_minute=int(
                os.environ.get("DITTO_EMBEDDING_GLOBAL_RPM", "100000")
            ),
            embedding_request_body_bytes=int(
                os.environ.get("DITTO_EMBEDDING_REQUEST_BODY_BYTES", str(1 << 20))
            ),
            embedding_response_body_bytes=int(
                os.environ.get("DITTO_EMBEDDING_RESPONSE_BODY_BYTES", str(16 << 20))
            ),
            per_ticket_concurrency=int(
                os.environ.get("DITTO_INFERENCE_TICKET_CONCURRENCY", "8")
            ),
            per_validator_concurrency=int(
                os.environ.get("DITTO_INFERENCE_VALIDATOR_CONCURRENCY", "24")
            ),
            global_concurrency=int(
                os.environ.get("DITTO_INFERENCE_GLOBAL_CONCURRENCY", "72")
            ),
            per_ticket_requests_per_minute=int(
                os.environ.get("DITTO_INFERENCE_TICKET_RPM", "240")
            ),
            per_validator_requests_per_minute=int(
                os.environ.get("DITTO_INFERENCE_VALIDATOR_RPM", "960")
            ),
            global_requests_per_minute=int(
                os.environ.get("DITTO_INFERENCE_GLOBAL_RPM", "2880")
            ),
            request_body_bytes=int(
                os.environ.get("DITTO_INFERENCE_REQUEST_BODY_BYTES", str(256 << 10))
            ),
            response_body_bytes=int(
                os.environ.get("DITTO_INFERENCE_RESPONSE_BODY_BYTES", str(2 << 20))
            ),
            timeout_seconds=float(
                os.environ.get("DITTO_INFERENCE_TIMEOUT_SECONDS", "90")
            ),
            max_output_tokens=int(
                os.environ.get("DITTO_INFERENCE_MAX_OUTPUT_TOKENS", "8192")
            ),
            discovery_url_template=os.environ.get(
                "DITTO_INFERENCE_DISCOVERY_URL_TEMPLATE",
                "https://openrouter.ai/api/v1/models/{model}/endpoints",
            ),
            discovery_interval_seconds=int(
                os.environ.get("DITTO_INFERENCE_DISCOVERY_INTERVAL_SECONDS", "300")
            ),
            route_speed_weight=float(
                os.environ.get("DITTO_INFERENCE_ROUTE_SPEED_WEIGHT", "0.65")
            ),
            route_cost_weight=float(
                os.environ.get("DITTO_INFERENCE_ROUTE_COST_WEIGHT", "0.25")
            ),
            route_exploration_weight=float(
                os.environ.get("DITTO_INFERENCE_ROUTE_EXPLORATION_WEIGHT", "0.10")
            ),
            route_ewma_alpha=float(
                os.environ.get("DITTO_INFERENCE_ROUTE_EWMA_ALPHA", "0.20")
            ),
            route_min_tool_accuracy=float(
                os.environ.get("DITTO_INFERENCE_ROUTE_MIN_TOOL_ACCURACY", "0.55")
            ),
            route_min_composite=float(
                os.environ.get("DITTO_INFERENCE_ROUTE_MIN_COMPOSITE", "0.15")
            ),
            route_min_calibration_samples=int(
                os.environ.get("DITTO_INFERENCE_ROUTE_MIN_CALIBRATION_SAMPLES", "60")
            ),
            route_exploration_ticket_budget=int(
                os.environ.get("DITTO_INFERENCE_ROUTE_EXPLORATION_TICKETS", "3")
            ),
            route_max_error_rate=float(
                os.environ.get("DITTO_INFERENCE_ROUTE_MAX_ERROR_RATE", "0.25")
            ),
            route_max_timeout_rate=float(
                os.environ.get("DITTO_INFERENCE_ROUTE_MAX_TIMEOUT_RATE", "0.15")
            ),
            route_cooldown_seconds=int(
                os.environ.get("DITTO_INFERENCE_ROUTE_COOLDOWN_SECONDS", "30")
            ),
            reviewed_calibration_manifest_sha256=(
                os.environ.get("DITTO_INFERENCE_REVIEWED_CALIBRATION_MANIFEST_SHA256")
                or None
            ),
        )
    except ValueError as error:
        raise ApiServerConfigError("inference proxy limits must be numeric") from error

    try:
        efficiency_bonus = EfficiencyBonusConfig(
            enabled=(
                os.environ.get("DITTO_EFFICIENCY_BONUS_ENABLED", "false")
                .strip()
                .lower()
                in _TRUTHY
            ),
            fold_enabled=(
                os.environ.get("DITTO_EFFICIENCY_BONUS_FOLD_ENABLED", "false")
                .strip()
                .lower()
                in _TRUTHY
            ),
            cap=float(os.environ.get("DITTO_EFFICIENCY_BONUS_CAP", "0.05")),
            deep_cap=float(os.environ.get("DITTO_EFFICIENCY_BONUS_DEEP_CAP", "0.10")),
            deep_frontier_ratio=float(
                os.environ.get("DITTO_EFFICIENCY_BONUS_DEEP_FRONTIER_RATIO", "0.5")
            ),
            cohort_size=int(os.environ.get("DITTO_EFFICIENCY_BONUS_COHORT_SIZE", "25")),
            min_cohort=int(os.environ.get("DITTO_EFFICIENCY_BONUS_MIN_COHORT", "8")),
            epoch_hours=int(os.environ.get("DITTO_EFFICIENCY_BONUS_EPOCH_HOURS", "24")),
            quality_floor=float(
                os.environ.get("DITTO_EFFICIENCY_BONUS_QUALITY_FLOOR", "0")
            ),
            memory_floor=float(
                os.environ.get("DITTO_EFFICIENCY_BONUS_MEMORY_FLOOR", "0")
            ),
        )
    except ValueError as error:
        raise ApiServerConfigError("efficiency bonus knobs must be numeric") from error

    try:
        top5_backoff_base = int(os.environ.get("TOP5_RESCORE_BACKOFF_BASE", "2"))
        top5_backoff_doubling_tempos = int(
            os.environ.get("TOP5_RESCORE_BACKOFF_K", "20")
        )
        top5_backoff_cap = int(os.environ.get("TOP5_RESCORE_BACKOFF_CAP", "8"))
    except ValueError as error:
        raise ApiServerConfigError(
            "TOP5_RESCORE_BACKOFF_BASE / _K / _CAP must be integer tempos"
        ) from error
    if top5_backoff_base < 0:
        raise ApiServerConfigError("TOP5_RESCORE_BACKOFF_BASE must be non-negative")
    if top5_backoff_doubling_tempos < 1:
        raise ApiServerConfigError("TOP5_RESCORE_BACKOFF_K must be >= 1")
    if top5_backoff_cap < max(1, top5_backoff_base):
        raise ApiServerConfigError("TOP5_RESCORE_BACKOFF_CAP must be >= max(1, base)")

    return ApiServerConfig(
        host=host,
        port=port,
        log_level=log_level,
        commit_hash=commit_hash,
        upload_payment_address=upload_payment_address,
        postgres=parse_postgres_config_from_env(),
        chain=parse_chain_config_from_env(),
        pricing=parse_pricing_config_from_env(),
        storage=parse_storage_config_from_env(),
        embedding=parse_embedding_config_from_env(),
        data_pipeline=parse_data_pipeline_config_from_env(),
        screener_auth=ScreenerAuthConfig(
            hotkey=screener_hotkey,
            api_token=screener_api_token,
        ),
        validator_names=parse_validator_names_config_from_env(),
        validator_compatibility=ValidatorCompatibilityConfig(
            minimum_software_version=minimum_validator_version,
            minimum_protocol_version=minimum_validator_protocol,
            heartbeat_max_age_seconds=validator_heartbeat_max_age,
        ),
        inference_proxy=inference_proxy,
        admin_api_token=os.environ.get("DITTO_ADMIN_API_TOKEN") or None,
        dashboard_enabled=dashboard_enabled,
        dashboard_wandb_url=dashboard_wandb_url,
        top5_backoff_base=top5_backoff_base,
        top5_backoff_doubling_tempos=top5_backoff_doubling_tempos,
        top5_backoff_cap=top5_backoff_cap,
        efficiency_bonus=efficiency_bonus,
    )


def check_config(config: ApiServerConfig) -> None:
    """Validate port range + log-level set membership.

    Raises:
        ApiServerConfigError: When ``port`` is outside ``1..65535`` or
            ``log_level`` is not a stdlib level name.
    """
    if not 1 <= config.port <= 65535:
        raise ApiServerConfigError(f"port out of range: {config.port}")
    if config.log_level not in _VALID_LOG_LEVELS:
        raise ApiServerConfigError(
            f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}; "
            f"got {config.log_level!r}"
        )
    auth = config.screener_auth
    if (auth.hotkey is None) != (auth.api_token is None):
        raise ApiServerConfigError(
            "SCREENER_HOTKEY and SCREENER_API_TOKEN must be set together"
        )
    if auth.hotkey is not None and not _SS58_RE.fullmatch(auth.hotkey):
        raise ApiServerConfigError("SCREENER_HOTKEY is not a valid SS58 address")
    if auth.api_token is not None and len(auth.api_token) < 32:
        raise ApiServerConfigError("SCREENER_API_TOKEN must be at least 32 characters")
    if config.admin_api_token is not None and len(config.admin_api_token) < 32:
        raise ApiServerConfigError(
            "DITTO_ADMIN_API_TOKEN must be at least 32 characters"
        )
    names = config.validator_names
    if (names.url is None) != (names.api_key is None):
        raise ApiServerConfigError(
            "DITTO_TAOSTATS_VALIDATOR_NAMES_URL and DITTO_TAOSTATS_API_KEY "
            "must be set together"
        )
    if names.url is not None:
        parsed = urlparse(names.url)
        query = parse_qs(parsed.query)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.taostats.io"
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/api/dtao/validator/available/v1"
            or query.get("netuid") != ["118"]
        ):
            raise ApiServerConfigError(
                "DITTO_TAOSTATS_VALIDATOR_NAMES_URL must use the documented "
                "https://api.taostats.io/api/dtao/validator/available/v1"
                "?netuid=118 endpoint"
            )
    if not 0.1 <= names.timeout_seconds <= 5.0:
        raise ApiServerConfigError(
            "DITTO_TAOSTATS_TIMEOUT_SECONDS must be between 0.1 and 5"
        )
    if names.retry_seconds < 60:
        raise ApiServerConfigError("DITTO_TAOSTATS_RETRY_SECONDS must be at least 60")
    if names.refresh_seconds < names.retry_seconds:
        raise ApiServerConfigError(
            "DITTO_TAOSTATS_REFRESH_SECONDS must be at least the retry interval"
        )
    if names.max_stale_seconds < names.refresh_seconds:
        raise ApiServerConfigError(
            "DITTO_TAOSTATS_MAX_STALE_SECONDS must be at least the refresh interval"
        )
    compatibility = config.validator_compatibility
    if compatibility.minimum_protocol_version < 1:
        raise ApiServerConfigError(
            "DITTO_MIN_VALIDATOR_PROTOCOL_VERSION must be at least 1"
        )
    if compatibility.heartbeat_max_age_seconds < 30:
        raise ApiServerConfigError(
            "DITTO_VALIDATOR_HEARTBEAT_MAX_AGE_SECONDS must be at least 30"
        )
    if compatibility.minimum_software_version is not None and not re.fullmatch(
        r"\d+\.\d+\.\d+", compatibility.minimum_software_version
    ):
        raise ApiServerConfigError(
            "DITTO_MIN_VALIDATOR_SOFTWARE_VERSION must be a stable X.Y.Z release"
        )
    inference = config.inference_proxy
    if inference.routing_mode not in {"aggregate_throughput", "adaptive"}:
        raise ApiServerConfigError(
            "DITTO_INFERENCE_ROUTING_MODE must be aggregate_throughput or adaptive"
        )
    if inference.enabled and inference.openrouter_api_key is None:
        raise ApiServerConfigError(
            "OPENROUTER_API_KEY is required when the inference proxy is enabled"
        )
    if inference.required and not inference.enabled:
        raise ApiServerConfigError(
            "inference proxy must be enabled before it can be required"
        )
    if not inference.allowed_models or len(inference.allowed_models) > 4:
        raise ApiServerConfigError("inference model allowlist must contain 1-4 models")
    upstream = urlparse(inference.upstream_url)
    if (
        upstream.scheme != "https"
        or upstream.hostname != "openrouter.ai"
        or upstream.path != "/api/v1/chat/completions"
    ):
        raise ApiServerConfigError(
            "inference upstream must be OpenRouter chat completions"
        )
    embedding_upstream = urlparse(inference.embedding_upstream_url)
    if (
        embedding_upstream.scheme != "https"
        or embedding_upstream.hostname != "openrouter.ai"
        or embedding_upstream.path != "/api/v1/embeddings"
    ):
        raise ApiServerConfigError("embedding upstream must be OpenRouter embeddings")
    if (
        inference.embedding_model != "perplexity/pplx-embed-v1-0.6b"
        or inference.embedding_profile
        != "dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1"
        or inference.embedding_provider != "Perplexity"
        or inference.embedding_dimensions != 768
    ):
        raise ApiServerConfigError("v7 embedding identity is not the reviewed contract")
    public_base = urlparse(inference.public_base_url)
    if public_base.scheme not in {"http", "https"} or not public_base.netloc:
        raise ApiServerConfigError("inference public base URL must be absolute")
    limits = (
        inference.request_budget,
        inference.token_budget,
        inference.per_ticket_concurrency,
        inference.per_validator_concurrency,
        inference.global_concurrency,
        inference.per_ticket_requests_per_minute,
        inference.per_validator_requests_per_minute,
        inference.global_requests_per_minute,
        inference.request_body_bytes,
        inference.response_body_bytes,
        inference.max_output_tokens,
        inference.embedding_request_budget,
        inference.embedding_token_budget,
        inference.embedding_per_ticket_concurrency,
        inference.embedding_per_validator_concurrency,
        inference.embedding_global_concurrency,
        inference.embedding_per_ticket_requests_per_minute,
        inference.embedding_per_validator_requests_per_minute,
        inference.embedding_global_requests_per_minute,
        inference.embedding_request_body_bytes,
        inference.embedding_response_body_bytes,
    )
    if any(value < 1 for value in limits):
        raise ApiServerConfigError("inference proxy limits must be positive")
    if inference.discovery_interval_seconds < 30:
        raise ApiServerConfigError(
            "inference provider discovery interval must be at least 30 seconds"
        )
    if "{model}" not in inference.discovery_url_template:
        raise ApiServerConfigError("inference discovery URL must contain {model}")
    discovery = urlparse(inference.discovery_url_template.replace("{model}", "model"))
    if discovery.scheme != "https" or discovery.hostname != "openrouter.ai":
        raise ApiServerConfigError("inference discovery must use OpenRouter HTTPS")
    route_weights = (
        inference.route_speed_weight,
        inference.route_cost_weight,
        inference.route_exploration_weight,
    )
    if any(weight < 0 for weight in route_weights) or sum(route_weights) <= 0:
        raise ApiServerConfigError(
            "inference route weights must be non-negative and non-zero"
        )
    if not 0 < inference.route_ewma_alpha <= 1:
        raise ApiServerConfigError("inference route EWMA alpha must be in (0, 1]")
    if not 0 <= inference.route_min_tool_accuracy <= 1:
        raise ApiServerConfigError(
            "inference route tool-accuracy floor must be in [0, 1]"
        )
    if not 0 <= inference.route_min_composite <= 1:
        raise ApiServerConfigError("inference route composite floor must be in [0, 1]")
    if inference.route_min_calibration_samples < 1:
        raise ApiServerConfigError(
            "inference route calibration sample floor must be positive"
        )
    if inference.route_exploration_ticket_budget < 0:
        raise ApiServerConfigError(
            "inference route exploration budget cannot be negative"
        )
    if not 0 <= inference.route_max_error_rate <= 1:
        raise ApiServerConfigError("inference route error ceiling must be in [0, 1]")
    if not 0 <= inference.route_max_timeout_rate <= 1:
        raise ApiServerConfigError("inference route timeout ceiling must be in [0, 1]")
    if inference.route_cooldown_seconds < 1:
        raise ApiServerConfigError("inference route cooldown must be positive")
    if inference.reviewed_calibration_manifest_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", inference.reviewed_calibration_manifest_sha256
    ):
        raise ApiServerConfigError(
            "reviewed inference calibration manifest must be lowercase sha256"
        )
    if not (
        inference.per_ticket_concurrency
        <= inference.per_validator_concurrency
        <= inference.global_concurrency
        <= 128
    ):
        raise ApiServerConfigError(
            "inference concurrency must be ordered ticket <= validator <= global <= 128"
        )
    if not (
        inference.embedding_per_ticket_concurrency
        <= inference.embedding_per_validator_concurrency
        <= inference.embedding_global_concurrency
        <= 128
    ):
        raise ApiServerConfigError(
            "embedding concurrency must be ordered ticket <= validator <= global <= 128"
        )
    if not (
        inference.per_ticket_requests_per_minute
        <= inference.per_validator_requests_per_minute
        <= inference.global_requests_per_minute
        <= 100_000
    ):
        raise ApiServerConfigError(
            "inference request rates must be ordered ticket <= validator <= global"
        )
    if not (
        inference.embedding_per_ticket_requests_per_minute
        <= inference.embedding_per_validator_requests_per_minute
        <= inference.embedding_global_requests_per_minute
        <= 100_000
    ):
        raise ApiServerConfigError(
            "embedding request rates must be ordered ticket <= validator <= global"
        )
    # Imported rather than repeated: the boot bound and the operator board's
    # ceiling are the same bound, and a board that accepts a number the boot
    # check would have rejected is a board that bricks the next restart.
    if (
        inference.request_budget > MAX_CHAT_REQUEST_BUDGET
        or inference.token_budget > MAX_CHAT_TOKEN_BUDGET
        or inference.request_body_bytes > 1 << 20
        or inference.response_body_bytes > 8 << 20
        or inference.max_output_tokens > 32_768
    ):
        raise ApiServerConfigError("inference proxy limit exceeds its safety bound")
    if (
        inference.embedding_request_budget > 100_000
        or inference.embedding_token_budget > 1_000_000_000
        or inference.embedding_request_body_bytes > 1 << 20
        or inference.embedding_response_body_bytes > 16 << 20
    ):
        raise ApiServerConfigError("embedding proxy limit exceeds its safety bound")
    if not 1 <= inference.timeout_seconds <= 120:
        raise ApiServerConfigError(
            "inference timeout must be between 1 and 120 seconds"
        )
    efficiency = config.efficiency_bonus
    if efficiency.fold_enabled and not efficiency.enabled:
        raise ApiServerConfigError(
            "efficiency bonus must be enabled before its fold exposure can be"
        )
    if not 0.0 < efficiency.cap <= 0.10:
        raise ApiServerConfigError("DITTO_EFFICIENCY_BONUS_CAP must be in (0, 0.10]")
    if not efficiency.cap <= efficiency.deep_cap <= 0.10:
        raise ApiServerConfigError(
            "DITTO_EFFICIENCY_BONUS_DEEP_CAP must satisfy cap <= deep_cap <= 0.10"
        )
    if not 0.0 < efficiency.deep_frontier_ratio < 1.0:
        raise ApiServerConfigError(
            "DITTO_EFFICIENCY_BONUS_DEEP_FRONTIER_RATIO must be in (0, 1)"
        )
    if efficiency.min_cohort < 2:
        raise ApiServerConfigError(
            "DITTO_EFFICIENCY_BONUS_MIN_COHORT must be at least 2"
        )
    if efficiency.cohort_size < efficiency.min_cohort:
        raise ApiServerConfigError(
            "DITTO_EFFICIENCY_BONUS_COHORT_SIZE must be at least the minimum cohort"
        )
    if efficiency.epoch_hours < 1:
        raise ApiServerConfigError(
            "DITTO_EFFICIENCY_BONUS_EPOCH_HOURS must be at least 1"
        )
    if not 0.0 <= efficiency.quality_floor <= 1.0:
        raise ApiServerConfigError(
            "DITTO_EFFICIENCY_BONUS_QUALITY_FLOOR must be in [0, 1]"
        )
    if not 0.0 <= efficiency.memory_floor <= 1.0:
        raise ApiServerConfigError(
            "DITTO_EFFICIENCY_BONUS_MEMORY_FLOOR must be in [0, 1]"
        )
