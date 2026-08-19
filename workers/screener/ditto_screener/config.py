"""Env-driven config for the screener worker.

Frozen dataclass + ``parse_screener_config_from_env`` builder, matching the
validator's convention (``SCREENER_*`` / ``NETUID`` env). The worker is a
standalone process; it talks to the platform only over the ``/screener/*`` HTTP
API and drives the local Docker daemon for the build gate. Nothing here imports
the DB.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ditto_screener.errors import ScreenerConfigError


@dataclass(frozen=True)
class ScreenerConfig:
    """Configuration for one screener worker instance."""

    # --- Platform API (HTTP-decoupled; the worker calls the platform, even on
    # localhost, exactly as any external screener would) ---
    platform_api_url: str
    """Base URL of the platform API, e.g. ``http://localhost:8000``."""

    api_token: str = field(repr=False)
    """Bearer token shared only with the platform's screener endpoints."""

    # --- Identity / chain ---
    screener_hotkey: str
    """Dedicated screener SS58 hotkey matching the loaded signing keypair.

    The platform explicitly allowlists this public key; it does not need an
    on-chain validator permit and should not hold funds.
    """

    wallet_name: str | None
    """bittensor wallet name to load the signing hotkey from (if used)."""

    wallet_hotkey: str | None
    """bittensor wallet hotkey name (paired with ``wallet_name``)."""

    screener_mnemonic: str | None
    """Alternative signing source: a hotkey mnemonic (secret). Prefer a wallet."""

    node_id: str | None
    """Provider-neutral enrolled node identity, or null for legacy workers."""

    node_provider: str | None
    """Capacity provider recorded during enrollment."""

    node_provider_resource_id: str | None
    """Opaque provider workload/instance identity recorded during enrollment."""

    node_credential_file: str | None
    """Mode-0600 rotating credential store for an enrolled worker."""

    netuid: int
    """Subnet netuid (118 for Ditto)."""

    # --- Build gate (Docker) ---
    docker_bin: str
    """Path/name of the docker CLI the gate shells out to."""

    docker_host: str | None
    """Explicit operator-owned Docker endpoint (normally a rootless socket)."""

    require_rootless_docker: bool
    """Fail closed before a build unless Docker advertises rootless mode."""

    build_timeout_seconds: float
    """Hard cap on a single ``docker build`` (cold dependency builds are slow).

    Defaults to 45 minutes. Keep this at or below the platform's screening lease
    window: the worker clamps a build to the remaining lease, so a cap larger
    than the lease can never be used in full, while a cap smaller than a
    legitimate slow image build false-fails it as a build timeout."""

    run_timeout_seconds: float
    """Hard cap on the container serve, health, and optional private audit."""

    image_build_memory: str
    """Memory/swap cap for the untrusted Docker image build (e.g. ``8g``).

    Compilers and linkers can need substantially more memory than the finished
    harness.  This separate, bounded envelope keeps the container contract
    language-neutral without granting the built image a larger runtime budget.
    """

    gh_token_file: str | None
    """Deprecated / retained for config compatibility only. It was the BuildKit
    ``gh_token`` secret for the once-private ``ditto-harness`` dep; that repo is
    now public, and mounting any credential into a submission-controlled build is
    an exfiltration vector, so ``_build`` no longer consumes this. Leave unset."""

    pids_limit: int
    """``docker run --pids-limit`` for the smoke container."""

    health_path: str
    """Harness health path to probe (contract: ``/health``)."""

    container_port: int
    """Port the harness serves on inside the container (contract: ``8080``)."""

    smoke_env: tuple[tuple[str, str], ...]
    """Env vars injected (``docker run -e K=V``) into the serve-smoke container.

    The fake gateway appends locked provider settings after these values. This
    tuple remains available for unrelated boot-time variables
    needed before ``/health`` binds. Defaults to a placeholder OpenRouter key for
    older reference harnesses; no real provider credential is ever injected."""

    max_tarball_bytes: int
    """Reject an artifact larger than this before building. It is a download DoS
    bound and MUST be >= the platform's upload cap (``DITTO_MAX_TARBALL_SIZE_BYTES``,
    default 20 MiB) — a smaller value here false-fails a tarball the platform
    legitimately accepted. Defaults to the platform's 20 MiB; raise both together."""

    # --- Cadence / limits ---
    poll_seconds: float
    """Seconds to sleep between queue sweeps when the queue was empty."""

    queue_limit: int
    """Max agents to pull from ``/screener/queue`` per sweep."""

    http_timeout_seconds: float
    """Per-request timeout for platform HTTP calls + artifact download."""

    policy_manifest_file: str | None
    """Private rotating policy manifest. ``None`` activates production v7."""

    review_journal_file: str | None
    """Mode-0600 append-only journal for quarantine/inconclusive outcomes."""

    source_review_api_key_file: str | None
    """Root-controlled OpenRouter key file for private source review."""

    source_review_model: str
    source_review_base_url: str
    source_review_timeout_seconds: float
    source_review_max_steps: int
    source_review_max_read_bytes: int
    source_review_reasoning_effort: str
    static_preflight_v2_mode: str
    """V2 detector rollout: legacy authority in off/shadow, v2 in enforce."""
    static_preflight_audit_file: str | None
    """Mode-0600 private v1/v2 reachability and causality journal."""

    l2_review_mode: str
    l2_review_model: str
    l2_review_provider: str
    l2_fallback_models: tuple[str, ...]
    l3_review_enabled: bool
    l3_review_model: str
    l3_review_provider: str
    l2_analyzer_image: str
    l2_cache_dir: str
    l2_audit_journal_file: str
    l2_timeout_seconds: float
    l2_max_steps: int
    l2_max_input_tokens: int
    l2_max_output_tokens: int
    l2_max_completion_tokens: int
    l2_max_cost_usd: float
    l2_analyst_reasoning_effort: str
    l2_critic_reasoning_effort: str
    l2_cache_ttl_seconds: float
    l2_audit_retention_days: int
    review_settings_cache_file: str
    review_settings_max_stale_seconds: int
    remote_build_mode: str = "prefer"
    """``prefer`` tries the attempt-bound Targon Kaniko queue before Docker."""
    remote_build_timeout_seconds: float = 1500.0
    """Maximum time to wait for a Targon Kaniko submission build.

    This is intentionally independent of ``build_timeout_seconds``. The latter
    caps the local Docker fallback, while the 70-minute screening lease budgets
    both stages: 25 minutes for Targon followed by up to 45 minutes locally.
    """

    def signing_source_present(self) -> bool:
        """Whether a usable signing key source is configured."""
        return bool(self.screener_mnemonic) or bool(
            self.wallet_name and self.wallet_hotkey
        )


def _require(name: str, value: str) -> str:
    if not value:
        raise ScreenerConfigError(f"{name} is required")
    return value


def _parse_float(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        return float(raw)
    except ValueError as e:
        raise ScreenerConfigError(f"{name} must be a number, got {raw!r}") from e


def _parse_int(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError as e:
        raise ScreenerConfigError(f"{name} must be an integer, got {raw!r}") from e


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ScreenerConfigError(f"{name} must be a boolean, got {raw!r}")


def _parse_env_pairs(name: str, default: str) -> tuple[tuple[str, str], ...]:
    """Parse ``K=V,K2=V2`` env-var pairs (for the smoke container's ``-e``)."""
    raw = os.environ.get(name, default)
    pairs: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ScreenerConfigError(
                f"{name} must be comma-separated K=V pairs, got {item!r}"
            )
        pairs.append((key.strip(), value))
    return tuple(pairs)


def _parse_csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(
        raw.strip() for raw in os.environ.get(name, default).split(",") if raw.strip()
    )


def parse_screener_config_from_env() -> ScreenerConfig:
    """Build a :class:`ScreenerConfig` from ``SCREENER_*`` / ``NETUID`` env.

    Raises:
        ScreenerConfigError: when a required value is missing or no signing
            source is configured.
    """
    config = ScreenerConfig(
        platform_api_url=_require(
            "SCREENER_PLATFORM_API_URL",
            os.environ.get("SCREENER_PLATFORM_API_URL", "http://localhost:8000"),
        ),
        api_token=_require(
            "SCREENER_API_TOKEN", os.environ.get("SCREENER_API_TOKEN", "")
        ),
        screener_hotkey=_require(
            "SCREENER_HOTKEY", os.environ.get("SCREENER_HOTKEY", "")
        ),
        wallet_name=os.environ.get("SCREENER_WALLET_NAME") or None,
        wallet_hotkey=os.environ.get("SCREENER_WALLET_HOTKEY") or None,
        screener_mnemonic=os.environ.get("SCREENER_MNEMONIC") or None,
        node_id=os.environ.get("SCREENER_NODE_ID") or None,
        node_provider=os.environ.get("SCREENER_NODE_PROVIDER") or None,
        node_provider_resource_id=(
            os.environ.get("SCREENER_NODE_PROVIDER_RESOURCE_ID") or None
        ),
        node_credential_file=os.environ.get("SCREENER_NODE_CREDENTIAL_FILE") or None,
        netuid=_parse_int("NETUID", os.environ.get("NETUID", "118")),
        docker_bin=os.environ.get("SCREENER_DOCKER_BIN", "docker"),
        docker_host=(
            os.environ.get("SCREENER_DOCKER_HOST")
            or os.environ.get("DOCKER_HOST")
            or None
        ),
        # Additive rollout: deployments set this true only after their dedicated
        # rootless daemon is present. A later release can remove the compatibility
        # default once every pet and fleet worker has converged.
        require_rootless_docker=_parse_bool("SCREENER_REQUIRE_ROOTLESS_DOCKER", False),
        build_timeout_seconds=_parse_float("SCREENER_BUILD_TIMEOUT_SECONDS", "2700"),
        run_timeout_seconds=_parse_float("SCREENER_RUN_TIMEOUT_SECONDS", "120"),
        image_build_memory=os.environ.get(
            "SCREENER_IMAGE_BUILD_MEMORY",
            # Additive rollout fallback for deployments that have not rendered
            # the more precise variable name yet.
            os.environ.get("SCREENER_BUILD_MEMORY", "8g"),
        ),
        gh_token_file=os.environ.get("SCREENER_GH_TOKEN_FILE") or None,
        pids_limit=_parse_int("SCREENER_PIDS_LIMIT", "512"),
        health_path=os.environ.get("SCREENER_HEALTH_PATH", "/health"),
        container_port=_parse_int("SCREENER_CONTAINER_PORT", "8080"),
        smoke_env=_parse_env_pairs(
            # Compatibility key for older harness startup. The isolated fake
            # gateway separately locks provider traffic away from the internet.
            "SCREENER_SMOKE_ENV",
            "OPENROUTER_API_KEY=sk-screener-smoke",
        ),
        max_tarball_bytes=_parse_int(
            # Match the platform's default upload cap (20 MiB); a smaller value
            # false-fails legitimately-uploaded tarballs. Keep >= the platform cap.
            "SCREENER_MAX_TARBALL_BYTES",
            str(20 * 1024 * 1024),
        ),
        poll_seconds=_parse_float("SCREENER_POLL_SECONDS", "30"),
        queue_limit=_parse_int("SCREENER_QUEUE_LIMIT", "20"),
        http_timeout_seconds=_parse_float("SCREENER_HTTP_TIMEOUT_SECONDS", "60"),
        policy_manifest_file=os.environ.get("SCREENER_POLICY_MANIFEST_FILE") or None,
        review_journal_file=os.environ.get("SCREENER_REVIEW_JOURNAL_FILE") or None,
        source_review_api_key_file=(
            os.environ.get("SCREENER_SOURCE_REVIEW_API_KEY_FILE") or None
        ),
        source_review_model=os.environ.get(
            "SCREENER_SOURCE_REVIEW_MODEL", "openai/gpt-5.6-luna"
        ),
        source_review_base_url=os.environ.get(
            "SCREENER_SOURCE_REVIEW_BASE_URL", "https://openrouter.ai/api/v1"
        ),
        source_review_timeout_seconds=_parse_float(
            "SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS", "1800"
        ),
        source_review_max_steps=_parse_int("SCREENER_SOURCE_REVIEW_MAX_STEPS", "200"),
        source_review_max_read_bytes=_parse_int(
            "SCREENER_SOURCE_REVIEW_MAX_READ_BYTES", "8000000"
        ),
        source_review_reasoning_effort=os.environ.get(
            "SCREENER_SOURCE_REVIEW_REASONING_EFFORT", "high"
        ),
        static_preflight_v2_mode=os.environ.get(
            "SCREENER_STATIC_PREFLIGHT_V2_MODE", "off"
        ),
        static_preflight_audit_file=(
            os.environ.get("SCREENER_STATIC_PREFLIGHT_AUDIT_FILE") or None
        ),
        l2_review_mode=os.environ.get("SCREENER_L2_REVIEW_MODE", "off"),
        l2_review_model=os.environ.get(
            "SCREENER_L2_REVIEW_MODEL", "moonshotai/kimi-k3"
        ),
        l2_review_provider=os.environ.get("SCREENER_L2_REVIEW_PROVIDER", "openrouter"),
        l2_fallback_models=_parse_csv(
            "SCREENER_L2_FALLBACK_MODELS", "z-ai/glm-5.2,openai/gpt-5.6-sol"
        ),
        l3_review_enabled=_parse_bool("SCREENER_L3_REVIEW_ENABLED", True),
        l3_review_model=os.environ.get(
            "SCREENER_L3_REVIEW_MODEL", "openai/gpt-5.6-sol"
        ),
        l3_review_provider=os.environ.get("SCREENER_L3_REVIEW_PROVIDER", "openrouter"),
        l2_analyzer_image=os.environ.get(
            "SCREENER_L2_ANALYZER_IMAGE", "ditto-screener-l2-analyzer:active"
        ),
        l2_cache_dir=os.environ.get(
            "SCREENER_L2_CACHE_DIR", "/opt/ditto/screener/state/l2-cache"
        ),
        l2_audit_journal_file=os.environ.get(
            "SCREENER_L2_AUDIT_JOURNAL_FILE",
            "/opt/ditto/screener/state/l2-audit.jsonl",
        ),
        l2_timeout_seconds=_parse_float("SCREENER_L2_TIMEOUT_SECONDS", "900"),
        l2_max_steps=_parse_int("SCREENER_L2_MAX_STEPS", "18"),
        l2_max_input_tokens=_parse_int("SCREENER_L2_MAX_INPUT_TOKENS", "425000"),
        l2_max_output_tokens=_parse_int("SCREENER_L2_MAX_OUTPUT_TOKENS", "20000"),
        l2_max_completion_tokens=_parse_int(
            "SCREENER_L2_MAX_COMPLETION_TOKENS", "2400"
        ),
        l2_max_cost_usd=_parse_float("SCREENER_L2_MAX_COST_USD", "2.00"),
        l2_analyst_reasoning_effort=os.environ.get(
            "SCREENER_L2_ANALYST_REASONING_EFFORT", "model_default"
        ),
        l2_critic_reasoning_effort=os.environ.get(
            "SCREENER_L2_CRITIC_REASONING_EFFORT", "medium"
        ),
        l2_cache_ttl_seconds=_parse_float(
            "SCREENER_L2_CACHE_TTL_SECONDS", str(7 * 86_400)
        ),
        l2_audit_retention_days=_parse_int("SCREENER_L2_AUDIT_RETENTION_DAYS", "30"),
        review_settings_cache_file=os.environ.get(
            "SCREENER_REVIEW_SETTINGS_CACHE_FILE",
            "/opt/ditto/screener/state/review-settings.json",
        ),
        review_settings_max_stale_seconds=_parse_int(
            "SCREENER_REVIEW_SETTINGS_MAX_STALE_SECONDS", "900"
        ),
        remote_build_mode=os.environ.get("SCREENER_REMOTE_BUILD_MODE", "prefer"),
        remote_build_timeout_seconds=_parse_float(
            "SCREENER_REMOTE_BUILD_TIMEOUT_SECONDS", "1500"
        ),
    )
    if not config.signing_source_present():
        raise ScreenerConfigError(
            "no signing key: set SCREENER_MNEMONIC or "
            "SCREENER_WALLET_NAME + SCREENER_WALLET_HOTKEY"
        )
    if len(config.api_token) < 32:
        raise ScreenerConfigError("SCREENER_API_TOKEN must be at least 32 characters")
    if not config.source_review_api_key_file:
        raise ScreenerConfigError(
            "SCREENER_SOURCE_REVIEW_API_KEY_FILE is required by screening policy v8"
        )
    if not 1 <= config.source_review_max_steps <= 240:
        raise ScreenerConfigError(
            "SCREENER_SOURCE_REVIEW_MAX_STEPS must be between 1 and 240"
        )
    if not 32_000 <= config.source_review_max_read_bytes <= 16_000_000:
        raise ScreenerConfigError(
            "SCREENER_SOURCE_REVIEW_MAX_READ_BYTES must be between 32000 and 16000000"
        )
    if config.source_review_reasoning_effort not in {"low", "medium", "high"}:
        raise ScreenerConfigError(
            "SCREENER_SOURCE_REVIEW_REASONING_EFFORT must be low, medium, or high"
        )
    if config.source_review_model != "openai/gpt-5.6-luna":
        raise ScreenerConfigError(
            "SCREENER_SOURCE_REVIEW_MODEL must be openai/gpt-5.6-luna"
        )
    if not 60 <= config.source_review_timeout_seconds <= 3_600:
        raise ScreenerConfigError(
            "SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS must be between 60 and 3600"
        )
    if config.static_preflight_v2_mode not in {"off", "shadow", "enforce"}:
        raise ScreenerConfigError(
            "SCREENER_STATIC_PREFLIGHT_V2_MODE must be off, shadow, or enforce"
        )
    if (
        config.static_preflight_v2_mode == "shadow"
        and config.static_preflight_audit_file is None
    ):
        raise ScreenerConfigError(
            "SCREENER_STATIC_PREFLIGHT_AUDIT_FILE is required in shadow mode"
        )
    if config.l2_review_mode not in {"off", "shadow", "enforce"}:
        raise ScreenerConfigError(
            "SCREENER_L2_REVIEW_MODE must be off, shadow, or enforce"
        )
    if config.l2_review_model != "moonshotai/kimi-k3":
        raise ScreenerConfigError("SCREENER_L2_REVIEW_MODEL must be moonshotai/kimi-k3")
    if config.l2_review_provider != "openrouter":
        raise ScreenerConfigError("SCREENER_L2_REVIEW_PROVIDER must be openrouter")
    if config.l2_fallback_models != ("z-ai/glm-5.2", "openai/gpt-5.6-sol"):
        raise ScreenerConfigError(
            "SCREENER_L2_FALLBACK_MODELS must be z-ai/glm-5.2,openai/gpt-5.6-sol"
        )
    if config.l3_review_model != "openai/gpt-5.6-sol":
        raise ScreenerConfigError("SCREENER_L3_REVIEW_MODEL must be openai/gpt-5.6-sol")
    if config.l3_review_provider != "openrouter":
        raise ScreenerConfigError("SCREENER_L3_REVIEW_PROVIDER must be openrouter")
    if config.l2_analyzer_image != "ditto-screener-l2-analyzer:active":
        raise ScreenerConfigError(
            "SCREENER_L2_ANALYZER_IMAGE must be ditto-screener-l2-analyzer:active"
        )
    if not 1 <= config.l2_max_steps <= 20:
        raise ScreenerConfigError("SCREENER_L2_MAX_STEPS must be between 1 and 20")
    if not 30 <= config.l2_timeout_seconds <= 900:
        raise ScreenerConfigError(
            "SCREENER_L2_TIMEOUT_SECONDS must be between 30 and 900"
        )
    if not 1 <= config.l2_max_output_tokens <= 128_000:
        raise ScreenerConfigError(
            "SCREENER_L2_MAX_OUTPUT_TOKENS must be between 1 and 128000"
        )
    if not 1 <= config.l2_max_completion_tokens <= config.l2_max_output_tokens:
        raise ScreenerConfigError(
            "SCREENER_L2_MAX_COMPLETION_TOKENS must be within the output budget"
        )
    if not 1 <= config.l2_max_input_tokens <= 1_000_000:
        raise ScreenerConfigError(
            "SCREENER_L2_MAX_INPUT_TOKENS must be between 1 and 1000000"
        )
    if not 0 < config.l2_max_cost_usd <= 10:
        raise ScreenerConfigError("SCREENER_L2_MAX_COST_USD must be in (0, 10]")
    if config.l2_analyst_reasoning_effort != "model_default":
        raise ScreenerConfigError(
            "SCREENER_L2_ANALYST_REASONING_EFFORT must be model_default"
        )
    if config.l2_critic_reasoning_effort not in {"low", "medium"}:
        raise ScreenerConfigError(
            "SCREENER_L2_CRITIC_REASONING_EFFORT must be low or medium"
        )
    if not 60 <= config.l2_cache_ttl_seconds <= 30 * 86_400:
        raise ScreenerConfigError(
            "SCREENER_L2_CACHE_TTL_SECONDS must be between 60 and 2592000"
        )
    if not 1 <= config.l2_audit_retention_days <= 365:
        raise ScreenerConfigError(
            "SCREENER_L2_AUDIT_RETENTION_DAYS must be between 1 and 365"
        )
    if not 60 <= config.review_settings_max_stale_seconds <= 86_400:
        raise ScreenerConfigError(
            "SCREENER_REVIEW_SETTINGS_MAX_STALE_SECONDS must be between 60 and 86400"
        )
    if config.remote_build_mode not in {"off", "prefer"}:
        raise ScreenerConfigError("SCREENER_REMOTE_BUILD_MODE must be off or prefer")
    if not 300 <= config.remote_build_timeout_seconds <= 2400:
        raise ScreenerConfigError(
            "SCREENER_REMOTE_BUILD_TIMEOUT_SECONDS must be between 300 and 2400"
        )
    return config
