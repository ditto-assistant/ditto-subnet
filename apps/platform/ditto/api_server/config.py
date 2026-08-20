"""Resolved configuration for the API server process."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ditto.api_models.inference_concurrency_settings import (
    DEFAULT_CHAT_GLOBAL_CONCURRENCY,
    DEFAULT_CHAT_PER_TICKET_CONCURRENCY,
    DEFAULT_CHAT_PER_VALIDATOR_CONCURRENCY,
    DEFAULT_CHAT_REQUEST_BUDGET,
    DEFAULT_CHAT_TOKEN_BUDGET,
    DEFAULT_EMBEDDING_GLOBAL_CONCURRENCY,
    DEFAULT_EMBEDDING_PER_TICKET_CONCURRENCY,
    DEFAULT_EMBEDDING_PER_VALIDATOR_CONCURRENCY,
    MAX_CHAT_CONCURRENCY,
    MAX_CHAT_REQUEST_BUDGET,
    MAX_CHAT_TOKEN_BUDGET,
    MAX_EMBEDDING_GLOBAL_CONCURRENCY,
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

    controller_api_token: str | None = None
    """Least-privilege token allowed to reconcile nodes and capacity only."""

    bootstrap_ttl_seconds: int = 600
    """Lifetime of a single-use node registration capability."""

    node_token_ttl_seconds: int = 6 * 60 * 60
    """Lifetime of a rotating per-node bearer token."""

    controller_lease_seconds: int = 180
    """Fencing lease renewed by the single normal capacity writer."""

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
    perplexity_api_key: str | None
    upstream_url: str
    allowed_models: tuple[str, ...]
    provider: str
    routing_mode: str
    request_budget: int
    token_budget: int
    embedding_upstream_url: str
    embedding_fallback_url: str
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

    factor_alpha: float = 0.25
    """Exponent for the bounded v3 efficiency factor
    (``DITTO_EFFICIENCY_BONUS_FACTOR_ALPHA``), in ``(0, 1]``. Values below
    one soften cost differences before the minimum/maximum clamps apply."""

    minimum_factor: float = 0.85
    """Lower clamp for a factor-curve envelope
    (``DITTO_EFFICIENCY_BONUS_MINIMUM_FACTOR``), in ``(0, 1]``."""

    maximum_factor: float = 1.10
    """Upper clamp for a factor-curve envelope
    (``DITTO_EFFICIENCY_BONUS_MAXIMUM_FACTOR``), in ``[1, 100]``.
    Curve v4 does not apply this bound."""

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
class TargonRentalConfig:
    """Create Kaniko, runtime-smoke, and L1 rentals from the Platform process."""

    api_key: str = field(repr=False)
    org_slug: str
    resource: str
    public_platform_url: str
    submission_builder_image: str
    candidate_writer_sa: str
    candidate_reader_sa: str
    bootstrap_sa: str
    source_review_secret_resource: str
    environment: str = "prod"
    interval_seconds: float = 15.0
    provision_timeout_seconds: float = 600.0
    build_timeout_seconds: float = 1500.0
    runtime_timeout_seconds: float = 180.0
    source_review_timeout_seconds: float = 1800.0
    max_inflight: int = 10
    """Cap on concurrent Targon rentals (Kaniko, smoke, and L1 combined)."""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and bool(self.submission_builder_image)


@dataclass(frozen=True)
class CloudRunScreeningConfig:
    """GCP Cloud Run fallback for Kaniko Jobs, L1 Jobs, and smoke Services."""

    project: str
    region: str
    untrusted_sa_email: str
    platform_invoker_sa_email: str

    @property
    def enabled(self) -> bool:
        return bool(
            self.project
            and self.region
            and self.untrusted_sa_email
            and self.platform_invoker_sa_email
        )


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
            perplexity_api_key=None,
            upstream_url="https://openrouter.ai/api/v1/chat/completions",
            allowed_models=("qwen/qwen3-32b", "openai/gpt-oss-20b"),
            provider="nebius",
            routing_mode="aggregate_throughput",
            request_budget=DEFAULT_CHAT_REQUEST_BUDGET,
            token_budget=DEFAULT_CHAT_TOKEN_BUDGET,
            embedding_upstream_url="https://openrouter.ai/api/v1/embeddings",
            embedding_fallback_url="https://api.perplexity.ai/v1/embeddings",
            embedding_model="perplexity/pplx-embed-v1-0.6b",
            embedding_profile="dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1",
            embedding_provider="Perplexity",
            embedding_dimensions=768,
            embedding_request_budget=100_000,
            embedding_token_budget=1_000_000_000,
            embedding_per_ticket_concurrency=(DEFAULT_EMBEDDING_PER_TICKET_CONCURRENCY),
            embedding_per_validator_concurrency=(
                DEFAULT_EMBEDDING_PER_VALIDATOR_CONCURRENCY
            ),
            embedding_global_concurrency=DEFAULT_EMBEDDING_GLOBAL_CONCURRENCY,
            embedding_per_ticket_requests_per_minute=10_000,
            embedding_per_validator_requests_per_minute=40_000,
            embedding_global_requests_per_minute=100_000,
            embedding_request_body_bytes=1 << 20,
            embedding_response_body_bytes=16 << 20,
            per_ticket_concurrency=DEFAULT_CHAT_PER_TICKET_CONCURRENCY,
            per_validator_concurrency=DEFAULT_CHAT_PER_VALIDATOR_CONCURRENCY,
            global_concurrency=DEFAULT_CHAT_GLOBAL_CONCURRENCY,
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

    targon: TargonRentalConfig | None = None
    """In-process Targon rental loop. Disabled when the API key is absent."""

    cloudrun: CloudRunScreeningConfig | None = None
    """Cloud Run Jobs/Service fallback. Disabled when project/SA env is absent."""


def _parse_targon_rental_config_from_env(commit_hash: str) -> TargonRentalConfig | None:
    api_key = os.environ.get("DITTO_TARGON_API_KEY", "").strip()
    if not api_key:
        key_file = os.environ.get("DITTO_TARGON_API_KEY_FILE", "").strip()
        if not key_file:
            return None
        try:
            api_key = Path(key_file).read_text().strip()
        except OSError as error:
            raise ApiServerConfigError(
                "DITTO_TARGON_API_KEY_FILE is unreadable"
            ) from error
    if len(api_key) < 32:
        raise ApiServerConfigError("DITTO_TARGON_API_KEY is invalid")
    public_url = os.environ.get("DITTO_TARGON_PUBLIC_PLATFORM_URL", "").strip()
    builder_image = os.environ.get("DITTO_TARGON_SUBMISSION_BUILDER_IMAGE", "").strip()
    image_name = builder_image.rsplit("/", 1)[-1]
    if builder_image and "@" not in image_name and ":" not in image_name:
        if not re.fullmatch(r"[0-9a-f]{40}", commit_hash):
            raise ApiServerConfigError(
                "Targon builder image tag requires a 40-character commit hash"
            )
        builder_image = f"{builder_image}:sha-{commit_hash}"
    if not public_url.startswith("https://") or not builder_image:
        raise ApiServerConfigError(
            "Targon rentals require DITTO_TARGON_PUBLIC_PLATFORM_URL and "
            "DITTO_TARGON_SUBMISSION_BUILDER_IMAGE"
        )
    return TargonRentalConfig(
        api_key=api_key,
        org_slug=os.environ.get("DITTO_TARGON_ORG_SLUG", "ditto").strip(),
        resource=os.environ.get("DITTO_TARGON_RESOURCE", "cpu-small").strip(),
        public_platform_url=public_url.rstrip("/"),
        submission_builder_image=builder_image,
        candidate_writer_sa=os.environ.get(
            "DITTO_TARGON_CANDIDATE_PUSH_SA", ""
        ).strip(),
        candidate_reader_sa=os.environ.get(
            "DITTO_TARGON_CANDIDATE_PULL_SA", ""
        ).strip(),
        bootstrap_sa=os.environ.get("DITTO_TARGON_BOOTSTRAP_SA", "").strip(),
        source_review_secret_resource=os.environ.get(
            "DITTO_TARGON_SOURCE_REVIEW_SECRET", ""
        ).strip(),
        environment=os.environ.get("DITTO_TARGON_ENVIRONMENT", "prod").strip()
        or "prod",
        max_inflight=_parse_targon_max_inflight(),
    )


def _parse_targon_max_inflight() -> int:
    raw = os.environ.get("DITTO_TARGON_MAX_INFLIGHT", "10").strip() or "10"
    try:
        value = int(raw)
    except ValueError as error:
        raise ApiServerConfigError(
            "DITTO_TARGON_MAX_INFLIGHT must be an integer"
        ) from error
    if value < 1:
        raise ApiServerConfigError("DITTO_TARGON_MAX_INFLIGHT must be >= 1")
    return value


def _parse_cloudrun_screening_config_from_env() -> CloudRunScreeningConfig | None:
    project = os.environ.get("DITTO_CLOUDRUN_PROJECT", "").strip()
    untrusted = os.environ.get("DITTO_CLOUDRUN_UNTRUSTED_SA", "").strip()
    invoker = os.environ.get("DITTO_CLOUDRUN_INVOKER_SA", "").strip()
    if not project or not untrusted or not invoker:
        return None
    return CloudRunScreeningConfig(
        project=project,
        region=os.environ.get("DITTO_CLOUDRUN_REGION", "us-central1").strip()
        or "us-central1",
        untrusted_sa_email=untrusted,
        platform_invoker_sa_email=invoker,
    )


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
    screener_controller_api_token = (
        os.environ.get("SCREENER_CONTROLLER_API_TOKEN") or None
    )
    try:
        screener_bootstrap_ttl_seconds = int(
            os.environ.get("SCREENER_BOOTSTRAP_TTL_SECONDS", "600")
        )
        screener_node_token_ttl_seconds = int(
            os.environ.get("SCREENER_NODE_TOKEN_TTL_SECONDS", str(6 * 60 * 60))
        )
        screener_controller_lease_seconds = int(
            os.environ.get("SCREENER_CONTROLLER_LEASE_SECONDS", "180")
        )
    except ValueError as error:
        raise ApiServerConfigError(
            "screener bootstrap, node token, and controller lease TTLs must be integers"
        ) from error
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
            perplexity_api_key=os.environ.get("PERPLEXITY_API_KEY") or None,
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
            request_budget=DEFAULT_CHAT_REQUEST_BUDGET,
            token_budget=DEFAULT_CHAT_TOKEN_BUDGET,
            embedding_upstream_url=os.environ.get(
                "DITTO_EMBEDDING_UPSTREAM_URL",
                "https://openrouter.ai/api/v1/embeddings",
            ),
            embedding_fallback_url=os.environ.get(
                "DITTO_EMBEDDING_FALLBACK_URL",
                "https://api.perplexity.ai/v1/embeddings",
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
            embedding_per_ticket_concurrency=(DEFAULT_EMBEDDING_PER_TICKET_CONCURRENCY),
            embedding_per_validator_concurrency=(
                DEFAULT_EMBEDDING_PER_VALIDATOR_CONCURRENCY
            ),
            embedding_global_concurrency=DEFAULT_EMBEDDING_GLOBAL_CONCURRENCY,
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
            per_ticket_concurrency=DEFAULT_CHAT_PER_TICKET_CONCURRENCY,
            per_validator_concurrency=DEFAULT_CHAT_PER_VALIDATOR_CONCURRENCY,
            global_concurrency=DEFAULT_CHAT_GLOBAL_CONCURRENCY,
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
            factor_alpha=float(
                os.environ.get("DITTO_EFFICIENCY_BONUS_FACTOR_ALPHA", "0.25")
            ),
            minimum_factor=float(
                os.environ.get("DITTO_EFFICIENCY_BONUS_MINIMUM_FACTOR", "0.85")
            ),
            maximum_factor=float(
                os.environ.get("DITTO_EFFICIENCY_BONUS_MAXIMUM_FACTOR", "1.10")
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

    targon = _parse_targon_rental_config_from_env(commit_hash)
    cloudrun = _parse_cloudrun_screening_config_from_env()

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
        targon=targon,
        cloudrun=cloudrun,
        screener_auth=ScreenerAuthConfig(
            hotkey=screener_hotkey,
            api_token=screener_api_token,
            controller_api_token=screener_controller_api_token,
            bootstrap_ttl_seconds=screener_bootstrap_ttl_seconds,
            node_token_ttl_seconds=screener_node_token_ttl_seconds,
            controller_lease_seconds=screener_controller_lease_seconds,
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
    if auth.controller_api_token is not None and len(auth.controller_api_token) < 32:
        raise ApiServerConfigError(
            "SCREENER_CONTROLLER_API_TOKEN must be at least 32 characters"
        )
    if not 60 <= auth.bootstrap_ttl_seconds <= 3600:
        raise ApiServerConfigError(
            "SCREENER_BOOTSTRAP_TTL_SECONDS must be between 60 and 3600"
        )
    if not 900 <= auth.node_token_ttl_seconds <= 86_400:
        raise ApiServerConfigError(
            "SCREENER_NODE_TOKEN_TTL_SECONDS must be between 900 and 86400"
        )
    if not 60 <= auth.controller_lease_seconds <= 900:
        raise ApiServerConfigError(
            "SCREENER_CONTROLLER_LEASE_SECONDS must be between 60 and 900"
        )
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
    embedding_fallback = urlparse(inference.embedding_fallback_url)
    if (
        embedding_fallback.scheme != "https"
        or embedding_fallback.hostname != "api.perplexity.ai"
        or embedding_fallback.path != "/v1/embeddings"
    ):
        raise ApiServerConfigError("embedding fallback must be Perplexity embeddings")
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
        <= MAX_CHAT_CONCURRENCY
    ):
        raise ApiServerConfigError(
            "inference concurrency must be ordered ticket <= validator <= global "
            f"<= {MAX_CHAT_CONCURRENCY}"
        )
    if not (
        inference.embedding_per_ticket_concurrency
        <= inference.embedding_per_validator_concurrency
        <= inference.embedding_global_concurrency
        <= MAX_EMBEDDING_GLOBAL_CONCURRENCY
    ):
        raise ApiServerConfigError(
            "embedding concurrency must be ordered ticket <= validator <= global "
            f"<= {MAX_EMBEDDING_GLOBAL_CONCURRENCY}"
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
    if not 0.0 < efficiency.factor_alpha <= 1.0:
        raise ApiServerConfigError(
            "DITTO_EFFICIENCY_BONUS_FACTOR_ALPHA must be in (0, 1]"
        )
    if not 0.0 < efficiency.minimum_factor <= 1.0:
        raise ApiServerConfigError(
            "DITTO_EFFICIENCY_BONUS_MINIMUM_FACTOR must be in (0, 1]"
        )
    if not 1.0 <= efficiency.maximum_factor <= 100.0:
        raise ApiServerConfigError(
            "DITTO_EFFICIENCY_BONUS_MAXIMUM_FACTOR must be in [1, 100]"
        )
    if efficiency.minimum_factor > efficiency.maximum_factor:
        raise ApiServerConfigError(
            "DITTO_EFFICIENCY_BONUS_MINIMUM_FACTOR must not exceed "
            "DITTO_EFFICIENCY_BONUS_MAXIMUM_FACTOR"
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
