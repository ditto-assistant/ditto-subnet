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

from ditto.screener.errors import ScreenerConfigError


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

    netuid: int
    """Subnet netuid (118 for Ditto)."""

    # --- Build gate (Docker) ---
    docker_bin: str
    """Path/name of the docker CLI the gate shells out to."""

    build_timeout_seconds: float
    """Hard cap on a single ``docker build`` (crate compile is slow)."""

    run_timeout_seconds: float
    """Hard cap on the container serve, health, and model-call smoke check."""

    build_memory: str
    """``docker run --memory`` limit for the serve-smoke container (e.g. ``2g``)."""

    gh_token_file: str | None
    """Path to a file holding a GitHub read token, mounted as the BuildKit
    ``gh_token`` secret so a crate that pulls the private ``ditto-harness`` dep
    builds (same token dittobench uses). ``None`` = plain build (public deps)."""

    pids_limit: int
    """``docker run --pids-limit`` for the smoke container."""

    health_path: str
    """Harness health path to probe (contract: ``/health``)."""

    container_port: int
    """Port the harness serves on inside the container (contract: ``8080``)."""

    smoke_env: tuple[tuple[str, str], ...]
    """Env vars injected (``docker run -e K=V``) into the serve-smoke container.

    The canary appends its locked Chutes/OpenAI-compatible gateway settings after
    these values. This tuple remains available for unrelated boot-time variables
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
        netuid=_parse_int("NETUID", os.environ.get("NETUID", "118")),
        docker_bin=os.environ.get("SCREENER_DOCKER_BIN", "docker"),
        build_timeout_seconds=_parse_float("SCREENER_BUILD_TIMEOUT_SECONDS", "1200"),
        run_timeout_seconds=_parse_float("SCREENER_RUN_TIMEOUT_SECONDS", "120"),
        build_memory=os.environ.get("SCREENER_BUILD_MEMORY", "2g"),
        gh_token_file=os.environ.get("SCREENER_GH_TOKEN_FILE") or None,
        pids_limit=_parse_int("SCREENER_PIDS_LIMIT", "512"),
        health_path=os.environ.get("SCREENER_HEALTH_PATH", "/health"),
        container_port=_parse_int("SCREENER_CONTAINER_PORT", "8080"),
        smoke_env=_parse_env_pairs(
            # Compatibility key for older harness startup. The runtime canary
            # separately forces the model path to its fake external gateway.
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
    )
    if not config.signing_source_present():
        raise ScreenerConfigError(
            "no signing key: set SCREENER_MNEMONIC or "
            "SCREENER_WALLET_NAME + SCREENER_WALLET_HOTKEY"
        )
    if len(config.api_token) < 32:
        raise ScreenerConfigError("SCREENER_API_TOKEN must be at least 32 characters")
    return config
